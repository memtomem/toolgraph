"""Regression evidence for the September implementation review (R1–R9)."""

from datetime import datetime, timedelta, timezone, tzinfo
import json
import os
from pathlib import Path
import subprocess
import sys

from jsonschema import Draft202012Validator
import pytest

from toolgraph.artifact_safety import safe_artifact
from toolgraph.control_plan import ControlPlanError, load_control_plan
from toolgraph.graph import driver, loader, queries, schema
from toolgraph.manifest.ingest import ingest_governance
from toolgraph.models import CrawlResult, Governance, ResourceRecord, ToolRecord
from toolgraph.policy_bundle import PolicyBundleError, build_policy_bundle
from toolgraph.preflight import build_preflight
from toolgraph.policy_review import build_policy_review_plan
from tests.test_control_plan import _document, _raw
from tests import test_negative_truth as negative, test_selector as selection


@pytest.mark.parametrize(
    "tools,resources,policies",
    [(t, r, p) for t in (False, True) for r in (False, True) for p in (False, True)],
)
def test_empty_collection_combinations(backend_graph, tools, resources, policies):
    from toolgraph.models import Policy

    before = queries.graph_state().generation
    loader.load_crawl_result(
        CrawlResult(
            server_name="s",
            tools=[ToolRecord(name="t")] if tools else [],
            resources=[ResourceRecord(uri="file:///r")] if resources else [],
        )
    )
    report = ingest_governance(
        Governance(policies=[Policy(id="p", effect="DENY")] if policies else [])
    )
    assert report.applied
    assert queries.graph_state().generation > before
    loader.load_crawl_result(CrawlResult(server_name="s"))
    assert queries.exposed_tool_contracts() == []
    assert ingest_governance(Governance()).applied
    with driver.session() as session:
        assert session.run("MATCH (p:Policy) RETURN count(p) AS n").single()["n"] == 0
        assert (
            session.run("MATCH ()-[p:PROVIDES]->() RETURN count(p) AS n").single()["n"]
            == 0
        )


@pytest.mark.parametrize(
    "case",
    [
        selection.test_rank_features_unknown_agent_aborts_selection,
        selection.test_strict_passes_only_clean_allow,
        selection.test_review_passes_exceptions_and_unmapped_but_not_violations,
        selection.test_explore_rejects_only_resolution_grant_and_drift,
        negative.test_audit_report_matches_the_per_agent_composition_exactly,
        negative.test_batched_audit_never_leaks_one_agents_exception_to_another,
    ],
)
def test_shared_backend_semantics(backend_graph, case):
    case(backend_graph)


def test_exception_keys_cannot_collide():
    assert schema.exception_key("a|b", "c", None, "p") != schema.exception_key(
        "a", "b|c", None, "p"
    )
    assert schema.exception_key("a", "s::t", None, "p") != schema.exception_key(
        "a", "s::t", "*", "p"
    )
    assert schema.exception_key("a", "s::t", None, "p").startswith("v2:")


class Reader:
    def __init__(self, size):
        self.keys = [f"s::t{i:05}" for i in range(size)]
        self.calls = []
        self.generation = 1

    def state(self):
        return queries.GraphState("injected", self.generation, "a" * 64)

    def exposed_tool_contracts(self):
        return [
            {"tool_key": k, "server": "s", "name": k.split("::")[1]}
            for k in reversed(self.keys)
        ]

    def evaluate(self, agent, candidates, profile):
        self.calls.append(list(candidates))
        return {
            "agent_found": True,
            "filtered": {"eligible": candidates, "rejected": []},
            "features": [{"tool_key": k, "risk_score": 0} for k in candidates],
        }


@pytest.mark.parametrize("size", [0, 4095, 4096, 4097])
def test_injected_reader_chunks_without_runtime(monkeypatch, size):
    monkeypatch.setattr(
        queries, "graph_state", lambda: pytest.fail("global state accessed")
    )
    reader = Reader(size)
    bundle = build_policy_bundle(agent="a", store=reader)
    assert [r["tool_key"] for r in bundle["tools"]] == reader.keys
    assert [len(c) for c in reader.calls] == ([size] if size <= 4096 else [4096, 1])


