"""Portable, product-neutral policy bundle compiler.

The bundle is a control-plane artifact.  It contains deterministic policy
decisions and crawl contract fingerprints, but no proxy/runtime behavior.
memtomem-stm is the first reference consumer; independent gateways can consume
the same schema without importing Toolgraph Python code.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from toolgraph.artifacts import canonical_json_bytes, sha256_bytes
from toolgraph.graph import queries, selector
from toolgraph.graph.store import GraphReader, RuntimeGraphStore
from toolgraph.preflight import redact

SCHEMA_VERSION = 1
KIND = "toolgraph.policy-bundle"


class PolicyBundleError(RuntimeError):
    """The graph cannot produce a trustworthy policy bundle."""


def tool_contract_digest(contract: dict[str, Any]) -> str:
    fields = {
        key: contract.get(key)
        for key in (
            "server",
            "name",
            "description",
            "input_schema",
            "read_only_hint",
            "destructive_hint",
            "idempotent_hint",
            "open_world_hint",
        )
    }
    return sha256_bytes(canonical_json_bytes(fields))


def _catalog_digest(contracts: list[dict[str, Any]]) -> str:
    rows = [
        {
            "tool_key": row["tool_key"],
            "tool_contract_digest": tool_contract_digest(row),
        }
        for row in contracts
    ]
    return sha256_bytes(canonical_json_bytes(rows))


def build_policy_bundle(
    *,
    agent: str,
    profile: str = selector.DEFAULT_PROFILE,
    created_at: datetime | None = None,
    store: GraphReader | None = None,
) -> dict[str, Any]:
    """Compile every currently exposed qualified tool under one policy profile."""
    if not agent.strip():
        raise PolicyBundleError("agent must not be empty")
    if profile not in selector.PROFILES:
        raise PolicyBundleError(
            f"unknown profile {profile!r} — expected one of {sorted(selector.PROFILES)}"
        )

    reader = store or RuntimeGraphStore()

    def fetch() -> dict[str, Any]:
        state = reader.state()
        contracts = reader.exposed_tool_contracts()
        candidates = [row["tool_key"] for row in contracts]
        filtered = reader.eligible_tools(agent, candidates, profile)
        if not filtered["agent_found"]:
            raise PolicyBundleError(f"agent {agent!r} not found in the graph")
        ranked = reader.rank_features(agent, candidates)
        return {
            "governance_digest": state.governance_digest,
            "contracts": contracts,
            "filtered": filtered,
            "features": ranked["features"],
        }

    compiled = queries.with_graph_state(fetch, strict=True)
    graph_state = compiled["graph_state"]
    if not isinstance(graph_state.get("instance_id"), str) or not graph_state["instance_id"]:
        raise PolicyBundleError(
            "graph has no instance id — run init-schema before compiling"
        )
    governance_digest = compiled["governance_digest"]
    if not governance_digest:
        raise PolicyBundleError(
            "graph has no governance digest — re-run ingest-manifest before compiling"
        )

    rejected = {
        row.get("tool_key") or row["candidate"]: row
        for row in compiled["filtered"]["rejected"]
    }
    features = {
        row.get("tool_key") or row["candidate"]: row
        for row in compiled["features"]
    }
    tools: list[dict[str, Any]] = []
    for contract in compiled["contracts"]:
        key = contract["tool_key"]
        rejection = rejected.get(key)
        item: dict[str, Any] = {
            "tool_key": key,
            "tool_contract_digest": tool_contract_digest(contract),
            "decision": "rejected" if rejection else "eligible",
            "risk_score": features[key]["risk_score"],
        }
        if rejection:
            item["reason"] = rejection["reason"]
            if rejection.get("paths"):
                item["paths"] = rejection["paths"]
        tools.append(item)

    timestamp = created_at or datetime.now(timezone.utc)
    return redact(
        {
            "schema_version": SCHEMA_VERSION,
            "kind": KIND,
            "created_at": timestamp.isoformat(),
            "graph_state": compiled["graph_state"],
            "agent": agent,
            "agent_found": True,
            "profile": profile,
            "governance_digest": governance_digest,
            "catalog_digest": _catalog_digest(compiled["contracts"]),
            "tools": tools,
        }
    )
