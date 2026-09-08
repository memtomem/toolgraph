"""Deterministic selector surface (ADR-0005): filter/features in, learning out.

Three batch-first entry points serve tool selection as a context-engineering
layer — shrink a candidate catalog deterministically before any relevance
ranker sees it:

- ``rank_features`` — per-candidate facts + a rule-based ``risk_score`` from
  the published fixed table below. No relevance, no learning.
- ``eligible_tools`` — hard filter with reject reasons and policy-evidence
  paths. Profiles are named rule sets, not learned thresholds; production
  default is ``strict``.
- ``selection_explain`` — compact human-readable reasons for one (agent, tool).

Batch-first because per-candidate ``check_access`` calls scale linearly with
candidate count — unacceptable on the online selection path. Everything here
runs a fixed number of queries regardless of N.

Hard boundary (ADR-0005): no relevance scoring, no learning. A learned
consumer may never override a hard reject (NOT_GRANTED, violation, drifted).
"""

from __future__ import annotations

from toolgraph.graph.driver import session
from toolgraph.graph.queries import _deny_evidence_batch, agent_exists, resolve_tool_refs

# Hard-filter rule sets (ADR-0005). Reasons are checked in _REASON_PRECEDENCE
# order; the first applicable reason that the profile rejects wins. These are
# named rule sets, NOT tunable thresholds — changing them is an ADR-level
# decision, not a config knob.
#
# - strict (production default): only a clean, fully-authored ALLOW passes —
#   granted, no DENY path of any classification, currently exposed, and with
#   authored data-flow (an unmapped tool is exactly the blind spot the graph
#   cannot vouch for, so production fails closed).
# - review (human-in-the-loop): operator-declared exceptions and unmapped
#   tools pass; violations still reject.
# - explore (offline eval / sandbox): only resolution failures, missing
#   grants, and drift reject — DENY rows become penalties via rank_features.
PROFILES: dict[str, frozenset[str]] = {
    "strict": frozenset(
        {
            "TOOL_NOT_FOUND",
            "AMBIGUOUS_TOOL",
            "NOT_GRANTED",
            "DENY_VIOLATION",
            "DENY_GOVERNED",
            "DRIFTED",
            "UNMAPPED",
        }
    ),
    "review": frozenset(
        {"TOOL_NOT_FOUND", "AMBIGUOUS_TOOL", "NOT_GRANTED", "DENY_VIOLATION", "DRIFTED"}
    ),
    "explore": frozenset({"TOOL_NOT_FOUND", "AMBIGUOUS_TOOL", "NOT_GRANTED", "DRIFTED"}),
}

DEFAULT_PROFILE = "strict"

# Batch bound, mirroring the control-plan limit: the selector is batch-first,
# but an unbounded candidate list still UNWINDs as one query parameter. Lives
# here so every surface — CLI, MCP server, library callers — is bounded.
MAX_CANDIDATES = 4096

# Drift rejects in EVERY profile (the report's regression criteria): a tool no
# server currently exposes cannot be called, so passing it is never useful.
_REASON_PRECEDENCE = (
    "TOOL_NOT_FOUND",
    "AMBIGUOUS_TOOL",
    "NOT_GRANTED",
    "DENY_VIOLATION",
    "DENY_GOVERNED",
    "DRIFTED",
    "UNMAPPED",
)


def _risk_score(feature: dict) -> float | None:
    """Rule-based risk from the published fixed table (ADR-0005 / report).

    First match wins, top-down. This is a fact-derived feature, not a learned
    weight — consumers may add penalties on top but never subtract a hard
    reject. ``None`` when the candidate didn't resolve to exactly one tool
    (no facts to score).

        1.0  DENY path with classification=violation
        0.8  drifted (no live EXPOSES edge)
        0.6  unmapped (no authored READS/WRITES)
        0.4  unbacked load-bearing edge in the tool's evidence chain
        0.2  DENY path fully covered by operator-declared exceptions
        0.0  otherwise
    """
    if not feature["found"] or feature["ambiguous"]:
        return None
    if feature["classification"] == "violation":
        return 1.0
    if feature["is_drifted"]:
        return 0.8
    if feature["is_unmapped"]:
        return 0.6
    if feature["has_unbacked_edges"]:
        return 0.4
    if feature["classification"] == "authorized_but_governed":
        return 0.2
    return 0.0


