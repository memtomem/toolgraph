"""Pins the contract memtomem-stm depends on (memtomem-stm#465).

The memtomem-stm proxy consumes ``eligible_tools`` + ``rank_features`` over
MCP stdio at startup and parses exactly what is asserted here: the seven
reject-reason literals (keys of its ``_TOOLGRAPH_REASON_MAP``; an
unrecognized reason degrades to a generic withhold there), the required
response keys, candidate input-order preservation on ``eligible``, the
``agent_found=false`` abort shape, DENY ``paths`` / ambiguity ``candidates``
payloads, and the ``graph_generation`` stamp on the MCP surface (its consult
cache key).

Additive fields are allowed — only changing, removing, or renaming something
asserted here is a breaking change for a shipped consumer, not a refactor.
Kept separate from test_selector.py on purpose: a contract pin must not
drift when the behavior suite's shared seed evolves.
"""

from __future__ import annotations

from toolgraph.graph import loader, selector
from toolgraph.manifest.ingest import ingest_governance
from toolgraph.models import (
    AccessGrant,
    CrawlResult,
    DataAccess,
    ExpectedException,
    Governance,
    Policy,
    Provenance,
    ResourceRecord,
    ToolRecord,
)

REPO = "repo:///work"
SECRETS = "file:///secrets.env"
EXPORT = "export:///sink"

EVIDENCE = Provenance(source="operator_asserted", confidence="high", evidence="runbook.md::pin")

# The literals memtomem-stm's _TOOLGRAPH_REASON_MAP keys on. Renaming any of
# these silently downgrades the consumer's reject handling to a generic
# withhold — treat this tuple as frozen.
REASONS = frozenset(
    {
        "TOOL_NOT_FOUND",
        "AMBIGUOUS_TOOL",
        "NOT_GRANTED",
        "DENY_VIOLATION",
        "DENY_GOVERNED",
        "DRIFTED",
        "UNMAPPED",
    }
)


def _seed() -> None:
    """One agent, one tool per reject state (plus one clean ALLOW):

    alpha::git_status   clean ALLOW
    alpha::read_file    DENY_VIOLATION (READS secrets governed by pii-deny)
    alpha::export_data  DENY_GOVERNED (fully covered by an operator exception)
    alpha::free_tool    UNMAPPED (granted, no authored data-flow)
    alpha::ghost_tool   DRIFTED (granted, then dropped from the crawl)
    alpha::loose_tool   NOT_GRANTED (exposed, no grant)
    beta::read_file     makes the bare name 'read_file' AMBIGUOUS_TOOL
    (a ref to a never-crawled tool exercises TOOL_NOT_FOUND)
    """
    loader.load_crawl_result(
        CrawlResult(
            server_name="alpha",
            transport="stdio",
            tools=[
                ToolRecord(name="git_status", read_only_hint=True),
                ToolRecord(name="read_file"),
                ToolRecord(name="export_data", destructive_hint=True),
                ToolRecord(name="free_tool"),
                ToolRecord(name="ghost_tool"),
                ToolRecord(name="loose_tool"),
            ],
            resources=[
                ResourceRecord(uri=REPO),
                ResourceRecord(uri=SECRETS),
                ResourceRecord(uri=EXPORT),
            ],
        )
    )
    loader.load_crawl_result(
        CrawlResult(
            server_name="beta",
            transport="stdio",
            tools=[ToolRecord(name="read_file")],
        )
    )
    report = ingest_governance(
        Governance(
            agents=["stm-proxy"],
            policies=[
                Policy(id="pii-deny", effect="DENY"),
                Policy(id="export-deny", effect="DENY"),
            ],
            grants=[
                AccessGrant(agent="stm-proxy", tool="alpha::git_status"),
                AccessGrant(agent="stm-proxy", tool="alpha::read_file"),
                AccessGrant(agent="stm-proxy", tool="alpha::export_data"),
                AccessGrant(agent="stm-proxy", tool="alpha::free_tool"),
                AccessGrant(agent="stm-proxy", tool="alpha::ghost_tool"),
            ],
            data_access=[
                DataAccess(
                    tool="alpha::git_status", resource=REPO, mode="READS",
                    provenance=EVIDENCE,
                ),
                DataAccess(
                    tool="alpha::read_file", resource=SECRETS, mode="READS",
                    provenance=EVIDENCE,
                ),
                DataAccess(
                    tool="alpha::export_data", resource=EXPORT, mode="WRITES",
                    provenance=EVIDENCE,
                ),
            ],
            governed_by={
                SECRETS: [{"policy": "pii-deny", "provenance": EVIDENCE.model_dump()}],
                EXPORT: [{"policy": "export-deny", "provenance": EVIDENCE.model_dump()}],
            },
            expected_exceptions=[
                ExpectedException(
                    agent="stm-proxy",
                    tool="alpha::export_data",
                    policy="export-deny",
                    reason="quarterly export is by design",
                )
            ],
        )
    )
    assert report.applied, report.warnings
    # Re-crawl alpha without ghost_tool: the loader keeps the governed node
    # but drops EXPOSES — the drifted state.
    loader.load_crawl_result(
        CrawlResult(
            server_name="alpha",
            transport="stdio",
            tools=[
                ToolRecord(name="git_status", read_only_hint=True),
                ToolRecord(name="read_file"),
                ToolRecord(name="export_data", destructive_hint=True),
                ToolRecord(name="free_tool"),
                ToolRecord(name="loose_tool"),
            ],
            resources=[
                ResourceRecord(uri=REPO),
                ResourceRecord(uri=SECRETS),
                ResourceRecord(uri=EXPORT),
            ],
        )
    )


