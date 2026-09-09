#!/usr/bin/env python3
"""Propose governance.yaml grant changes from observed gateway usage.

Closes the observe half of the review -> strict promotion loop as a PROPOSAL
generator. A gateway running a review-profile bundle keeps rejected tools
callable and records the calls that strict mode WOULD have blocked. Today a
human reads those counters ad hoc; this example turns them into a reviewable
governance change — and nothing more. In ADR-0012 vocabulary the output is
``decision_mode: human_required`` / ``automatic_change: false``: the script
never writes governance.yaml, it prints a proposal for the maintainer to
apply (or refuse) via the normal manifest edit + ``ingest-manifest`` path.

Input contract: one JSON object per line, describing an observation window for
an (agent, tool) pair::

    {"agent": "vibe-coder", "candidate": "filesystem::read_file",
     "tool_key": "filesystem::read_file",
     "decision": "eligible" | "rejected", "calls": 41,
     "would_block_calls": 41,
     "reject_reasons": ["NOT_GRANTED", "UNMAPPED"]}

``calls`` is optional but never defaulted: an absent count means UNSTATED, not
observed-zero. Only an explicit ``"calls": 0`` can support a removal, and a row
carrying ``would_block_calls`` without a total is refused as an incomplete
window rather than read as weak evidence.

``reject_reasons`` is the FULL list the selector computed for the row, not the
single winning one. That distinction is the whole safety story: reasons
accumulate in ``_applicable_reasons`` and only the first by precedence is
reported, and ``NOT_GRANTED`` outranks ``DENY_VIOLATION``, ``DENY_GOVERNED``,
``DRIFTED`` and ``UNMAPPED``. A tool that is both ungranted and DENY-governed
therefore *reports* as ``NOT_GRANTED``, so proposing a grant off the winning
reason alone would propose eroding a DENY policy — the one thing this script
promises never to do. An export that carries only the legacy single
``reject_reason`` is accepted and counted, but never proposed from: it cannot
prove the absence of the other conditions, so it fails closed into the
"insufficient evidence" section.

Proposal rules, in order:

1. DENY is never negotiable. If ANY reason on the row contains ``DENY`` the row
   is a policy conflict — a grant would not change the decision, and asking for
   one invites eroding the policy instead.
2. A grant is proposed only when the row's complete reason set is exactly
   ``{NOT_GRANTED}``, above the ``--min-would-block`` evidence floor, and the
   grant does not already exist. Real usage is the evidence; a tool nobody
   called does not earn a grant just for being rejected.
3. Every other reason is reported with the remedy that applies. ``UNMAPPED``
   means the tool IS granted and lacks authored data-flow — "the blind spot the
   graph cannot vouch for" — so it wants ``data_access`` edges; ``DRIFTED``
   wants a re-crawl; ``TOOL_NOT_FOUND`` a fixed ref; ``AMBIGUOUS_TOOL`` a
   qualified one. A row that is ungranted *and* something else is reported too,
   because a grant alone would not make it eligible.
4. Grant removals (least privilege) need an EXPLICIT zero-call observation for
   that grant. The export never claims to cover every tool an agent holds, so an
   omitted grant is unobserved, not unused, and treating silence as disuse would
   turn a partial window into a revocation proposal. They stay lower-confidence
   regardless: absence of use in one window is weaker evidence than presence.
5. Unknown agents are refused loudly, mirroring the compiler's fail-closed
   ``agent not found`` stance.

Provenance: the export SHOULD open with one envelope line binding the window to
the state it was recorded against::

    {"envelope": {"governance_digest": "<64 hex>", "graph_generation": 12,
                  "profile": "strict", "window_start": "2026-08-01T00:00:00Z",
                  "window_end": "2026-08-31T23:59:59Z"}}

The digest is compared against ``governance_digest()`` of the manifest supplied,
and a mismatch is a refusal: a window recorded before a DENY policy existed
still parses cleanly and still reads as ``NOT_GRANTED``, so without that
comparison a replay is indistinguishable from evidence. The profile is recorded
because the same rejection means different things under ``review`` (UNMAPPED
passes) and ``strict`` (it does not).

An export with no envelope is refused unless ``--unverified-provenance`` says
otherwise, so "we could not check" never reads the same as "we checked".

Usage:
    uv run python examples/policy_propose_from_observed.py GOVERNANCE_YAML OBSERVED_JSONL
        [--min-would-block 3] [--window-label 2026-08] [--unverified-provenance]
    uv run python examples/policy_propose_from_observed.py --self-test
"""

from __future__ import annotations

import argparse
from datetime import datetime
import json
import re
import sys
import textwrap
from collections import defaultdict
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover
    print("PyYAML is required (uv run python examples/policy_propose_from_observed.py ...)",
          file=sys.stderr)
    raise