def _resolve_all(s, refs: list[str]) -> dict[str, list[str]]:
    """Use the same bounded resolution as ingest and single-tool queries."""
    return resolve_tool_refs(s, refs)


def _tool_facts(s, keys: list[str], agent: str) -> dict[str, dict]:
    """Batch per-tool facts for resolved keys: grant, drift, mapping, evidence
    coverage, and the #14 annotation self-claims (which enrich features but
    never gate by themselves — they are crawled/medium claims, ADR-0006)."""
    rows = s.run(
        """
        MATCH (t:Tool) WHERE t.key IN $keys
        RETURN t.key AS key,
               t.name AS name, t.server AS server,
               t.read_only_hint AS read_only_hint,
               t.destructive_hint AS destructive_hint,
               t.idempotent_hint AS idempotent_hint,
               t.open_world_hint AS open_world_hint,
               EXISTS { MATCH (:Agent {id:$agent})-[:CAN_CALL]->(t) } AS permitted,
               NOT EXISTS { MATCH (:MCPServer)-[:EXPOSES]->(t) } AS is_drifted,
               NOT EXISTS { MATCH (t)-[:READS|WRITES]->(:Resource) } AS is_unmapped,
               EXISTS {
                   MATCH (t)-[r:READS|WRITES|GOVERNED_BY]->()
                   WHERE r.evidence IS NULL OR trim(r.evidence) = ''
               } OR EXISTS {
                   MATCH (t)-[:READS|WRITES]->(:Resource)-[g:GOVERNED_BY]->()
                   WHERE g.evidence IS NULL OR trim(g.evidence) = ''
               } AS has_unbacked_edges
        """,
        keys=keys,
        agent=agent,
    )
    return {r["key"]: dict(r) for r in rows}


def _classify(deny_rows: list[dict]) -> str | None:
    """Worst-case classification across a tool's deny paths: any violation row
    dominates; all-exception rows collapse to authorized_but_governed."""
    if not deny_rows:
        return None
    if any(d.get("classification") == "violation" for d in deny_rows):
        return "violation"
    return "authorized_but_governed"


