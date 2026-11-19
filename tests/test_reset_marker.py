"""`toolgraph reset` marks the graph while it erases it (#271).

Before this, a reader bracketing its query between reset's delete batches
read the same stamp before and after and got ``graph_state_verified: true``
for a half-erased graph; once the batch that deleted GraphMeta committed,
it read ``(None, 0)`` twice and got the same. Reproduced on main e141646
(Ladybug, batches of one node): five reads during one reset, all verified.
"""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from tests._helpers import seed_basic
from toolgraph import cli, health
from toolgraph.graph import driver, queries, schema

RESET_LINE = (
    "ERROR: a graph reset is in progress or did not finish; retry after it finishes, "
    "or run toolgraph reset --yes once no reset process is left"
)


def _agents() -> int:
    with driver.read_session() as s:
        return s.run("MATCH (a:Agent) RETURN count(a) AS c").single()["c"]


def _graph_meta_ids() -> list[str]:
    with driver.session() as s:
        return sorted(r["id"] for r in s.run("MATCH (m:GraphMeta) RETURN m.id AS id"))


def _mark_reset() -> None:
    with driver.session() as s:
        s.run("MERGE (r:GraphMeta {id:'reset'})")


def _read() -> dict:
    return queries.with_graph_state(lambda: {"agents": _agents()})


# --- the bracket -----------------------------------------------------------


def _reader(states):
    calls = []

    def read():
        calls.append(len(calls))
        return states[min(len(calls) - 1, len(states) - 1)]

    return read, calls


STABLE = queries.GraphState("i-1", 4, "d" * 64)
RESETTING = queries.GraphState("i-1", 5, "d" * 64, reset_in_progress=True)


@pytest.mark.parametrize(
    ("states", "reads"),
    [([RESETTING], 1), ([STABLE, RESETTING], 2)],
    ids=["marked-before", "marked-after"],
)
def test_a_marked_state_is_unverified_without_a_retry(states, reads):
    read, calls = _reader(states)
    fetched = []

    result = queries.with_graph_state(lambda: fetched.append(1) or {"x": 1}, state_reader=read)

    assert result == {
        "x": 1,
        "graph_generation": None,
        "graph_instance_id": None,
        "graph_state": None,
        "graph_state_verified": False,
    }
    assert len(calls) == reads
    assert fetched == [1]


@pytest.mark.parametrize("states", [[RESETTING], [STABLE, RESETTING]], ids=["before", "after"])
def test_a_strict_bracket_refuses_a_marked_state(states):
    read, calls = _reader(states)
    fetched = []

    with pytest.raises(queries.GraphResetInProgressError) as raised:
        queries.with_graph_state(lambda: fetched.append(1) or {}, strict=True, state_reader=read)

    assert isinstance(raised.value, queries.GraphStateChurnError)
    assert f"ERROR: {raised.value}" == RESET_LINE
    # Marked before the query: nothing is fetched for an artifact it cannot label.
    assert fetched == ([] if states == [RESETTING] else [1])


def test_a_strict_query_failing_because_a_reset_began_names_the_reset():
    read, _ = _reader([STABLE, RESETTING])

    def fetch():
        raise RuntimeError("agent 'planner' not found in the graph")

    with pytest.raises(queries.GraphResetInProgressError) as raised:
        queries.with_graph_state(fetch, strict=True, state_reader=read)
    assert str(raised.value.__cause__) == "agent 'planner' not found in the graph"


def test_a_failing_query_keeps_its_error_without_a_reset_or_when_not_strict():
    def fetch():
        raise RuntimeError("agent 'planner' not found in the graph")

    for strict, states in [(True, [STABLE, STABLE]), (False, [STABLE, RESETTING])]:
        read, _ = _reader(states)
        with pytest.raises(RuntimeError) as raised:
            queries.with_graph_state(fetch, strict=strict, state_reader=read)
        assert type(raised.value) is RuntimeError
        assert str(raised.value) == "agent 'planner' not found in the graph"


def test_a_failing_state_read_after_a_failed_query_keeps_the_query_error():
    calls = []

    def read():
        calls.append(1)
        if len(calls) > 1:
            raise ConnectionError("backend went away")
        return STABLE

    def fetch():
        raise RuntimeError("agent 'planner' not found in the graph")

    with pytest.raises(RuntimeError, match="not found"):
        queries.with_graph_state(fetch, strict=True, state_reader=read)


def test_the_state_reads_the_marker_row(backend_graph):
    schema.init_schema()  # the Neo4j fixture wipes the singleton too
    assert queries.graph_state().reset_in_progress is False
    _mark_reset()
    state = queries.graph_state()
    assert state.reset_in_progress is True
    assert state.instance_id  # the singleton is still read
    assert _read()["graph_state_verified"] is False


# --- reset itself ------------------------------------------------------------


