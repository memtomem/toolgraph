"""Propose governance.yaml grant changes from observed gateway usage with graph grounding."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
import json
from pathlib import Path
import re
import textwrap
from typing import Any

import yaml

from toolgraph.graph import driver, queries
from toolgraph.graph.selector import PROFILES
from toolgraph.manifest.parser import Governance, governance_digest, load_governance


class PolicyProposeError(Exception):
    """Base error for policy proposal failures."""


class ProvenanceError(PolicyProposeError):
    """The window cannot be shown to describe the manifest it was handed."""


class StateMismatchError(PolicyProposeError):
    """Live graph state contradicts export envelope metadata."""


class StateChurnError(PolicyProposeError):
    """Live graph state churn prevented stable bracketed read."""


class AmbiguousGrantError(PolicyProposeError):
    """A bare grant is ambiguous against the live graph catalog."""


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
UNSAFE_IN_NAME = re.compile(r"[\x00-\x1f\x7f]")
QUALIFIED_KEY = re.compile(r"^[^:\s]+::[^:\s]+$")
ENVELOPE_KEY = "envelope"
DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
ENVELOPE_FIELDS = (
    "governance_digest", "graph_generation", "profile",
    "window_start", "window_end",
)

UNVERIFIED_PROVENANCE = (
    "UNVERIFIED PROVENANCE - the export carries no governance digest, graph "
    "generation, or selector profile, so nothing here can distinguish evidence "
    "recorded against THIS manifest from a replay of an older one. A window "
    "captured before a DENY policy existed, or before a tool was retired, still "
    "parses cleanly and still reads as NOT_GRANTED. Confirm the window was "
    "recorded against the current manifest and profile before applying anything "
    "below; reading the proposal carefully cannot establish provenance the "
    "export never carried."
)

_TIMESTAMP = re.compile(
    r"(?P<date>[0-9]{4}-[0-9]{2}-[0-9]{2})"
    r"T(?P<hh>[0-9]{2}):(?P<mm>[0-9]{2}):(?P<ss>[0-9]{2})"
    r"(?:\.[0-9]{1,6})?"
    r"(?:Z|(?P<sign>[+-])(?P<oh>[0-9]{2}):(?P<om>[0-9]{2}))"
)


def _instant(value: str, where: str) -> datetime:
    match = _TIMESTAMP.fullmatch(value)
    if not match:
        raise ProvenanceError(
            f"{where} must be a timestamp of the form "
            '"2026-08-01T00:00:00Z" or "2026-08-01T02:00:00+02:00" '
            f"(seconds required, explicit offset required), got {value!r}"
        )
    for part, ceiling in (("hh", 23), ("mm", 59), ("ss", 59)):
        if int(match.group(part)) > ceiling:
            raise ProvenanceError(
                f"{where} has an out-of-range {part} in {value!r}; note that "
                '"24:00:00" is an error on some Python versions and the next '
                "day on others, so it is refused rather than interpreted"
            )
    if match.group("oh") is not None:
        if int(match.group("oh")) > 23 or int(match.group("om")) > 59:
            raise ProvenanceError(
                f"{where} has an out-of-range UTC offset in {value!r}; "
                "the parser would silently repair it"
            )
        if (
            match.group("sign") == "-"
            and match.group("oh") == "00"
            and match.group("om") == "00"
        ):
            raise ProvenanceError(
                f'{where} uses "-00:00", which RFC 3339 reads as "offset '
                f'unknown"; write "Z" if the bound is UTC, got {value!r}'
            )
    try:
        return datetime.fromisoformat(value)
    except ValueError as exc:
        raise ProvenanceError(f"{where} is not a real date: {value!r} ({exc})") from exc


def verify_envelope(envelope: dict, expected_digest: str, known_profiles: set[str]) -> dict:
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
            "this export declares a different governance digest "
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
    if bounds["window_start"] > bounds["window_end"]:
        raise ProvenanceError(
            f'window_start ({envelope["window_start"]}) is after '
            f'window_end ({envelope["window_end"]})'
        )
    out = {f: envelope[f] for f in ENVELOPE_FIELDS}
    if "graph_instance_id" in envelope:
        out["graph_instance_id"] = envelope["graph_instance_id"]
    if "instance_id" in envelope:
        out["instance_id"] = envelope["instance_id"]
    return out


def _count(row: dict, key: str, where: str, *, missing: int | None = 0) -> int | None:
    if key not in row:
        return missing
    value = row[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{where}: {key} must be an integer, got {value!r}")
    if value < 0:
        raise ValueError(f"{where}: {key} must be >= 0, got {value}")
    return value


def load_export(path: Path) -> tuple[dict | None, list[dict]]:
    lines = path.read_text(encoding="utf-8").splitlines()
    envelope: dict | None = None
    body: list[tuple[int, str]] = []
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
    return load_export(path)[1]


def _load_rows(path: Path, lines: list[tuple[int, str]]) -> list[dict]:
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


def resolve_grants_from_graph(
    session: Any, grants: list[dict]
) -> tuple[dict[tuple[str, str], str], list[dict]]:
    """Map manifest grants to live qualified keys via graph-grounded catalog queries.

    Bare references in `grants` are resolved against the live database catalog
    using `queries.resolve_tool_refs(session, bare_refs)`. If a bare reference matches
    multiple live tools across servers, it is marked as AMBIGUOUS_GRANT and excluded
    from resolved grants, protecting valid servers from inadvertent revocations.
    """
    bare_refs = sorted({g["tool"] for g in grants if "::" not in g["tool"]})
    graph_resolved = queries.resolve_tool_refs(session, bare_refs) if bare_refs else {}

    resolved: dict[tuple[str, str], str] = {}
    ambiguous: list[dict] = []

    for g in grants:
        agent, tool = g["agent"], g["tool"]
        if "::" in tool:
            resolved[(agent, tool)] = tool
            continue
        matches = graph_resolved.get(tool, [])
        if len(matches) == 1:
            resolved[(agent, matches[0])] = tool
        elif len(matches) > 1:
            ambiguous.append({
                "agent": agent,
                "tool": tool,
                "would_block_calls": 0,
                "reason": "AMBIGUOUS_GRANT",
                "note": (
                    f"this bare grant matches multiple tools in the live graph: "
                    f"{' or '.join(sorted(matches))} — qualify it in the manifest before reasoning about it"
                ),
            })
        else:
            # Unmatched in live graph (drifted/uncrawled): keep as bare ref so
            # it cannot accidentally match any resolved qualified observation row.
            resolved[(agent, tool)] = tool

    return resolved, ambiguous


def propose(
    gov: dict,
    observed: list[dict],
    min_would_block: int,
    *,
    granted: dict[tuple[str, str], str],
    ambiguous_grants: list[dict],
) -> dict[str, Any]:
    known_agents = set(gov.get("agents") or [])
    grants = gov.get("grants") or []
    for g in grants:
        known_agents.add(g["agent"])

    unknown = sorted({o["agent"] for o in observed} - known_agents)
    if unknown:
        raise PolicyProposeError(
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
            used[key] += o["calls"] or 0
            if o["calls"] == 0:
                zero_call.add(key)
        if o["decision"] != "rejected":
            continue
        reasons = set(o["reasons"])
        wb = o["would_block_calls"]
        entry = {
            "agent": agent,
            "tool": ref,
            "would_block_calls": wb,
            "reason": ", ".join(sorted(reasons)),
        }

        deny = sorted(r for r in reasons if "DENY" in r)
        if deny:
            conflicts.append({
                **entry,
                "reason": ", ".join(deny),
                "note": (
                    "governed by a DENY policy — do not grant; change the "
                    "policy or the workflow, not the grant"
                ),
            })
            continue
        if wb < min_would_block:
            continue
        if not o["reasons_complete"]:
            unproven.append({
                **entry,
                "note": (
                    "export carries only the winning reject_reason; "
                    "re-export reject_reasons before proposing"
                ),
            })
            continue
        if reasons == {"NOT_GRANTED"}:
            if not o["has_tool_key"]:
                unproven.append({
                    **entry,
                    "note": (
                        "no canonical tool_key — resolve the ref "
                        "before proposing a grant for it"
                    ),
                })
            elif key not in granted:
                additions.append({**entry, "reason": "NOT_GRANTED"})
            continue
        if "NOT_GRANTED" in reasons:
            blockers = sorted(reasons - {"NOT_GRANTED"})
            other.append({
                **entry,
                "reason": ", ".join(blockers),
                "note": (
                    "ungranted AND "
                    + "; ".join(NON_GRANT_REMEDIES.get(r, r.lower()) for r in blockers)
                    + " — a grant alone would not make it eligible"
                ),
            })
            continue
        first = sorted(reasons)[0]
        other.append({
            **entry,
            "note": NON_GRANT_REMEDIES.get(
                first, "not a grant problem — read the selector reason before acting"
            ),
        })

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
    body = yaml.safe_dump(mapping, default_flow_style=True, sort_keys=False).strip()
    body = body.removesuffix("...").strip()
    return f"  - {body}  # {' '.join(comment.split())}"


def render(
    proposal: dict,
    window_label: str,
    min_would_block: int,
    envelope: dict | None = None,
    provenance_note: str | None = None,
) -> str:
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
        if provenance_note:
            a(f"> provenance: {provenance_note}")
            a(">")
        a(f'> governance_digest: `{envelope["governance_digest"]}` (verified)')
        a(
            f'> graph_generation: {envelope["graph_generation"]} · '
            f'profile: `{envelope["profile"]}` (declared)'
        )
        a(
            f'> window: {envelope["window_start"]} .. {envelope["window_end"]} '
            "(declared)"
        )
        if envelope["profile"] != "strict":
            a(">")
            a(
                f'> Declared as the `{envelope["profile"]}` profile, which rejects '
                "fewer reasons than `strict`. A tool absent from these rows was not"
            )
            a("> necessarily allowed under strict.")
    a("")
    a(
        f"decision_mode: human_required · automatic_change: false · "
        f"window: {window_label} · evidence floor: would_block >= {min_would_block}"
    )
    a("")
    if proposal["additions"]:
        a("## Proposed grant additions (the export reports blocked calls)")
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
            a(yaml_entry(
                {"agent": r["agent"], "tool": r["tool"]},
                "verify the window is representative before removing",
            ))
        a("")
    if proposal["insufficient_evidence"]:
        a("## Insufficient evidence to propose — re-export first")
        a("")
        for r in proposal["insufficient_evidence"]:
            a(
                f'- {r["agent"]} → `{r["tool"]}` ({r["reason"]}, '
                f'would_block={r["would_block_calls"]}): {r["note"]}'
            )
        a("")
    if proposal["not_grant_shaped"]:
        a("## Blocked, but a grant is not the remedy — NOT proposed")
        a("")
        for r in proposal["not_grant_shaped"]:
            a(
                f'- {r["agent"]} → `{r["tool"]}` ({r["reason"]}, '
                f'would_block={r["would_block_calls"]}): {r["note"]}'
            )
        a("")
    if proposal["conflicts"]:
        a("## Policy conflicts — NOT proposed")
        a("")
        for r in proposal["conflicts"]:
            a(
                f'- {r["agent"]} → `{r["tool"]}` ({r["reason"]}, '
                f'would_block={r["would_block_calls"]}): {r["note"]}'
            )
        a("")
    a(
        "Apply by editing governance.yaml yourself, then `toolgraph ingest-manifest` "
        "and a fresh `policy compile` per agent. This file is evidence, not authority."
    )
    return "\n".join(out) + "\n"


def run_propose(
    governance_path: Path,
    observed_path: Path,
    *,
    min_would_block: int = 3,
    window_label: str = "unspecified",
    unverified_provenance: bool = False,
    revalidate_current: bool = False,
) -> tuple[dict[str, Any], str]:
    """Execute end-to-end policy proposal with single graph-bracketed transaction."""
    gov_model: Governance = load_governance(governance_path)
    gov = gov_model.model_dump(mode="json")
    expected_digest = governance_digest(gov_model)

    raw_envelope, observed = load_export(observed_path)
    envelope: dict | None = None
    if raw_envelope is not None:
        envelope = verify_envelope(raw_envelope, expected_digest, set(PROFILES))
    elif not unverified_provenance:
        raise ProvenanceError(
            "this export carries no envelope, so nothing can tell it from a replay "
            "of a window whose rows were recorded against an older manifest.\n"
            "Re-export with an envelope line:\n"
            '  {"envelope": {"governance_digest": "<64 hex>", "graph_generation": 0, '
            '"profile": "strict", "window_start": "...", "window_end": "..."}}\n'
            f"The digest for {governance_path} right now is {expected_digest}.\n"
            "Or pass --unverified-provenance to proceed without envelope verification."
        )

    # Perform graph-bracketed read for live state & grant resolution
    def fetch_bracketed():
        st = queries.graph_state()
        with driver.session() as s:
            granted, ambiguous_grants = resolve_grants_from_graph(s, gov.get("grants") or [])
            # Also resolve live tool keys for revalidation if needed
            live_tools = queries.resolve_tool_refs(s, [o["ref"] for o in observed if o["has_tool_key"]])
        return {
            "state": st,
            "granted": granted,
            "ambiguous_grants": ambiguous_grants,
            "live_tools": live_tools,
        }

    try:
        bracket_result = queries.with_graph_state(fetch_bracketed, strict=True)
    except RuntimeError as exc:
        if "graph state changed during every read attempt" in str(exc) or "generation changed" in str(exc):
            raise StateChurnError("graph state churn during observation proposal: concurrent graph modifications exhausted 5 read attempts") from exc
        raise

    state: queries.GraphState = bracket_result["state"]
    granted = bracket_result["granted"]
    ambiguous_grants = bracket_result["ambiguous_grants"]
    live_tools = bracket_result["live_tools"]

    provenance_note: str | None = None

    # Composite state validation if envelope is present
    if envelope is not None:
        env_instance = envelope.get("graph_instance_id") or envelope.get("instance_id")
        if state.instance_id and env_instance and env_instance != state.instance_id:
            raise StateMismatchError(
                f"graph instance mismatch: export declared {env_instance}, "
                f"live graph is {state.instance_id}"
            )
        if state.governance_digest and envelope["governance_digest"] != state.governance_digest:
            raise StateMismatchError(
                f"graph governance digest mismatch: export declared {envelope['governance_digest'][:12]}..., "
                f"live graph is {state.governance_digest[:12]}..."
            )
        if envelope["graph_generation"] != state.generation:
            if not revalidate_current:
                raise StateMismatchError(
                    f"export declared graph_generation {envelope['graph_generation']} but "
                    f"live graph generation is {state.generation}. The catalog may have changed. "
                    "Re-export or pass --revalidate-current to revalidate against the current catalog."
                )
            provenance_note = (
                f"declared (revalidated against current catalog; observed: "
                f"instance={env_instance or 'none'}, gen={envelope['graph_generation']}; "
                f"current: instance={state.instance_id}, gen={state.generation})"
            )
        else:
            provenance_note = (
                f"declared (exact composite token matched: {state.instance_id}, "
                f"gen {state.generation})"
            )

    proposal = propose(
        gov,
        observed,
        min_would_block,
        granted=granted,
        ambiguous_grants=ambiguous_grants,
    )

    # In revalidate-current mode, verify that proposed additions still exist in the current graph
    if revalidate_current and proposal["additions"]:
        surviving_additions = []
        for add in proposal["additions"]:
            tool_key = add["tool"]
            matches = live_tools.get(tool_key, [])
            if matches:
                surviving_additions.append(add)
            else:
                proposal["not_grant_shaped"].append({
                    **add,
                    "reason": "DRIFTED",
                    "note": f"tool '{tool_key}' no longer exists in the current live graph catalog (revalidation)",
                })
        proposal["additions"] = surviving_additions

    rendered = render(
        proposal,
        window_label,
        min_would_block,
        envelope=envelope,
        provenance_note=provenance_note,
    )
    return proposal, rendered
