"""Contract for the governance-proposal example.

The example decides whether to propose widening an agent's authority, so its
mistakes are governance mistakes. It shipped with an embedded ``--self-test``
that nobody runs: CI runs pytest, and a cross-server revocation bug passed that
self-test unnoticed. These tests put the same logic where CI will see it.

Pure unit tests — the example reads an export and a manifest, never the graph,
so none of this needs Neo4j.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


_EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "policy_propose_from_observed.py"


def _module():
    spec = importlib.util.spec_from_file_location("_policy_propose", _EXAMPLE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def example():
    return _module()


def _row(agent, ref, reasons, *, calls=9, complete=True, qualified=True):
    return {
        "agent": agent, "ref": ref, "has_tool_key": qualified,
        "decision": "rejected" if reasons else "eligible",
        "calls": calls, "would_block_calls": calls if reasons else 0,
        "reasons": reasons, "reasons_complete": complete,
    }


GOV = {
    "agents": ["bot"],
    "grants": [{"agent": "bot", "tool": "alpha::read_file", "granted_by": "team"}],
}


def test_deny_wins_over_the_reported_reason(example):
    """NOT_GRANTED outranks DENY on precedence, so the set is what must decide."""
    p = example.propose(GOV, [_row("bot", "alpha::write_file",
                                   ["NOT_GRANTED", "DENY_GOVERNED"])], 3)
    assert p["additions"] == []
    assert [r["tool"] for r in p["conflicts"]] == ["alpha::write_file"]


def test_ungranted_and_unmapped_is_not_a_grant(example):
    p = example.propose(GOV, [_row("bot", "alpha::edit_file",
                                   ["NOT_GRANTED", "UNMAPPED"])], 3)
    assert p["additions"] == []
    assert "a grant alone" in p["not_grant_shaped"][0]["note"]


def test_a_grant_does_not_cross_servers(example):
    """`alpha::read_file` and `beta::read_file` are different tools.

    Reconciling bare and qualified refs by dropping the server made one grant
    answer for every server exposing that name — a missed addition one way, a
    wrong revocation the other.
    """
    p = example.propose(GOV, [_row("bot", "beta::read_file", ["NOT_GRANTED"])], 3)
    assert [r["tool"] for r in p["additions"]] == ["beta::read_file"]

    zero = example.propose(GOV, [_row("bot", "beta::read_file", [], calls=0)], 3)
    assert zero["removals"] == []


def test_bare_grant_adopts_a_single_resolution_and_refuses_two(example):
    gov = {"agents": ["bot"], "grants": [{"agent": "bot", "tool": "read_file"}]}
    resolved, ambiguous = example.resolve_grants(gov["grants"], {"alpha::read_file"})
    assert resolved == {("bot", "alpha::read_file"): "read_file"}
    assert ambiguous == []

    _, ambiguous = example.resolve_grants(
        gov["grants"], {"alpha::read_file", "beta::read_file"}
    )
    assert [r["reason"] for r in ambiguous] == ["AMBIGUOUS_GRANT"]


def test_removal_needs_an_explicit_zero_call_row(example):
    """Silence about a grant is not evidence that it is unused."""
    p = example.propose(GOV, [_row("bot", "alpha::other", ["NOT_GRANTED"])], 3)
    assert p["removals"] == []

    observed = example.propose(GOV, [_row("bot", "alpha::read_file", [], calls=0)], 3)
    assert observed["removals"] == [{"agent": "bot", "tool": "alpha::read_file"}]


def test_legacy_single_reason_export_is_never_proposed_from(example):
    p = example.propose(
        GOV, [_row("bot", "alpha::move_file", ["NOT_GRANTED"], complete=False)], 3
    )
    assert p["additions"] == []
    assert [r["tool"] for r in p["insufficient_evidence"]] == ["alpha::move_file"]


def test_candidate_only_row_is_report_only(example):
    p = example.propose(
        GOV, [_row("bot", "read_file", ["NOT_GRANTED"], qualified=False)], 3
    )
    assert p["additions"] == []
    assert "canonical tool_key" in p["insufficient_evidence"][0]["note"]


def test_candidate_only_row_cannot_drive_a_removal(example):
    """A candidate that merely looks qualified is still not a resolved tool.

    The ref in a candidate-only row is what the agent asked for, not what the
    graph resolved; letting it establish zero-call evidence made it propose
    revoking a real grant of the same name.
    """
    p = example.propose(
        GOV, [_row("bot", "alpha::read_file", [], calls=0, qualified=False)], 3
    )
    assert p["removals"] == []


def test_bare_tool_key_is_malformed_not_canonical(example, tmp_path):
    path = tmp_path / "observed.jsonl"
    path.write_text(
        json.dumps({"agent": "bot", "decision": "rejected", "tool_key": "read_file",
                    "calls": 9, "would_block_calls": 9,
                    "reject_reasons": ["NOT_GRANTED"]}) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="server::tool"):
        example.load_observations(path)


def test_unknown_agent_is_refused(example):
    with pytest.raises(SystemExit):
        example.propose(GOV, [_row("ghost", "alpha::read_file", ["NOT_GRANTED"])], 3)


def test_rendered_entries_are_yaml_not_string_interpolation(example):
    import yaml

    hostile = example.render(
        {"additions": [{"agent": "bot", "tool": 'a::b", extra: "x',
                        "would_block_calls": 9, "reason": "NOT_GRANTED"}],
         "removals": [], "conflicts": [], "not_grant_shaped": [],
         "insufficient_evidence": []},
        "test", 3,
    )
    entry = next(line for line in hostile.splitlines() if line.startswith("  - "))
    parsed = yaml.safe_load(entry[len("  - "):].split("  #")[0])
    assert parsed["tool"] == 'a::b", extra: "x'
    assert set(parsed) == {"agent", "tool", "granted_by"}
    # A fragment, not a section: declarative ingest would drop everything a
    # replacement `grants:` key omitted.
    assert "\ngrants:" not in hostile


@pytest.mark.parametrize(
    "line",
    [
        '{"agent": "bot", "decision": "maybe", "tool_key": "a::b"}',
        '{"agent": "bot", "decision": "rejected", "tool_key": "a::b", "calls": -1}',
        '{"agent": "bot", "decision": "rejected", "tool_key": "a::b", "calls": 1,'
        ' "would_block_calls": 5}',
        '{"agent": "bot", "decision": "rejected", "tool_key": "a::b",'
        ' "reject_reasons": ["NOPE"]}',
        '{"agent": "bot", "decision": "rejected", "tool_key": "a::b"}',
        '{"agent": "bo\\u0000t", "decision": "eligible", "tool_key": "a::b"}',
    ],
    ids=["bad-decision", "negative", "would-block-exceeds", "unknown-reason",
         "rejected-without-reason", "control-char"],
)
def test_malformed_rows_are_refused(example, tmp_path, line):
    path = tmp_path / "observed.jsonl"
    path.write_text(line + "\n", encoding="utf-8")
    with pytest.raises(ValueError):
        example.load_observations(path)


def test_duplicate_rows_are_refused(example, tmp_path):
    path = tmp_path / "observed.jsonl"
    row = {"agent": "bot", "decision": "eligible", "tool_key": "a::b", "calls": 1}
    path.write_text(json.dumps(row) + "\n" + json.dumps(row) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate"):
        example.load_observations(path)


def test_min_would_block_must_be_positive(example):
    import argparse

    with pytest.raises(argparse.ArgumentTypeError):
        example.positive_int("0")


def test_embedded_self_test_still_passes(example):
    assert example.self_test() == 0