def test_every_read_during_a_reset_is_unverified(backend_graph, monkeypatch):
    seed_basic(None)
    with driver.session() as s:
        for i in range(3):
            s.run(f"CREATE (:Agent {{id:'extra-{i}'}})")
    before = queries.graph_state()
    assert _read()["graph_state_verified"] is True
    monkeypatch.setattr(schema, "RESET_BATCH_SIZE", 1)
    # A reader after every committed reset transaction except the last one.
    observed = []
    real_session = schema.session

    class Observed:
        def __init__(self, inner):
            self._inner = inner

        def __getattr__(self, name):
            return getattr(self._inner, name)

        def execute_write(self, fn, *args):
            result = self._inner.execute_write(fn, *args)
            if args[0] != schema._FINISH_RESET:
                observed.append((args[0] == schema._BEGIN_RESET, _read(), _agents()))
            return result

    class Session:
        def __enter__(self):
            self._cm = real_session()
            return Observed(self._cm.__enter__())

        def __exit__(self, *exc):
            return self._cm.__exit__(*exc)

    monkeypatch.setattr(schema, "session", Session)

    schema.reset_graph()

    begun = [read for is_begin, read, _ in observed if is_begin]
    assert len(begun) == 1
    # Transaction 1 and every batch (the last one deletes nothing): the reader
    # saw the graph shrink to nothing and was never told it was verified.
    agents_seen = [agents for _, _, agents in observed]
    assert agents_seen[0] == 4 and agents_seen[-1] == 0 and len(agents_seen) >= 6
    assert [read["graph_state_verified"] for _, read, _ in observed] == [False] * len(observed)
    assert all(read["graph_state"] is None for _, read, _ in observed)
    after = queries.graph_state()
    assert after.reset_in_progress is False
    assert after.generation == 0
    assert after.instance_id and after.instance_id != before.instance_id
    assert after.governance_digest is None
    assert _graph_meta_ids() == ["singleton"]
    assert _read()["graph_state_verified"] is True


def test_reset_keeps_only_the_singleton(backend_graph):
    seed_basic(None)

    result = CliRunner().invoke(cli.app, ["reset", "--yes"])

    assert result.exit_code == 0, result.output
    assert result.stdout == "Graph cleared.\n"
    assert _graph_meta_ids() == ["singleton"]
    with driver.session() as s:
        assert s.run("MATCH (n) RETURN count(n) AS c").single()["c"] == 1


@pytest.mark.neo4j
def test_a_multi_label_singleton_survives_the_batches(graph):
    schema.init_schema()  # the fixture wipes the singleton too
    with driver.session() as s:
        s.run("MATCH (m:GraphMeta {id:'singleton'}) SET m:Extra")
        s.run("CREATE (:Agent {id:'a'}), (:Tool:Destructive {key:'s::t'})")
        before = [sorted(r["l"]) for r in s.run("MATCH (m:GraphMeta) RETURN labels(m) AS l")]
    assert before == [["Extra", "GraphMeta"]]

    schema.reset_graph()

    with driver.session() as s:
        labels = [sorted(r["l"]) for r in s.run("MATCH (n) RETURN labels(n) AS l")]
    assert labels == [["Extra", "GraphMeta"]]
    assert queries.graph_state().reset_in_progress is False


def test_an_interrupted_reset_stays_marked_until_a_reset_finishes(backend_graph, monkeypatch):
    seed_basic(None)
    monkeypatch.setattr(schema, "RESET_BATCH_SIZE", 1)
    real_deleted = schema._deleted
    batches = []

    def failing(tx, statement, limit):
        if batches:
            raise RuntimeError("injected outage")
        batches.append(1)
        return real_deleted(tx, statement, limit)

    monkeypatch.setattr(schema, "_deleted", failing)
    with pytest.raises(RuntimeError, match="injected outage"):
        schema.reset_graph()

    assert queries.graph_state().reset_in_progress is True
    assert _read()["graph_state_verified"] is False
    report = health.run_doctor()
    graph_check = next(c for c in report.checks if c.name == "Graph State")
    assert report.healthy is False
    assert graph_check.status == "FAIL"
    assert graph_check.message == (
        "A graph reset is running or did not finish: wait for a running reset, or once "
        "no reset process is left, run 'toolgraph reset --yes' to finish it"
    )
    doctor_json = CliRunner().invoke(cli.app, ["doctor", "--json"])
    assert doctor_json.exit_code == 1
    states = [c for c in json.loads(doctor_json.stdout)["checks"] if c["name"] == "Graph State"]
    assert states[0]["status"] == "FAIL"
    assert states[0]["details"]["reset_in_progress"] is True

    monkeypatch.setattr(schema, "_deleted", real_deleted)
    schema.reset_graph()

    assert queries.graph_state().reset_in_progress is False
    assert _read()["graph_state_verified"] is True
    assert _agents() == 0


# --- commands during a reset -----------------------------------------------


@pytest.fixture
def governance_file(tmp_path):
    path = tmp_path / "governance.yaml"
    path.write_text("agents: [planner]\n", encoding="utf-8")
    return path


@pytest.mark.parametrize("extra", [[], ["--json"]], ids=["text", "json"])
def test_dry_run_during_a_reset_is_one_error_line(backend_graph, governance_file, extra):
    seed_basic(None)
    _mark_reset()

    result = CliRunner().invoke(
        cli.app, ["ingest-manifest", "--governance", str(governance_file), "--dry-run", *extra]
    )

    assert result.exit_code == 1
    assert result.stdout == ""
    assert result.stderr == RESET_LINE + "\n"


def test_policy_compile_during_a_reset_is_one_error_line(backend_graph, tmp_path):
    seed_basic(None)
    _mark_reset()
    output = tmp_path / "bundle.json"

    result = CliRunner().invoke(
        cli.app,
        ["policy", "compile", "--agent", "planner", "--profile", "review", "--output", str(output)],
    )

    assert result.exit_code == 1
    assert result.stdout == ""
    assert result.stderr == RESET_LINE + "\n"
    assert not output.exists()


def test_policy_impact_names_the_reset_before_its_own_state_checks(backend_graph):
    """A marked graph with no ingested governance names the reset, not the missing digest."""
    from toolgraph import policy_impact

    schema.init_schema()
    _mark_reset()
    assert queries.graph_state().governance_digest is None

    with driver.read_session() as s, pytest.raises(queries.GraphResetInProgressError):
        policy_impact._state(s)
