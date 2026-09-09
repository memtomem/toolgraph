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


def test_absent_calls_is_not_an_explicit_zero(example, tmp_path):
    """Silence about a count must not read as an observed zero.

    ``_count`` defaulted a missing ``calls`` field to 0 and the removal rule
    keys on ``calls == 0``, so an export that never mentioned a count produced
    the same revocation proposal as one that explicitly observed zero calls.
    That is precisely the "an omitted grant is unobserved, not unused" rule the
    module claims to enforce, so the guarantee was decorative. The earlier
    removal test only ever omitted whole rows, and drove ``propose`` directly,
    which is why it never reached the defaulting in ``load_observations``.
    """
    granted = {"agent": "bot", "decision": "eligible", "tool_key": "alpha::read_file"}

    silent = tmp_path / "silent.jsonl"
    silent.write_text(json.dumps(granted) + "\n", encoding="utf-8")
    assert example.propose(GOV, example.load_observations(silent), 3)["removals"] == []

    stated = tmp_path / "stated.jsonl"
    stated.write_text(json.dumps({**granted, "calls": 0}) + "\n", encoding="utf-8")
    assert example.propose(GOV, example.load_observations(stated), 3)["removals"] == [
        {"agent": "bot", "tool": "alpha::read_file"}
    ]


def test_would_block_without_a_calls_count_is_refused(example, tmp_path):
    """Blocked calls with no total is an incomplete window, not weak evidence."""
    path = tmp_path / "observed.jsonl"
    path.write_text(json.dumps({
        "agent": "bot", "decision": "rejected", "tool_key": "alpha::write_file",
        "would_block_calls": 7, "reject_reasons": ["NOT_GRANTED"],
    }) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="without a 'calls' count"):
        example.load_observations(path)


def test_unstated_calls_never_count_as_usage(example, tmp_path):
    """An unstated count is not a zero to be summed into usage evidence."""
    path = tmp_path / "observed.jsonl"
    path.write_text(json.dumps({
        "agent": "bot", "decision": "eligible", "tool_key": "alpha::read_file",
    }) + "\n", encoding="utf-8")
    rows = example.load_observations(path)
    assert rows[0]["calls"] is None


def test_every_report_carries_the_provenance_limitation(example):
    """#89: the export cannot prove which manifest it was recorded against.

    The docstring said so at length, but argparse renders only its first line
    and the report opened straight into pasteable grants, so the reader
    deciding whether to apply the change never met the limitation.
    """
    report = example.render(
        example.propose(GOV, [_row("bot", "alpha::write_file", ["NOT_GRANTED"])], 3),
        "2026-08", 3,
    )
    assert "UNVERIFIED PROVENANCE" in report
    assert "#89" in report
    assert report.index("UNVERIFIED PROVENANCE") < report.index("```yaml")


def test_help_carries_the_provenance_limitation():
    import subprocess
    import sys

    out = subprocess.run(
        [sys.executable, str(_EXAMPLE), "--help"],
        capture_output=True, text=True, check=True,
    )
    assert "UNVERIFIED PROVENANCE" in out.stdout


ENVELOPE = {
    "governance_digest": "a" * 64,
    "graph_generation": 7,
    "profile": "review",
    "window_start": "2026-08-01T00:00:00Z",
    "window_end": "2026-08-31T23:59:59Z",
}
PROFILES = {"strict", "review", "explore"}


def _export(tmp_path, envelope, *rows):
    path = tmp_path / "observed.jsonl"
    lines = []
    if envelope is not None:
        lines.append(json.dumps({"envelope": envelope}))
    lines += [json.dumps(r) for r in rows]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_a_window_recorded_against_other_governance_is_refused(example):
    """#89: the digest is what separates evidence from a replay.

    A window captured before a DENY policy existed still parses cleanly and
    still reads as NOT_GRANTED, so without this comparison a stale export
    produces a confidently worded proposal against today's manifest.
    """
    with pytest.raises(example.ProvenanceError, match="different governance"):
        example.verify_envelope(ENVELOPE, "b" * 64, PROFILES)

    bound = example.verify_envelope(ENVELOPE, "a" * 64, PROFILES)
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
    ids=["bad-digest", "negative-generation", "bool-generation",
         "unknown-profile", "empty-bound"],
)
def test_malformed_envelopes_are_refused(example, field, value, match):
    envelope = {**ENVELOPE, field: value}
    if field == "governance_digest":
        expected = "a" * 64
    else:
        expected = ENVELOPE["governance_digest"]
    with pytest.raises(example.ProvenanceError, match=match):
        example.verify_envelope(envelope, expected, PROFILES)


def test_an_incomplete_envelope_binds_nothing(example):
    """Half an envelope is refused rather than half-trusted."""
    for field in example.ENVELOPE_FIELDS:
        partial = {k: v for k, v in ENVELOPE.items() if k != field}
        with pytest.raises(example.ProvenanceError, match="missing"):
            example.verify_envelope(partial, ENVELOPE["governance_digest"], PROFILES)


def test_a_backwards_window_is_refused(example):
    envelope = _win("2026-09-01T00:00:00Z", "2026-08-01T00:00:00Z")
    with pytest.raises(example.ProvenanceError, match="is after"):
        example.verify_envelope(envelope, ENVELOPE["governance_digest"], PROFILES)


