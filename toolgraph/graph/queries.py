"""Governance and audit queries — the product.

Each returns explainable structure (the path + evidence), not a bare boolean.
Reachability spans Agent -> Tool -> Resource -> Policy across four node types,
which a vector index cannot express. Shared by the CLI and the MCP server.

A tool can be denied two ways, both honored here:
- transitively: Tool -[:READS|WRITES]-> Resource -[:GOVERNED_BY]-> Policy(DENY)
- directly:     Tool -[:GOVERNED_BY]-> Policy(DENY)

Tool refs accept the exact key ("<server>::<tool>") or a bare name. A bare name
that matches tools on multiple servers is reported as AMBIGUOUS_TOOL rather than
silently resolved. ``blast_radius`` auto-detects a resource URI (contains "://")
vs a policy id.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from toolgraph.artifact_safety import display_resource
from toolgraph.graph.driver import read_session
from toolgraph.uris import is_resource_ref, normalize_resource_uri

if TYPE_CHECKING:
    from neo4j import Session

_GENERATION_RETRIES = 5
# Exponential backoff between bracket retries (0.05/0.1/0.2/0.4s). Without
# it, a strict producer on a graph under sustained write churn re-reads
# immediately and can burn all five attempts inside one mutation burst —
# a liveness failure the pause largely avoids for ~750ms worst case.
_RETRY_BACKOFF_SECONDS = 0.05


def _bracket_backoff(attempt: int) -> None:
    if attempt + 1 < _GENERATION_RETRIES:
        time.sleep(_RETRY_BACKOFF_SECONDS * (2**attempt))


class GraphStateChurnError(RuntimeError):
    """A strict bracket found the graph state moving on every retry."""


@dataclass(frozen=True)
class GraphState:
    """Collision-safe identity for one concrete graph state."""

    instance_id: str | None
    generation: int
    governance_digest: str | None = None


def with_graph_state(
    fetch: Callable[[], dict],
    *,
    strict: bool = False,
    state_reader: Callable[[], GraphState] | None = None,
) -> dict:
    """Return ``fetch`` bracketed by the collision-safe graph state token.

    Neo4j is read-committed rather than snapshot-isolated. Reading the state
    before and after the query and retrying on a change prevents a result
    from being labelled with a state that committed midway through it.

    Persisted artifact producers pass ``strict=True`` and get a
    ``GraphStateChurnError`` rather than write a mislabelled artifact. A live
    query still returns its data after retry exhaustion, because an answer
    under churn beats an error, but every state field comes back null with
    ``graph_state_verified: False``. Stamping the state read afterwards would
    label the result with a state it was never read at, and callers cache per
    state, so a verdict from before a permission change could be cached under
    the very state that changed it. Null does not enforce anything by itself:
    the obligation to skip the cache is the consumer's (ADR-0004).
    """
    read_state = state_reader if state_reader is not None else graph_state
    for attempt in range(_GENERATION_RETRIES):
        before = read_state()
        result = fetch()
        if read_state() == before:
            token = {"instance_id": before.instance_id, "generation": before.generation}
            return {
                **result,
                "graph_generation": before.generation,
                "graph_instance_id": before.instance_id,
                "graph_state": token,
                "graph_state_verified": True,
            }
        _bracket_backoff(attempt)
    if strict:
        raise GraphStateChurnError("graph state changed during every read attempt")
    return {
        **result,
        "graph_generation": None,
        "graph_instance_id": None,
        "graph_state": None,
        "graph_state_verified": False,
    }


RESOLVE_BATCH_SIZE = 4096


def resolve_tool_refs(s: Session, refs: list[str]) -> dict[str, list[str]]:
    """Resolve unique refs in bounded batches on the caller's transaction.

    Qualified keys and bare names stay separate, preserving index-friendly
    resolution and every ambiguous match. Empty and repeated refs cost no
    additional queries. Results are deterministic on both graph backends.
    """
    unique = sorted(set(refs))
    resolved: dict[str, list[str]] = {ref: [] for ref in unique}
    qualified = [ref for ref in unique if "::" in ref]
    bare = [ref for ref in unique if "::" not in ref]
    for start in range(0, len(qualified), RESOLVE_BATCH_SIZE):
        for row in s.run(
            "MATCH (t:Tool) WHERE t.key IN $keys RETURN t.key AS key",
            keys=qualified[start : start + RESOLVE_BATCH_SIZE],
        ):
            resolved[row["key"]].append(row["key"])
    for start in range(0, len(bare), RESOLVE_BATCH_SIZE):
        for row in s.run(
            "MATCH (t:Tool) WHERE t.name IN $names RETURN t.name AS name, t.key AS key",
            names=bare[start : start + RESOLVE_BATCH_SIZE],
        ):
            resolved[row["name"]].append(row["key"])
    return {ref: sorted(keys) for ref, keys in resolved.items()}


def resolve_tool_keys(s: Session, ref: str) -> list[str]:
    """All matches for one exact qualified key or bare name."""
    return resolve_tool_refs(s, [ref])[ref]


def graph_generation() -> int:
    """Current graph generation; 0 when no GraphMeta node exists (empty/reset graph).

    ADR-0004: a monotonic id bumped in the same transaction as every successful
    crawl load and manifest ingest. Consumers use it for cache invalidation
    (one integer comparison instead of re-running audit queries) and for
    pinning selection-telemetry rows to a replayable graph state.
    """
    with read_session() as s:
        rec = s.run(
            "MATCH (m:GraphMeta {id:'singleton'}) RETURN coalesce(m.generation, 0) AS generation"
        ).single()
        return rec["generation"] if rec else 0


def graph_state(s: Session | None = None) -> GraphState:
    """Return the current collision-safe graph state and governance digest."""
    if s is None:
        with read_session() as current:
            return _read_graph_state(current)
    return _read_graph_state(s)


def _read_graph_state(s: Session) -> GraphState:
    rec = s.run(
        "MATCH (m:GraphMeta {id:'singleton'}) "
        "RETURN m.instance_id AS instance_id, "
        "coalesce(m.generation, 0) AS generation, "
        "m.governance_digest AS governance_digest"
    ).single()
    if rec is None:
        return GraphState(instance_id=None, generation=0)
    return GraphState(
        instance_id=rec["instance_id"],
        generation=rec["generation"],
        governance_digest=rec["governance_digest"],
    )


def exposed_tool_contracts() -> list[dict]:
    """Canonical crawl facts for every tool currently exposed by a server."""
    with read_session() as s:
        rows = s.run(
            """
            MATCH (server:MCPServer)-[:EXPOSES]->(tool:Tool)
            RETURN tool.key AS tool_key,
                   server.name AS server,
                   tool.name AS name,
                   tool.description AS description,
                   tool.input_schema AS input_schema,
                   tool.read_only_hint AS read_only_hint,
                   tool.destructive_hint AS destructive_hint,
                   tool.idempotent_hint AS idempotent_hint,
                   tool.open_world_hint AS open_world_hint
            ORDER BY tool.key
            """
        )
        result = []
        for row in rows:
            item = dict(row)
            raw_schema = item.get("input_schema")
            item["input_schema"] = json.loads(raw_schema) if raw_schema else None
            result.append(item)
        return result


def _agent_present(s: Session, agent: str) -> bool:
    return s.run("RETURN EXISTS { MATCH (:Agent {id:$agent}) } AS ok", agent=agent).single()["ok"]


def agent_exists(agent: str) -> bool:
    """Is there an Agent node with this id? Used by the CLI + MCP surfaces to
    return a discriminated AGENT_NOT_FOUND signal instead of silently
    returning ``[]`` (which is indistinguishable from 'no unsafe tools')."""
    with read_session() as s:
        return _agent_present(s, agent)


def _agent_ids() -> list[str]:
    with read_session() as s:
        return [r["id"] for r in s.run("MATCH (a:Agent) RETURN a.id AS id ORDER BY id")]


# Paths show a resource or policy through display_resource while the value is
# still whole: once interpolated, where an unquoted password ends cannot be
# told. Rows keep the raw value beside the path.
def _resource_path(tool: str, mode: str, resource: str, policy: str) -> str:
    resource, policy = display_resource(resource), display_resource(policy)
    return f"({tool}) -{mode}-> ({resource}) -GOVERNED_BY-> ({policy}:DENY)"


def _tool_path(tool: str, policy: str) -> str:
    return f"({tool}) -GOVERNED_BY-> ({display_resource(policy)}:DENY)"


def _radius_path(
    agent: str | None, tool: str | None, mode: str | None, resource: str
) -> str | None:
    resource = display_resource(resource)
    if agent and tool:
        return f"({agent}) -CAN_CALL-> ({tool}) -{mode}-> ({resource})"
    if tool:
        return f"({tool}) -{mode}-> ({resource})"
    return None


# A tool reaches a DENY policy two ways: through a resource it reads or
# writes, or through a direct GOVERNED_BY edge. Each route names its pattern,
# the extra exception predicate, and the provenance columns it returns.
#
# An exception matches when ``exc IS NOT NULL``, never ``exc.reason IS NOT
# NULL``: ``reason`` is optional on ExpectedException, so testing it would
# misclassify a valid no-reason exception as a violation.
#
# Exceptions are COLLECTED, not row-multiplied: a wildcard (resource IS NULL)
# and a resource-specific exception can both match one deny path, and
# DISTINCT cannot collapse rows whose ``reason`` strings differ. One row per
# path; ``_picked_exception`` lets the most specific one supply the reason.
_DENY_ROUTES = (
    (
        "resource",
        "-[acc:READS|WRITES]->(r:Resource)-[gov:GOVERNED_BY]->(p:Policy {effect:'DENY'})",
        "(exc.resource IS NULL OR exc.resource = r.uri)",
        "type(acc) AS mode, r.uri AS resource, p.id AS policy, "
        "acc.source AS source, acc.confidence AS confidence, acc.evidence AS evidence, "
        "gov.source AS gov_source, gov.confidence AS gov_confidence, gov.evidence AS gov_evidence",
    ),
    (
        "tool",
        "-[g:GOVERNED_BY]->(p:Policy {effect:'DENY'})",
        "exc.resource IS NULL",
        "null AS mode, null AS resource, p.id AS policy, "
        "g.source AS source, g.confidence AS confidence, g.evidence AS evidence",
    ),
)


def _deny_paths(
    s: Session,
    *,
    keys: list[str] | None = None,
    agents: list[str] | None = None,
    agent: str | None = None,
):
    """Yield ``(via, record)`` for every deny path, one query per route.

    ``keys`` scopes to those tools, whether or not anyone may call them, and
    matches exceptions for ``agent`` (any agent when None). ``agents`` scopes
    to tools those agents hold a CAN_CALL grant on, matching each row's
    exceptions against its own agent; records then carry ``agent``,
    ``tool_key``, ``tool`` and ``server``.
    """
    if agents is not None:
        head = "(a:Agent)-[:CAN_CALL]->(t:Tool)"
        scope = "a.id IN $agents"
        exc_agent = "exc.agent = a.id"
        columns = "a.id AS agent, t.key AS tool_key, t.name AS tool, t.server AS server, "
        order = "agent, tool, resource, tool_key, policy, mode"
        params = {"agents": agents}
    else:
        head = "(t:Tool)"
        scope = "t.key IN $keys"
        exc_agent = "($agent IS NULL OR exc.agent = $agent)"
        columns = "t.key AS key, "
        order = "key, resource, policy, mode"
        params = {"keys": keys, "agent": agent}
    # The trailing sort keys only break ties the leading ones left to the
    # backend. That makes the order total because ingest MERGEs each edge,
    # so one deny path never yields two rows.
    for via, route, exc_resource, route_columns in _DENY_ROUTES:
        for r in s.run(
            f"""
            MATCH {head}{route}
            WHERE {scope}
            OPTIONAL MATCH (exc:ExpectedException {{policy:p.id}})
              WHERE exc.tool_key = t.key AND {exc_agent} AND {exc_resource}
            RETURN {columns}{route_columns},
                   collect(DISTINCT CASE WHEN exc IS NULL THEN NULL ELSE
                     {{resource: exc.resource, reason: exc.reason}} END) AS exceptions
            ORDER BY {order}
            """,
            **params,
        ):
            yield via, r


def _deny_fields(via: str, r, tool: str, *, classify: bool) -> dict:
    """Path, provenance and (when ``classify``) exception verdict of one path."""
    if via == "resource":
        path = _resource_path(tool, r["mode"], r["resource"], r["policy"])
    else:
        path = _tool_path(tool, r["policy"])
    d: dict = {"path": path, "provenance": row_provenance(r)}
    if via == "resource" and (policy_prov := _gov_prov(r)) is not None:
        d["policy_provenance"] = policy_prov
    if classify:
        exception = _picked_exception(r)
        d["classification"] = "authorized_but_governed" if exception else "violation"
        if exception and exception.get("reason") is not None:
            d["exception_reason"] = exception["reason"]
    return d


def _deny_evidence_batch(
    s: Session, keys: list[str], agent: str | None = None
) -> dict[str, list[dict]]:
    """All ways each tool in ``keys`` reaches a DENY policy, in two queries.

    Batched so the selector surface (ADR-0005) can evaluate N candidates
    without N round trips — per-candidate calls scale linearly, which is
    unacceptable on the online selection path.

    Each row carries:
    - ``provenance``: the load-bearing edge's provenance — the data-access edge
      for via='resource', the GOVERNED_BY edge for via='tool'.
    - ``policy_provenance`` (optional, via='resource' only): the GOVERNED_BY
      binding's provenance, attached only when an author cited evidence for
      the resource↔policy binding (otherwise omitted to keep output clean).
    - ``classification`` (when ``agent`` given): ``violation`` or
      ``authorized_but_governed`` based on a matching ExpectedException.
    """
    evidence: dict[str, list[dict]] = {k: [] for k in keys}
    for via, r in _deny_paths(s, keys=keys, agent=agent):
        key = r["key"]
        d: dict = {"via": via}
        if via == "resource":
            d["mode"] = r["mode"]
            d["resource"] = r["resource"]
        d["policy"] = r["policy"]
        d.update(_deny_fields(via, r, _name(key), classify=agent is not None))
        evidence[key].append(d)
    return evidence


def row_provenance(row) -> dict:
    """Provenance fields as a sub-dict — honest pass-through.

    No silent defaulting: a null ``source`` stays null so a reader can tell
    "no provenance recorded" from "operator said so". Every authored edge has
    these set by ingest; nulls only appear on edge types no query currently
    surfaces.
    """
    return {
        "source": row["source"],
        "confidence": row["confidence"],
        "evidence": row["evidence"],
    }


def _gov_prov(row) -> dict | None:
    """Policy-binding (GOVERNED_BY edge) provenance for resource-branch rows.

    Returns None for the default operator_asserted/high-with-no-evidence
    case — that's noise, not signal. Only attaches when an author cited a
    real source pointer for the (resource, policy) binding itself.
    """
    evidence = row["gov_evidence"]
    source = row["gov_source"]
    if evidence is None and (source is None or source == "operator_asserted"):
        return None
    return {
        "source": source,
        "confidence": row["gov_confidence"],
        "evidence": evidence,
    }


def _picked_exception(row) -> dict | None:
    """Prefer a resource-specific exception over a wildcard."""
    exceptions = [item for item in (row.get("exceptions") or []) if item]
    return next(
        (item for item in exceptions if item.get("resource") is not None),
        exceptions[0] if exceptions else None,
    )


def _name(key: str) -> str:
    return key.split("::", 1)[1] if "::" in key else key


def check_access(agent: str, tool: str) -> dict:
    """Can the agent call the tool, and does the call cross a DENY policy?

    verdict: AGENT_NOT_FOUND | TOOL_NOT_FOUND | AMBIGUOUS_TOOL | NOT_GRANTED | DENY | ALLOW.

    When verdict=DENY, the result includes ``all_authorized: bool`` — True when
    every deny_evidence row is ``authorized_but_governed`` (operator declared the
    reach intentional). Lets callers branch on "real violation" vs "expected
    DENY" without re-aggregating row classifications.
    """
    with read_session() as s:
        if not _agent_present(s, agent):
            # Three-state ``found``: True/False when tool resolution ran,
            # None when it didn't (AGENT_NOT_FOUND fires before tool lookup).
            # Hard-coding False would lie about a tool that actually exists;
            # README documents the contract so callers can use
            # ``result.get('found')`` instead of indexing.
            return {
                "agent": agent,
                "tool": tool,
                "agent_found": False,
                "found": None,
                "verdict": "AGENT_NOT_FOUND",
            }

        keys = resolve_tool_keys(s, tool)
        if not keys:
            return {
                "agent": agent,
                "tool": tool,
                "agent_found": True,
                "found": False,
                "verdict": "TOOL_NOT_FOUND",
            }
        if len(keys) > 1:
            return {
                "agent": agent,
                "tool": tool,
                "agent_found": True,
                "found": True,
                "verdict": "AMBIGUOUS_TOOL",
                "candidates": keys,
            }

        key = keys[0]
        meta = s.run(
            """
            MATCH (t:Tool {key:$key})
            RETURN t.name AS name, t.server AS server,
                   EXISTS { MATCH (:Agent {id:$agent})-[:CAN_CALL]->(t) } AS permitted
            """,
            key=key,
            agent=agent,
        ).single()
        if meta is None:  # tool deleted between resolve and this read
            return {
                "agent": agent,
                "tool": tool,
                "agent_found": True,
                "found": False,
                "verdict": "TOOL_NOT_FOUND",
            }
        deny = _deny_evidence_batch(s, [key], agent=agent)[key]

    if not meta["permitted"]:
        verdict = "NOT_GRANTED"
    elif deny:
        verdict = "DENY"
    else:
        verdict = "ALLOW"

    result = {
        "agent": agent,
        "tool": meta["name"],
        "tool_key": key,
        "server": meta["server"],
        "agent_found": True,
        "found": True,
        "permitted": meta["permitted"],
        "verdict": verdict,
        "deny_evidence": deny,
    }
    if verdict == "DENY":
        result["all_authorized"] = all(
            d.get("classification") == "authorized_but_governed" for d in deny
        )
    return result


def unsafe_callable_tools(agent: str) -> list[dict]:
    """Tools the agent can call that reach a DENY policy (via resource or directly).

    Each row carries:
    - ``provenance``: load-bearing edge — data-access for via='resource',
      GOVERNED_BY for via='tool'.
    - ``policy_provenance`` (via='resource', optional): GOVERNED_BY binding's
      provenance when the author cited a source for the resource↔policy link.
    - ``classification``: ``violation`` (no exception matches) or
      ``authorized_but_governed`` (operator declared this reach intentional via
      ``expected_exceptions`` in the manifest). Replaces the old bare 'unsafe'
      framing so operators don't have to mentally filter by-design grants.
    - ``exception_reason`` when classification is ``authorized_but_governed``.

    Returns ``[]`` for an unknown agent — pair with ``check_access`` (which has
    an ``AGENT_NOT_FOUND`` verdict) if you need to distinguish "no unsafe
    tools" from "agent doesn't exist".
    """
    with read_session() as s:
        rows = _unsafe_callable_rows(s, [agent])
    for d in rows:
        d.pop("agent", None)
    return rows


def _unsafe_callable_rows(s: Session, agents: list[str]) -> list[dict]:
    """Unsafe-callable rows for every agent in ``agents``, in two queries.

    Batched so ``audit_report`` costs a fixed number of queries instead of
    two per agent. Rows carry an ``agent`` key. Every resource-route row comes
    before every tool-route row; within each route rows are ordered by
    (agent, tool, resource), with ties broken by key, policy and mode.
    """
    rows: list[dict] = []
    for via, r in _deny_paths(s, agents=agents):
        d = {
            "agent": r["agent"],
            "tool_key": r["tool_key"],
            "tool": r["tool"],
            "server": r["server"],
            "via": via,
            "mode": r["mode"],
            "resource": r["resource"],
            "policy": r["policy"],
        }
        d.update(_deny_fields(via, r, r["tool"], classify=True))
        rows.append(d)
    return rows


def audit_report(include_grants: bool = False) -> dict:
    """Whole-graph operational audit summary.

    This is an aggregation layer over the existing advisory queries. It keeps
    their row shapes intact and adds counts/status so operators and CI can see
    the graph's current risk posture without stitching six commands together.
    """
    unsafe_violations: list[dict] = []
    authorized_but_governed: list[dict] = []
    agents = _agent_ids()
    # One batched pass instead of two whole-graph traversals per agent.
    # Rows regroup agent-major so each agent's rows keep the route order
    # ``unsafe_callable_tools`` returns for that agent alone.
    by_agent: dict[str, list[dict]] = {agent: [] for agent in agents}
    if agents:
        with read_session() as s:
            for row in _unsafe_callable_rows(s, agents):
                by_agent[row["agent"]].append(row)
    for agent in agents:
        for row in by_agent[agent]:
            if row.get("classification") == "violation":
                unsafe_violations.append(row)
            else:
                authorized_but_governed.append(row)

    findings = {
        "unsafe_violations": unsafe_violations,
        "authorized_but_governed": authorized_but_governed,
        "drifted_tools": drifted_tools(),
        "unmapped_tools": unmapped_tools(),
        "unbacked_edges": unbacked_edges(include_grants=include_grants),
        "orphan_policies": orphan_policies(),
        "destructive_unsafeguarded": destructive_unsafeguarded(),
        "annotation_contradictions": annotation_contradictions(),
    }
    counts = {name: len(rows) for name, rows in findings.items()}
    blocking = [name for name in ("unsafe_violations", "drifted_tools") if counts[name] > 0]
    if blocking:
        status = "fail"
    elif any(counts.values()):
        status = "warn"
    else:
        status = "pass"
    return {
        "status": status,
        "summary": {
            "agents": len(agents),
            "counts": counts,
            "blocking_findings": blocking,
            "include_grants": include_grants,
        },
        "findings": findings,
    }


# --- Negative-truth / drift queries -------------------------------------
#
# Audit views that surface what the graph DOESN'T say but probably should.
# An advisory analyzer that only answers positive reachability questions can
# look more authoritative than it is; these queries make the unspecified, the
# unsupported, and the stale parts of the graph visible.


def unmapped_tools(only_granted: bool = True) -> list[dict]:
    """Tools with no authored READS/WRITES — data-flow effects unclassified.

    Default scopes to tools an agent has a CAN_CALL grant on (those matter
    most for safety review). Pass ``only_granted=False`` to include all
    crawled tools with no data-access edges.
    """
    cypher = (
        """
        MATCH (t:Tool)
        WHERE NOT EXISTS { MATCH (t)-[:READS|WRITES]->() }
        """
        + ("  AND EXISTS { MATCH (:Agent)-[:CAN_CALL]->(t) }\n" if only_granted else "")
        + """
        OPTIONAL MATCH (a:Agent)-[:CAN_CALL]->(t)
        RETURN t.key AS tool_key, t.name AS tool, t.server AS server,
               collect(DISTINCT a.id) AS granted_to
        ORDER BY tool_key
        """
    )
    with read_session() as s:
        return [dict(r) for r in s.run(cypher)]


def orphan_policies() -> list[dict]:
    """Policies that no agent currently reaches — likely obsolete or over-narrow.

    A policy is an orphan when neither a resource it governs is read/written
    by a CAN_CALL-grantable tool, nor a tool it governs directly has any
    caller. Useful before pruning policies that look authoritative but in
    practice gate nothing.
    """
    cypher = """
    MATCH (p:Policy)
    WHERE NOT EXISTS {
        MATCH (:Agent)-[:CAN_CALL]->(:Tool)-[:READS|WRITES]->(:Resource)-[:GOVERNED_BY]->(p)
    }
      AND NOT EXISTS {
        MATCH (:Agent)-[:CAN_CALL]->(:Tool)-[:GOVERNED_BY]->(p)
    }
    OPTIONAL MATCH (r:Resource)-[:GOVERNED_BY]->(p)
    OPTIONAL MATCH (t:Tool)-[:GOVERNED_BY]->(p)
    RETURN p.id AS policy, p.effect AS effect, p.scope AS scope,
           collect(DISTINCT r.uri) AS resources_governed,
           collect(DISTINCT t.key) AS tools_governed_directly
    ORDER BY policy
    """
    with read_session() as s:
        return [dict(r) for r in s.run(cypher)]


def unbacked_edges(include_grants: bool = False) -> list[dict]:
    """Authored edges with no usable evidence pointer.

    These are unsupported claims — the gate-review fiction was exactly this
    shape. Surfacing them at audit time lets operators decide which assertions
    need source citation before being trusted. This includes both
    operator_asserted and inferred claims; either is unreviewable without a
    supporting pointer.

    ``CAN_CALL`` grants are EXCLUDED by default: a grant is intent declaration,
    not a factual claim about the world, so requiring evidence on every grant
    pollutes the output with un-fixable rows. Pass ``include_grants=True`` to
    audit grants too (useful when grants should reference a ticket / change
    request). The remaining edges (READS/WRITES/GOVERNED_BY) are claims about
    what the world does or which policy applies, and SHOULD carry evidence.
    """
    edge_types = (
        "CAN_CALL|READS|WRITES|GOVERNED_BY" if include_grants else "READS|WRITES|GOVERNED_BY"
    )
    cypher = f"""
    MATCH (src)-[r:{edge_types}]->(dst)
    WHERE r.evidence IS NULL OR trim(r.evidence) = ''
    WITH type(r) AS edge_type,
         labels(src)[0] AS src_label,
         coalesce(src.id, src.key, src.uri, src.name) AS src_id,
         labels(dst)[0] AS dst_label,
         coalesce(dst.id, dst.key, dst.uri, dst.name) AS dst_id,
         coalesce(r.source, 'operator_asserted') AS source,
         coalesce(r.confidence, 'high') AS confidence
    RETURN edge_type, src_label, src_id AS src, dst_label, dst_id AS dst,
           source, confidence
    ORDER BY edge_type, src_id, dst_id
    """
    with read_session() as s:
        return [dict(r) for r in s.run(cypher)]


def drifted_tools() -> list[dict]:
    """Tools with authored governance but no live EXPOSES edge.

    Happens when a server stops advertising a tool but the operator hasn't
    re-run ingest-manifest. Loader keeps these so a DENY never silently
    becomes ALLOW; this query lets operators find and resolve them.
    """
    cypher = """
    MATCH (t:Tool)
    WHERE NOT EXISTS { MATCH (:MCPServer)-[:EXPOSES]->(t) }
      AND EXISTS { MATCH (t)-[:CAN_CALL|READS|WRITES|GOVERNED_BY]-() }
    OPTIONAL MATCH (a:Agent)-[:CAN_CALL]->(t)
    OPTIONAL MATCH (t)-[acc:READS|WRITES]->(res:Resource)
    RETURN t.key AS tool_key, t.name AS tool, t.server AS server,
           collect(DISTINCT a.id) AS granted_to,
           collect(DISTINCT {mode:type(acc), resource:res.uri}) AS data_access
    ORDER BY tool_key
    """
    with read_session() as s:
        rows = []
        for row in s.run(cypher):
            item = dict(row)
            item["granted_to"] = [value for value in (item["granted_to"] or []) if value]
            item["data_access"] = [
                value
                for value in (item["data_access"] or [])
                if value and value.get("mode") is not None and value.get("resource") is not None
            ]
            rows.append(item)
        return rows


def _annotation_provenance(exposes_evidence: str | None) -> dict:
    """Provenance for a tool-annotation claim (ADR-0006).

    Annotations are the server's self-claims: crawled facts, but unverified —
    so ``confidence: medium``, never the ``high`` of EXPOSES/PROVIDES. The
    evidence pointer is the EXPOSES edge's (the annotation arrived on the same
    crawl pass); null when the tool has drifted off its server.
    """
    return {"source": "crawled", "confidence": "medium", "evidence": exposes_evidence}


def destructive_unsafeguarded() -> list[dict]:
    """Tools hinting ``destructiveHint: true`` with no GOVERNED_BY path at all —
    neither directly on the tool nor via any resource it reads/writes.

    This is an authoring priority queue, not a verdict: the server claims the
    tool is destructive, and no operator has bound a policy anywhere near it.
    Annotations never auto-create edges (ADR-0006) — each row cites the
    annotation's provenance and leaves the authoring decision to the operator.
    """
    cypher = """
    MATCH (t:Tool)
    WHERE t.destructive_hint = true
      AND NOT EXISTS { MATCH (t)-[:GOVERNED_BY]->(:Policy) }
      AND NOT EXISTS { MATCH (t)-[:READS|WRITES]->(:Resource)-[:GOVERNED_BY]->(:Policy) }
    OPTIONAL MATCH (:MCPServer)-[e:EXPOSES]->(t)
    OPTIONAL MATCH (a:Agent)-[:CAN_CALL]->(t)
    OPTIONAL MATCH (t)-[acc:READS|WRITES]->(res:Resource)
    RETURN t.key AS tool_key, t.name AS tool, t.server AS server,
           e.evidence AS exposes_evidence,
           collect(DISTINCT a.id) AS granted_to,
           collect(DISTINCT {mode:type(acc), resource:res.uri}) AS data_access
    ORDER BY tool_key
    """
    rows: list[dict] = []
    with read_session() as s:
        for r in s.run(cypher):
            d = dict(r)
            d["granted_to"] = [value for value in (d["granted_to"] or []) if value]
            d["data_access"] = [
                value
                for value in (d["data_access"] or [])
                if value and value.get("mode") is not None and value.get("resource") is not None
            ]
            d["annotation_claim"] = "destructiveHint: true"
            d["annotation_provenance"] = _annotation_provenance(r["exposes_evidence"])
            d.pop("exposes_evidence", None)
            rows.append(d)
    return rows


def annotation_contradictions() -> list[dict]:
    """Authored WRITES edges on tools hinting ``readOnlyHint: true``.

    One side is wrong — the server's self-claim or the operator's assertion —
    and the analyzer's job is to make the disagreement visible, not to pick a
    winner. Both sides cite their provenance so the operator can chase the
    stronger evidence (ADR-0006).
    """
    cypher = """
    MATCH (t:Tool)-[w:WRITES]->(res:Resource)
    WHERE t.read_only_hint = true
    OPTIONAL MATCH (:MCPServer)-[e:EXPOSES]->(t)
    RETURN t.key AS tool_key, t.name AS tool, t.server AS server,
           res.uri AS resource, e.evidence AS exposes_evidence,
           w.source AS source, w.confidence AS confidence, w.evidence AS evidence
    ORDER BY tool_key, resource
    """
    rows: list[dict] = []
    with read_session() as s:
        for r in s.run(cypher):
            rows.append(
                {
                    "tool_key": r["tool_key"],
                    "tool": r["tool"],
                    "server": r["server"],
                    "resource": r["resource"],
                    "annotation_claim": "readOnlyHint: true",
                    "annotation_provenance": _annotation_provenance(r["exposes_evidence"]),
                    "authored_claim": f"WRITES {r['resource']}",
                    "authored_provenance": row_provenance(r),
                }
            )
    return rows


def blast_radius(node: str) -> dict:
    """Who/what is affected if a resource or a policy changes (with evidence paths).

    Every impacted row has the same keys (via/resource/agent/tool_key/tool/mode/path);
    `path` is None when an affected tool has no caller. A tool that touches the
    resource but has no caller is still reported (it is affected).

    The response carries ``found`` — False when the node argument matches no
    Resource (for a URI) or Policy (otherwise). Without this, a typo'd URI is
    indistinguishable from a real resource that nothing touches; the analyzer's
    negative-truth promise depends on telling the two apart.
    """
    if is_resource_ref(node):
        uri = normalize_resource_uri(node)
        with read_session() as s:
            found = s.run("RETURN EXISTS { MATCH (:Resource {uri:$uri}) } AS ok", uri=uri).single()[
                "ok"
            ]
            if not found:
                # is_resource_ref is purely lexical: a policy id like 'mailto:foo'
                # or 'urn:bar' looks like a URI scheme. If no Resource matches,
                # try Policy id BEFORE declaring not-found, so URI-shaped policy
                # ids work as `blast-radius` arguments.
                policy_match = s.run(
                    "RETURN EXISTS { MATCH (:Policy {id:$node}) } AS ok", node=node
                ).single()["ok"]
                if policy_match:
                    return _blast_radius_policy(s, node)
                return {"kind": "resource", "node": uri, "found": False, "impacted": []}
            return _blast_radius_resource(s, uri)

    with read_session() as s:
        return _blast_radius_policy(s, node)


def _blast_radius_resource(s: Session, uri: str) -> dict:
    impacted = []
    for r in s.run(
        """
        MATCH (r:Resource {uri:$uri})
        OPTIONAL MATCH (t:Tool)-[acc:READS|WRITES]->(r)
        OPTIONAL MATCH (a:Agent)-[:CAN_CALL]->(t)
        RETURN DISTINCT a.id AS agent, t.key AS tool_key, t.name AS tool, type(acc) AS mode,
               acc.source AS source, acc.confidence AS confidence, acc.evidence AS evidence
        ORDER BY agent, tool
        """,
        uri=uri,
    ):
        if r["tool"] is None:
            continue  # resource exists but nothing reads/writes it
        d = dict(r)
        d["path"] = _radius_path(d["agent"], d["tool"], d["mode"], uri)
        d["provenance"] = row_provenance(r)
        for k in ("source", "confidence", "evidence"):
            d.pop(k, None)
        impacted.append(d)
    return {"kind": "resource", "node": uri, "found": True, "impacted": impacted}


def _blast_radius_policy(s: Session, node: str) -> dict:
    found = s.run("RETURN EXISTS { MATCH (:Policy {id:$node}) } AS ok", node=node).single()["ok"]
    if not found:
        return {"kind": "policy", "node": node, "found": False, "impacted": []}
    impacted = []
    # resources this policy governs, and who reaches them. Bind the tool to
    # the resource first, then OPTIONALLY the agent, so a tool that
    # reads/writes the resource with no CAN_CALL grant is still reported.
    for r in s.run(
        """
        MATCH (res:Resource)-[:GOVERNED_BY]->(:Policy {id:$node})
        OPTIONAL MATCH (t:Tool)-[acc:READS|WRITES]->(res)
        OPTIONAL MATCH (a:Agent)-[:CAN_CALL]->(t)
        RETURN 'resource' AS via, res.uri AS resource, a.id AS agent,
               t.key AS tool_key, t.name AS tool, type(acc) AS mode,
               acc.source AS source, acc.confidence AS confidence, acc.evidence AS evidence
        ORDER BY resource, agent, tool
        """,
        node=node,
    ):
        if r["tool"] is None:
            continue  # policy governs a resource, but no tool currently touches it
        d = dict(r)
        d["path"] = _radius_path(d["agent"], d["tool"], d["mode"], d["resource"])
        d["provenance"] = row_provenance(r)
        for k in ("source", "confidence", "evidence"):
            d.pop(k, None)
        impacted.append(d)
    # tools this policy governs directly, and who can call them
    for r in s.run(
        """
        MATCH (t:Tool)-[g:GOVERNED_BY]->(:Policy {id:$node})
        OPTIONAL MATCH (a:Agent)-[:CAN_CALL]->(t)
        RETURN 'tool' AS via, null AS resource, a.id AS agent,
               t.key AS tool_key, t.name AS tool, null AS mode,
               g.source AS source, g.confidence AS confidence, g.evidence AS evidence
        ORDER BY tool, agent
        """,
        node=node,
    ):
        d = dict(r)
        d["path"] = (
            f"({d['agent']}) -CAN_CALL-> ({d['tool']}) [GOVERNED_BY {display_resource(node)}]"
            if d["agent"]
            else None
        )
        d["provenance"] = row_provenance(r)
        for k in ("source", "confidence", "evidence"):
            d.pop(k, None)
        impacted.append(d)
    return {"kind": "policy", "node": node, "found": True, "impacted": impacted}
