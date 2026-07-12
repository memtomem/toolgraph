from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


SCRIPTS = Path(__file__).parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
SPEC = importlib.util.spec_from_file_location(
    "gate_e_operational", SCRIPTS / "gate_e_operational.py"
)
assert SPEC is not None and SPEC.loader is not None
gate_e = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gate_e)


def test_provider_scope_is_explicit_and_non_gating() -> None:
    assert gate_e.REQUIRED_PROVIDERS == ("codex", "claude", "kimi")
    assert gate_e.provider_scope() == {
        "required": ["codex", "claude", "kimi"],
        "excluded": [{
            "provider": "agy",
            "classification": "compatibility_only",
            "reason": "not_syncmill_agent_runner",
        }],
    }


def test_provider_versions_never_probes_antigravity(monkeypatch: pytest.MonkeyPatch) -> None:
    commands: list[list[str]] = []

    def fake_run(command: list[str], **_kwargs: object):
        commands.append(command)
        return gate_e.subprocess.CompletedProcess(command, 0, stdout="v1\n", stderr="")

    monkeypatch.setattr(gate_e, "run", fake_run)

    versions = gate_e.provider_versions()

    assert set(versions) == {"codex", "claude", "kimi"}
    assert [command[0] for command in commands] == ["codex", "claude", "kimi"]


@pytest.mark.parametrize(
    ("observed", "expected"),
    [
        ([
            {"tool_key": "gate_e::always_ok", "outcome": "succeeded"},
            {"tool_key": "gate_e::always_fail", "outcome": "failed"},
        ], True),
        ([], False),
        ([{"tool_key": "gate_e::always_ok", "outcome": "succeeded"}], False),
        ([
            {"tool_key": "gate_e::always_ok", "outcome": "succeeded"},
            {"tool_key": "gate_e::always_fail", "outcome": "failed"},
            {"tool_key": "gate_e::always_fail", "outcome": "failed"},
        ], False),
        ([
            {"tool_key": "gate_e::always_fail", "outcome": "failed"},
            {"tool_key": "gate_e::always_ok", "outcome": "succeeded"},
        ], False),
    ],
)
def test_exact_outcome_contract(
    observed: list[dict[str, str]], expected: bool
) -> None:
    assert gate_e._has_exact_outcomes(observed) is expected


def test_antigravity_command_is_not_supported(tmp_path: Path) -> None:
    with pytest.raises(gate_e.SmokeError, match="unknown provider: agy"):
        gate_e._provider_command("agy", tmp_path, tmp_path / "spool.ndjson")
