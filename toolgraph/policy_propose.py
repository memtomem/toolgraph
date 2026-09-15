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

from toolgraph.graph import driver, queries, selector
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


def _instance_id(value: object, where: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or UNSAFE_IN_NAME.search(value):
        raise ProvenanceError(f"{where} must be a string without control characters")
    if not value.strip():
        return None
    if value != value.strip():
        raise ProvenanceError(f"{where} must not have surrounding whitespace")
    return value


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
    canonical = _instance_id(envelope.get("graph_instance_id"), "graph_instance_id")
    alias = _instance_id(envelope.get("instance_id"), "instance_id")
    if canonical is not None and alias is not None and canonical != alias:
        raise ProvenanceError("graph_instance_id and instance_id conflict")
    out["graph_instance_id"] = canonical or alias
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
        a(f'> governance_digest: `{envelope["governance_digest"]}` (matches supplied manifest)')
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
    if provenance_note:
        a(">")
        a(f"> provenance: {provenance_note}")
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

    # Recompute every graph-dependent input together on each bracket retry.
    def fetch_bracketed():
        st = queries.graph_state()
        with driver.session() as session:
            granted, ambiguous = resolve_grants_from_graph(session, gov.get("grants") or [])
        proposal = propose(
            gov, observed, min_would_block,
            granted=granted, ambiguous_grants=ambiguous,
        )
        ranked = []
        if revalidate_current:
            candidates: dict[str, list[str]] = defaultdict(list)
            for addition in proposal["additions"]:
                candidates[addition["agent"]].append(addition["tool"])
            for agent, keys in sorted(candidates.items()):
                for start in range(0, len(keys), selector.MAX_CANDIDATES):
                    ranked.append(selector.rank_features(
                        agent, keys[start:start + selector.MAX_CANDIDATES],
                    ))
        return {"state": st, "proposal": proposal, "ranked": ranked}

    try:
        bracket_result = queries.with_graph_state(fetch_bracketed, strict=True)
    except RuntimeError as exc:
        if str(exc) in {
            "graph state changed during every read attempt",
            "graph generation changed during every read attempt",
        }:
            raise StateChurnError(
                "graph state churn during observation proposal: "
                "concurrent graph modifications exhausted 5 read attempts"
            ) from exc
        raise

    provenance = _proposal_provenance(
        envelope, bracket_result["state"], expected_digest=expected_digest,
        unverified_provenance=unverified_provenance,
        revalidate_current=revalidate_current,
    )
    proposal = bracket_result["proposal"]
    if revalidate_current:
        _revalidate_additions(proposal, bracket_result["ranked"])
    proposal["provenance"] = provenance
    rendered = render(
        proposal, window_label, min_would_block, envelope=envelope,
        provenance_note=_provenance_note(provenance),
    )
    return proposal, rendered


def _proposal_provenance(
    envelope: dict | None,
    state: queries.GraphState,
    *,
    expected_digest: str,
    unverified_provenance: bool,
    revalidate_current: bool,
) -> dict[str, Any]:
    instance = _instance_id(state.instance_id, "live instance_id")
    digest = state.governance_digest
    if digest is not None:
        if not isinstance(digest, str):
            raise ProvenanceError("live governance_digest must be a string")
        if not digest.strip():
            digest = None
        elif not DIGEST_RE.fullmatch(digest):
            raise ProvenanceError("live governance_digest must be 64 lowercase hex characters")
    if digest is not None and digest != expected_digest:
        raise StateMismatchError("graph governance digest mismatch: supplied manifest and live graph differ")
    if type(state.generation) is not int or state.generation < 0:
        raise ProvenanceError("live graph generation must be a non-negative integer")
    current = {"instance_id": instance, "generation": state.generation, "governance_digest": digest}
    observed = None
    missing = []
    if envelope is None:
        missing.append("envelope")
    else:
        env_instance = envelope["graph_instance_id"]
        observed = {
            "instance_id": env_instance,
            "generation": envelope["graph_generation"],
            "governance_digest": envelope["governance_digest"],
        }
        if instance is not None and env_instance is not None and instance != env_instance:
            raise StateMismatchError(
                f"graph instance mismatch: export declared {env_instance}, live graph is {instance}"
            )
        if digest is not None and digest != observed["governance_digest"]:
            raise StateMismatchError("graph governance digest mismatch: export and live graph differ")
        if observed["generation"] != state.generation and not revalidate_current:
            raise StateMismatchError(
                f"export declared graph_generation {observed['generation']} but "
                f"live graph generation is {state.generation}. The catalog may have changed. "
                "Re-export or pass --revalidate-current to revalidate against the current catalog."
            )
        if env_instance is None:
            missing.append("observed.instance_id")
    if instance is None:
        missing.append("current.instance_id")
    if digest is None:
        missing.append("current.governance_digest")
    if missing and not unverified_provenance:
        raise ProvenanceError(
            f"incomplete provenance: missing {', '.join(missing)}. "
            "Re-export/initialize the graph, or pass --unverified-provenance "
            "to proceed with explicitly unverified provenance."
        )
    return {
        "status": "unverified" if missing else "revalidated" if revalidate_current else "exact",
        "missing_fields": missing,
        "observed_state": observed,
        "current_state": current,
        "current_revalidated": revalidate_current,
        "revalidation_profile": "strict" if revalidate_current else None,
    }


def _provenance_note(provenance: dict) -> str:
    current = provenance["current_state"]
    if provenance["status"] == "exact":
        return (
            f"declared (exact composite token matched: {current['instance_id']}, "
            f"gen {current['generation']})"
        )
    observed = provenance["observed_state"] or {}
    tokens = (
        f"observed: instance={observed.get('instance_id') or 'none'}, "
        f"gen={observed.get('generation', 'none')}; "
        f"current: instance={current['instance_id']}, gen={current['generation']}"
    )
    checked = "revalidated against current catalog; profile=strict; " if provenance["current_revalidated"] else ""
    if provenance["status"] == "unverified":
        return (
            "UNVERIFIED PROVENANCE (missing: " + ", ".join(provenance["missing_fields"])
            + f"; {checked}{tokens}; current checks do not establish historical provenance)"
        )
    return f"declared ({checked}{tokens})"


def _revalidate_additions(proposal: dict, ranked_batches: list[dict]) -> None:
    features = {
        (ranked["agent"], feature["candidate"]): feature
        for ranked in ranked_batches for feature in ranked["features"]
    }
    missing_agents = {ranked["agent"] for ranked in ranked_batches if not ranked["agent_found"]}
    survivors = []
    for addition in proposal["additions"]:
        agent, tool = addition["agent"], addition["tool"]
        if agent in missing_agents:
            proposal["insufficient_evidence"].append({
                **addition, "reason": "AGENT_NOT_FOUND",
                "note": "agent is absent from the current graph (strict revalidation)",
            })
            continue
        feature = features[(agent, tool)]
        # Only the hypothetical grant changes. Drift, mapping and DENY facts
        # remain authoritative, including reasons hidden behind NOT_GRANTED.
        hypothetical = {**feature, "permitted": True}
        filtered = selector.filter_features(
            {"agent": agent, "agent_found": True, "features": [hypothetical]}, "strict",
        )
        if filtered["rejected"]:
            reason = filtered["rejected"][0]["reason"]
            bucket = "conflicts" if reason in {"DENY_VIOLATION", "DENY_GOVERNED"} else "not_grant_shaped"
            proposal[bucket].append({
                **addition, "reason": reason,
                "note": f"current strict revalidation rejected {tool}: {reason}; a grant cannot clear this blocker",
            })
        elif feature["permitted"]:
            proposal["insufficient_evidence"].append({
                **addition, "reason": "ALREADY_GRANTED",
                "note": "the current graph already grants this tool; no addition proposed",
            })
        else:
            survivors.append(addition)
    proposal["additions"] = survivors
    proposal["conflicts"].sort(key=lambda row: (row["agent"], row["tool"]))
    for bucket in ("not_grant_shaped", "insufficient_evidence"):
        proposal[bucket].sort(key=lambda row: (-row["would_block_calls"], row["agent"], row["tool"]))
