"""G2 advisory preflight artifact tests (hermetic query seams)."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

from typer.testing import CliRunner

from toolgraph import cli
from toolgraph.preflight import build_preflight, redact


NOW = datetime(2026, 7, 11, tzinfo=timezone.utc)


def _bracket(monkeypatch, payload, generation=42):
    monkeypatch.setattr(
        "toolgraph.preflight.queries.with_graph_state",
        lambda fetch, **kwargs: {
            **fetch(),
            "graph_generation": generation,
            "graph_instance_id": "test-instance",
            "graph_state": {"instance_id": "test-instance", "generation": generation},
        },
    )
    monkeypatch.setattr(
        "toolgraph.preflight.selector.eligible_tools", lambda *a, **k: payload
    )


def test_allow_artifact_is_deterministic(monkeypatch):
    _bracket(monkeypatch, {
        "agent": "codex", "agent_found": True, "profile": "review",
        "eligible": ["alpha::read"], "rejected": [],
    })
    kwargs = dict(agent="codex", candidates=["alpha::read"], profile="review",
                  run_id="run-1", created_at=NOW)
    assert build_preflight(**kwargs) == build_preflight(**kwargs)
    doc = build_preflight(**kwargs)
    assert doc["decision"] == "advisory_allow"
    assert doc["graph_generation"] == 42
    assert doc["graph_state"] == {"instance_id": "test-instance", "generation": 42}
    assert doc["created_at"] == "2026-07-11T00:00:00+00:00"


def test_unknown_agent_is_unresolved(monkeypatch):
    _bracket(monkeypatch, {
        "agent": "ghost", "agent_found": False, "profile": "review",
        "eligible": [], "rejected": [],
    })
    doc = build_preflight(agent="ghost", candidates=[], profile="review",
                          run_id="run-2", created_at=NOW)
    assert doc["decision"] == "unresolved_identity"


def test_allow_matches_committed_golden_except_timestamp(monkeypatch):
    golden = json.loads(
        (Path(__file__).parent.parent / "contracts" / "fixtures" /
         "preflight-allow.json").read_text()
    )
    _bracket(monkeypatch, {
        "agent": golden["agent"], "agent_found": True, "profile": golden["profile"],
        "eligible": golden["eligible"], "rejected": golden["rejected"],
    }, generation=golden["graph_generation"])
    doc = build_preflight(
        agent=golden["agent"], candidates=golden["eligible"], profile=golden["profile"],
        run_id=golden["run_id"], created_at=NOW,
    )
    assert doc == golden


def test_warn_and_recursive_redaction(monkeypatch):
    _bracket(monkeypatch, {
        "agent": "codex", "agent_found": True, "profile": "review", "eligible": [],
        "rejected": [{
            "candidate": "alpha::write", "tool_key": "alpha::write",
            "reason": "DENY_VIOLATION",
            "paths": ["(write) -WRITES-> (https://user:secret@example.test/data?q=token)"],
        }],
    })
    doc = build_preflight(agent="codex", candidates=["alpha::write"], profile="review",
                          run_id="run-3", created_at=NOW)
    assert doc["decision"] == "advisory_warn"
    path = doc["rejected"][0]["paths"][0]
    assert path == "(write) -WRITES-> (https://example.test/data)"
    assert "secret" not in json.dumps(doc)
    assert "q=token" not in json.dumps(doc)


def test_redaction_removes_fragment_and_preserves_surrounding_text():
    assert redact("x (mcp://user:pw@CaseHost:8080/r?a=b#frag) y") == (
        "x (mcp://CaseHost:8080/r) y"
    )


def test_cli_writes_artifact(monkeypatch, tmp_path):
    artifact = {
        "schema_version": 1, "kind": "toolgraph.preflight", "run_id": "r",
        "created_at": "2026-07-11T00:00:00+00:00", "graph_generation": 1,
        "agent": "codex", "agent_found": True, "profile": "review",
        "mode": "advisory", "eligible": [], "rejected": [],
        "decision": "advisory_allow",
    }
    monkeypatch.setattr(cli, "build_preflight", lambda **kwargs: artifact)
    out = tmp_path / "nested" / "preflight.json"
    result = CliRunner().invoke(
        cli.app, ["preflight", "codex", "--profile", "review", "--run-id", "r",
                  "--out", str(out), "alpha::read"],
    )
    assert result.exit_code == 0, result.output
    assert json.loads(out.read_text()) == artifact


def test_cli_out_prints_digest_of_exact_file_bytes(monkeypatch, tmp_path):
    _bracket(monkeypatch, {
        "agent": "codex", "agent_found": True, "profile": "review",
        "eligible": ["alpha::read"], "rejected": [],
    })
    out = tmp_path / "preflight.json"
    result = CliRunner().invoke(
        cli.app, ["preflight", "codex", "alpha::read", "--profile", "review",
                  "--run-id", "r", "--out", str(out)],
    )
    assert result.exit_code == 0, result.output
    envelope = json.loads(result.stdout)
    assert envelope["artifact_digest"] == hashlib.sha256(out.read_bytes()).hexdigest()
    assert envelope["output"] == str(out)
    assert envelope["kind"] == "toolgraph.preflight"
    assert envelope["run_id"] == "r"
    assert envelope["graph_generation"] == 42
    assert envelope["decision"] == "advisory_allow"
    assert envelope["eligible"] == 1 and envelope["rejected"] == 0
    assert out.stat().st_mode & 0o777 == 0o600


def test_cli_stdout_emits_json(monkeypatch):
    artifact = {"decision": "advisory_allow", "eligible": ["alpha::read"]}
    monkeypatch.setattr(cli, "build_preflight", lambda **kwargs: artifact)
    result = CliRunner().invoke(
        cli.app, ["preflight", "codex", "alpha::read", "--run-id", "r"],
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == artifact


def test_cli_file_path_composes_producer_redaction(monkeypatch, tmp_path):
    _bracket(monkeypatch, {
        "agent": "codex", "agent_found": True, "profile": "review", "eligible": [],
        "rejected": [{
            "candidate": "alpha::write", "tool_key": "alpha::write",
            "reason": "DENY_VIOLATION",
            "paths": ["(write) -> (mcp://user:secret@CaseHost/data?token=bad)"],
        }],
    })
    out = tmp_path / "preflight.json"
    result = CliRunner().invoke(
        cli.app, ["preflight", "codex", "alpha::write", "--profile", "review",
                  "--run-id", "r", "--out", str(out)],
    )
    assert result.exit_code == 0, result.output
    raw = out.read_text()
    assert "secret" not in raw and "token=bad" not in raw
    assert "mcp://CaseHost/data" in raw


def test_cli_atomic_replace_failure_cleans_temporary(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "build_preflight", lambda **kwargs: {"decision": "advisory_allow"})
    monkeypatch.setattr(
        "toolgraph.artifacts.os.replace",
        lambda *args: (_ for _ in ()).throw(OSError("full")),
    )
    out = tmp_path / "preflight.json"
    result = CliRunner().invoke(
        cli.app, ["preflight", "codex", "alpha::read", "--run-id", "r",
                  "--out", str(out)],
    )
    assert result.exit_code == 1
    assert not out.exists()
    assert list(tmp_path.iterdir()) == []
