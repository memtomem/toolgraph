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
