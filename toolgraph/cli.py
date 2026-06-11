"""toolgraph command line — grows one command per phase."""

from __future__ import annotations

import json
from pathlib import Path

import typer

from toolgraph import config
from toolgraph.crawler.crawl import DEFAULT_TIMEOUT, crawl_all, load_servers_config
from toolgraph.graph import driver, loader, queries, schema
from toolgraph.manifest.ingest import governance_counts, ingest_governance
from toolgraph.manifest.parser import load_governance

app = typer.Typer(
    add_completion=False,
    help="Graph-native MCP/tool registry for advisory governance analysis.",
)


@app.command()
def check() -> None:
    """Verify Neo4j connectivity."""
    driver.verify_connectivity()
    typer.echo(f"OK: connected to {config.settings.neo4j_uri}")


@app.command("init-schema")
def init_schema() -> None:
    """Create uniqueness constraints (idempotent)."""
    schema.init_schema()
    names = [c.get("name") for c in schema.list_constraints()]
    typer.echo("Constraints present: " + ", ".join(sorted(n for n in names if n)))


@app.command()
def crawl(
    servers: Path = typer.Option(None, help="Path to servers.yaml (defaults to config)."),
    timeout: float = typer.Option(DEFAULT_TIMEOUT, help="Per-server crawl timeout (seconds)."),
) -> None:
    """Crawl every server in servers.yaml and load its tools/resources into the graph."""
    path = servers or config.settings.servers_config
    specs = load_servers_config(path)
    typer.echo(f"Crawling {len(specs)} server(s) from {path} ...")

    results, failures = crawl_all(specs, timeout=timeout)
    schema.init_schema()
    for result in results:
        warnings = loader.load_crawl_result(result)
        typer.echo(
            f"  ✓ {result.server_name} v{result.server_version} "
            f"({len(result.tools)} tools, {len(result.resources)} resources)"
        )
        for w in warnings:
            typer.echo(f"    ⚠ {w}")
    for label, error in failures:
        typer.echo(f"  ✗ {label}: {error}")

    counts = loader.graph_counts()
    typer.echo(f"Graph now: {counts}")
    if failures:
        raise typer.Exit(code=1)


@app.command("ingest-manifest")
def ingest_manifest(
    governance: Path = typer.Option(None, help="Path to governance.yaml (defaults to config)."),
    strict_drift: bool = typer.Option(
        False, "--strict-drift",
        help="Treat drifted tool refs (no live EXPOSES edge) as errors that "
             "reject the manifest. Default: non-fatal notice.",
    ),
) -> None:
    """Ingest authored governance (agents, policies, ACL, data access, GOVERNED_BY)."""
    path = governance or config.settings.governance_config
    gov = load_governance(path)
    schema.init_schema()
    report = ingest_governance(gov, strict_drift=strict_drift)
    if report.warnings:
        # Ingest is atomic: any warning rejects the whole manifest. Reflect
        # that in the headline so an operator with a typo doesn't read
        # "Ingested" as success.
        typer.echo(f"REJECTED — manifest at {path} not applied (graph unchanged)")
        for w in report.warnings:
            typer.echo(f"  ⚠ {w}")
        for n in report.notices:
            typer.echo(f"  ℹ {n}")
        raise typer.Exit(code=1)
    for n in report.notices:
        typer.echo(f"  ℹ {n}")
    typer.echo(f"Ingested governance from {path}")
    typer.echo(f"Governance now: {governance_counts()}")


@app.command()
def reset(yes: bool = typer.Option(False, "--yes", help="Confirm deletion of ALL graph data.")) -> None:
    """Delete every node and relationship (keeps constraints). Destructive."""
    if not yes:
        typer.echo("Refusing to wipe the graph without --yes")
        raise typer.Exit(code=1)
    with driver.session() as s:
        s.run("MATCH (n) DETACH DELETE n")
    typer.echo("Graph cleared.")


@app.command()
def serve(
    http: bool = typer.Option(False, "--http", help="Serve over streamable-http instead of stdio."),
) -> None:
    """Run toolgraph as an MCP server exposing the governance queries as tools."""
    from toolgraph.server.app import mcp

    mcp.run(transport="streamable-http") if http else mcp.run()


@app.command("check-access")
def check_access(agent: str, tool: str) -> None:
    """Is AGENT allowed to call TOOL, and does the call cross a DENY policy?"""
    typer.echo(json.dumps(queries.check_access(agent, tool), indent=2))


@app.command("unsafe-tools")
def unsafe_tools(
    agent: str,
    all: bool = typer.Option(
        False, "--all",
        help="Include authorized_but_governed rows (operator-declared exceptions).",
    ),
) -> None:
    """Tools AGENT can call that touch a DENY-governed resource.

    Defaults to ``classification='violation'`` only — operator-declared
    exceptions (``authorized_but_governed``) are filtered out so reviewers
    don't drown in by-design grants. Pass ``--all`` to see both.

    A typo'd agent prints AGENT_NOT_FOUND and exits 1 — not silently empty.
    """
    if not queries.agent_exists(agent):
        typer.echo(f"AGENT_NOT_FOUND: no agent named {agent!r} in the graph")
        raise typer.Exit(code=1)
    rows = queries.unsafe_callable_tools(agent)
    if not all:
        rows = [r for r in rows if r.get("classification") == "violation"]
    typer.echo(json.dumps(rows, indent=2))


@app.command("blast-radius")
def blast_radius(node: str) -> None:
    """Agents/tools affected if NODE (a resource URI or policy id) changes."""
    typer.echo(json.dumps(queries.blast_radius(node), indent=2))


@app.command("unmapped-tools")
def unmapped_tools(
    all: bool = typer.Option(False, "--all", help="Include tools with no CAN_CALL grant."),
) -> None:
    """Tools whose data-flow effects (READS/WRITES) are not yet authored."""
    typer.echo(json.dumps(queries.unmapped_tools(only_granted=not all), indent=2))


@app.command("orphan-policies")
def orphan_policies() -> None:
    """Policies no agent currently reaches — likely obsolete or over-narrow."""
    typer.echo(json.dumps(queries.orphan_policies(), indent=2))


@app.command("unbacked-edges")
def unbacked_edges(
    include_grants: bool = typer.Option(
        False, "--include-grants",
        help="Include CAN_CALL grants (intent declarations) — usually noise.",
    ),
) -> None:
    """Authored edges with no evidence pointer — unsupported claims.

    Default scope is READS/WRITES/GOVERNED_BY (factual / load-bearing claims).
    CAN_CALL grants are excluded by default — a grant is intent, not a
    factual claim, so requiring evidence on every grant pollutes the audit.
    """
    typer.echo(
        json.dumps(queries.unbacked_edges(include_grants=include_grants), indent=2)
    )


@app.command("drift")
def drift() -> None:
    """Tools with authored governance but no live EXPOSES edge (re-ingest needed)."""
    typer.echo(json.dumps(queries.drifted_tools(), indent=2))


if __name__ == "__main__":
    app()
