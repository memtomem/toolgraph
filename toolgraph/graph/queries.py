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

from neo4j import Session

from toolgraph.graph.driver import session
from toolgraph.uris import is_resource_ref, normalize_resource_uri


def resolve_tool_keys(s: Session, ref: str) -> list[str]:
    """All Tool keys matching a ref (exact key, or bare name across servers)."""
    rows = s.run(
        "MATCH (t:Tool) WHERE t.key = $ref OR (NOT $ref CONTAINS '::' AND t.name = $ref) "
        "RETURN t.key AS key ORDER BY key",
        ref=ref,
    )
    return [r["key"] for r in rows]


def agent_exists(agent: str) -> bool:
    """Is there an Agent node with this id? Used by the CLI + MCP surfaces to
    return a discriminated AGENT_NOT_FOUND signal instead of silently
    returning ``[]`` (which is indistinguishable from 'no unsafe tools')."""
    with session() as s:
        return s.run(
            "RETURN EXISTS { MATCH (:Agent {id:$agent}) } AS ok", agent=agent
        ).single()["ok"]


def _resource_path(tool: str, mode: str, resource: str, policy: str) -> str:
    return f"({tool}) -{mode}-> ({resource}) -GOVERNED_BY-> ({policy}:DENY)"


def _tool_path(tool: str, policy: str) -> str:
    return f"({tool}) -GOVERNED_BY-> ({policy}:DENY)"


def _radius_path(agent: str | None, tool: str | None, mode: str | None, resource: str) -> str | None:
    if agent and tool:
        return f"({agent}) -CAN_CALL-> ({tool}) -{mode}-> ({resource})"
    if tool:
        return f"({tool}) -{mode}-> ({resource})"
    return None


def _deny_evidence_for(s: Session, key: str, agent: str | None = None) -> list[dict]:
    """All ways the tool identified by `key` reaches a DENY policy.

    Each row carries:
    - ``provenance``: the load-bearing edge's provenance — the data-access edge
      for via='resource', the GOVERNED_BY edge for via='tool'.
    - ``policy_provenance`` (optional, via='resource' only): the GOVERNED_BY
      binding's provenance, attached only when an author cited evidence for
      the resource↔policy binding (otherwise omitted to keep output clean).
    - ``classification`` (when ``agent`` given): ``violation`` or
      ``authorized_but_governed`` based on a matching ExpectedException.
    """
    evidence: list[dict] = []
    # Codex PR #1 review: detect exception match via `exc IS NOT NULL`, NOT via
    # `exc.reason IS NOT NULL`. ``reason`` is optional on ExpectedException;
    # using it as the match signal would misclassify a valid no-reason
    # exception as a violation.
    for r in s.run(
        """
        MATCH (t:Tool {key:$key})-[acc:READS|WRITES]->(r:Resource)-[gov:GOVERNED_BY]->(p:Policy {effect:'DENY'})
        OPTIONAL MATCH (exc:ExpectedException {tool_key:$key, policy:p.id})
          WHERE ($agent IS NULL OR exc.agent = $agent)
            AND (exc.resource IS NULL OR exc.resource = r.uri)
        RETURN DISTINCT type(acc) AS mode, r.uri AS resource, p.id AS policy,
               acc.source AS source, acc.confidence AS confidence, acc.evidence AS evidence,
               gov.source AS gov_source, gov.confidence AS gov_confidence, gov.evidence AS gov_evidence,
               exc IS NOT NULL AS has_exception, exc.reason AS exception_reason
        ORDER BY resource, policy
        """,
        key=key,
        agent=agent,
    ):
        d = {
            "via": "resource",
            "mode": r["mode"],
            "resource": r["resource"],
            "policy": r["policy"],
            "path": _resource_path(_name(key), r["mode"], r["resource"], r["policy"]),
            "provenance": _prov(r),
        }
        policy_prov = _gov_prov(r)
        if policy_prov is not None:
            d["policy_provenance"] = policy_prov
        if agent is not None:
            d["classification"] = (
                "authorized_but_governed" if r["has_exception"] else "violation"
            )
            if r["exception_reason"] is not None:
                d["exception_reason"] = r["exception_reason"]
        evidence.append(d)
    for r in s.run(
        """
        MATCH (t:Tool {key:$key})-[g:GOVERNED_BY]->(p:Policy {effect:'DENY'})
        OPTIONAL MATCH (exc:ExpectedException {tool_key:$key, policy:p.id})
          WHERE ($agent IS NULL OR exc.agent = $agent) AND exc.resource IS NULL
        RETURN DISTINCT p.id AS policy,
               g.source AS source, g.confidence AS confidence, g.evidence AS evidence,
               exc IS NOT NULL AS has_exception, exc.reason AS exception_reason
        ORDER BY policy
        """,
        key=key,
        agent=agent,
    ):
        d = {
            "via": "tool",
            "policy": r["policy"],
            "path": _tool_path(_name(key), r["policy"]),
            "provenance": _prov(r),
        }
        if agent is not None:
            d["classification"] = (
                "authorized_but_governed" if r["has_exception"] else "violation"
            )
            if r["exception_reason"] is not None:
                d["exception_reason"] = r["exception_reason"]
        evidence.append(d)
    return evidence