# What each non-DENY rejection actually asks for. Taken from the selector's own
# reason vocabulary (toolgraph/graph/selector.py): six of the seven reasons are
# not answered by a grant, and proposing one for them buries the real gap under
# a governance edit that changes nothing.
NON_GRANT_REMEDIES = {
    "UNMAPPED": "granted already, but no authored data-flow — add the "
                "data_access edges; a grant will not clear this in strict mode",
    "DRIFTED": "no server currently exposes this tool — re-crawl, or retire the "
               "workflow; a grant cannot make it callable",
    "TOOL_NOT_FOUND": "the tool ref does not resolve — fix the ref or crawl the "
                      "server that provides it",
    "AMBIGUOUS_TOOL": "the ref matches more than one tool — qualify it as "
                      "'server::tool'; granting one of them hides the ambiguity",
}


VALID_REASONS = frozenset({
    "TOOL_NOT_FOUND", "AMBIGUOUS_TOOL", "NOT_GRANTED", "DENY_VIOLATION",
    "DENY_GOVERNED", "DRIFTED", "UNMAPPED",
})
DECISIONS = frozenset({"eligible", "rejected"})
# Anything that would break out of a YAML scalar or a one-line report entry.
UNSAFE_IN_NAME = re.compile(r"[\x00-\x1f\x7f]")
# The repo writes a resolved tool as "<server>::<tool>". A bare tool_key is not
# a canonical key that happens to be short — it is a malformed export, and
# trusting it would author a bare grant nobody asked for.
QUALIFIED_KEY = re.compile(r"^[^:\s]+::[^:\s]+$")


# Shown at the top of every report and in --help. The docstring explains this at
# length, but argparse renders only the first line of __doc__ and the report
# opened straight into pasteable grants — so the one limitation that decides
# whether a proposal is safe to apply was the one thing a reader never saw.
UNVERIFIED_PROVENANCE = (
    "UNVERIFIED PROVENANCE - the export carries no governance digest, graph "
    "generation, or selector profile, so nothing here can distinguish evidence "
    "recorded against THIS manifest from a replay of an older one. A window "
    "captured before a DENY policy existed, or before a tool was retired, still "
    "parses cleanly and still reads as NOT_GRANTED. Confirm the window was "
    "recorded against the current manifest and profile before applying anything "
    "below; reading the proposal carefully cannot establish provenance the "
    "export never carried. Tracked in issue #89."
)


# The export may open with one envelope line binding the window to the state it
# was recorded against. Everything below exists because a window is otherwise
# indistinguishable from a replay: see #89.
ENVELOPE_KEY = "envelope"
DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
ENVELOPE_FIELDS = ("governance_digest", "graph_generation", "profile",
                   "window_start", "window_end")


class ProvenanceError(ValueError):
    """The window cannot be shown to describe the manifest it was handed."""


# A window bound is a moment, and this file is evidence for a governance
# change, so the grammar is stated rather than delegated. `fromisoformat` is
# more permissive than ISO-8601: it accepts a bare date, any single character
# as the date/time separator, and an out-of-range offset like "+00:60" which it
# silently repairs to "+01:00". Each of those would let an ambiguous or
# malformed bound through looking exact.
_TIMESTAMP = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?"
    r"(?:Z|[+-](?P<oh>\d{2}):(?P<om>\d{2}))$"
)


def _instant(value: str, where: str) -> datetime:
    """Parse a fully qualified ISO-8601 timestamp into a comparable instant.

    An explicit offset is required. Reading a naive bound as UTC would invent a
    zone the exporter never stated, and reading it as local time would make the
    same file mean different things on different machines. Neither is a good
    foundation for widening an agent's authority, so it is refused instead.
    """
    match = _TIMESTAMP.match(value)
    if not match:
        raise ProvenanceError(
            f"{where} must be an ISO-8601 timestamp with an explicit offset, "
            f'e.g. "2026-08-01T00:00:00Z" or "2026-08-01T02:00:00+02:00", '
            f"got {value!r}"
        )
    hours, minutes = match.group("oh"), match.group("om")
    if hours is not None and (int(hours) > 23 or int(minutes) > 59):
        raise ProvenanceError(
            f"{where} has an out-of-range UTC offset {value!r}; "
            "fromisoformat would silently repair it"
        )
    return datetime.fromisoformat(value)