@pytest.mark.parametrize(
    "failure",
    ["missing", "duplicate", "conflict", "feature_missing", "feature_duplicate"],
)
def test_compiler_rejects_incomplete_evaluation(failure):
    reader = Reader(2)
    evaluate = reader.evaluate

    def broken(*args):
        result = evaluate(*args)
        if failure == "missing":
            result["filtered"]["eligible"] = []
        if failure == "duplicate":
            result["filtered"]["eligible"] = reader.keys * 2
        if failure == "conflict":
            result["filtered"]["rejected"] = [
                {"tool_key": reader.keys[0], "reason": "DENY_VIOLATION"}
            ]
        if failure == "feature_missing":
            result["features"] = []
        if failure == "feature_duplicate":
            result["features"] *= 2
        return result

    reader.evaluate = broken
    with pytest.raises(PolicyBundleError):
        build_policy_bundle(agent="a", store=reader)


@pytest.mark.parametrize("always", [False, True])
def test_chunk_state_change_retries_whole_catalog(monkeypatch, always):
    reader = Reader(4097)
    evaluate = reader.evaluate

    def changing(*args):
        result = evaluate(*args)
        if len(reader.calls) == 2 or (always and len(reader.calls) % 2 == 0):
            reader.generation += 1
        return result

    reader.evaluate = changing
    monkeypatch.setattr(queries, "_bracket_backoff", lambda _: None)
    if always:
        with pytest.raises(RuntimeError, match="every read attempt"):
            build_policy_bundle(agent="a", store=reader)
        assert len(reader.calls) == 10
    else:
        assert (
            build_policy_bundle(agent="a", store=reader)["graph_state"]["generation"]
            == 2
        )
        assert reader.calls[:2] == reader.calls[2:]


@pytest.mark.parametrize(
    "mutation",
    [
        "missing_cycles",
        "string_number",
        "bool_number",
        "bool_version",
        "null_bound",
        "false_string",
        "null_edge",
    ],
)
def test_control_loader_rejects_schema_invalid_mutations(mutation):
    doc = _document()
    agent = next(n for n in doc["nodes"] if n["kind"] == "agent")
    if mutation == "missing_cycles":
        del doc["limits"]["maximum_cycles"]
    if mutation == "string_number":
        agent["max_invocations"] = "2"
    if mutation == "bool_number":
        agent["max_invocations"] = True
    if mutation == "bool_version":
        doc["schema_version"] = True
    if mutation == "null_bound":
        doc["nodes"][0]["max_invocations"] = None
    if mutation == "false_string":
        doc["nodes"][0]["agent_call"] = "false"
    if mutation == "null_edge":
        doc["edges"][0]["max_fan_out"] = None
    with pytest.raises(ControlPlanError):
        load_control_plan(_raw(doc))


def test_control_loader_accepts_json_integer_numbers():
    doc = _document()
    doc["schema_version"] = 1.0
    doc["limits"]["maximum_cycles"] = 0.0
    doc["limits"]["maximum_parallelism"] = 2.0
    assert load_control_plan(_raw(doc)).limits.maximum_parallelism == 2


def test_identity_preservation_and_evidence_redaction():
    identity = "https://identity.test/a?role=one#part"
    artifact = {
        "agent": identity,
        "eligible": [identity],
        "rejected": [
            {
                "candidate": identity,
                "candidates": [identity],
                "paths": ["https://user:secret@host/r?token=secret#secret"],
            }
        ],
        "run_id": identity,
    }
    result = safe_artifact(artifact)
    assert (
        result["agent"]
        == result["eligible"][0]
        == result["rejected"][0]["candidate"]
        == result["run_id"]
        == identity
    )
    assert result["rejected"][0]["paths"] == ["https://host/r"]
    assert artifact["rejected"][0]["paths"][0].startswith("https://user:")


