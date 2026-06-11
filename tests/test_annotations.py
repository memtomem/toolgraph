"""ADR-0006: tool annotations crawl as self-claims (crawled/medium), reconcile
on re-crawl, and feed the two annotation-vs-authored-truth audit queries."""

from __future__ import annotations

import sys
from pathlib import Path

from toolgraph.crawler.client import crawl_server
from toolgraph.graph import driver, loader, queries
from toolgraph.manifest.ingest import ingest_governance
from toolgraph.models import (
    AccessGrant,
    CrawlResult,
    DataAccess,
    Governance,
    Policy,
    ResourceRecord,
    ServerSpec,
    ToolRecord,
)

FIXTURE = str(Path(__file__).parent / "fixtures" / "sample_server.py")
CSV = "file:///data/customers.csv"


# --- crawl: annotations surface on ToolRecord -----------------------------


async def test_crawl_extracts_tool_annotations():
    spec = ServerSpec(name="sample", transport="stdio", command=sys.executable, args=[FIXTURE])
    result = await crawl_server(spec)

    read_tool = next(t for t in result.tools if t.name == "read_file")
    assert read_tool.read_only_hint is True
    assert read_tool.idempotent_hint is True
    assert read_tool.destructive_hint is None  # not claimed — stays None, not False

    write_tool = next(t for t in result.tools if t.name == "write_file")
    assert write_tool.destructive_hint is True
    assert write_tool.read_only_hint is None


# --- load + reconcile: hints land as node properties and clear when dropped


def _tool_props(key: str) -> dict:
    with driver.session() as s:
        rec = s.run("MATCH (t:Tool {key:$key}) RETURN t", key=key).single()
        return dict(rec["t"]) if rec else {}


def test_loader_stores_and_clears_annotations(graph):
    loader.load_crawl_result(
        CrawlResult(
            server_name="sample",
            transport="stdio",
            tools=[ToolRecord(name="write_file", destructive_hint=True)],
        )
    )
    assert _tool_props("sample::write_file")["destructive_hint"] is True

    # Server stops sending the annotation: same crawl pass clears the property
    # (ADR-0006 — same lifecycle as description).
    loader.load_crawl_result(
        CrawlResult(
            server_name="sample",
            transport="stdio",
            tools=[ToolRecord(name="write_file")],
        )
    )
    assert "destructive_hint" not in _tool_props("sample::write_file")


def test_drifted_tool_makes_no_annotation_claims(graph):
    """A governed tool the server stopped exposing is KEPT (a DENY must not
    silently become ALLOW) — but the server no longer claims anything about
    it, so its annotation hints must clear with the EXPOSES edge. Without
    this, destructive_unsafeguarded keeps citing a crawled/medium claim
    nobody is making (Codex review of this branch)."""
    loader.load_crawl_result(
        CrawlResult(
            server_name="sample",
            transport="stdio",
            tools=[ToolRecord(name="write_file", destructive_hint=True)],
        )
    )
    report = ingest_governance(
        Governance(
            agents=["support-bot"],
            grants=[AccessGrant(agent="support-bot", tool="sample::write_file")],
        )
    )
    assert report.applied, report.warnings
    assert queries.destructive_unsafeguarded() != []

    # Server drops the tool; the grant keeps the node alive — drifted.
    loader.load_crawl_result(CrawlResult(server_name="sample", transport="stdio"))
    props = _tool_props("sample::write_file")
    assert props, "governed tool must survive the re-crawl"
    assert "destructive_hint" not in props
    assert queries.destructive_unsafeguarded() == []


# --- destructive-unsafeguarded --------------------------------------------


def _seed_destructive(governed_by: dict | None = None, data_access: list | None = None):
    loader.load_crawl_result(
        CrawlResult(
            server_name="sample",
            transport="stdio",
            endpoint="python sample_server.py",
            tools=[
                ToolRecord(name="write_file", destructive_hint=True),
                ToolRecord(name="read_file", read_only_hint=True),
            ],
            resources=[ResourceRecord(uri=CSV)],
        )
    )
    report = ingest_governance(
        Governance(
            agents=["support-bot"],
            policies=[Policy(id="pii-deny", effect="DENY")],
            grants=[AccessGrant(agent="support-bot", tool="sample::write_file")],
            data_access=data_access or [],
            governed_by=governed_by or {},
        )
    )
    assert report.applied, report.warnings


def test_destructive_unsafeguarded_surfaces_ungoverned_tool(graph):
    _seed_destructive()
    rows = queries.destructive_unsafeguarded()
    assert [r["tool_key"] for r in rows] == ["sample::write_file"]
    row = rows[0]
    assert row["granted_to"] == ["support-bot"]
    assert row["annotation_claim"] == "destructiveHint: true"
    # Self-claim, not a protocol fact: crawled/medium, evidence = crawl pass.
    assert row["annotation_provenance"] == {
        "source": "crawled",
        "confidence": "medium",
        "evidence": "crawled from python sample_server.py",
    }


def test_destructive_unsafeguarded_cleared_by_resource_path(graph):
    """A policy bound to a resource the tool touches counts as a safeguard."""
    _seed_destructive(
        data_access=[DataAccess(tool="sample::write_file", resource=CSV, mode="WRITES")],
        governed_by={CSV: ["pii-deny"]},
    )
    assert queries.destructive_unsafeguarded() == []


def test_destructive_unsafeguarded_cleared_by_direct_governance(graph):
    _seed_destructive(governed_by={"sample::write_file": ["pii-deny"]})
    assert queries.destructive_unsafeguarded() == []


def test_destructive_unsafeguarded_ignores_unhinted_tools(graph):
    """An ungoverned tool with no destructive hint is unmapped-tools territory,
    not this queue."""
    _seed_destructive()
    rows = queries.destructive_unsafeguarded()
    assert "sample::read_file" not in {r["tool_key"] for r in rows}


# --- annotation-contradictions --------------------------------------------


def test_annotation_contradiction_cites_both_provenances(graph):
    _seed_destructive(
        data_access=[
            DataAccess(
                tool="sample::read_file",
                resource=CSV,
                mode="WRITES",
                provenance={
                    "source": "operator_asserted",
                    "confidence": "high",
                    "evidence": "audit/2026-06-runbook.md:12",
                },
            )
        ],
    )
    rows = queries.annotation_contradictions()
    assert len(rows) == 1
    row = rows[0]
    assert row["tool_key"] == "sample::read_file"
    assert row["annotation_claim"] == "readOnlyHint: true"
    assert row["annotation_provenance"]["confidence"] == "medium"
    assert row["authored_claim"] == f"WRITES {CSV}"
    assert row["authored_provenance"] == {
        "source": "operator_asserted",
        "confidence": "high",
        "evidence": "audit/2026-06-runbook.md:12",
    }


def test_no_contradiction_without_read_only_hint(graph):
    """WRITES on a tool that never claimed read-only is normal authoring."""
    _seed_destructive(
        data_access=[DataAccess(tool="sample::write_file", resource=CSV, mode="WRITES")],
    )
    assert queries.annotation_contradictions() == []


def test_no_contradiction_for_authored_reads(graph):
    """READS on a read-only-hinted tool agrees with the claim — no row."""
    _seed_destructive(
        data_access=[DataAccess(tool="sample::read_file", resource=CSV, mode="READS")],
    )
    assert queries.annotation_contradictions() == []
