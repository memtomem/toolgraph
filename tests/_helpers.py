"""Helpers shared by more than one test module."""

from __future__ import annotations

import json
from importlib.metadata import version
from pathlib import Path

from toolgraph.graph import driver, loader, queries
from toolgraph.manifest.ingest import ingest_governance
from toolgraph.models import (
    AccessGrant,
    CrawlResult,
    DataAccess,
    Governance,
    Policy,
    ResourceRecord,
    ToolRecord,
)

ROOT = Path(__file__).resolve().parent.parent


CSV = "file:///data/customers.csv"


CONTROL_PLAN_FIXTURE = ROOT / "contracts" / "fixtures" / "control-plan-v1.json"


# click >= 8.2 always splits streams (result.stdout / result.stderr);
# older click mixes stderr into stdout by default, so purity can't be
# asserted there.
CLICK_SPLITS_STREAMS = tuple(int(p) for p in version("click").split(".")[:2]) >= (8, 2)


def seed_basic(graph) -> None:
    loader.load_crawl_result(
        CrawlResult(
            server_name="sample",
            transport="stdio",
            tools=[ToolRecord(name="read_file"), ToolRecord(name="write_file")],
            resources=[ResourceRecord(uri=CSV)],
        )
    )
    ingest_governance(
        Governance(
            agents=["planner"],
            policies=[Policy(id="pii-deny", effect="DENY")],
            grants=[AccessGrant(agent="planner", tool="sample::read_file")],
            data_access=[DataAccess(tool="sample::read_file", resource=CSV, mode="READS")],
            governed_by={CSV: ["pii-deny"]},
        )
    )


def load_crawl(server: str, tool_names: list[str], resources: list[str] | None = None) -> None:
    loader.load_crawl_result(
        CrawlResult(
            server_name=server,
            transport="stdio",
            tools=[ToolRecord(name=n) for n in tool_names],
            resources=[ResourceRecord(uri=u) for u in (resources or [])],
        )
    )


def crawl_sample(graph):
    loader.load_crawl_result(
        CrawlResult(
            server_name="sample",
            transport="stdio",
            tools=[ToolRecord(name="read_file")],
            resources=[ResourceRecord(uri=CSV)],
        )
    )


class FakeBundleReader:
    def __init__(self, size):
        self.keys = [f"s::t{i:05}" for i in range(size)]
        self.calls = []
        self.generation = 1

    def state(self):
        return queries.GraphState("injected", self.generation, "a" * 64)

    def exposed_tool_contracts(self):
        return [
            {"tool_key": k, "server": "s", "name": k.split("::")[1]} for k in reversed(self.keys)
        ]

    def evaluate(self, agent, candidates, profile):
        self.calls.append(list(candidates))
        return {
            "agent_found": True,
            "filtered": {"eligible": candidates, "rejected": []},
            "features": [{"tool_key": k, "risk_score": 0} for k in candidates],
        }


def control_plan_document() -> dict:
    return json.loads(CONTROL_PLAN_FIXTURE.read_text(encoding="utf-8"))


def control_plan_raw(document: dict | None = None) -> bytes:
    value = control_plan_document() if document is None else document
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def control_plan_bracket(monkeypatch, *, rejected: bool = False) -> None:
    calls = []

    def eligible(principal, candidates, *, profile):
        calls.append((principal, candidates, profile))
        rejected_rows = []
        eligible_rows = list(candidates)
        if rejected and principal == "claude":
            eligible_rows = candidates[:-1]
            rejected_rows = [
                {
                    "candidate": candidates[-1],
                    "tool_key": candidates[-1],
                    "reason": "DENY_VIOLATION",
                    "paths": [],
                }
            ]
        return {
            "agent": principal,
            "agent_found": True,
            "profile": profile,
            "eligible": eligible_rows,
            "rejected": rejected_rows,
        }

    def bracket(fetch, *, strict):
        assert strict is True
        return {
            **fetch(),
            "graph_state": {"instance_id": "test-instance", "generation": 42},
        }

    class MockSession:
        def run(self, *args, **kwargs):
            return []

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    monkeypatch.setattr("toolgraph.control_plan.selector.eligible_tools", eligible)
    monkeypatch.setattr("toolgraph.control_plan.queries.with_graph_state", bracket)
    monkeypatch.setattr("toolgraph.control_plan.driver.read_session", lambda: MockSession())
    monkeypatch.setattr("toolgraph.control_plan._test_calls", calls, raising=False)