def _prov(row) -> dict:
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
    with session() as s:
        agent_present = s.run(
            "RETURN EXISTS { MATCH (:Agent {id:$agent}) } AS ok", agent=agent
        ).single()["ok"]
        if not agent_present:
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
        deny = _deny_evidence_for(s, key, agent=agent)

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
    rows: list[dict] = []
    with session() as s:
        for r in s.run(
            """
            MATCH (:Agent {id:$agent})-[:CAN_CALL]->(t:Tool)
                  -[acc:READS|WRITES]->(res:Resource)-[gov:GOVERNED_BY]->(p:Policy {effect:'DENY'})
            OPTIONAL MATCH (exc:ExpectedException {agent:$agent, tool_key:t.key, policy:p.id})
              WHERE exc.resource IS NULL OR exc.resource = res.uri
            RETURN DISTINCT t.key AS tool_key, t.name AS tool, t.server AS server,
                   'resource' AS via, type(acc) AS mode, res.uri AS resource, p.id AS policy,
                   acc.source AS source, acc.confidence AS confidence, acc.evidence AS evidence,
                   gov.source AS gov_source, gov.confidence AS gov_confidence, gov.evidence AS gov_evidence,
                   exc IS NOT NULL AS has_exception, exc.reason AS exception_reason
            ORDER BY tool, resource
            """,
            agent=agent,
        ):
            d = dict(r)
            d["path"] = _resource_path(d["tool"], d["mode"], d["resource"], d["policy"])
            d["provenance"] = _prov(r)
            policy_prov = _gov_prov(r)
            if policy_prov is not None:
                d["policy_provenance"] = policy_prov
            d["classification"] = (
                "authorized_but_governed" if r["has_exception"] else "violation"
            )
            for k in ("source", "confidence", "evidence",
                      "gov_source", "gov_confidence", "gov_evidence",
                      "has_exception"):
                d.pop(k, None)
            if d.get("exception_reason") is None:
                d.pop("exception_reason", None)
            rows.append(d)
        for r in s.run(
            """
            MATCH (:Agent {id:$agent})-[:CAN_CALL]->(t:Tool)-[g:GOVERNED_BY]->(p:Policy {effect:'DENY'})
            OPTIONAL MATCH (exc:ExpectedException {agent:$agent, tool_key:t.key, policy:p.id})
              WHERE exc.resource IS NULL
            RETURN DISTINCT t.key AS tool_key, t.name AS tool, t.server AS server,
                   'tool' AS via, null AS mode, null AS resource, p.id AS policy,
                   g.source AS source, g.confidence AS confidence, g.evidence AS evidence,
                   exc IS NOT NULL AS has_exception, exc.reason AS exception_reason
            ORDER BY tool
            """,
            agent=agent,
        ):
            d = dict(r)
            d["path"] = _tool_path(d["tool"], d["policy"])
            d["provenance"] = _prov(r)
            d["classification"] = (
                "authorized_but_governed" if r["has_exception"] else "violation"
            )
            for k in ("source", "confidence", "evidence", "has_exception"):
                d.pop(k, None)
            if d.get("exception_reason") is None:
                d.pop("exception_reason", None)
            rows.append(d)
    return rows