def verify_envelope(envelope: dict, expected_digest: str, known_profiles: set[str]) -> dict:
    """Bind an observation window to the governance and graph it was recorded against.

    What this establishes, and what it does not. `governance_digest()` is a
    stable semantic hash of the validated model. An unequal digest proves the
    export DECLARED a different manifest, which is reason enough to refuse: its
    NOT_GRANTED rows may already have been answered, retired, or overruled by a
    DENY. An equal digest proves only that the exporter declared this
    manifest's digest -- copying it onto rows from any window passes. It does not
    show the gateway enforced it, that these rows came from that gateway, or
    that the stated generation and window ever happened -- the generation and
    bounds are validated for shape and compared against nothing. A digest can
    also stay equal across crawls that changed which tools exist.

    The profile is not decoration either. The same rejection means different
    things under `review` (UNMAPPED passes) and `strict` (it does not), so a
    proposal that cannot name the profile cannot say what its evidence proves.
    """
    missing = [f for f in ENVELOPE_FIELDS if f not in envelope]
    if missing:
        raise ProvenanceError(
            f"envelope is missing {missing} - an incomplete envelope binds nothing, "
            "so it is refused rather than half-trusted"
        )
    digest = envelope["governance_digest"]
    if not isinstance(digest, str) or not DIGEST_RE.match(digest):
        raise ProvenanceError(
            f"governance_digest must be 64 lowercase hex characters, got {digest!r}"
        )
    if digest != expected_digest:
        raise ProvenanceError(
            "this window was recorded against a different governance "
            f"({digest[:12]}...) than the manifest supplied ({expected_digest[:12]}...). "
            "Re-export against the current manifest; a stale window's NOT_GRANTED rows "
            "may already be answered, retired, or overruled by a DENY"
        )
    generation = envelope["graph_generation"]
    if isinstance(generation, bool) or not isinstance(generation, int) or generation < 0:
        raise ProvenanceError(
            f"graph_generation must be a non-negative integer, got {generation!r}"
        )
    profile = envelope["profile"]
    # Membership alone raises TypeError on an unhashable JSON value such as a
    # list, which escapes the clean refusal this function promises.
    if not isinstance(profile, str) or profile not in known_profiles:
        raise ProvenanceError(
            f"unknown selector profile {profile!r} - expected one of {sorted(known_profiles)}"
        )
    bounds = {}
    for bound in ("window_start", "window_end"):
        value = envelope[bound]
        if not isinstance(value, str) or not value.strip():
            raise ProvenanceError(f"{bound} must be a non-empty string, got {value!r}")
        if UNSAFE_IN_NAME.search(value):
            raise ProvenanceError(f"{bound} contains a control character")
        bounds[bound] = _instant(value, bound)
    # Compare instants, not strings. Lexical ordering accepts a reversed window
    # written across two offsets and rejects a correctly ordered one, because
    # "2026-08-01T01:00:00+02:00" sorts after "2026-08-01T00:00:00Z" while
    # naming an earlier moment.
    if bounds["window_start"] > bounds["window_end"]:
        raise ProvenanceError(
            f'window_start ({envelope["window_start"]}) is after '
            f'window_end ({envelope["window_end"]})'
        )
    return {f: envelope[f] for f in ENVELOPE_FIELDS}