def rank_features(agent: str, candidates: list[str]) -> dict:
    """Batch selection features for every candidate, in input order.

    ``agent_found: false`` means selection itself should abort (a context
    construction error, not an empty result) — features come back empty.

    Each feature row keeps the input ref as its stable id (``candidate``) and
    carries: resolution (found/ambiguous/tool_key), grant (``permitted``),
    verdict (mirrors ``check_access``), worst-case deny ``classification``
    with the full ``deny_paths``, drift / mapping / evidence-coverage facts,
    the four annotation self-claims (ADR-0006), and the rule-based
    ``risk_score`` (see ``_risk_score`` for the fixed table).
    """
    if len(candidates) > MAX_CANDIDATES:
        raise ValueError(
            f"{len(candidates)} candidates exceed the {MAX_CANDIDATES} limit"
        )
    if not agent_exists(agent):
        return {"agent": agent, "agent_found": False, "features": []}

    with session() as s:
        resolved = _resolve_all(s, candidates)
        unique_keys = sorted(
            {keys[0] for keys in resolved.values() if len(keys) == 1}
        )
        facts = _tool_facts(s, unique_keys, agent)
        deny = _deny_evidence_batch(s, unique_keys, agent=agent)

    features: list[dict] = []
    for ref in candidates:  # input order is the contract — never re-sorted
        keys = resolved.get(ref, [])
        row: dict = {
            "candidate": ref,
            "tool_key": None,
            "found": bool(keys),
            "ambiguous": len(keys) > 1,
            "permitted": None,
            "verdict": None,
            "classification": None,
            "deny_paths": [],
            "is_drifted": None,
            "is_unmapped": None,
            "has_unbacked_edges": None,
            "read_only_hint": None,
            "destructive_hint": None,
            "idempotent_hint": None,
            "open_world_hint": None,
        }
        if not keys:
            row["verdict"] = "TOOL_NOT_FOUND"
        elif len(keys) > 1:
            # Never auto-resolved: the consumer must re-submit the
            # server-qualified key (regression criterion #1).
            row["verdict"] = "AMBIGUOUS_TOOL"
            row["candidates"] = keys
        elif keys[0] not in facts:
            # Tool deleted between resolution and the facts read — Neo4j is
            # read-committed, so the two batch queries can straddle a
            # concurrent crawl/ingest (the same window check_access closes
            # with its meta-is-None branch). Report the truth signal instead
            # of crashing the whole batch (Codex review).
            row["found"] = False
            row["verdict"] = "TOOL_NOT_FOUND"
        else:
            key = keys[0]
            f = facts[key]
            deny_rows = deny[key]
            row["tool_key"] = key
            row["permitted"] = f["permitted"]
            row["classification"] = _classify(deny_rows)
            row["deny_paths"] = deny_rows
            row["is_drifted"] = f["is_drifted"]
            row["is_unmapped"] = f["is_unmapped"]
            row["has_unbacked_edges"] = f["has_unbacked_edges"]
            for hint in (
                "read_only_hint",
                "destructive_hint",
                "idempotent_hint",
                "open_world_hint",
            ):
                row[hint] = f[hint]
            if not f["permitted"]:
                row["verdict"] = "NOT_GRANTED"
            elif deny_rows:
                row["verdict"] = "DENY"
            else:
                row["verdict"] = "ALLOW"
        row["risk_score"] = _risk_score(row)
        features.append(row)

    return {"agent": agent, "agent_found": True, "features": features}


def _applicable_reasons(feature: dict) -> list[str]:
    """All reject conditions this feature row triggers, in precedence order."""
    reasons: list[str] = []
    if not feature["found"]:
        reasons.append("TOOL_NOT_FOUND")
        return reasons  # nothing else is knowable about an unresolved ref
    if feature["ambiguous"]:
        reasons.append("AMBIGUOUS_TOOL")
        return reasons
    if not feature["permitted"]:
        reasons.append("NOT_GRANTED")
    if feature["classification"] == "violation":
        reasons.append("DENY_VIOLATION")
    elif feature["classification"] == "authorized_but_governed":
        reasons.append("DENY_GOVERNED")
    if feature["is_drifted"]:
        reasons.append("DRIFTED")
    if feature["is_unmapped"]:
        reasons.append("UNMAPPED")
    return reasons


def filter_features(ranked: dict, profile: str = DEFAULT_PROFILE) -> dict:
    """Pure hard filter over an already-computed ``rank_features`` result.

    Runs no queries: producers that need both the filter verdict and the
    feature rows (policy bundle, preflight ``--features``, explain) call
    ``rank_features`` once and filter its output, instead of paying the full
    graph evaluation twice inside the generation-retry bracket.
    """
    if profile not in PROFILES:
        raise ValueError(
            f"unknown profile {profile!r} — expected one of {sorted(PROFILES)}"
        )
    agent = ranked["agent"]
    if not ranked["agent_found"]:
        return {
            "agent": agent,
            "agent_found": False,
            "profile": profile,
            "eligible": [],
            "rejected": [],
        }

    rejects = PROFILES[profile]
    eligible: list[str] = []
    rejected: list[dict] = []
    for feature in ranked["features"]:
        reason = next(
            (r for r in _applicable_reasons(feature) if r in rejects), None
        )
        if reason is None:
            eligible.append(feature["tool_key"])
            continue
        row = {
            "candidate": feature["candidate"],
            "tool_key": feature["tool_key"],
            "reason": reason,
        }
        if reason == "AMBIGUOUS_TOOL":
            row["candidates"] = feature["candidates"]
        if reason in ("DENY_VIOLATION", "DENY_GOVERNED"):
            wanted = (
                "violation" if reason == "DENY_VIOLATION" else "authorized_but_governed"
            )
            row["paths"] = [
                d["path"]
                for d in feature["deny_paths"]
                if d.get("classification") == wanted
            ]
        rejected.append(row)

    return {
        "agent": agent,
        "agent_found": True,
        "profile": profile,
        "eligible": eligible,
        "rejected": rejected,
    }