CANDIDATES = [
    "alpha::git_status",     # clean ALLOW
    "read_file",             # AMBIGUOUS_TOOL (alpha + beta)
    "alpha::read_file",      # DENY_VIOLATION
    "alpha::export_data",    # DENY_GOVERNED
    "alpha::ghost_tool",     # DRIFTED
    "alpha::free_tool",      # UNMAPPED
    "alpha::loose_tool",     # NOT_GRANTED
    "alpha::no_such_tool",   # TOOL_NOT_FOUND
]


def test_reject_reason_vocabulary_is_frozen(graph):
    """Both the published strict reject set and the reasons actually emitted
    are exactly the seven literals the consumer maps on."""
    _seed()
    assert selector.PROFILES["strict"] == REASONS
    got = selector.eligible_tools("stm-proxy", CANDIDATES, profile="strict")
    assert {row["reason"] for row in got["rejected"]} == REASONS


def test_responses_carry_the_keys_the_consumer_parses(graph):
    """Required keys only — additive fields are allowed, absence is breaking."""
    _seed()
    filtered = selector.eligible_tools("stm-proxy", CANDIDATES, profile="strict")
    assert {"agent", "agent_found", "profile", "eligible", "rejected"} <= set(filtered)
    assert filtered["agent"] == "stm-proxy"
    assert filtered["profile"] == "strict"
    for row in filtered["rejected"]:
        assert {"candidate", "reason"} <= set(row)

    ranked = selector.rank_features("stm-proxy", CANDIDATES)
    assert {"agent", "agent_found", "features"} <= set(ranked)
    for row in ranked["features"]:
        assert {"candidate", "risk_score"} <= set(row)


def test_eligible_preserves_candidate_input_order(graph):
    """The consumer relies on input order — the eligible list is positional
    output, never re-sorted. Under review, governed + unmapped pass too, so
    three tools prove the ordering."""
    _seed()
    candidates = ["alpha::free_tool", "alpha::git_status", "alpha::export_data"]
    got = selector.eligible_tools("stm-proxy", candidates, profile="review")
    assert got["eligible"] == candidates


def test_agent_not_found_is_a_structured_abort(graph):
    """agent_found=false with empty collections is the consumer's abort
    signal (on_agent_not_found knob) — not an empty catalog, never an error."""
    _seed()
    filtered = selector.eligible_tools("nobody", CANDIDATES, profile="strict")
    assert filtered["agent_found"] is False
    assert filtered["eligible"] == []
    assert filtered["rejected"] == []

    ranked = selector.rank_features("nobody", CANDIDATES)
    assert ranked["agent_found"] is False
    assert ranked["features"] == []


def test_deny_rows_carry_paths_and_ambiguous_rows_carry_candidates(graph):
    _seed()
    got = selector.eligible_tools("stm-proxy", CANDIDATES, profile="strict")
    by_reason = {row["reason"]: row for row in got["rejected"]}

    for reason in ("DENY_VIOLATION", "DENY_GOVERNED"):
        paths = by_reason[reason]["paths"]
        assert paths and all(isinstance(p, str) for p in paths)

    ambiguous = by_reason["AMBIGUOUS_TOOL"]["candidates"]
    assert ambiguous == ["alpha::read_file", "beta::read_file"]


def test_mcp_surface_stamps_graph_generation(graph):
    """The consumer keys its consult cache on the integer graph_generation
    stamped by the MCP wrappers (ADR-0004). The full protocol round-trip is
    covered by test_server.py::test_mcp_selector_surface_mirrors_direct_query;
    here we pin the stamp itself on both consulted tools."""
    from toolgraph.server import app

    _seed()
    filtered = app.eligible_tools("stm-proxy", CANDIDATES, "strict")
    ranked = app.rank_features("stm-proxy", CANDIDATES)
    for got in (filtered, ranked):
        assert isinstance(got["graph_generation"], int)
