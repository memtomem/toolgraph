"""Portable, product-neutral policy bundle compiler.

The bundle is a control-plane artifact.  It contains deterministic policy
decisions and crawl contract fingerprints, but no proxy/runtime behavior.
memtomem-stm is the first reference consumer; independent gateways can consume
the same schema without importing Toolgraph Python code.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from typing import Any

from toolgraph.artifacts import (
    canonical_json_bytes,
    rfc3339_offset_error,
    sha256_bytes,
)
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
    if created_at is not None:
        problem = rfc3339_offset_error(created_at)
        if problem:
            raise PolicyBundleError(problem)

    reader = store or RuntimeGraphStore()

    def fetch() -> dict[str, Any]:
        state = reader.state()
        contracts = reader.exposed_tool_contracts()
        # Consumers key decisions by tool_key and may reject the whole bundle
        # on a duplicate (the schema's tools description makes this normative),
        # so failing compilation here is strictly better than shipping one.
        duplicates = sorted(
            key
            for key, count in Counter(row["tool_key"] for row in contracts).items()
            if count > 1
        )
        if duplicates:
            raise PolicyBundleError(
                f"duplicate tool_key rows in exposed catalog: {', '.join(duplicates)}"
                " — repair the graph (e.g. multiple EXPOSES edges per tool) before"
                " compiling"
            )
        candidates = [row["tool_key"] for row in contracts]
        # One graph evaluation for the whole catalog when the adapter supports
        # it: the hard filter is a pure function over the ranked features
        # (ADR-0005), so compiling must not run the full selector pass twice
        # per bracket attempt. Adapters without ``evaluate`` keep the original
        # two-call contract — their rank rows never promised selector-internal
        # fields, so we must not filter them ourselves.
        evaluate = getattr(reader, "evaluate", None)
        if evaluate is not None:
            evaluated = evaluate(agent, candidates, profile)
            if not evaluated["agent_found"]:
                raise PolicyBundleError(f"agent {agent!r} not found in the graph")
            filtered = evaluated["filtered"]
            features = evaluated["features"]
        else:
            filtered = reader.eligible_tools(agent, candidates, profile)
            if not filtered["agent_found"]:
                raise PolicyBundleError(f"agent {agent!r} not found in the graph")
            features = reader.rank_features(agent, candidates)["features"]
        return {
            "governance_digest": state.governance_digest,
            "contracts": contracts,
            "filtered": filtered,
            "features": features,
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
        feature = features.get(key)
        if feature is None:
            # Both passes ran inside one graph-state bracket, so every catalog
            # key must have a feature row; a miss means the selector contract
            # broke — fail as a typed compile error, not a KeyError traceback.
            raise PolicyBundleError(
                f"selector returned no feature row for catalog tool {key!r}"
            )
        item: dict[str, Any] = {
            "tool_key": key,
            "tool_contract_digest": tool_contract_digest(contract),
            "decision": "rejected" if rejection else "eligible",
            "risk_score": feature["risk_score"],
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
