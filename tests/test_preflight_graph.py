"""G2 preflight against a real Neo4j testcontainer."""

from __future__ import annotations

from datetime import datetime, timezone

from toolgraph.graph import loader
from toolgraph.manifest.ingest import ingest_governance
from toolgraph.models import AccessGrant, CrawlResult, Governance, ToolRecord
from toolgraph.preflight import build_preflight


NOW = datetime(2026, 7, 11, tzinfo=timezone.utc)


def _seed() -> None:
    loader.load_crawl_result(CrawlResult(
        server_name="alpha", transport="stdio",
        tools=[ToolRecord(name="read"), ToolRecord(name="loose")],
    ))
    report = ingest_governance(Governance(
        agents=["codex"],
        grants=[AccessGrant(agent="codex", tool="alpha::read")],
    ))
    assert report.applied


def test_real_graph_allow_warn_and_unknown_identity(graph):
    _seed()
    allow = build_preflight(
        agent="codex", candidates=["alpha::read"], profile="review",
        run_id="real-allow", created_at=NOW,
    )
    assert allow["decision"] == "advisory_allow"
    assert allow["eligible"] == ["alpha::read"]

    warn = build_preflight(
        agent="codex", candidates=["alpha::loose", "alpha::missing"], profile="review",
        run_id="real-warn", created_at=NOW,
    )
    assert warn["decision"] == "advisory_warn"
    assert [row["reason"] for row in warn["rejected"]] == [
        "NOT_GRANTED", "TOOL_NOT_FOUND",
    ]

    unknown = build_preflight(
        agent="ghost", candidates=["alpha::read"], profile="review",
        run_id="real-unknown", created_at=NOW,
    )
    assert unknown["decision"] == "unresolved_identity"
    assert unknown["eligible"] == unknown["rejected"] == []


def test_real_graph_same_generation_is_deterministic_except_created_at(graph):
    _seed()
    kwargs = dict(
        agent="codex", candidates=["alpha::read", "alpha::loose"], profile="review",
        run_id="stable", created_at=NOW,
    )
    assert build_preflight(**kwargs) == build_preflight(**kwargs)