def eligible_tools(
    agent: str, candidates: list[str], profile: str = DEFAULT_PROFILE
) -> dict:
    """Hard filter with reject reasons and policy-evidence paths (ADR-0005).

    ``eligible`` holds resolved tool keys in candidate input order; every
    rejected row keeps the input ref (``candidate``), the winning ``reason``
    (first by precedence among the profile's reject set), and — for DENY
    reasons — the graph paths that prove it. A learned consumer may rerank
    ``eligible`` but may never resurrect a rejected row.
    """
    if profile not in PROFILES:
        raise ValueError(
            f"unknown profile {profile!r} — expected one of {sorted(PROFILES)}"
        )
    return filter_features(rank_features(agent, candidates), profile)


def selection_explain(
    agent: str, tool: str, profile: str = DEFAULT_PROFILE
) -> dict:
    """Compact human-readable reasons for one (agent, tool) — a view over the
    same facts ``rank_features`` returns, for operators and end users."""
    if profile not in PROFILES:
        raise ValueError(
            f"unknown profile {profile!r} — expected one of {sorted(PROFILES)}"
        )
    ranked = rank_features(agent, [tool])
    if not ranked["agent_found"]:
        return {
            "agent": agent,
            "tool": tool,
            "tool_key": None,
            "agent_found": False,
            "profile": profile,
            "decision": "rejected",
            "reasons": [f"agent {agent!r} not found in the graph — selection aborted"],
        }

    feature = ranked["features"][0]
    filtered = filter_features(ranked, profile)
    decision = "eligible" if filtered["eligible"] else "rejected"

    reasons: list[str] = []
    if not feature["found"]:
        reasons.append(f"no tool named {tool!r} in the graph")
    elif feature["ambiguous"]:
        reasons.append(
            f"bare name matches multiple servers {feature['candidates']} — "
            f"pass the server-qualified key; never auto-selected"
        )
    else:
        reasons.append(
            "agent has CAN_CALL grant"
            if feature["permitted"]
            else "agent has no CAN_CALL grant"
        )
        if not feature["deny_paths"]:
            reasons.append("no DENY policy path found")
        for d in feature["deny_paths"]:
            label = d.get("classification", "deny")
            line = f"DENY path [{label}]: {d['path']}"
            if d.get("exception_reason"):
                line += f" (operator: {d['exception_reason']})"
            reasons.append(line)
        reasons.append(
            "tool has drifted: no server currently exposes it"
            if feature["is_drifted"]
            else "tool is currently exposed by a crawled server"
        )
        reasons.append(
            "no authored READS/WRITES — data-flow effects unclassified"
            if feature["is_unmapped"]
            else "data-flow effects are authored (READS/WRITES present)"
        )
        reasons.append(
            "load-bearing edge(s) missing evidence pointers"
            if feature["has_unbacked_edges"]
            else "all load-bearing edges carry evidence"
        )
        for hint in ("read_only_hint", "destructive_hint", "idempotent_hint", "open_world_hint"):
            if feature[hint] is not None:
                reasons.append(
                    f"server self-claim (crawled/medium): {hint}={feature[hint]}"
                )

    return {
        "agent": agent,
        "tool": tool,
        "tool_key": feature["tool_key"],
        "agent_found": True,
        "found": feature["found"],
        "profile": profile,
        "decision": decision,
        "risk_score": feature["risk_score"],
        "reasons": reasons,
    }
