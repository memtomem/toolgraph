"""Domain-level graph ports used by policy services.

No Cypher or backend result object crosses this boundary. The current runtime
implementation delegates to the established query/selector services; backend
adapters remain responsible for DDL, transactions, and row normalization.
"""

from __future__ import annotations

from typing import Protocol

from toolgraph.graph import queries, selector


class GraphReader(Protocol):
    def state(self) -> queries.GraphState: ...

    def exposed_tool_contracts(self) -> list[dict]: ...

    def eligible_tools(self, agent: str, candidates: list[str], profile: str) -> dict: ...

    def rank_features(self, agent: str, candidates: list[str]) -> dict: ...

    # Optional: adapters that can evaluate once and filter in memory expose
    # this combined form; build_policy_bundle prefers it and falls back to the
    # eligible_tools + rank_features pair for adapters that do not.
    def evaluate(self, agent: str, candidates: list[str], profile: str) -> dict: ...


class RuntimeGraphStore:
    """Backend-neutral policy read service over the configured graph adapter."""

    def state(self) -> queries.GraphState:
        return queries.graph_state()

    def exposed_tool_contracts(self) -> list[dict]:
        return queries.exposed_tool_contracts()

    def eligible_tools(self, agent: str, candidates: list[str], profile: str) -> dict:
        return selector.eligible_tools(agent, candidates, profile=profile)

    def rank_features(self, agent: str, candidates: list[str]) -> dict:
        return selector.rank_features(agent, candidates)

    def evaluate(self, agent: str, candidates: list[str], profile: str) -> dict:
        """One graph evaluation; the hard filter is pure over the ranked rows."""
        ranked = selector.rank_features(agent, candidates)
        if not ranked["agent_found"]:
            return {"agent_found": False, "features": [], "filtered": None}
        filtered = selector.filter_features(ranked, profile)
        return {
            "agent_found": True,
            "features": ranked["features"],
            "filtered": filtered,
        }