def _count(row: dict, key: str, where: str, *, missing: int | None = 0) -> int | None:
    """Validate a count, keeping "absent" distinguishable from "explicitly zero".

    ``missing=None`` is the load-bearing case. A removal claims a grant went
    unused, and the only thing that can support that is a row which SAYS zero.
    Defaulting an absent ``calls`` field to 0 made silence indistinguishable
    from an explicit zero-call observation, so an export that never mentioned a
    count still proposed revoking the grant.
    """
    if key not in row:
        return missing
    value = row[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{where}: {key} must be an integer, got {value!r}")
    if value < 0:
        raise ValueError(f"{where}: {key} must be >= 0, got {value}")
    return value


def load_export(path: Path) -> tuple[dict | None, list[dict]]:
    """Split the optional envelope line off the observation rows.

    The envelope, when present, is the FIRST line and carries only the
    ``envelope`` key. Putting it inline keeps the export one file that a gateway
    can append to, and making it first means a truncated export loses rows
    rather than its provenance.
    """
    lines = path.read_text(encoding="utf-8").splitlines()
    envelope: dict | None = None
    body: list[str] = []
    for lineno, line in enumerate(lines, 1):
        if not line.strip():
            continue
        if envelope is None and not body:
            try:
                first = json.loads(line)
            except json.JSONDecodeError:
                first = None
            if isinstance(first, dict) and ENVELOPE_KEY in first:
                if not isinstance(first[ENVELOPE_KEY], dict):
                    raise ProvenanceError(f"{path}:{lineno}: envelope must be a JSON object")
                if set(first) != {ENVELOPE_KEY}:
                    raise ProvenanceError(
                        f"{path}:{lineno}: the envelope line carries only "
                        f"{ENVELOPE_KEY!r}; observation fields belong on their own rows"
                    )
                envelope = first[ENVELOPE_KEY]
                continue
        body.append((lineno, line))
    return envelope, _load_rows(path, body)


def load_observations(path: Path) -> list[dict]:
    """Row-only view of the export, for callers that handle provenance themselves."""
    return load_export(path)[1]


def _load_rows(path: Path, lines: list[tuple[int, str]]) -> list[dict]:
    """Parse and validate the observation rows, refusing anything unreasonable.

    Coercion is the enemy here: a silently ``int()``-ed count, a decision string
    nobody recognizes treated as "eligible", or a negative call count cancelling
    real usage all end the same way — a governance proposal resting on evidence
    that was never checked.
    """
    rows: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for lineno, raw in lines:
        line = raw.strip()
        where = f"{path}:{lineno}"
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{where}: not valid JSON ({exc.msg})") from exc
        if not isinstance(row, dict):
            raise ValueError(f"{where}: each line must be a JSON object")
        for key in ("agent", "decision"):
            if key not in row:
                raise ValueError(f"{where}: missing {key!r}")
        if row["decision"] not in DECISIONS:
            raise ValueError(
                f"{where}: decision must be one of {sorted(DECISIONS)}, "
                f"got {row['decision']!r}"
            )
        # An unresolved or ambiguous ref has no canonical tool_key — the selector
        # keeps the input `candidate` instead. Requiring a qualified key made
        # exactly those rows unrepresentable.
        tool_key = row.get("tool_key")
        if tool_key is not None and not QUALIFIED_KEY.match(str(tool_key)):
            raise ValueError(
                f"{where}: tool_key must be 'server::tool', got {tool_key!r} "
                "(an unresolved ref belongs in 'candidate')"
            )
        ref = tool_key or row.get("candidate")
        if not ref:
            raise ValueError(f"{where}: need tool_key or candidate")
        for field, value in (("agent", row["agent"]), ("ref", ref)):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{where}: {field} must be a non-empty string")
            if UNSAFE_IN_NAME.search(value):
                raise ValueError(f"{where}: {field} contains a control character")
        calls = _count(row, "calls", where, missing=None)
        would_block = _count(row, "would_block_calls", where)
        if calls is None:
            if would_block > 0:
                raise ValueError(
                    f"{where}: would_block_calls ({would_block}) without a 'calls' "
                    "count - state the total explicitly, because an unstated count "
                    "is not evidence"
                )
        elif would_block > calls:
            raise ValueError(
                f"{where}: would_block_calls ({would_block}) exceeds calls ({calls})"
            )
        reasons = row.get("reject_reasons")
        if reasons is None:
            legacy = row.get("reject_reason")
            reasons = [legacy] if legacy else []
            row["reasons_complete"] = False
        else:
            if not isinstance(reasons, list):
                raise ValueError(f"{where}: reject_reasons must be a list")
            row["reasons_complete"] = True
        reasons = [str(r).upper() for r in reasons]
        unknown = sorted(set(reasons) - VALID_REASONS)
        if unknown:
            raise ValueError(f"{where}: unknown reject reason(s) {unknown}")
        if row["decision"] == "rejected" and not reasons:
            raise ValueError(f"{where}: a rejected row needs a reject reason")
        identity = (row["agent"], ref)
        if identity in seen:
            raise ValueError(f"{where}: duplicate row for {identity} — merge the window first")
        seen.add(identity)
        row["ref"] = ref
        row["has_tool_key"] = bool(row.get("tool_key"))
        row["calls"] = calls
        row["would_block_calls"] = would_block
        row["reasons"] = reasons
        rows.append(row)
    return rows


def resolve_grants(grants: list[dict], observed_refs: set[str]) -> tuple[dict, list[dict]]:
    """Map each grant to the qualified ref it governs, refusing what is ambiguous.

    models.AccessGrant allows a grant to be written bare (`read_file`) or
    qualified (`filesystem::read_file`), while an observation of a resolved tool
    is always qualified. Comparing on the bare name alone would make one grant
    stand for every server that happens to expose that tool name — an
    `alpha::read_file` grant would answer a `beta::read_file` observation, which
    is both a missed addition and a wrong revocation. So: qualified grants match
    exactly, and a bare grant is adopted only when the window resolves its name
    to exactly one server. A bare name seen on several servers is reported, not
    guessed at.
    """
    by_name: dict[str, set[str]] = defaultdict(set)
    for ref in observed_refs:
        if "::" in ref:
            by_name[ref.split("::", 1)[1]].add(ref)

    resolved: dict[tuple[str, str], str] = {}
    ambiguous: list[dict] = []
    for g in grants:
        agent, tool = g["agent"], g["tool"]
        if "::" in tool:
            resolved[(agent, tool)] = tool
            continue
        candidates = by_name.get(tool, set())
        if len(candidates) == 1:
            resolved[(agent, next(iter(candidates)))] = tool
        elif len(candidates) > 1:
            ambiguous.append({
                "agent": agent, "tool": tool, "would_block_calls": 0,
                "reason": "AMBIGUOUS_GRANT",
                "note": "this bare grant could mean " + " or ".join(sorted(candidates))
                        + " — qualify it in the manifest before reasoning about it",
            })
        else:
            # Unobserved entirely: keep it so an exact-qualified observation can
            # still match, but it can never become a removal (no zero-call row).
            resolved[(agent, tool)] = tool
    return resolved, ambiguous


def propose(gov: dict, observed: list[dict], min_would_block: int) -> dict:
    known_agents = set(gov.get("agents") or [])
    grants = gov.get("grants") or []
    # Keyed on the qualified ref a grant governs; the value is the ref exactly as
    # the manifest writes it, so a removal names the line to find and delete.
    # Only rows that resolved to a canonical key may speak about grants. A
    # candidate is the ref as the agent asked for it, and one that merely looks
    # qualified is not evidence about the tool of that name — letting it through
    # made a candidate-only row propose revoking a real grant.
    resolved_rows = [o for o in observed if o["has_tool_key"]]
    granted, ambiguous_grants = resolve_grants(grants, {o["ref"] for o in resolved_rows})
    for g in grants:
        known_agents.add(g["agent"])

    unknown = sorted({o["agent"] for o in observed} - known_agents)
    if unknown:
        raise SystemExit(
            f"refusing to propose for unknown agents {unknown}: register them in the "
            "manifest first (the compiler would fail-closed on them anyway)"
        )

    additions: list[dict] = []
    conflicts: list[dict] = []
    other: list[dict] = []
    unproven: list[dict] = list(ambiguous_grants)
    zero_call: set[tuple[str, str]] = set()
    used: dict[tuple[str, str], int] = defaultdict(int)

    for o in observed:
        agent, ref = o["agent"], o["ref"]
        key = (agent, ref)
        if o["has_tool_key"]:
            # `calls` is None when the export never stated one. Only an explicit
            # zero is evidence of disuse, and `None == 0` is False — that is the
            # whole mechanism keeping silence out of the removal list.
            used[key] += o["calls"] or 0
            if o["calls"] == 0:
                zero_call.add(key)
        if o["decision"] != "rejected":
            continue
        reasons = set(o["reasons"])
        wb = o["would_block_calls"]
        entry = {"agent": agent, "tool": ref, "would_block_calls": wb,
                 "reason": ", ".join(sorted(reasons))}

        deny = sorted(r for r in reasons if "DENY" in r)
        if deny:
            conflicts.append({**entry, "reason": ", ".join(deny),
                              "note": "governed by a DENY policy — do not grant; change the "
                                      "policy or the workflow, not the grant"})
            continue
        if wb < min_would_block:
            continue
        if not o["reasons_complete"]:
            # Only the winning reason was exported. NOT_GRANTED outranks DENY,
            # drift and unmapped, so this row cannot prove those are absent.
            unproven.append({**entry, "note": "export carries only the winning reject_reason; "
                                              "re-export reject_reasons before proposing"})
            continue
        if reasons == {"NOT_GRANTED"}:
            # A candidate-only row has no canonical key; granting the ref as
            # written would author a grant for something that does not resolve.
            if not o["has_tool_key"]:
                unproven.append({**entry, "note": "no canonical tool_key — resolve the ref "
                                                  "before proposing a grant for it"})
            elif key not in granted:
                additions.append({**entry, "reason": "NOT_GRANTED"})
            continue
        if "NOT_GRANTED" in reasons:
            blockers = sorted(reasons - {"NOT_GRANTED"})
            other.append({**entry, "reason": ", ".join(blockers),
                          "note": "ungranted AND " + "; ".join(
                              NON_GRANT_REMEDIES.get(r, r.lower()) for r in blockers)
                          + " — a grant alone would not make it eligible"})
            continue
        first = sorted(reasons)[0]
        other.append({**entry, "note": NON_GRANT_REMEDIES.get(
            first, "not a grant problem — read the selector reason before acting")})

    # A removal needs an explicit zero-call observation, not silence. The export
    # never claims to cover every tool an agent holds, so an omitted grant is
    # unobserved, not unused.
    removals = [
        {"agent": a, "tool": granted[(a, t)]}
        for (a, t) in sorted(granted)
        if (a, t) in zero_call and used.get((a, t), 0) == 0
    ]

    additions.sort(key=lambda r: (-r["would_block_calls"], r["agent"], r["tool"]))
    conflicts.sort(key=lambda r: (r["agent"], r["tool"]))
    other.sort(key=lambda r: (-r["would_block_calls"], r["agent"], r["tool"]))
    unproven.sort(key=lambda r: (-r["would_block_calls"], r["agent"], r["tool"]))
    return {
        "additions": additions,
        "removals": removals,
        "conflicts": conflicts,
        "not_grant_shaped": other,
        "insufficient_evidence": unproven,
    }


def yaml_entry(mapping: dict, comment: str) -> str:
    """One list entry, serialized by PyYAML rather than by f-string.

    The values come from an observation export, so they are untrusted: a tool
    name carrying a quote could close the scalar and open a second grant, and a
    bare `yes` would load as a boolean rather than the tool called "yes".
    safe_dump quotes whatever needs quoting; the comment is stripped of newlines
    so it cannot escape its own line.
    """
    body = yaml.safe_dump(mapping, default_flow_style=True, sort_keys=False).strip()
    body = body.removesuffix("...").strip()
    return f"  - {body}  # {' '.join(comment.split())}"


def render(proposal: dict, window_label: str, min_would_block: int,
           envelope: dict | None = None) -> str:
    out: list[str] = []
    a = out.append
    a("# Governance change proposal (generated — NOT applied)")
    a("")
    if envelope is None:
        for line in textwrap.wrap(UNVERIFIED_PROVENANCE, width=76):
            a("> " + line)
    else:
        a("> DECLARED DIGEST MATCHES — the digest this export declares equals")
        a("> the digest of the manifest supplied. That is the whole of it.")
        a(">")
        a("> It does NOT show these rows came from a gateway enforcing that")
        a("> manifest, nor that the stated generation or window ever happened:")
        a("> copying today's digest onto old rows passes this check too. The")
        a("> generation and window below are what the file claims. A digest can")
        a("> also stay equal across crawls that changed which tools exist.")
        a(">")
        a(f'> governance_digest: `{envelope["governance_digest"]}` (verified)')
        a(f'> graph_generation: {envelope["graph_generation"]} · '
          f'profile: `{envelope["profile"]}` (declared)')
        a(f'> window: {envelope["window_start"]} .. {envelope["window_end"]} '
          "(declared)")
        if envelope["profile"] != "strict":
            a(">")
            a(f'> Recorded under the `{envelope["profile"]}` profile, which rejects '
              "fewer")
            a("> reasons than `strict`. A tool absent from these rows was not")
            a("> necessarily allowed under strict.")
    a("")
    a(f"decision_mode: human_required · automatic_change: false · "
      f"window: {window_label} · evidence floor: would_block >= {min_would_block}")
    a("")
    if proposal["additions"]:
        a("## Proposed grant additions (evidence: strict would have blocked real usage)")
        a("")
        a("Append these entries to the EXISTING `grants:` list. This is a fragment,")
        a("not a section: ingest is declarative, so pasting it as a second `grants:`")
        a("key would either be rejected as a duplicate or drop every grant not listed.")
        a("")
        a("```yaml")
        for r in proposal["additions"]:
            a(yaml_entry(
                {"agent": r["agent"], "tool": r["tool"], "granted_by": "REVIEW-ME"},
                f'would_block={r["would_block_calls"]} reason={r["reason"]}',
            ))
        a("```")
        a("")
    else:
        a("## Proposed grant additions: none")
        a("")
    if proposal["removals"]:
        a("## Candidate grant removals (lower confidence: an explicit zero-call row)")
        a("")
        for r in proposal["removals"]:
            a(yaml_entry({"agent": r["agent"], "tool": r["tool"]},
                         "verify the window is representative before removing"))
        a("")
    if proposal["insufficient_evidence"]:
        a("## Insufficient evidence to propose — re-export first")
        a("")
        for r in proposal["insufficient_evidence"]:
            a(f'- {r["agent"]} → `{r["tool"]}` ({r["reason"]}, '
              f'would_block={r["would_block_calls"]}): {r["note"]}')
        a("")
    if proposal["not_grant_shaped"]:
        a("## Blocked, but a grant is not the remedy — NOT proposed")
        a("")
        for r in proposal["not_grant_shaped"]:
            a(f'- {r["agent"]} → `{r["tool"]}` ({r["reason"]}, '
              f'would_block={r["would_block_calls"]}): {r["note"]}')
        a("")
    if proposal["conflicts"]:
        a("## Policy conflicts — NOT proposed")
        a("")
        for r in proposal["conflicts"]:
            a(f'- {r["agent"]} → `{r["tool"]}` ({r["reason"]}, '
              f'would_block={r["would_block_calls"]}): {r["note"]}')
        a("")
    a("Apply by editing governance.yaml yourself, then `toolgraph ingest-manifest` "
      "and a fresh `policy compile` per agent. This file is evidence, not authority.")
    return "\n".join(out) + "\n"


FIXTURE_GOV = {
    "agents": ["ops-agent", "public-bot"],
    "grants": [
        {"agent": "ops-agent", "tool": "filesystem::read_file", "granted_by": "platform-team"},
        {"agent": "ops-agent", "tool": "filesystem::write_file", "granted_by": "platform-team"},
        # never mentioned by the window: unobserved, not unused
        {"agent": "ops-agent", "tool": "filesystem::list_directory",
         "granted_by": "platform-team"},
    ],
}
FIXTURE_OBSERVED = [
    # ungranted and nothing else -> the one shape a grant actually fixes
    {"agent": "public-bot", "ref": "filesystem::read_file", "has_tool_key": True,
     "decision": "rejected", "calls": 12, "would_block_calls": 12,
     "reasons": ["NOT_GRANTED"], "reasons_complete": True},
    # ungranted AND deny-governed: NOT_GRANTED wins on precedence, so a
    # single-reason export would have proposed eroding a DENY policy
    {"agent": "public-bot", "ref": "filesystem::write_file", "has_tool_key": True,
     "decision": "rejected", "calls": 4, "would_block_calls": 4,
     "reasons": ["NOT_GRANTED", "DENY_GOVERNED"], "reasons_complete": True},
    # granted already; the gap is authored data-flow, not a grant
    {"agent": "public-bot", "ref": "filesystem::move_file", "has_tool_key": True,
     "decision": "rejected", "calls": 8, "would_block_calls": 8,
     "reasons": ["UNMAPPED"], "reasons_complete": True},
    # ungranted AND unmapped: no DENY involved, so only the complete reason set
    # keeps this out of additions — a grant alone would not make it eligible
    {"agent": "public-bot", "ref": "filesystem::edit_file", "has_tool_key": True,
     "decision": "rejected", "calls": 6, "would_block_calls": 6,
     "reasons": ["NOT_GRANTED", "UNMAPPED"], "reasons_complete": True},
    # below the evidence floor -> ignored
    {"agent": "public-bot", "ref": "filesystem::list_directory", "has_tool_key": True,
     "decision": "rejected", "calls": 1, "would_block_calls": 1,
     "reasons": ["NOT_GRANTED"], "reasons_complete": True},
    # legacy single-reason export -> counted, never proposed from
    {"agent": "public-bot", "ref": "filesystem::create_directory", "has_tool_key": True,
     "decision": "rejected", "calls": 9, "would_block_calls": 9,
     "reasons": ["NOT_GRANTED"], "reasons_complete": False},
    # granted and used -> keeps its grant
    {"agent": "ops-agent", "ref": "filesystem::read_file", "has_tool_key": True,
     "decision": "eligible", "calls": 30, "would_block_calls": 0,
     "reasons": [], "reasons_complete": True},
    # granted, explicitly observed at zero calls -> removal candidate
    {"agent": "ops-agent", "ref": "filesystem::write_file", "has_tool_key": True,
     "decision": "eligible", "calls": 0, "would_block_calls": 0,
     "reasons": [], "reasons_complete": True},
]


def self_test() -> int:
    p = propose(FIXTURE_GOV, FIXTURE_OBSERVED, min_would_block=3)
    assert [r["tool"] for r in p["additions"]] == ["filesystem::read_file"], p["additions"]
    assert p["additions"][0]["agent"] == "public-bot"

    # The precedence trap: NOT_GRANTED outranks DENY_GOVERNED, so a row carrying
    # both REPORTS as NOT_GRANTED. It must land in conflicts, never additions.
    assert [r["tool"] for r in p["conflicts"]] == ["filesystem::write_file"], p["conflicts"]
    assert "filesystem::write_file" not in {r["tool"] for r in p["additions"]}

    # UNMAPPED is a granted tool missing authored data-flow.
    not_grants = {r["tool"]: r for r in p["not_grant_shaped"]}
    assert set(not_grants) == {"filesystem::move_file", "filesystem::edit_file"}, not_grants
    assert "data_access" in not_grants["filesystem::move_file"]["note"]
    # Ungranted AND unmapped is not a grant proposal: the complete reason set is
    # what keeps it out, since no DENY branch catches this one.
    assert "filesystem::edit_file" not in {r["tool"] for r in p["additions"]}
    assert "a grant alone" in not_grants["filesystem::edit_file"]["note"]

    # A legacy single-reason export cannot prove the absence of the others.
    assert [r["tool"] for r in p["insufficient_evidence"]] == [
        "filesystem::create_directory"
    ], p["insufficient_evidence"]

    # Removals need an explicit zero-call row, not silence.
    # write_file was explicitly observed at zero calls; list_directory was never
    # mentioned at all and must not be proposed for removal.
    assert p["removals"] == [
        {"agent": "ops-agent", "tool": "filesystem::write_file"}
    ], p["removals"]

    text = render(p, "self-test", 3)
    assert "human_required" in text and "REVIEW-ME" in text
    assert "data_access" in text and "Append these entries" in text
    # The pasteable block is a fragment, not a `grants:` section that would
    # replace the existing one under declarative ingest.
    assert "\ngrants:" not in text, text

    # Untrusted names are serialized by PyYAML, not interpolated.
    hostile = render(
        {"additions": [{"agent": "bot", "tool": 'a::b", extra: "x',
                        "would_block_calls": 9, "reason": "NOT_GRANTED"}],
         "removals": [], "conflicts": [], "not_grant_shaped": [],
         "insufficient_evidence": []},
        "self-test", 3,
    )
    entry = next(line for line in hostile.splitlines() if line.startswith("  - "))
    parsed = yaml.safe_load(entry[len("  - "):].split("  #")[0])
    assert parsed["tool"] == 'a::b", extra: "x', parsed
    assert set(parsed) == {"agent", "tool", "granted_by"}, parsed

    # A bare manifest grant and a qualified observation are the same grant.
    resolved, amb = resolve_grants(
        [{"agent": "a", "tool": "read_file"}], {"filesystem::read_file"}
    )
    assert resolved == {("a", "filesystem::read_file"): "read_file"}, resolved
    assert amb == []
    # The same bare grant seen on two servers is reported, never guessed.
    _, amb2 = resolve_grants(
        [{"agent": "a", "tool": "read_file"}], {"alpha::read_file", "beta::read_file"}
    )
    assert [r["reason"] for r in amb2] == ["AMBIGUOUS_GRANT"], amb2

    # unknown agent must refuse
    try:
        propose(FIXTURE_GOV, [{"agent": "ghost", "ref": "a::b", "has_tool_key": True,
                               "decision": "rejected", "calls": 9, "would_block_calls": 9,
                               "reasons": ["NOT_GRANTED"], "reasons_complete": True}], 1)
    except SystemExit:
        pass
    else:  # pragma: no cover
        raise AssertionError("unknown agent was not refused")

    try:
        positive_int("0")
    except argparse.ArgumentTypeError:
        pass
    else:  # pragma: no cover
        raise AssertionError("--min-would-block must reject 0")

    print("self-test: ok")
    return 0


def positive_int(value: str) -> int:
    """A floor below 1 makes every rejected row its own evidence."""
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("--min-would-block must be >= 1")
    return number


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        epilog=textwrap.fill(UNVERIFIED_PROVENANCE, width=76) + "\n\n"
        + textwrap.fill(
            "An export carrying an envelope line (governance_digest, "
            "graph_generation, profile, window_start, window_end) is bound to the "
            "manifest supplied and reported as such; a digest mismatch is refused. "
            "Without an envelope this script refuses unless "
            "--unverified-provenance is given.", width=76,
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("governance", nargs="?", type=Path)
    ap.add_argument("observed", nargs="?", type=Path)
    ap.add_argument("--min-would-block", type=positive_int, default=3)
    ap.add_argument("--window-label", default="unspecified")
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument(
        "--unverified-provenance", action="store_true",
        help="proceed on an export that carries no envelope, accepting that "
             "nothing can tell this window from a replay of an older one",
    )
    args = ap.parse_args()

    if args.self_test:
        return self_test()
    if not args.governance or not args.observed:
        ap.error("need GOVERNANCE_YAML and OBSERVED_JSONL (or --self-test)")

    # The repo's own loader: it rejects duplicate keys and validates against the
    # strict Governance model, so a proposal can never rest on a manifest that
    # `ingest-manifest` would refuse. Falling back to a raw yaml.safe_load here
    # would quietly reinstate exactly that risk, so an unimportable toolgraph is
    # a refusal, not a downgrade.
    try:
        from toolgraph.manifest.parser import load_governance
    except ImportError as exc:  # pragma: no cover - depends on the caller's env
        raise SystemExit(
            "toolgraph is not importable, so governance.yaml cannot be validated — "
            "run this as `uv run python examples/policy_propose_from_observed.py ...` "
            "from the repo root"
        ) from exc
    from toolgraph.graph.selector import PROFILES
    from toolgraph.manifest.parser import governance_digest

    gov_model = load_governance(args.governance)
    gov = gov_model.model_dump(mode="json")

    try:
        raw_envelope, observed = load_export(args.observed)
    except ProvenanceError as exc:
        raise SystemExit(f"refusing this export: {exc}") from exc
    envelope = None
    if raw_envelope is not None:
        # A mismatch is a refusal, not a warning: the whole point of the digest
        # is that "recorded elsewhere" and "recorded here" stop looking alike.
        try:
            envelope = verify_envelope(
                raw_envelope, governance_digest(gov_model), set(PROFILES)
            )
        except ProvenanceError as exc:
            raise SystemExit(f"refusing this window: {exc}") from exc
    elif not args.unverified_provenance:
        # Fail closed. The proposal widens authority, so "we could not check"
        # must not read the same as "we checked and it was fine".
        raise SystemExit(
            "this export carries no envelope, so nothing can tell it from a replay "
            "of a window recorded against an older manifest.\n"
            "Re-export with an envelope line:\n"
            '  {"envelope": {"governance_digest": "<64 hex>", "graph_generation": 0, '
            '"profile": "strict", "window_start": "...", "window_end": "..."}}\n'
            f"The digest for {args.governance} right now is "
            f"{governance_digest(gov_model)}.\n"
            "Or pass --unverified-provenance to proceed without that guarantee."
        )

    proposal = propose(gov, observed, args.min_would_block)
    sys.stdout.write(
        render(proposal, args.window_label, args.min_would_block, envelope)
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