@pytest.mark.parametrize("identity", ["https://u:p@host/a", "s::t?token=secret"])
def test_credentials_rejected_before_graph_access(monkeypatch, identity):
    monkeypatch.setattr(
        queries, "with_graph_state", lambda *a, **kw: pytest.fail("graph accessed")
    )
    with pytest.raises(ValueError, match="credential"):
        build_preflight(agent="a", candidates=[identity], profile="review", run_id="r")
    with pytest.raises(ValueError, match="credential"):
        build_policy_bundle(agent=identity, store=Reader(0))


class NoOffset(tzinfo):
    def utcoffset(self, dt):
        return None


@pytest.mark.parametrize(
    "offset",
    [
        None,
        NoOffset(),
        timezone(timedelta(seconds=30)),
        timezone.utc,
        timezone(timedelta(hours=5, minutes=30)),
    ],
)
@pytest.mark.parametrize("producer", ["preflight", "review"])
def test_timestamp_contract_before_io(
    monkeypatch, artifact_format_checker, offset, producer
):
    from tests.test_preflight import _bracket

    now = datetime(2026, 9, 7, tzinfo=offset)
    invalid = (
        offset is None
        or isinstance(offset, NoOffset)
        or offset.utcoffset(None).total_seconds() % 60 != 0
    )
    if invalid:
        monkeypatch.setattr(
            queries, "with_graph_state", lambda *a, **kw: pytest.fail("graph accessed")
        )
        with pytest.raises((ValueError, RuntimeError), match="created_at"):
            if producer == "preflight":
                build_preflight(
                    agent="a",
                    candidates=[],
                    profile="review",
                    run_id="r",
                    created_at=now,
                )
            else:
                build_policy_review_plan(
                    report=Path("nonexistent"), agent="a", created_at=now
                )
    elif producer == "preflight":
        _bracket(
            monkeypatch,
            {"agent": "a", "agent_found": True, "eligible": [], "rejected": []},
        )
        doc = build_preflight(
            agent="a", candidates=[], profile="review", run_id="r", created_at=now
        )
        schema_doc = json.loads(Path("contracts/preflight.schema.json").read_text())
        Draft202012Validator(
            schema_doc, format_checker=artifact_format_checker
        ).validate(doc)
    else:
        from tests.test_policy_review import _mock_inputs

        _mock_inputs(monkeypatch)
        doc = build_policy_review_plan(
            report=Path("trace.json"), agent="planner", created_at=now
        )
        schema_doc = json.loads(
            Path("contracts/policy-review-plan.schema.json").read_text()
        )
        Draft202012Validator(
            schema_doc, format_checker=artifact_format_checker
        ).validate(doc)


@pytest.mark.parametrize("mode", ["help", "version", "explicit", "invalid"])
def test_broken_default_configuration_subprocess(tmp_path, mode):
    (tmp_path / ".toolgraph").mkdir()
    broken = tmp_path / ".toolgraph/config.json"
    broken.write_text("{broken")
    valid = tmp_path / "valid.json"
    valid.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "backend": "ladybug",
                "db_path": str(tmp_path / "graph.lbug"),
            }
        )
    )
    env = dict(os.environ, TOOLGRAPH_CONFIG=str(broken))
    commands = {
        "help": ["--help"],
        "version": ["--version"],
        "explicit": ["--config", str(valid), "init-schema"],
        "invalid": ["--config", str(broken), "check"],
    }
    result = subprocess.run(
        [sys.executable, "-c", "from toolgraph.cli import app; app()", *commands[mode]],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
    )
    assert result.returncode == (2 if mode == "invalid" else 0), (
        result.stdout + result.stderr
    )
    assert "Traceback" not in result.stderr


