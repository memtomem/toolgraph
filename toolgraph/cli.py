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
) -> None:
    """Ingest authored governance (agents, policies, ACL, data access, GOVERNED_BY)."""
    path = governance or config.settings.governance_config
    gov = load_governance(path)
    schema.init_schema()
    warnings = ingest_governance(gov)
    typer.echo(f"Ingested governance from {path}")
    for w in warnings:
        typer.echo(f"  ⚠ {w}")
    typer.echo(f"Governance now: {governance_counts()}")
    if warnings:
        raise typer.Exit(code=1)


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
def unsafe_tools(agent: str) -> None:
    """Tools AGENT can call that touch a DENY-governed resource."""
    typer.echo(json.dumps(queries.unsafe_callable_tools(agent), indent=2))


@app.command("blast-radius")
def blast_radius(node: str) -> None:
    """Agents/tools affected if NODE (a resource URI or policy id) changes."""
    typer.echo(json.dumps(queries.blast_radius(node), indent=2))


if __name__ == "__main__":
    app()