def accepted_candidate_list() -> dict:
    return {
        "schema_version": 1,
        "kind": "toolgraph.review-candidate-list",
        "source_report_digest": f"sha256:{'a' * 64}",
        "candidates": [
            {
                "candidate_id": "11111111-1111-5111-8111-111111111111",
                "run_id": "run-1",
                "pattern_id": "repeated-failure",
                "pattern_version": 1,
                "tool_key": "github::merge",
                "artifact_digest": f"sha256:{'b' * 64}",
                "disposition": "accepted",
                "history": [],
            }
        ],
    }


def mock_policy_review_inputs(monkeypatch, *, accepted: bool = True) -> None:
    payload = accepted_candidate_list()
    if not accepted:
        payload["candidates"] = []
    monkeypatch.setattr("toolgraph.policy_review.list_candidates", lambda *a, **k: payload)
    monkeypatch.setattr(
        "toolgraph.policy_review.selector.eligible_tools",
        lambda *a, **k: {
            "agent": "planner",
            "agent_found": True,
            "profile": "strict",
            "eligible": [],
            "rejected": [
                {
                    "candidate": "github::merge",
                    "tool_key": "github::merge",
                    "reason": "DENY_VIOLATION",
                    "paths": ["(planner)-[:CAN_CALL]->(github::merge)-[:WRITES]->(prod)"],
                }
            ],
        },
    )

    def with_state(fn, *, strict):
        assert strict is True
        value = fn()
        value["graph_state"] = {"instance_id": "graph-1", "generation": 9}
        return value

    monkeypatch.setattr("toolgraph.policy_review.queries.with_graph_state", with_state)


def _ladybug_id(value: dict) -> str:
    return json.dumps(value, sort_keys=True)


def _ladybug_properties(entity: dict) -> dict:
    # Ladybug returns every column of the table, unset ones as None, plus its
    # bookkeeping keys; Neo4j's properties() has neither, so it is left as is.
    bookkeeping = {"_ID", "_LABEL", "_SRC", "_DST"}
    return {k: v for k, v in entity.items() if v is not None and k not in bookkeeping}


def graph_content() -> list[str]:
    """Every node and relationship, by backend identity, with all its properties.

    Comparing this before and after an operation on the same database proves
    the operation wrote nothing: a changed property, label or endpoint, and a
    node or relationship deleted and recreated identically, all show up, where
    the generation counter only moves on writes that bump it.
    """
    with driver.session() as session:
        if driver.backend_name() == "ladybug":
            nodes = [
                (_ladybug_id(row["n"]["_ID"]), [row["label"]], _ladybug_properties(row["n"]))
                for row in session.run("MATCH (n) RETURN label(n) AS label, n AS n")
            ]
            edges = []
            for row in session.run("MATCH ()-[r]->() RETURN label(r) AS type, r AS r"):
                r = row["r"]
                edges.append(
                    (
                        _ladybug_id(r["_ID"]),
                        row["type"],
                        _ladybug_id(r["_SRC"]),
                        _ladybug_id(r["_DST"]),
                        _ladybug_properties(r),
                    )
                )
        else:
            nodes = [
                (row["id"], sorted(row["labels"]), row["props"])
                for row in session.run(
                    "MATCH (n) RETURN elementId(n) AS id, labels(n) AS labels, properties(n) AS "
                    "props"
                )
            ]
            edges = [
                (row["id"], row["type"], row["a"], row["b"], row["props"])
                for row in session.run(
                    "MATCH (a)-[r]->(b) RETURN elementId(r) AS id, type(r) AS type, "
                    "properties(r) AS props, elementId(a) AS a, elementId(b) AS b"
                )
            ]
    return sorted(
        [json.dumps(["node", *node], sort_keys=True, default=str) for node in nodes]
        + [json.dumps(["edge", *edge], sort_keys=True, default=str) for edge in edges]
    )