def test_revocation_and_failed_ingest_preserve_state(backend_graph, monkeypatch):
    from toolgraph.models import AccessGrant
    from toolgraph.manifest import ingest

    loader.load_crawl_result(CrawlResult(server_name="s", tools=[ToolRecord(name="t")]))
    gov = Governance(agents=["a"], grants=[AccessGrant(agent="a", tool="s::t")])
    assert ingest_governance(gov).applied
    before = queries.graph_state()
    original = ingest.bump_generation

    def fail(tx):
        raise RuntimeError("injected after mutation")

    monkeypatch.setattr(ingest, "bump_generation", fail)
    with pytest.raises(RuntimeError, match="injected"):
        ingest_governance(Governance())
    assert queries.graph_state() == before
    assert queries.agent_exists("a")
    monkeypatch.setattr(ingest, "bump_generation", original)
    assert ingest_governance(Governance()).applied
    assert not queries.agent_exists("a")
    assert queries.graph_state().generation > before.generation


def test_colliding_exception_tuples_and_legacy_reingest(backend_graph):
    from toolgraph.models import AccessGrant, ExpectedException, Policy

    for server in ("s", "b|s"):
        loader.load_crawl_result(
            CrawlResult(server_name=server, tools=[ToolRecord(name="t")])
        )
    pairs = [("a|b", "s::t"), ("a", "b|s::t")]
    gov = Governance(
        policies=[Policy(id="p", effect="DENY")],
        grants=[AccessGrant(agent=a, tool=k) for a, k in pairs],
        governed_by={k: ["p"] for _, k in pairs},
        expected_exceptions=[
            ExpectedException(agent=a, tool=k, policy="p", reason="first")
            for a, k in pairs
        ]
        + [ExpectedException(agent="a", tool="b|s::t", policy="p", reason="last")],
    )
    assert ingest_governance(gov).applied
    with driver.session() as session:
        rows = list(
            session.run(
                "MATCH (e:ExpectedException) RETURN e.key AS key, e.agent AS agent, e.reason AS reason"
            )
        )
        assert len(rows) == 2
        assert {r["agent"]: r["reason"] for r in rows} == {"a|b": "first", "a": "last"}
        session.run("MATCH (e:ExpectedException {agent:'a'}) DELETE e")
        session.run(
            "CREATE (:ExpectedException {key:'legacy|key', agent:'a', tool_key:'b|s::t', policy:'p', reason:'last'})"
        )
    assert ingest_governance(gov).applied
    with driver.session() as session:
        rows = list(session.run("MATCH (e:ExpectedException) RETURN e.key AS key"))
        assert len(rows) == 2 and all(r["key"].startswith("v2:") for r in rows)


def test_uri_identities_keep_opposite_decisions_in_export(backend_graph, tmp_path):
    from toolgraph.models import AccessGrant, Policy
    from toolgraph.artifacts import atomic_write_private, canonical_json_bytes

    agent = "https://id.test/a?role=one#part"
    keys = ["s::https://id.test/t?role=one#part", "s::https://id.test/t?role=two#part"]
    loader.load_crawl_result(
        CrawlResult(
            server_name="s", tools=[ToolRecord(name=k.split("::", 1)[1]) for k in keys]
        )
    )
    assert ingest_governance(
        Governance(
            agents=[agent],
            policies=[Policy(id="p", effect="DENY")],
            grants=[AccessGrant(agent=agent, tool=k) for k in keys],
            governed_by={keys[1]: ["p"]},
        )
    ).applied
    bundle = build_policy_bundle(agent=agent, profile="review")
    assert bundle["agent"] == agent
    assert {r["tool_key"]: r["decision"] for r in bundle["tools"]} == {
        keys[0]: "eligible",
        keys[1]: "rejected",
    }
    doc = build_preflight(
        agent=agent,
        candidates=keys,
        profile="review",
        run_id=agent,
        include_features=True,
    )
    assert doc["run_id"] == agent and doc["eligible"] == [keys[0]]
    assert doc["rejected"][0]["tool_key"] == keys[1]
    output = tmp_path / "artifact.json"
    atomic_write_private(output, canonical_json_bytes(doc))
    assert json.loads(output.read_text()) == doc