# --- Negative-truth / drift queries -------------------------------------
#
# Audit views that surface what the graph DOESN'T say but probably should.
# Codex review of the gate exhibit noted that an advisory analyzer that only
# answers positive reachability questions can look more authoritative than it
# is; these queries make the unspecified, the unsupported, and the stale
# parts of the graph visible.


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
        + (
            "  AND EXISTS { MATCH (:Agent)-[:CAN_CALL]->(t) }\n"
            if only_granted
            else ""
        )
        + """
        OPTIONAL MATCH (a:Agent)-[:CAN_CALL]->(t)
        RETURN t.key AS tool_key, t.name AS tool, t.server AS server,
               collect(DISTINCT a.id) AS granted_to
        ORDER BY tool_key
        """
    )
    with session() as s:
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
    with session() as s:
        return [dict(r) for r in s.run(cypher)]


def unbacked_edges(include_grants: bool = False) -> list[dict]:
    """Authored edges with source='operator_asserted' but no evidence pointer.

    These are unsupported claims — the gate-review fiction was exactly this
    shape. Surfacing them at audit time lets operators decide which assertions
    need source citation before being trusted.

    ``CAN_CALL`` grants are EXCLUDED by default: a grant is intent declaration,
    not a factual claim about the world, so requiring evidence on every grant
    pollutes the output with un-fixable rows. Pass ``include_grants=True`` to
    audit grants too (useful when grants should reference a ticket / change
    request). The remaining edges (READS/WRITES/GOVERNED_BY) are claims about
    what the world does or which policy applies, and SHOULD carry evidence.
    """
    edge_types = (
        "CAN_CALL|READS|WRITES|GOVERNED_BY" if include_grants
        else "READS|WRITES|GOVERNED_BY"
    )
    cypher = f"""
    MATCH (src)-[r:{edge_types}]->(dst)
    WHERE coalesce(r.source, 'operator_asserted') = 'operator_asserted'
      AND r.evidence IS NULL
    RETURN type(r) AS edge_type,
           labels(src)[0] AS src_label,
           coalesce(src.id, src.key, src.uri, src.name) AS src,
           labels(dst)[0] AS dst_label,
           coalesce(dst.id, dst.key, dst.uri, dst.name) AS dst,
           coalesce(r.source, 'operator_asserted') AS source,
           coalesce(r.confidence, 'high') AS confidence
    ORDER BY edge_type, src, dst
    """
    with session() as s:
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
           [x IN collect(DISTINCT {mode:type(acc), resource:res.uri})
            WHERE x.mode IS NOT NULL AND x.resource IS NOT NULL] AS data_access
    ORDER BY tool_key
    """
    with session() as s:
        return [dict(r) for r in s.run(cypher)]


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
        with session() as s:
            found = s.run(
                "RETURN EXISTS { MATCH (:Resource {uri:$uri}) } AS ok", uri=uri
            ).single()["ok"]
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

    with session() as s:
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
        d["provenance"] = _prov(r)
        for k in ("source", "confidence", "evidence"):
            d.pop(k, None)
        impacted.append(d)
    return {"kind": "resource", "node": uri, "found": True, "impacted": impacted}


def _blast_radius_policy(s: Session, node: str) -> dict:
    found = s.run(
        "RETURN EXISTS { MATCH (:Policy {id:$node}) } AS ok", node=node
    ).single()["ok"]
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
        d["provenance"] = _prov(r)
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
            f"({d['agent']}) -CAN_CALL-> ({d['tool']}) [GOVERNED_BY {node}]"
            if d["agent"]
            else None
        )
        d["provenance"] = _prov(r)
        for k in ("source", "confidence", "evidence"):
            d.pop(k, None)
        impacted.append(d)
    return {"kind": "policy", "node": node, "found": True, "impacted": impacted}
