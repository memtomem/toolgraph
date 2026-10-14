"""Tests for toolgraph policy propose with graph-grounded grant resolution."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml
from typer.testing import CliRunner

from toolgraph import cli
from toolgraph import policy_propose as pp
from toolgraph.graph import driver, loader, queries, schema, selector
from toolgraph.manifest.ingest import ingest_governance
from toolgraph.manifest.parser import governance_digest, load_governance
from toolgraph.models import CrawlResult, Governance, ResourceRecord, ToolRecord
from toolgraph.policy_propose import (
    ProvenanceError,
    StateChurnError,
    StateMismatchError,
    propose,
    resolve_grants_from_graph,
    run_propose,
)


@pytest.fixture
def sample_gov_path(tmp_path) -> Path:
    p = tmp_path / "governance.yaml"
    p.write_text(
        """
agents:
  - ops-agent
  - public-bot
grants:
  - agent: ops-agent
    tool: filesystem::read_file
    granted_by: security-team
  - agent: ops-agent
    tool: filesystem::write_file
    granted_by: security-team
"""
    )
    return p


@pytest.fixture
def sample_observed_jsonl(tmp_path, sample_gov_path) -> Path:
    gov = load_governance(sample_gov_path)
    digest = governance_digest(gov)
    p = tmp_path / "observed.jsonl"
    lines = [
        json.dumps(
            {
                "envelope": {
                    "governance_digest": digest,
                    "graph_generation": 1,
                    "graph_instance_id": "test-instance",
                    "profile": "strict",
                    "window_start": "2026-08-01T00:00:00Z",
                    "window_end": "2026-08-31T23:59:59Z",
                }
            }
        ),
        # 1. Un-granted and called -> additions candidate
        json.dumps(
            {
                "agent": "public-bot",
                "tool_key": "filesystem::read_file",
                "decision": "rejected",
                "calls": 12,
                "would_block_calls": 12,
                "reject_reasons": ["NOT_GRANTED"],
            }
        ),
        # 2. Deny governed -> conflicts candidate
        json.dumps(
            {
                "agent": "public-bot",
                "tool_key": "filesystem::write_file",
                "decision": "rejected",
                "calls": 5,
                "would_block_calls": 5,
                "reject_reasons": ["NOT_GRANTED", "DENY_GOVERNED"],
            }
        ),
        # 3. Granted and 0 calls -> removals candidate
        json.dumps(
            {
                "agent": "ops-agent",
                "tool_key": "filesystem::write_file",
                "decision": "eligible",
                "calls": 0,
                "would_block_calls": 0,
                "reject_reasons": [],
            }
        ),
    ]
    p.write_text("\n".join(lines) + "\n")
    return p


def test_deny_invariant_never_proposed_as_addition():
    gov = {
        "agents": ["bot"],
        "grants": [],
    }
    observed = [
        {
            "agent": "bot",
            "ref": "filesystem::write_file",
            "has_tool_key": True,
            "decision": "rejected",
            "calls": 10,
            "would_block_calls": 10,
            "reasons": ["NOT_GRANTED", "DENY_GOVERNED"],
            "reasons_complete": True,
        }
    ]
    res = propose(gov, observed, min_would_block=1, granted={}, ambiguous_grants=[])
    assert len(res["additions"]) == 0
    assert len(res["conflicts"]) == 1
    assert res["conflicts"][0]["tool"] == "filesystem::write_file"


def test_ambiguous_bare_grant_protection_regression(backend_graph, tmp_path):
    """Regression test: bare grant in manifest must not be mistakenly removed

    when multiple servers provide that tool name, even if observations report
    zero calls on one of the servers.
    """
    schema.init_schema()
    with driver.session() as s:
        # Populate live graph with two servers exposing read_file
        s.run(
            "CREATE (m1:MCPServer {name: 'alpha'}) CREATE (t1:Tool {key: 'alpha::read_file', name: 'read_file'}) CREATE (m1)-[:EXPOSES]->(t1)"
        )
        s.run(
            "CREATE (m2:MCPServer {name: 'beta'}) CREATE (t2:Tool {key: 'beta::read_file', name: 'read_file'}) CREATE (m2)-[:EXPOSES]->(t2)"
        )

        # Bare grant in governance: agent 'worker' has tool 'read_file'
        grants = [{"agent": "worker", "tool": "read_file"}]
        resolved, ambiguous = resolve_grants_from_graph(s, grants)

        # It matches 2 tools in live graph -> must be flagged as AMBIGUOUS_GRANT
        assert len(ambiguous) == 1
        assert ambiguous[0]["reason"] == "AMBIGUOUS_GRANT"
        assert "alpha::read_file" in ambiguous[0]["note"]
        assert "beta::read_file" in ambiguous[0]["note"]
        # It must NOT be added to resolved grants as a single server mapping
        assert ("worker", "beta::read_file") not in resolved
        assert ("worker", "alpha::read_file") not in resolved

        # If observation window reports beta::read_file with 0 calls
        observed = [
            {
                "agent": "worker",
                "ref": "beta::read_file",
                "has_tool_key": True,
                "decision": "eligible",
                "calls": 0,
                "would_block_calls": 0,
                "reasons": [],
                "reasons_complete": True,
            }
        ]
        gov = {"agents": ["worker"], "grants": grants}
        res = propose(
            gov, observed, min_would_block=1, granted=resolved, ambiguous_grants=ambiguous
        )

        # Crucial check: removals must be EMPTY. Neither read_file nor beta::read_file is removed!
        assert res["removals"] == []
        assert len(res["insufficient_evidence"]) == 1
        assert res["insufficient_evidence"][0]["reason"] == "AMBIGUOUS_GRANT"


def test_bare_grant_adopts_a_single_live_resolution(backend_graph):
    schema.init_schema()
    with driver.session() as s:
        s.run(
            "CREATE (m:MCPServer {name: 'alpha'}) CREATE (t:Tool {key: 'alpha::read_file', name: 'read_file'}) CREATE (m)-[:EXPOSES]->(t)"
        )
        resolved, ambiguous = resolve_grants_from_graph(s, [{"agent": "bot", "tool": "read_file"}])
    assert resolved == {("bot", "alpha::read_file"): "read_file"}
    assert ambiguous == []


def test_run_propose_composite_token_validation(
    backend_graph, sample_gov_path, sample_observed_jsonl
):
    schema.init_schema()
    gov = load_governance(sample_gov_path)
    digest = governance_digest(gov)

    # 1. Update live graph GraphMeta to match envelope
    with driver.session() as s:
        s.run("CREATE (t:Tool {key: 'filesystem::read_file', name: 'read_file'})")
        s.run("CREATE (t:Tool {key: 'filesystem::write_file', name: 'write_file'})")
        s.run(
            "MERGE (m:GraphMeta {id: 'singleton'}) "
            "SET m.instance_id = 'test-instance', m.generation = 1, m.governance_digest = $d",
            d=digest,
        )

    # Exact token match -> should succeed
    proposal, rendered = run_propose(sample_gov_path, sample_observed_jsonl, min_would_block=3)
    assert len(proposal["additions"]) == 1
    assert proposal["additions"][0]["tool"] == "filesystem::read_file"
    assert len(proposal["conflicts"]) == 1
    assert len(proposal["removals"]) == 1
    assert "exact composite token matched: test-instance, gen 1" in rendered


def test_run_propose_instance_mismatch(backend_graph, sample_gov_path, sample_observed_jsonl):
    schema.init_schema()
    gov = load_governance(sample_gov_path)
    digest = governance_digest(gov)

    with driver.session() as s:
        s.run(
            "MERGE (m:GraphMeta {id: 'singleton'}) "
            "SET m.instance_id = 'live-instance', m.generation = 1, m.governance_digest = $d",
            d=digest,
        )

    with pytest.raises(StateMismatchError, match="graph instance mismatch"):
        run_propose(sample_gov_path, sample_observed_jsonl)


def test_run_propose_generation_shift_and_revalidation(
    backend_graph, sample_gov_path, sample_observed_jsonl
):
    schema.init_schema()
    gov = load_governance(sample_gov_path)
    digest = governance_digest(gov)

    with driver.session() as s:
        s.run("CREATE (t:Tool {key: 'filesystem::read_file', name: 'read_file'})")
        s.run("CREATE (t:Tool {key: 'filesystem::write_file', name: 'write_file'})")
        s.run(
            "MERGE (m:GraphMeta {id: 'singleton'}) "
            "SET m.instance_id = 'test-instance', m.generation = 2, m.governance_digest = $d",
            d=digest,
        )

    # Generation 2 vs envelope generation 1 without --revalidate-current -> StateMismatchError
    with pytest.raises(
        StateMismatchError,
        match="export declared graph_generation 1 but live graph generation is 2",
    ):
        run_propose(sample_gov_path, sample_observed_jsonl, revalidate_current=False)

    # Strict revalidation needs a live agent, exposure, and authored data flow.
    with driver.session() as s:
        s.run("CREATE (:Agent {id:'public-bot'})")
        s.run(
            "MATCH (t:Tool {key:'filesystem::read_file'}) "
            "CREATE (m:MCPServer {name:'filesystem'}) CREATE (m)-[:EXPOSES]->(t) "
            "CREATE (r:Resource {uri:'file:///data'}) CREATE (t)-[:READS]->(r)"
        )

    # With revalidate_current=True -> succeeds with distinguishing header
    proposal, rendered = run_propose(
        sample_gov_path, sample_observed_jsonl, revalidate_current=True
    )
    assert len(proposal["additions"]) == 1
    assert "revalidated against current catalog" in rendered
    assert "observed: instance=test-instance, gen=1" in rendered
    assert "current: instance=test-instance, gen=2" in rendered


def test_state_churn_exhaustion_typed_refusal(
    backend_graph, sample_gov_path, sample_observed_jsonl
):
    schema.init_schema()
    gov = load_governance(sample_gov_path)
    digest = governance_digest(gov)

    with driver.session() as s:
        s.run(
            "MERGE (m:GraphMeta {id: 'singleton'}) "
            "SET m.instance_id = 'test-instance', m.generation = 1, m.governance_digest = $d",
            d=digest,
        )

    # Simulate 5-attempt churn exhaustion
    from toolgraph.graph import queries

    churn = queries.GraphStateChurnError("graph state changed during every read attempt")
    with patch("toolgraph.graph.queries.with_graph_state", side_effect=churn):
        with pytest.raises(StateChurnError) as raised:
            run_propose(sample_gov_path, sample_observed_jsonl)
    assert str(raised.value) == (
        "graph state churn during observation proposal: concurrent graph "
        "modifications exhausted 5 read attempts"
    )

    # Any other RuntimeError is not churn and must surface unchanged, whatever
    # its text says.
    other = RuntimeError("graph state changed during every read attempt")
    with patch("toolgraph.graph.queries.with_graph_state", side_effect=other):
        with pytest.raises(RuntimeError) as raised:
            run_propose(sample_gov_path, sample_observed_jsonl)
    assert raised.value is other


def test_cli_policy_propose(backend_graph, sample_gov_path, sample_observed_jsonl):
    schema.init_schema()
    gov = load_governance(sample_gov_path)
    digest = governance_digest(gov)

    with driver.session() as s:
        s.run("CREATE (t:Tool {key: 'filesystem::read_file', name: 'read_file'})")
        s.run("CREATE (t:Tool {key: 'filesystem::write_file', name: 'write_file'})")
        s.run(
            "MERGE (m:GraphMeta {id: 'singleton'}) "
            "SET m.instance_id = 'test-instance', m.generation = 1, m.governance_digest = $d",
            d=digest,
        )

    runner = CliRunner()
    # 1. Standard markdown to stdout
    result = runner.invoke(
        cli.app, ["policy", "propose", str(sample_gov_path), str(sample_observed_jsonl)]
    )
    assert result.exit_code == 0, result.output
    assert "# Governance change proposal" in result.stdout
    assert "filesystem::read_file" in result.stdout

    # 2. JSON output
    result_json = runner.invoke(
        cli.app, ["policy", "propose", str(sample_gov_path), str(sample_observed_jsonl), "--json"]
    )
    assert result_json.exit_code == 0, result_json.output
    parsed = json.loads(result_json.stdout)
    assert len(parsed["additions"]) == 1

    # 3. File output
    out_file = sample_gov_path.parent / "out.md"
    result_file = runner.invoke(
        cli.app,
        [
            "policy",
            "propose",
            str(sample_gov_path),
            str(sample_observed_jsonl),
            "-o",
            str(out_file),
        ],
    )
    assert result_file.exit_code == 0, result_file.output
    assert out_file.exists()
    assert "# Governance change proposal" in out_file.read_text()


def test_observation_window_schema_conformance(sample_observed_jsonl, artifact_format_checker):
    import jsonschema

    schema_path = (
        Path(__file__).resolve().parent.parent / "contracts" / "observation-window.schema.json"
    )
    schema = json.loads(schema_path.read_text())
    validator = jsonschema.Draft202012Validator(schema, format_checker=artifact_format_checker)

    for line in sample_observed_jsonl.read_text().splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        validator.validate(record)


def test_propose_output_file_is_private_and_atomic(tmp_path, monkeypatch):
    import os
    import stat

    target = tmp_path / "proposal.md"
    monkeypatch.setattr(cli, "run_propose", lambda *a, **k: ({"proposed": []}, "# proposal\n"))

    result = CliRunner().invoke(
        cli.app,
        ["policy", "propose", "gov.yaml", "obs.jsonl", "--output", str(target)],
    )

    assert result.exit_code == 0, result.output
    assert target.read_text(encoding="utf-8") == "# proposal\n"
    if os.name != "nt":
        assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert [p.name for p in tmp_path.iterdir()] == ["proposal.md"]


def test_propose_reports_malformed_governance_yaml_without_traceback(tmp_path):
    governance = tmp_path / "governance.yaml"
    governance.write_text("agents: [\n  - id: x\n bad")
    observed = tmp_path / "observed.jsonl"
    observed.write_text("")

    result = CliRunner().invoke(cli.app, ["policy", "propose", str(governance), str(observed)])

    assert result.exit_code == 1
    assert isinstance(result.exception, SystemExit)
    assert result.stderr.startswith("ERROR: ")
    assert result.stdout == ""


def test_load_export_refuses_an_oversized_file(tmp_path, monkeypatch):
    from toolgraph import policy_propose

    monkeypatch.setattr(policy_propose, "MAX_EXPORT_BYTES", 16)
    export = tmp_path / "observed.jsonl"
    export.write_text("x" * 17)

    with pytest.raises(policy_propose.PolicyProposeError) as raised:
        policy_propose.load_export(export)

    assert str(raised.value) == f"refusing to load {export}: exceeds the 16-byte limit"


def _seed_matching_graph(gov_path: Path) -> None:
    schema.init_schema()
    digest = governance_digest(load_governance(gov_path))
    with driver.session() as s:
        s.run("CREATE (t:Tool {key: 'filesystem::read_file', name: 'read_file'})")
        s.run("CREATE (t:Tool {key: 'filesystem::write_file', name: 'write_file'})")
        s.run(
            "MERGE (m:GraphMeta {id: 'singleton'}) "
            "SET m.instance_id = 'test-instance', m.generation = 1, m.governance_digest = $d",
            d=digest,
        )


@pytest.mark.parametrize("extra", [[], ["--json"]])
@pytest.mark.parametrize(
    ("field", "value", "position"),
    [
        ("tool_key", "filesystem::read_file?token=hunter2", "additions[0].tool"),
        ("tool_key", "filesystem::password=hunter2", "additions[0].tool"),
        ("candidate", "Authorization: Bearer hunter2", "insufficient_evidence[0].tool"),
    ],
)
def test_cli_propose_refuses_a_credential_shaped_ref(
    backend_graph, sample_gov_path, sample_observed_jsonl, extra, field, value, position
):
    _seed_matching_graph(sample_gov_path)
    lines = sample_observed_jsonl.read_text().splitlines()
    row = json.loads(lines[1])
    row.pop("tool_key")
    row[field] = value
    lines[1] = json.dumps(row)
    sample_observed_jsonl.write_text("\n".join(lines) + "\n")

    result = CliRunner().invoke(
        cli.app,
        ["policy", "propose", str(sample_gov_path), str(sample_observed_jsonl), *extra],
    )

    assert result.exit_code == 1
    assert result.stdout == ""
    assert result.stderr == (
        f"ERROR: refusing to emit a proposal: {position} contains credential-shaped data\n"
    )


@pytest.mark.parametrize(
    ("label", "shown"),
    [
        ("august token=hunter2", "august token=<redacted>"),
        ("august Bearer hunter2.x", "august Bearer <redacted>"),
    ],
)
def test_window_label_is_redacted_in_the_rendered_proposal(
    backend_graph, sample_gov_path, sample_observed_jsonl, label, shown
):
    _seed_matching_graph(sample_gov_path)

    _, rendered = run_propose(sample_gov_path, sample_observed_jsonl, window_label=label)

    [header] = [line for line in rendered.splitlines() if line.startswith("decision_mode:")]
    assert header.split(" · ")[2] == f"window: {shown}"
    assert "hunter2" not in rendered


def test_tool_key_with_credential_word_in_server_name_is_proposed(
    backend_graph, sample_gov_path, sample_observed_jsonl
):
    _seed_matching_graph(sample_gov_path)
    lines = sample_observed_jsonl.read_text().splitlines()
    row = json.loads(lines[1])
    row["tool_key"] = "vault-secret::read_file"
    lines[1] = json.dumps(row)
    sample_observed_jsonl.write_text("\n".join(lines) + "\n")

    proposal, _ = run_propose(sample_gov_path, sample_observed_jsonl)

    assert [row["tool"] for row in proposal["additions"]] == ["vault-secret::read_file"]


def test_output_identity_check_names_the_position_not_the_value():
    from toolgraph.policy_propose import ProvenanceError, _check_output_identities

    proposal = {
        "additions": [],
        "removals": [],
        "conflicts": [],
        "not_grant_shaped": [],
        "insufficient_evidence": [
            {
                "agent": "ops-agent",
                "tool": "read_file",
                "reason": "AMBIGUOUS_GRANT",
                "note": "matches a::read_file or https://u:pw@host/x::read_file",
            }
        ],
        "provenance": {"observed_state": None, "current_state": {"instance_id": "g1"}},
    }

    with pytest.raises(ProvenanceError) as raised:
        _check_output_identities(proposal)

    assert str(raised.value) == (
        "refusing to emit a proposal: insufficient_evidence[0].note contains credential-shaped data"
    )


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("srv::password=hunter2", True),
        ("Authorization: Bearer abc", True),
        ("bearer abc.def", True),
        ("api-key: xyz", True),
        ("srv::get_token", False),
        ("token::refresh", False),
        ("vault-secret::read_file", False),
        ("srv::secret:abc", True),
        ("filesystem::read_file", False),
        ("token bucket refill", False),
        ("credential=abc", True),
    ],
)
def test_contains_credential(text, expected):
    from toolgraph.redaction import contains_credential

    assert contains_credential(text) is expected


@pytest.mark.parametrize(
    "keyword", ["api_key", "api-key", "access_token", "token", "password", "secret", "credential"]
)
def test_redact_text_scrubs_every_credential_keyword(keyword):
    from toolgraph.redaction import redact_text

    assert redact_text(f"{keyword}=hunter2") == f"{keyword}=<redacted>"


@pytest.mark.parametrize(
    "text",
    [
        "request failed: password::hunter2",
        "token:::hunter2",
        # A newly added name must not swallow the next one as its value.
        "credential=password =hunter2",
        "credential=token :hunter2",
    ],
)
def test_redact_text_never_leaves_a_value_readable(text):
    """Scrubbing errs toward hiding; only the refusal check treats '::' as a tool key."""
    from toolgraph.redaction import redact_text

    assert "hunter2" not in redact_text(text)


@pytest.mark.parametrize(
    ("uri", "credential"),
    [
        ("https://u:p@host/a", True),
        ("https://me@corp.example:p@host/a", True),
        ("https://u:p?x@host/a", True),
        ("https://me@corp.example:p?x@host", True),
        ("https://me@corp.example:p#x@host", True),
        # Refused on purpose: an '@' in the query after a port reads as userinfo.
        ("https://user@host:443?x=a@b", True),
        ("https://user@host:443/p@q", False),
        ("https://user@host:8080", False),
        ("http://[::1]:8080/x", False),
    ],
)
def test_credential_uri_shapes(uri, credential):
    from toolgraph.redaction import CREDENTIAL_URI

    assert bool(CREDENTIAL_URI.search(uri)) is credential


# Pure-logic contract, ported from the tests of the removed offline example
# (examples/policy_propose_from_observed.py). Its loader, envelope checks and
# proposal rules were copied into this module, so these drive the copy that
# ships. None of them needs a graph.

GOV = {
    "agents": ["bot"],
    "grants": [{"agent": "bot", "tool": "alpha::read_file", "granted_by": "team"}],
}
GRANTED = {("bot", "alpha::read_file"): "alpha::read_file"}


def _row(agent, ref, reasons, *, calls=9, complete=True, qualified=True):
    return {
        "agent": agent,
        "ref": ref,
        "has_tool_key": qualified,
        "decision": "rejected" if reasons else "eligible",
        "calls": calls,
        "would_block_calls": calls if reasons else 0,
        "reasons": reasons,
        "reasons_complete": complete,
    }


def _propose(rows, gov=GOV, granted=GRANTED):
    return propose(gov, rows, 3, granted=granted, ambiguous_grants=[])


def _rows_from(tmp_path, *lines):
    path = tmp_path / "observed.jsonl"
    path.write_text("".join(line + "\n" for line in lines), encoding="utf-8")
    return pp.load_export(path)[1]


def test_ungranted_and_unmapped_is_not_a_grant():
    p = _propose([_row("bot", "alpha::edit_file", ["NOT_GRANTED", "UNMAPPED"])])
    assert p["additions"] == []
    assert "a grant alone" in p["not_grant_shaped"][0]["note"]


def test_a_grant_does_not_cross_servers(backend_graph):
    """`alpha::read_file` and `beta::read_file` are different tools.

    Resolved through the graph, because resolution is where a grant could leak
    onto the other server: both servers expose the name here.
    """
    schema.init_schema()
    with driver.session() as s:
        for server in ("alpha", "beta"):
            s.run(
                "CREATE (m:MCPServer {name: $server}) "
                "CREATE (t:Tool {key: $server + '::read_file', name: 'read_file'}) "
                "CREATE (m)-[:EXPOSES]->(t)",
                server=server,
            )
        granted, ambiguous = resolve_grants_from_graph(s, GOV["grants"])
    assert granted == GRANTED and ambiguous == []

    p = _propose([_row("bot", "beta::read_file", ["NOT_GRANTED"])], granted=granted)
    assert [r["tool"] for r in p["additions"]] == ["beta::read_file"]

    zero = _propose([_row("bot", "beta::read_file", [], calls=0)], granted=granted)
    assert zero["removals"] == []


def test_removal_needs_an_explicit_zero_call_row():
    """Silence about a grant is not evidence that it is unused."""
    p = _propose([_row("bot", "alpha::other", ["NOT_GRANTED"])])
    assert p["removals"] == []

    observed = _propose([_row("bot", "alpha::read_file", [], calls=0)])
    assert observed["removals"] == [{"agent": "bot", "tool": "alpha::read_file"}]


def test_legacy_single_reason_export_is_never_proposed_from():
    p = _propose([_row("bot", "alpha::move_file", ["NOT_GRANTED"], complete=False)])
    assert p["additions"] == []
    assert [r["tool"] for r in p["insufficient_evidence"]] == ["alpha::move_file"]


def test_candidate_only_row_is_report_only():
    p = _propose([_row("bot", "read_file", ["NOT_GRANTED"], qualified=False)])
    assert p["additions"] == []
    assert "canonical tool_key" in p["insufficient_evidence"][0]["note"]


def test_candidate_only_row_cannot_drive_a_removal():
    """The ref in a candidate-only row is what the agent asked for, not a resolved tool."""
    p = _propose([_row("bot", "alpha::read_file", [], calls=0, qualified=False)])
    assert p["removals"] == []


def test_unknown_agent_is_refused():
    with pytest.raises(pp.PolicyProposeError, match="unknown agents"):
        _propose([_row("ghost", "alpha::read_file", ["NOT_GRANTED"])])


def test_bare_tool_key_is_malformed_not_canonical(tmp_path):
    line = json.dumps(
        {
            "agent": "bot",
            "decision": "rejected",
            "tool_key": "read_file",
            "calls": 9,
            "would_block_calls": 9,
            "reject_reasons": ["NOT_GRANTED"],
        }
    )
    with pytest.raises(ValueError, match="server::tool"):
        _rows_from(tmp_path, line)


@pytest.mark.parametrize(
    "line",
    [
        '{"agent": "bot", "decision": "maybe", "tool_key": "a::b"}',
        '{"agent": "bot", "decision": "rejected", "tool_key": "a::b", "calls": -1}',
        '{"agent": "bot", "decision": "rejected", "tool_key": "a::b", "calls": 1,'
        ' "would_block_calls": 5}',
        '{"agent": "bot", "decision": "rejected", "tool_key": "a::b", "reject_reasons": ["NOPE"]}',
        '{"agent": "bot", "decision": "rejected", "tool_key": "a::b"}',
        '{"agent": "bo\\u0000t", "decision": "eligible", "tool_key": "a::b"}',
    ],
    ids=[
        "bad-decision",
        "negative",
        "would-block-exceeds",
        "unknown-reason",
        "rejected-without-reason",
        "control-char",
    ],
)
def test_malformed_rows_are_refused(tmp_path, line):
    with pytest.raises(ValueError):
        _rows_from(tmp_path, line)


def test_duplicate_rows_are_refused(tmp_path):
    row = json.dumps({"agent": "bot", "decision": "eligible", "tool_key": "a::b", "calls": 1})
    with pytest.raises(ValueError, match="duplicate"):
        _rows_from(tmp_path, row, row)


def test_absent_calls_is_not_an_explicit_zero(tmp_path):
    """Silence about a count must not read as an observed zero."""
    granted = {"agent": "bot", "decision": "eligible", "tool_key": "alpha::read_file"}

    silent = _rows_from(tmp_path, json.dumps(granted))
    assert _propose(silent)["removals"] == []

    stated = _rows_from(tmp_path, json.dumps({**granted, "calls": 0}))
    assert _propose(stated)["removals"] == [{"agent": "bot", "tool": "alpha::read_file"}]


def test_would_block_without_a_calls_count_is_refused(tmp_path):
    """Blocked calls with no total is an incomplete window, not weak evidence."""
    line = json.dumps(
        {
            "agent": "bot",
            "decision": "rejected",
            "tool_key": "alpha::write_file",
            "would_block_calls": 7,
            "reject_reasons": ["NOT_GRANTED"],
        }
    )
    with pytest.raises(ValueError, match="without a 'calls' count"):
        _rows_from(tmp_path, line)


def test_unstated_calls_never_count_as_usage(tmp_path):
    rows = _rows_from(
        tmp_path,
        json.dumps({"agent": "bot", "decision": "eligible", "tool_key": "alpha::read_file"}),
    )
    assert rows[0]["calls"] is None


def test_rendered_entries_are_yaml_not_string_interpolation():
    import yaml

    hostile = pp.render(
        {
            "additions": [
                {
                    "agent": "bot",
                    "tool": 'a::b", extra: "x',
                    "would_block_calls": 9,
                    "reason": "NOT_GRANTED",
                }
            ],
            "removals": [],
            "conflicts": [],
            "not_grant_shaped": [],
            "insufficient_evidence": [],
        },
        "test",
        3,
    )
    entry = next(line for line in hostile.splitlines() if line.startswith("  - "))
    parsed = yaml.safe_load(entry[len("  - ") :].split("  #")[0])
    assert parsed["tool"] == 'a::b", extra: "x'
    assert set(parsed) == {"agent", "tool", "granted_by"}
    # A fragment, not a section: declarative ingest would drop everything a
    # replacement `grants:` key omitted.
    assert "\ngrants:" not in hostile


def test_an_unbound_report_leads_with_the_provenance_limitation():
    report = pp.render(_propose([_row("bot", "alpha::write_file", ["NOT_GRANTED"])]), "2026-08", 3)
    assert "UNVERIFIED PROVENANCE" in report
    assert report.index("UNVERIFIED PROVENANCE") < report.index("```yaml")


def test_a_bound_report_states_what_it_is_bound_to():
    proposal = _propose([_row("bot", "alpha::write_file", ["NOT_GRANTED"])])
    bound = pp.render(proposal, "2026-08", 3, ENVELOPE)
    assert "DECLARED DIGEST MATCHES" in bound
    assert "UNVERIFIED PROVENANCE" not in bound
    assert ENVELOPE["governance_digest"] in bound
    # A non-strict window must say so: absence from these rows is not permission.
    assert "`review` profile" in bound
    # The report must not claim more than the digest comparison establishes.
    assert "(declared)" in bound

    unbound = pp.render(proposal, "2026-08", 3, None)
    assert "UNVERIFIED PROVENANCE" in unbound
    assert "DECLARED DIGEST MATCHES" not in unbound


def test_an_export_without_an_envelope_is_refused_by_default(sample_gov_path, tmp_path):
    """Refused before the graph is read, unless provenance is waived explicitly."""
    observed = tmp_path / "bare.jsonl"
    observed.write_text(
        json.dumps(
            {
                "agent": "public-bot",
                "decision": "eligible",
                "tool_key": "filesystem::read_file",
                "calls": 1,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    with patch.object(pp.queries, "with_graph_state") as read:
        with pytest.raises(pp.ProvenanceError, match="carries no envelope"):
            run_propose(sample_gov_path, observed)
    read.assert_not_called()


ENVELOPE = {
    "governance_digest": "a" * 64,
    "graph_generation": 7,
    "profile": "review",
    "window_start": "2026-08-01T00:00:00Z",
    "window_end": "2026-08-31T23:59:59Z",
}
PROFILES = {"strict", "review", "explore"}


def _win(start, end):
    return {**ENVELOPE, "window_start": start, "window_end": end}


def _verify(envelope, digest=ENVELOPE["governance_digest"]):
    return pp.verify_envelope(envelope, digest, PROFILES)


def _export(tmp_path, envelope, *rows):
    path = tmp_path / "observed.jsonl"
    lines = [] if envelope is None else [json.dumps({"envelope": envelope})]
    lines += [json.dumps(r) for r in rows]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_a_window_recorded_against_other_governance_is_refused():
    """The digest is what separates evidence from a replay of an older manifest."""
    with pytest.raises(pp.ProvenanceError, match="different governance"):
        _verify(ENVELOPE, "b" * 64)

    bound = _verify(ENVELOPE)
    assert bound["graph_generation"] == 7
    assert bound["profile"] == "review"


@pytest.mark.parametrize(
    "field, value, match",
    [
        ("governance_digest", "not-hex", "64 lowercase hex"),
        ("graph_generation", -1, "non-negative integer"),
        ("graph_generation", True, "non-negative integer"),
        ("profile", "paranoid", "unknown selector profile"),
        ("window_start", "", "non-empty string"),
    ],
    ids=["bad-digest", "negative-generation", "bool-generation", "unknown-profile", "empty-bound"],
)
def test_malformed_envelopes_are_refused(field, value, match):
    with pytest.raises(pp.ProvenanceError, match=match):
        _verify({**ENVELOPE, field: value})


def test_an_incomplete_envelope_binds_nothing():
    """Half an envelope is refused rather than half-trusted."""
    for field in pp.ENVELOPE_FIELDS:
        partial = {k: v for k, v in ENVELOPE.items() if k != field}
        with pytest.raises(pp.ProvenanceError, match="missing"):
            _verify(partial)


def test_a_backwards_window_is_refused():
    with pytest.raises(pp.ProvenanceError, match="is after"):
        _verify(_win("2026-09-01T00:00:00Z", "2026-08-01T00:00:00Z"))


def test_window_bounds_are_compared_as_instants_not_strings():
    """ "2026-08-01T01:00:00+02:00" sorts after "...00:00:00Z" but names an earlier moment."""
    ok = _win("2026-08-01T01:00:00+02:00", "2026-08-01T00:00:00Z")
    assert _verify(ok)["window_start"] == "2026-08-01T01:00:00+02:00"

    with pytest.raises(pp.ProvenanceError, match="is after"):
        _verify(_win("2026-08-01T00:00:00-02:00", "2026-08-01T01:00:00Z"))


def test_a_window_bound_that_is_not_a_timestamp_is_refused():
    with pytest.raises(pp.ProvenanceError, match="must be a timestamp"):
        _verify(_win("last tuesday", "2026-08-31T00:00:00Z"))


@pytest.mark.parametrize(
    "bound",
    [
        "2026-09-09",
        "2026-09-09X00:00:00+00:00",
        "2026-08-01T00:00:00",
        "2026-08-01T00:00:00+00:60",
    ],
    ids=["date-only", "wrong-separator", "no-offset", "out-of-range-offset"],
)
def test_ambiguous_or_repaired_timestamps_are_refused(bound):
    """`fromisoformat` takes a bare date, any separator, and repairs "+00:60"."""
    with pytest.raises(pp.ProvenanceError, match="offset|must be a timestamp"):
        _verify(_win(bound, "2026-08-31T00:00:00Z"))


@pytest.mark.parametrize(
    "bound",
    [
        "2026-02-30T00:00:00Z",
        "2026-08-01T24:00:00Z",
        "2026-08-01T00:60:00Z",
        "2026-08-01T00:00:60Z",
        "2026-08-01T00:00:00-00:00",
        "2026-08-01t00:00:00z",
        "2026-08-01T00:00:00.1234567Z",
    ],
    ids=[
        "impossible-date",
        "hour-24",
        "minute-60",
        "second-60",
        "minus-zero-offset",
        "lowercase",
        "too-many-fractions",
    ],
)
def test_every_rejected_bound_leaves_as_a_clean_refusal(bound):
    """Never a bare ValueError: "24:00:00" parses differently on 3.13 and 3.14,
    and "-00:00" is RFC 3339 for "offset unknown"."""
    with pytest.raises(pp.ProvenanceError):
        _verify(_win(bound, "2026-08-31T00:00:00Z"))


def test_fully_qualified_timestamps_are_accepted():
    for start in (
        "2026-08-01T00:00:00Z",
        "2026-08-01T00:00:00.123456Z",
        "2026-08-01T02:00:00+02:00",
    ):
        assert _verify(_win(start, "2026-08-31T00:00:00Z"))["window_start"] == start


@pytest.mark.parametrize("bad", [[], {}, 3], ids=["list", "dict", "int"])
def test_a_non_string_profile_is_refused_cleanly(bad):
    """Bare membership raises TypeError on an unhashable value."""
    with pytest.raises(pp.ProvenanceError, match="unknown selector profile"):
        _verify({**ENVELOPE, "profile": bad})


def test_the_envelope_is_split_off_and_rows_keep_their_line_numbers(tmp_path):
    """An envelope must not be parsed as an observation, nor shift error lines."""
    good = {"agent": "bot", "decision": "eligible", "tool_key": "alpha::read_file", "calls": 3}
    envelope, rows = pp.load_export(_export(tmp_path, ENVELOPE, good))
    assert envelope == ENVELOPE
    assert [r["agent"] for r in rows] == ["bot"]

    # The bad row is on file line 3; the message must say 3, not 2.
    bad = {"agent": "bot", "decision": "nonsense", "tool_key": "alpha::write_file"}
    with pytest.raises(ValueError, match=r":3: decision must be"):
        pp.load_export(_export(tmp_path, ENVELOPE, good, bad))


def test_an_envelope_line_may_not_smuggle_observation_fields(tmp_path):
    path = tmp_path / "observed.jsonl"
    path.write_text(
        json.dumps({"envelope": ENVELOPE, "agent": "bot", "decision": "eligible"}) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(pp.ProvenanceError, match="own rows"):
        pp.load_export(path)


def test_an_export_without_an_envelope_still_parses(tmp_path):
    """The loader stays usable; refusing is run_propose's decision, not the parser's."""
    envelope, rows = pp.load_export(
        _export(
            tmp_path,
            None,
            {"agent": "bot", "decision": "eligible", "tool_key": "alpha::read_file", "calls": 3},
        )
    )
    assert envelope is None
    assert len(rows) == 1


def test_min_would_block_must_be_positive(sample_gov_path, sample_observed_jsonl):
    result = CliRunner().invoke(
        cli.app,
        [
            "policy",
            "propose",
            str(sample_gov_path),
            str(sample_observed_jsonl),
            "--min-would-block",
            "0",
        ],
    )
    assert result.exit_code == 2


@pytest.fixture
def proposal_case(backend_graph, tmp_path):
    keys = ["clean", "unmapped", "denied", "retired", "deleted", "already"]
    loader.load_crawl_result(
        CrawlResult(
            server_name="sample",
            tools=[ToolRecord(name=key) for key in keys],
            resources=[ResourceRecord(uri="file:///data")],
        )
    )
    gov = Governance.model_validate(
        {
            "agents": ["bot"],
            "policies": [{"id": "deny", "effect": "DENY"}],
            "data_access": [
                {"tool": f"sample::{key}", "resource": "file:///data", "mode": "READS"}
                for key in keys
                if key != "unmapped"
            ],
            "governed_by": {"sample::denied": [{"policy": "deny"}]},
        }
    )
    assert ingest_governance(gov).applied
    governance = tmp_path / "governance.yaml"
    governance.write_text(yaml.safe_dump(gov.model_dump(mode="json")))
    state = queries.graph_state()
    envelope = {
        "graph_instance_id": state.instance_id,
        "graph_generation": state.generation,
        "governance_digest": state.governance_digest,
        "profile": "strict",
        "window_start": "2026-09-01T00:00:00Z",
        "window_end": "2026-09-02T00:00:00Z",
    }
    observed = tmp_path / "observed.jsonl"

    def export(env=envelope, tools=("clean",)):
        lines = [] if env is None else [json.dumps({"envelope": env})]
        lines += [
            json.dumps(
                {
                    "agent": "bot",
                    "tool_key": f"sample::{key}",
                    "decision": "rejected",
                    "calls": 5,
                    "would_block_calls": 5,
                    "reject_reasons": ["NOT_GRANTED"],
                }
            )
            for key in tools
        ]
        observed.write_text("\n".join(lines) + "\n")
        return governance, observed

    export()
    return envelope, export


@pytest.mark.parametrize(
    "missing", ["envelope", "observed.instance", "current.instance", "current.digest"]
)
def test_incomplete_identity_requires_opt_in_and_stays_unverified(proposal_case, missing):
    envelope, export = proposal_case
    envelope = dict(envelope)
    if missing == "envelope":
        envelope = None
    elif missing == "observed.instance":
        envelope.pop("graph_instance_id")
    else:
        field = "instance_id" if missing == "current.instance" else "governance_digest"
        with driver.session() as session:
            session.run(f"MATCH (m:GraphMeta {{id:'singleton'}}) SET m.{field}=NULL")
    args = export(envelope)
    for revalidate in (False, True):
        with pytest.raises(ProvenanceError):
            run_propose(*args, revalidate_current=revalidate)
        proposal, rendered = run_propose(
            *args,
            unverified_provenance=True,
            revalidate_current=revalidate,
        )
        assert proposal["provenance"]["status"] == "unverified"
        assert proposal["provenance"]["missing_fields"]
        assert proposal["provenance"]["current_revalidated"] is revalidate
        assert "UNVERIFIED PROVENANCE" in rendered
        assert "exact composite token matched" not in rendered
        assert len(proposal["additions"]) == 1
    result = CliRunner().invoke(
        cli.app,
        [
            "policy",
            "propose",
            *map(str, args),
            "--unverified-provenance",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["provenance"]["status"] == "unverified"


@pytest.mark.parametrize(
    "field,value",
    [
        ("graph_instance_id", 1),
        ("graph_instance_id", True),
        ("graph_instance_id", "bad\nidentity"),
        ("graph_instance_id", " spaced "),
        ("instance_id", ["bad"]),
    ],
)
def test_invalid_identity_is_not_an_opt_in(proposal_case, field, value):
    envelope, export = proposal_case
    args = export({**envelope, field: value})
    with pytest.raises(ProvenanceError):
        run_propose(*args, unverified_provenance=True, revalidate_current=True)


def test_alias_compatibility_and_conflict(proposal_case):
    envelope, export = proposal_case
    alias = dict(envelope)
    alias["instance_id"] = alias.pop("graph_instance_id")
    for env in (alias, {**envelope, **alias}):
        proposal, rendered = run_propose(*export(env))
        assert proposal["provenance"]["status"] == "exact"
        assert "exact composite token matched" in rendered
    with pytest.raises(ProvenanceError, match="conflict"):
        run_propose(*export({**envelope, "instance_id": "different"}), unverified_provenance=True)
    for value in (None, "", "   "):
        args = export({**envelope, "graph_instance_id": value})
        with pytest.raises(ProvenanceError, match="incomplete provenance"):
            run_propose(*args)
        proposal, _ = run_propose(*args, unverified_provenance=True)
        assert proposal["provenance"]["status"] == "unverified"


@pytest.mark.parametrize("mismatch", ["instance", "live_digest", "manifest_digest", "generation"])
def test_mismatches_cannot_be_bypassed(proposal_case, mismatch):
    envelope, export = proposal_case
    env = dict(envelope)
    if mismatch == "instance":
        env["graph_instance_id"] = "different-instance"
    elif mismatch == "manifest_digest":
        env["governance_digest"] = "a" * 64
    elif mismatch == "generation":
        env["graph_generation"] -= 1
    else:
        with driver.session() as session:
            session.run(
                "MATCH (m:GraphMeta {id:'singleton'}) SET m.governance_digest=$d", d="b" * 64
            )
    args = export(env)
    for unverified in (False, True):
        if mismatch == "generation":
            with pytest.raises(StateMismatchError):
                run_propose(*args, unverified_provenance=unverified)
            proposal, _ = run_propose(
                *args, revalidate_current=True, unverified_provenance=unverified
            )
            assert proposal["provenance"]["status"] == "revalidated"
        else:
            with pytest.raises((ProvenanceError, StateMismatchError)):
                run_propose(*args, unverified_provenance=unverified, revalidate_current=True)


@pytest.mark.parametrize("profile", ["strict", "review", "explore"])
def test_current_revalidation_checks_all_strict_blockers_even_same_generation(
    proposal_case, profile
):
    envelope, export = proposal_case
    with driver.session() as session:
        session.run("MATCH (:MCPServer)-[e:EXPOSES]->(:Tool {key:'sample::retired'}) DELETE e")
        session.run("MATCH (t:Tool {key:'sample::deleted'}) DETACH DELETE t")
        session.run(
            "MATCH (a:Agent {id:'bot'}), (t:Tool {key:'sample::already'}) CREATE (a)-[:CAN_CALL]->(t)"
        )
    args = export(
        {**envelope, "profile": profile},
        ("clean", "unmapped", "denied", "retired", "deleted", "already"),
    )
    proposal, rendered = run_propose(*args, revalidate_current=True)
    assert [row["tool"] for row in proposal["additions"]] == ["sample::clean"]
    assert {row["reason"] for row in proposal["not_grant_shaped"]} == {
        "UNMAPPED",
        "DRIFTED",
        "TOOL_NOT_FOUND",
    }
    assert [row["reason"] for row in proposal["conflicts"]] == ["DENY_VIOLATION"]
    assert [row["reason"] for row in proposal["insufficient_evidence"]] == ["ALREADY_GRANTED"]
    assert proposal["provenance"]["revalidation_profile"] == "strict"
    assert "profile=strict" in rendered
    # Hypothetical grants are never persisted.
    with driver.session() as session:
        assert (
            session.run(
                "MATCH (:Agent {id:'bot'})-[:CAN_CALL]->(t:Tool) RETURN t.key AS k"
            ).single()["k"]
            == "sample::already"
        )


def test_missing_current_agent_is_insufficient_evidence(proposal_case):
    _, export = proposal_case
    with driver.session() as session:
        session.run("MATCH (a:Agent {id:'bot'}) DETACH DELETE a")
    proposal, _ = run_propose(*export(), revalidate_current=True)
    assert not proposal["additions"]
    assert proposal["insufficient_evidence"][0]["reason"] == "AGENT_NOT_FOUND"


def test_revalidation_retries_all_reads_after_catalog_change(proposal_case, monkeypatch):
    _, export = proposal_case
    original = selector.rank_features
    calls = []

    def change_after_read(agent, candidates):
        ranked = original(agent, candidates)
        calls.append(candidates)
        if len(calls) == 1:
            with driver.session() as session:
                session.run(
                    "MATCH (:MCPServer)-[e:EXPOSES]->(:Tool {key:'sample::clean'}) DELETE e"
                )
                session.run("MATCH (m:GraphMeta {id:'singleton'}) SET m.generation=m.generation+1")
        return ranked

    monkeypatch.setattr(selector, "rank_features", change_after_read)
    monkeypatch.setattr(queries, "_bracket_backoff", lambda _: None)
    proposal, _ = run_propose(*export(), revalidate_current=True)
    assert len(calls) == 2
    assert not proposal["additions"]
    assert proposal["not_grant_shaped"][0]["reason"] == "DRIFTED"


def test_revalidation_chunks_candidates_without_mutating_features(tmp_path, monkeypatch):
    # Exercise the public workflow with more than the selector's real limit;
    # bounded calls are instrumented, graph semantics are covered above.
    count = selector.MAX_CANDIDATES + 1
    governance = tmp_path / "gov.yaml"
    governance.write_text("agents: [bot]\n")
    observed = tmp_path / "observed.jsonl"
    observed.write_text(
        "\n".join(
            json.dumps(
                {
                    "agent": "bot",
                    "tool_key": f"sample::t{i}",
                    "decision": "rejected",
                    "calls": 5,
                    "would_block_calls": 5,
                    "reject_reasons": ["NOT_GRANTED"],
                }
            )
            for i in range(count)
        )
    )
    monkeypatch.setattr(queries, "graph_state", lambda: queries.GraphState("instance", 1))
    from contextlib import nullcontext

    monkeypatch.setattr(driver, "session", lambda: nullcontext(None))
    batches, originals = [], []

    def ranked(agent, candidates):
        batches.append(len(candidates))
        rows = [
            {
                "candidate": key,
                "tool_key": key,
                "found": True,
                "ambiguous": False,
                "permitted": False,
                "classification": None,
                "is_drifted": False,
                "is_unmapped": False,
            }
            for key in candidates
        ]
        originals.extend(rows)
        return {"agent": agent, "agent_found": True, "features": rows}

    monkeypatch.setattr(selector, "rank_features", ranked)
    proposal, _ = run_propose(
        governance, observed, unverified_provenance=True, revalidate_current=True
    )
    assert batches == [selector.MAX_CANDIDATES, 1]
    assert len(proposal["additions"]) == count
    assert all(not row["permitted"] for row in originals)


def test_no_envelope_does_not_waive_known_live_manifest_mismatch(proposal_case):
    _, export = proposal_case
    with driver.session() as session:
        session.run("MATCH (m:GraphMeta {id:'singleton'}) SET m.governance_digest=$d", d="a" * 64)
    with pytest.raises(StateMismatchError, match="digest mismatch"):
        run_propose(*export(None), unverified_provenance=True, revalidate_current=True)


@pytest.mark.parametrize("value", ["invalid-digest", "a" * 63, "A" * 64])
def test_invalid_live_digest_cannot_be_bypassed(proposal_case, value):
    _, export = proposal_case
    with driver.session() as session:
        session.run("MATCH (m:GraphMeta {id:'singleton'}) SET m.governance_digest=$d", d=value)
    with pytest.raises(ProvenanceError, match="live governance_digest"):
        run_propose(*export(), unverified_provenance=True, revalidate_current=True)


def test_current_expected_exception_remains_deny_governed(proposal_case):
    envelope, export = proposal_case
    with driver.session() as session:
        session.run(
            "CREATE (:ExpectedException {key:'probe', agent:'bot', "
            "tool_key:'sample::denied', policy:'deny', reason:'operator exception'})"
        )
    proposal, _ = run_propose(
        *export({**envelope, "profile": "explore"}, ("denied",)), revalidate_current=True
    )
    assert not proposal["additions"]
    assert proposal["conflicts"][0]["reason"] == "DENY_GOVERNED"


def test_revalidation_churn_exhausts_five_complete_attempts(proposal_case, monkeypatch):
    _, export = proposal_case
    original = selector.rank_features
    attempts = []

    def unstable(agent, candidates):
        ranked = original(agent, candidates)
        attempts.append(agent)
        with driver.session() as session:
            session.run("MATCH (m:GraphMeta {id:'singleton'}) SET m.generation=m.generation+1")
        return ranked

    monkeypatch.setattr(selector, "rank_features", unstable)
    monkeypatch.setattr(queries, "_bracket_backoff", lambda _: None)
    from toolgraph.policy_propose import StateChurnError

    with pytest.raises(StateChurnError, match="exhausted 5 read attempts"):
        run_propose(*export(), revalidate_current=True)
    assert len(attempts) == 5


def test_unverified_flag_alone_does_not_waive_generation_change(proposal_case):
    envelope, export = proposal_case
    env = dict(envelope)
    env.pop("graph_instance_id")
    env["graph_generation"] -= 1
    args = export(env)
    with pytest.raises(StateMismatchError):
        run_propose(*args, unverified_provenance=True)
    with pytest.raises(ProvenanceError):
        run_propose(*args, revalidate_current=True)
    proposal, rendered = run_propose(*args, unverified_provenance=True, revalidate_current=True)
    assert proposal["provenance"]["status"] == "unverified"
    assert proposal["provenance"]["current_revalidated"] is True
    assert "UNVERIFIED PROVENANCE" in rendered


def test_proposal_file_write_failure_has_redacted_error(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "run_propose", lambda *a, **k: ({}, "proposal"))
    result = CliRunner().invoke(
        cli.app, ["policy", "propose", "gov.yaml", "obs.jsonl", "-o", str(tmp_path)]
    )
    assert result.exit_code == 1
    assert "ERROR:" in result.stderr
    assert not isinstance(result.exception, OSError)
    assert "Traceback" not in result.output