def test_policy_review_cli_scrubs_evidence(monkeypatch, tmp_path):
    from tests.test_policy_review import _mock_inputs
    from toolgraph import cli
    from typer.testing import CliRunner

    _mock_inputs(monkeypatch)
    monkeypatch.setattr(
        "toolgraph.policy_review.selector.eligible_tools",
        lambda *a, **k: {
            "agent_found": True,
            "eligible": [],
            "rejected": [
                {
                    "tool_key": "github::merge",
                    "reason": "DENY_VIOLATION",
                    "paths": [
                        {
                            "resource": "https://user:secret@host/r?token=secret#secret",
                            "provenance": {
                                "evidence": "https://user:secret@host/r?token=secret"
                            },
                            "exception_reason": "see https://host/r?token=secret",
                        }
                    ],
                }
            ],
        },
    )
    output = tmp_path / "review.json"
    result = CliRunner().invoke(
        cli.app,
        [
            "policy",
            "review-plan",
            "trace.json",
            "--agent",
            "planner",
            "--output",
            str(output),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "secret" not in output.read_text()
    assert (
        json.loads(output.read_text())["candidates"][0]["tool_key"] == "github::merge"
    )


def test_legacy_reader_fallback():
    class Legacy:
        def state(self):
            return queries.GraphState("legacy", 1, "a" * 64)

        def exposed_tool_contracts(self):
            return [{"tool_key": "s::t"}]

        def eligible_tools(self, *args):
            return {"agent_found": True, "eligible": ["s::t"], "rejected": []}

        def rank_features(self, *args):
            return {
                "agent_found": True,
                "features": [{"tool_key": "s::t", "risk_score": 0}],
            }

    assert (
        build_policy_bundle(agent="a", store=Legacy())["tools"][0]["decision"]
        == "eligible"
    )


def test_control_preflight_preserves_principal_identity(monkeypatch):
    from toolgraph.control_plan import build_control_preflight
    from tests.test_control_plan import _bracket

    _bracket(monkeypatch)
    doc = _document()
    previous = doc["principals"][0]["id"]
    identity = "https://id.test/agent?role=one#part"
    doc["principals"][0]["id"] = identity
    for node in doc["nodes"]:
        node["principals"] = [
            identity if p == previous else p for p in node["principals"]
        ]
    result = build_control_preflight(_raw(doc), profile="review")
    assert any(row["principal"] == identity for row in result["evaluations"])


@pytest.mark.parametrize("profile", ["strict", "review", "explore"])
def test_ambiguity_and_drift_on_both_backends(backend_graph, profile):
    selection.test_ambiguous_bare_name_is_never_auto_selected(backend_graph, profile)
    selection.test_drifted_tools_are_rejected_in_every_profile(backend_graph, profile)


def test_empty_catalog_unknown_agent_fails():
    reader = Reader(0)
    reader.evaluate = lambda *args: {"agent_found": False}
    with pytest.raises(PolicyBundleError, match="not found"):
        build_policy_bundle(agent="missing", store=reader)


def test_generation_exhaustion_preserves_existing_output(monkeypatch, tmp_path):
    from toolgraph import cli
    from typer.testing import CliRunner

    output = tmp_path / "bundle.json"
    output.write_bytes(b"previous artifact")

    def fail(*args, **kwargs):
        raise RuntimeError("graph state changed during every read attempt")

    monkeypatch.setattr("toolgraph.policy_bundle.queries.with_graph_state", fail)
    result = CliRunner().invoke(
        cli.app, ["policy", "compile", "--agent", "a", "--output", str(output)]
    )
    assert result.exit_code == 1
    assert output.read_bytes() == b"previous artifact"