def _win(start, end):
    return {**ENVELOPE, "window_start": start, "window_end": end}


def test_window_bounds_are_compared_as_instants_not_strings(example):
    """Lexical ordering gets both directions wrong once offsets are involved.

    "2026-08-01T01:00:00+02:00" sorts AFTER "2026-08-01T00:00:00Z" while naming
    an earlier moment, so a string compare rejected a correct window and
    accepted a reversed one.
    """
    ok = _win("2026-08-01T01:00:00+02:00", "2026-08-01T00:00:00Z")
    assert example.verify_envelope(
        ok, ENVELOPE["governance_digest"], PROFILES
    )["window_start"] == "2026-08-01T01:00:00+02:00"

    reversed_window = _win("2026-08-01T00:00:00-02:00", "2026-08-01T01:00:00Z")
    with pytest.raises(example.ProvenanceError, match="is after"):
        example.verify_envelope(
            reversed_window, ENVELOPE["governance_digest"], PROFILES
        )


def test_a_window_bound_that_is_not_a_timestamp_is_refused(example):
    with pytest.raises(example.ProvenanceError, match="ISO-8601"):
        example.verify_envelope(
            _win("last tuesday", "2026-08-31T00:00:00Z"),
            ENVELOPE["governance_digest"], PROFILES,
        )


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
def test_ambiguous_or_repaired_timestamps_are_refused(example, bound):
    """`fromisoformat` is looser than ISO-8601 and repairs rather than refuses.

    It takes a bare date, any single character as the separator, and turns the
    invalid offset "+00:60" into "+01:00" without a word. Each would let a
    malformed bound through looking exact, on a file that is evidence for
    widening an agent's authority.
    """
    with pytest.raises(example.ProvenanceError, match="offset|ISO-8601"):
        example.verify_envelope(
            _win(bound, "2026-08-31T00:00:00Z"),
            ENVELOPE["governance_digest"], PROFILES,
        )


def test_fully_qualified_timestamps_are_accepted(example):
    for start in ("2026-08-01T00:00:00Z", "2026-08-01T00:00:00.123456Z",
                  "2026-08-01T02:00:00+02:00"):
        assert example.verify_envelope(
            _win(start, "2026-08-31T00:00:00Z"),
            ENVELOPE["governance_digest"], PROFILES,
        )["window_start"] == start


@pytest.mark.parametrize("bad", [[], {}, 3], ids=["list", "dict", "int"])
def test_a_non_string_profile_is_refused_cleanly(example, bad):
    """Bare membership raises TypeError on an unhashable value."""
    with pytest.raises(example.ProvenanceError, match="unknown selector profile"):
        example.verify_envelope(
            {**ENVELOPE, "profile": bad}, ENVELOPE["governance_digest"], PROFILES
        )


def test_the_envelope_is_split_off_and_rows_keep_their_line_numbers(example, tmp_path):
    """An envelope must not be parsed as an observation, nor shift error lines."""
    good = {"agent": "bot", "decision": "eligible", "tool_key": "alpha::read_file",
            "calls": 3}
    path = _export(tmp_path, ENVELOPE, good)
    envelope, rows = example.load_export(path)
    assert envelope == ENVELOPE
    assert [r["agent"] for r in rows] == ["bot"]

    # The bad row is on file line 3; the message must say 3, not 2.
    bad = {"agent": "bot", "decision": "nonsense", "tool_key": "alpha::write_file"}
    path = _export(tmp_path, ENVELOPE, good, bad)
    with pytest.raises(ValueError, match=r":3: decision must be"):
        example.load_export(path)


def test_an_envelope_line_may_not_smuggle_observation_fields(example, tmp_path):
    path = tmp_path / "observed.jsonl"
    path.write_text(
        json.dumps({"envelope": ENVELOPE, "agent": "bot", "decision": "eligible"})
        + "\n", encoding="utf-8",
    )
    with pytest.raises(example.ProvenanceError, match="own rows"):
        example.load_export(path)


def test_an_export_without_an_envelope_still_parses(example, tmp_path):
    """The loader stays usable; refusing is main()'s decision, not the parser's."""
    path = _export(tmp_path, None,
                   {"agent": "bot", "decision": "eligible",
                    "tool_key": "alpha::read_file", "calls": 3})
    envelope, rows = example.load_export(path)
    assert envelope is None
    assert len(rows) == 1


def test_a_bound_report_states_what_it_is_bound_to(example):
    proposal = example.propose(
        GOV, [_row("bot", "alpha::write_file", ["NOT_GRANTED"])], 3
    )
    bound = example.render(proposal, "2026-08", 3, ENVELOPE)
    assert "DECLARED DIGEST MATCHES" in bound
    assert "UNVERIFIED PROVENANCE" not in bound
    assert ENVELOPE["governance_digest"] in bound
    # A non-strict window must say so: absence from these rows is not permission.
    assert "review" in bound
    # The report must not claim more than the digest comparison establishes.
    assert "(declared)" in bound
    assert "does NOT show" in bound

    unbound = example.render(proposal, "2026-08", 3, None)
    assert "UNVERIFIED PROVENANCE" in unbound
    assert "DECLARED DIGEST MATCHES" not in unbound
