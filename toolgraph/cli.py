"""toolgraph command line — grows one command per phase."""

from __future__ import annotations

import functools
import json
import os
from enum import Enum
from importlib.resources import as_file, files
from pathlib import Path
import shutil
import sys
from typing import NoReturn

import typer

from toolgraph import __version__
from toolgraph import config
from toolgraph.artifacts import atomic_write_private, canonical_json_bytes
from toolgraph.control_plan import (
    MAX_PLAN_BYTES,
    ControlPlanError,
    build_control_preflight,
)
from toolgraph.crawler.crawl import (
    DEFAULT_TIMEOUT,
    crawl_all,
    duplicate_identities,
    duplicate_name_overrides,
    fleet_keep_names,
    load_servers_config,
    spec_label,
)
from toolgraph.graph import driver, loader, queries, schema, selector
from toolgraph.manifest.ingest import governance_counts, ingest_governance
from toolgraph.manifest.parser import load_governance
from toolgraph.preflight import build_preflight
from toolgraph.policy_bundle import PolicyBundleError, build_policy_bundle
from toolgraph.policy_review import PolicyReviewPlanError, build_policy_review_plan
from toolgraph.review_candidates import (
    ReviewCandidateError,
    annotate_candidate,
    list_candidates,
    normalize_note,
    normalize_reviewer,
)
from toolgraph.redaction import redact_text


class AuditReportFormat(str, Enum):
    json = "json"
    markdown = "markdown"


class ReviewDisposition(str, Enum):
    open = "open"
    accepted = "accepted"
    dismissed = "dismissed"


app = typer.Typer(
    add_completion=False,
    help="Graph-native MCP/tool registry for advisory governance analysis.",
)
review_candidates_app = typer.Typer(
    add_completion=False,
    help="List and annotate Tracegraph governance review candidates.",
)
policy_app = typer.Typer(
    add_completion=False,
    help="Compile and review portable policy artifacts.",
)
example_app = typer.Typer(
    add_completion=False,
    help="Create a self-contained local quickstart.",
)
app.add_typer(review_candidates_app, name="review-candidates")
app.add_typer(policy_app, name="policy")
app.add_typer(example_app, name="example")


def _reports_backend_outage(fn):
    """Convert a typed backend outage into the CLI's single-line ERROR contract.

    Without this, an unreachable backend surfaces as a raw traceback whose
    driver message can carry the connection URI. The MCP surface already
    types and redacts this failure (server/app.py); the CLI must match.
    Applied below ``@app.command`` so Typer registers the wrapped function
    (``functools.wraps`` preserves the signature Typer introspects).
    """

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except config.RuntimeConfigurationError as exc:
            typer.echo(f"ERROR: {redact_text(exc)}", err=True)
            raise typer.Exit(code=1) from None
        except driver.BackendUnavailableError as exc:
            typer.echo(f"ERROR: backend unavailable: {redact_text(exc)}", err=True)
            raise typer.Exit(code=1) from None

    return wrapper


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"toolgraph {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    config_path: Path | None = typer.Option(
        None,
        "--config",
        help="Runtime config path (overrides TOOLGRAPH_CONFIG and cwd .env).",
    ),
    version: bool = typer.Option(
        False,
        "--version",
        callback=_version_callback,
        is_eager=True,
        help="Show the installed version and exit.",
    ),
) -> None:
    """Configure Toolgraph before running a command."""
    del version
    if config_path is not None:
        try:
            config.configure(config_path)
        except RuntimeError as exc:
            raise typer.BadParameter(str(exc), param_hint="--config") from None
        driver.close_driver()


@example_app.command("init")
def example_init(
    destination: Path = typer.Argument(
        Path("toolgraph-quickstart"), help="New directory for quickstart files."
    ),
) -> None:
    """Create an install-only quickstart without cloning the repository."""
    destination = destination.expanduser().resolve()
    if destination.exists():
        raise typer.BadParameter(
            f"destination already exists: {destination}", param_hint="DESTINATION"
        )
    source = files("toolgraph").joinpath("quickstart")
    # Stage next to the destination and rename once complete: a failure
    # mid-copy must not leave a half-populated quickstart that blocks the
    # "destination already exists" guard on the next run.
    staging = destination.parent / f".{destination.name}.tmp-{os.getpid()}"
    try:
        with as_file(source) as source_path:
            shutil.copytree(source_path, staging)
        servers_path = staging / "servers.yaml"
        servers_path.write_text(
            servers_path.read_text(encoding="utf-8").replace(
                "__TOOLGRAPH_PYTHON__", json.dumps(sys.executable)
            ),
            encoding="utf-8",
        )
        os.rename(staging, destination)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    typer.echo(f"Created Toolgraph quickstart at {destination}")
    typer.echo(f"Next: cd {destination} && toolgraph init")


@app.command()
@_reports_backend_outage
def check() -> None:
    """Verify the configured graph backend."""
    driver.verify_connectivity()
    if driver.backend_name() == "ladybug":
        typer.echo(f"OK: backend=ladybug path={config.settings.db_path}")
    else:
        typer.echo(f"OK: backend=neo4j uri={config.settings.neo4j_uri}")


@app.command("init")
@_reports_backend_outage
def init_backend(
    backend: str = typer.Option(
        "ladybug", "--backend", help="Local backend: ladybug (default) or neo4j."
    ),
    state_dir: Path = typer.Option(
        Path(".toolgraph"), "--state-dir", help="Local config/database directory."
    ),
) -> None:
    """Initialize a versioned local backend configuration."""
    normalized = backend.lower().strip()
    if normalized not in {"ladybug", "neo4j"}:
        raise typer.BadParameter("expected ladybug or neo4j", param_hint="--backend")
    state_dir = state_dir.expanduser().resolve()
    runtime_path = state_dir / "config.json"
    db_path = state_dir / "toolgraph.lbug"
    previous = config.settings
    db_existed = db_path.exists()
    config.settings = config.Settings(backend=normalized, db_path=db_path)
    driver.close_driver()
    try:
        schema.init_schema()
        payload = canonical_json_bytes(
            {
                "schema_version": 1,
                "backend": normalized,
                "db_path": str(db_path),
            }
        )
        atomic_write_private(runtime_path, payload)
    except Exception as exc:
        config.settings = previous
        driver.close_driver()
        # Clean up only a database this invocation plausibly created.
        # BACKEND_LOCKED means another process owns it — it may have created
        # the file after our db_existed check, and deleting here would
        # destroy the winner's database.
        if not db_existed and not isinstance(exc, driver.BackendLockedError):
            for leftover in (db_path, Path(f"{db_path}.wal")):
                if leftover.is_dir():
                    shutil.rmtree(leftover, ignore_errors=True)
                else:
                    leftover.unlink(missing_ok=True)
        raise
    typer.echo(f"Initialized backend={normalized} config={runtime_path}")


@app.command("init-schema")
@_reports_backend_outage
def init_schema() -> None:
    """Create uniqueness constraints (idempotent)."""
    schema.init_schema()
    names = [c.get("name") for c in schema.list_constraints()]
    typer.echo("Constraints present: " + ", ".join(sorted(n for n in names if n)))


@app.command()
@_reports_backend_outage
def crawl(
    servers: Path = typer.Option(None, help="Path to servers.yaml (defaults to config)."),
    timeout: float = typer.Option(DEFAULT_TIMEOUT, help="Per-server crawl timeout (seconds)."),
    prune: bool = typer.Option(
        True, "--prune/--no-prune",
        help="Fleet reconciliation (ADR-0007): after loading, retire crawled "
             "subgraphs of servers no longer in servers.yaml. Pass --no-prune "
             "when crawling a subset file that does not declare the whole fleet.",
    ),
) -> None:
    """Crawl every server in servers.yaml and load its tools/resources into the graph.

    servers.yaml declares the fleet: by default a successful pass also retires
    servers (and their crawl-owned subgraphs) that are no longer listed, so
    stale EXPOSES edges cannot defeat the drift checks. Failed servers are
    protected by their ``name`` override; an unnamed failure skips the prune.
    """
    path = servers or config.settings.servers_config
    try:
        specs = load_servers_config(path)
    except Exception as exc:  # noqa: BLE001 - config errors share one CLI contract
        typer.echo(f"Invalid servers config at {path}: {redact_text(exc)}", err=True)
        raise typer.Exit(code=1) from None
    dupes = duplicate_name_overrides(specs)
    if dupes:
        # Two specs claiming one name would MERGE into one MCPServer node and
        # last-load-wins reconcile each other's tools away — reject before
        # connecting to anything.
        typer.echo(
            f"  [error] duplicate name override(s) in {path}: {', '.join(sorted(dupes))}",
            err=True,
        )
        raise typer.Exit(code=1)
    typer.echo(f"Crawling {len(specs)} server(s) from {path} ...")

    results, failures = crawl_all(specs, timeout=timeout)
    dupes = duplicate_identities(results, failures)
    if dupes:
        typer.echo(
            "  [error] multiple servers claim the same graph identity: "
            f"{', '.join(sorted(dupes))} — add distinct `name:` overrides in "
            f"{path}; nothing was loaded",
            err=True,
        )
        raise typer.Exit(code=1)
    schema.init_schema()
    for result in results:
        warnings = loader.load_crawl_result(result)
        typer.echo(
            f"  [ok] {result.server_name} v{result.server_version} "
            f"({len(result.tools)} tools, {len(result.resources)} resources)"
        )
        for w in warnings:
            typer.echo(f"    [warn] {w}")
    for spec, error in failures:
        typer.echo(f"  [error] {spec_label(spec)}: {error}")

    if prune:
        keep, blockers = fleet_keep_names(specs, results, failures)
        if blockers:
            typer.echo(
                "  [warn] fleet reconciliation skipped: failed server(s) "
                f"{', '.join(sorted(blockers))} have no `name:` override in "
                f"{path}, so a crawl failure cannot be told apart from a "
                "removed server — add `name:` to keep reconciliation running "
                "through failures"
            )
        else:
            retired, warnings = loader.retire_unlisted_servers(keep)
            for name in retired:
                typer.echo(f"  retired {name!r}: no longer in {path}")
            for w in warnings:
                typer.echo(f"    [warn] {w}")

    counts = loader.graph_counts()
    typer.echo(f"Graph now: {counts}")
    if failures:
        raise typer.Exit(code=1)


@app.command("ingest-manifest")
@_reports_backend_outage
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
    try:
        gov = load_governance(path)
    except Exception as exc:  # noqa: BLE001 - config errors share one CLI contract
        typer.echo(f"Invalid governance config at {path}: {redact_text(exc)}", err=True)
        raise typer.Exit(code=1) from None
    schema.init_schema()
    report = ingest_governance(gov, strict_drift=strict_drift)
    if report.warnings:
        # Ingest is atomic: any warning rejects the whole manifest. Reflect
        # that in the headline so an operator with a typo doesn't read
        # "Ingested" as success.
        typer.echo(f"REJECTED — manifest at {path} not applied (graph unchanged)")
        for w in report.warnings:
            typer.echo(f"  [warn] {w}")
        for n in report.notices:
            typer.echo(f"  [info] {n}")
        raise typer.Exit(code=1)
    for n in report.notices:
        typer.echo(f"  [info] {n}")
    typer.echo(f"Ingested governance from {path}")
    typer.echo(f"Governance now: {governance_counts()}")


@app.command()
@_reports_backend_outage
def reset(yes: bool = typer.Option(False, "--yes", help="Confirm deletion of ALL graph data.")) -> None:
    """Delete every node and relationship (keeps constraints). Destructive.

    Not atomic: deletion runs in batches, so run it inside a maintenance
    window on a shared backend — a concurrent writer can interleave, and an
    outage mid-way leaves a partially erased graph (re-run to finish).
    """
    if not yes:
        typer.echo("Refusing to wipe the graph without --yes")
        raise typer.Exit(code=1)
    # Enforce the schema-version gate BEFORE destroying GraphMeta: an older
    # binary must not erase a newer database and then re-stamp it as its own.
    schema.init_schema()
    with driver.session() as s:
        # Batched: one unbounded DETACH DELETE builds a transaction holding
        # every node and can OOM the server on a large graph.
        while True:
            record = s.run(
                "MATCH (n) WITH n LIMIT 10000 DETACH DELETE n RETURN count(n) AS deleted"
            ).single()
            if not record or not record["deleted"]:
                break
    # Recreate GraphMeta at generation 0 with a fresh instance id. A reset may
    # reuse generation numbers but must never reuse an external graph-state key.
    schema.init_schema()
    typer.echo("Graph cleared.")


@app.command()
def serve(
    http: bool = typer.Option(False, "--http", help="Serve over streamable-http instead of stdio."),
) -> None:
    """Run toolgraph as an MCP server exposing the governance queries as tools."""
    from toolgraph.server.app import mcp

    mcp.run(transport="streamable-http") if http else mcp.run()


@app.command("check-access")
@_reports_backend_outage
def check_access(
    agent: str,
    tool: str,
    fail_on_deny: bool = typer.Option(
        False, "--fail-on-deny",
        help="CI gate (ADR-0003): exit 1 on a DENY that isn't fully covered "
             "by operator exceptions, 2 on a not-found/ambiguous verdict. "
             "Default: always exit 0.",
    ),
) -> None:
    """Is AGENT allowed to call TOOL, and does the call cross a DENY policy?

    Exit codes are advisory (always 0) unless ``--fail-on-deny`` is passed.
    """
    result = queries.check_access(agent, tool)
    typer.echo(json.dumps(result, indent=2))
    if not fail_on_deny:
        return
    if result["verdict"] in ("AGENT_NOT_FOUND", "TOOL_NOT_FOUND", "AMBIGUOUS_TOOL"):
        # A typo'd name must not pass a CI gate as a clean run.
        raise typer.Exit(code=2)
    if result["verdict"] == "DENY" and not result["all_authorized"]:
        raise typer.Exit(code=1)


@app.command("unsafe-tools")
@_reports_backend_outage
def unsafe_tools(
    agent: str,
    all: bool = typer.Option(
        False, "--all",
        help="Include authorized_but_governed rows (operator-declared exceptions).",
    ),
    fail_on_violation: bool = typer.Option(
        False, "--fail-on-violation",
        help="CI gate (ADR-0003): exit 1 when any violation row remains in "
             "the output. Default: exit 0 regardless of findings.",
    ),
) -> None:
    """Tools AGENT can call that touch a DENY-governed resource.

    Defaults to ``classification='violation'`` only — operator-declared
    exceptions (``authorized_but_governed``) are filtered out so reviewers
    don't drown in by-design grants. Pass ``--all`` to see both.

    A typo'd agent prints AGENT_NOT_FOUND on stderr and exits 1 — not
    silently empty. Stdout carries only the JSON (ADR-0003).
    """
    if not queries.agent_exists(agent):
        typer.echo(f"AGENT_NOT_FOUND: no agent named {agent!r} in the graph", err=True)
        raise typer.Exit(code=1)
    rows = queries.unsafe_callable_tools(agent)
    if not all:
        rows = [r for r in rows if r.get("classification") == "violation"]
    typer.echo(json.dumps(rows, indent=2))
    if fail_on_violation and any(r.get("classification") == "violation" for r in rows):
        raise typer.Exit(code=1)


@app.command("blast-radius")
@_reports_backend_outage
def blast_radius(node: str) -> None:
    """Agents/tools affected if NODE (a resource URI or policy id) changes."""
    typer.echo(json.dumps(queries.blast_radius(node), indent=2))


@app.command("unmapped-tools")
@_reports_backend_outage
def unmapped_tools(
    all: bool = typer.Option(False, "--all", help="Include tools with no CAN_CALL grant."),
) -> None:
    """Tools whose data-flow effects (READS/WRITES) are not yet authored."""
    typer.echo(json.dumps(queries.unmapped_tools(only_granted=not all), indent=2))


@app.command("orphan-policies")
@_reports_backend_outage
def orphan_policies() -> None:
    """Policies no agent currently reaches — likely obsolete or over-narrow."""
    typer.echo(json.dumps(queries.orphan_policies(), indent=2))


@app.command("unbacked-edges")
@_reports_backend_outage
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
@_reports_backend_outage
def drift() -> None:
    """Tools with authored governance but no live EXPOSES edge (re-ingest needed)."""
    typer.echo(json.dumps(queries.drifted_tools(), indent=2))


@app.command("destructive-unsafeguarded")
@_reports_backend_outage
def destructive_unsafeguarded() -> None:
    """Destructive-hinted tools with no GOVERNED_BY path (direct or via any resource).

    The hint is the server's self-claim (crawled/medium, ADR-0006) — these rows
    are an authoring priority queue, not violations.
    """
    typer.echo(json.dumps(queries.destructive_unsafeguarded(), indent=2))


@app.command("annotation-contradictions")
@_reports_backend_outage
def annotation_contradictions() -> None:
    """Authored WRITES on tools hinting readOnlyHint: true — one side is wrong.

    Both sides cite their provenance; the operator picks the stronger evidence.
    """
    typer.echo(json.dumps(queries.annotation_contradictions(), indent=2))


def _audit_report_markdown(report: dict) -> str:
    lines = [
        "# toolgraph audit report",
        "",
        f"Status: **{report['status'].upper()}**",
        "",
        "| Finding | Count | Blocking |",
        "| --- | ---: | --- |",
    ]
    blocking = set(report["summary"]["blocking_findings"])
    for name, count in report["summary"]["counts"].items():
        lines.append(f"| `{name}` | {count} | {'yes' if name in blocking else 'no'} |")
    lines.append("")
    for name, rows in report["findings"].items():
        if not rows:
            continue
        lines.extend(
            [
                f"## {name.replace('_', ' ').title()}",
                "",
                "```json",
                json.dumps(rows, indent=2),
                "```",
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


@app.command("audit-report")
@_reports_backend_outage
def audit_report(
    format: AuditReportFormat = typer.Option(
        AuditReportFormat.json,
        "--format",
        help="Output format: json (automation) or markdown (human review).",
    ),
    include_grants: bool = typer.Option(
        False,
        "--include-grants",
        help="Include CAN_CALL grants in the unbacked-edge evidence audit.",
    ),
    fail_on_blocking: bool = typer.Option(
        False,
        "--fail-on-blocking",
        help="CI gate: exit 1 when blocking findings remain. Default: exit 0.",
    ),
) -> None:
    """Whole-graph operational audit summary."""
    report = queries.audit_report(include_grants=include_grants)
    if format is AuditReportFormat.markdown:
        typer.echo(_audit_report_markdown(report), nl=False)
    else:
        typer.echo(json.dumps(report, indent=2))
    if fail_on_blocking and report["status"] == "fail":
        raise typer.Exit(code=1)


def _selector_profile(profile: str) -> str:
    if profile not in selector.PROFILES:
        raise typer.BadParameter(
            f"unknown profile {profile!r} — expected one of {sorted(selector.PROFILES)}"
        )
    return profile


# The binding bound lives in the selector (every surface shares it); the CLI
# pre-checks only to produce a usage error instead of a traceback.
MAX_CLI_CANDIDATES = selector.MAX_CANDIDATES


def _bounded_candidates(candidates: list[str]) -> list[str]:
    if len(candidates) > MAX_CLI_CANDIDATES:
        raise typer.BadParameter(
            f"{len(candidates)} candidates exceed the {MAX_CLI_CANDIDATES} limit",
            param_hint="CANDIDATES",
        )
    return candidates


def _require_agent(agent: str) -> None:
    """Selection with an unknown agent is a context-construction error, not an
    empty result — mirror unsafe-tools: diagnostic to stderr, exit 1."""
    if not queries.agent_exists(agent):
        typer.echo(f"AGENT_NOT_FOUND: no agent named {agent!r} in the graph", err=True)
        raise typer.Exit(code=1)


@app.command("rank-features")
@_reports_backend_outage
def rank_features(agent: str, candidates: list[str]) -> None:
    """Batch selection features for CANDIDATES (ADR-0005), in input order.

    Facts only — resolution, grant, DENY classification + paths, drift,
    mapping, evidence coverage, annotation self-claims — plus the rule-based
    risk_score from the published fixed table. No relevance, no learning.
    """
    _bounded_candidates(candidates)
    _require_agent(agent)
    typer.echo(json.dumps(selector.rank_features(agent, candidates), indent=2))


@app.command("eligible-tools")
@_reports_backend_outage
def eligible_tools(
    agent: str,
    candidates: list[str],
    profile: str = typer.Option(
        selector.DEFAULT_PROFILE, "--profile",
        help="Named rule set: strict (production default) / review / explore.",
    ),
) -> None:
    """Hard-filter CANDIDATES with reject reasons + policy-evidence paths.

    A learned consumer may rerank the eligible list but may never resurrect
    a rejected row (ADR-0005).
    """
    _selector_profile(profile)
    _bounded_candidates(candidates)
    _require_agent(agent)
    typer.echo(
        json.dumps(selector.eligible_tools(agent, candidates, profile=profile), indent=2)
    )


@app.command("selection-explain")
@_reports_backend_outage
def selection_explain(
    agent: str,
    tool: str,
    profile: str = typer.Option(
        selector.DEFAULT_PROFILE, "--profile",
        help="Named rule set: strict (production default) / review / explore.",
    ),
) -> None:
    """Compact human-readable reasons why TOOL is eligible/rejected for AGENT."""
    _selector_profile(profile)
    _require_agent(agent)
    typer.echo(
        json.dumps(selector.selection_explain(agent, tool, profile=profile), indent=2)
    )


@app.command("preflight")
@_reports_backend_outage
def preflight(
    agent: str,
    candidates: list[str],
    profile: str = typer.Option(
        selector.DEFAULT_PROFILE, "--profile",
        help="Named selector profile: strict / review / explore.",
    ),
    run_id: str = typer.Option(..., "--run-id", help="Stable ecosystem run identifier."),
    features: bool = typer.Option(False, "--features", help="Include selector feature rows."),
    out: Path | None = typer.Option(None, "--out", help="Write the JSON artifact to this path."),
) -> None:
    """Produce an advisory preflight artifact for a candidate tool batch."""
    _selector_profile(profile)
    _bounded_candidates(candidates)
    try:
        artifact = build_preflight(
            agent=agent,
            candidates=candidates,
            profile=profile,
            run_id=run_id,
            include_features=features,
        )
        # Indented rather than canonical bytes: this artifact is read by operators
        # as often as by consumers. The digest covers the exact bytes on disk, so a
        # consumer can verify it with sha256sum without re-serializing.
        payload = json.dumps(artifact, indent=2) + "\n"
        if out is None:
            typer.echo(payload, nl=False)
            return
        digest = atomic_write_private(out, payload.encode("utf-8"))
    except (RuntimeError, OSError, ValueError) as exc:
        typer.echo(f"ERROR: {redact_text(exc)}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(
        json.dumps(
            {
                "kind": artifact["kind"],
                "output": str(out),
                # Prefixed form: SyncMill's artifact_digest contract is
                # ^sha256:[0-9a-f]{64}$, so this value is copyable as-is.
                # `policy compile` deliberately keeps bare hex — memtomem-stm
                # asserts len(bundle_digest) == 64. The two formats answer to
                # different consumers; do not unify them.
                "artifact_digest": f"sha256:{digest}",
                "run_id": artifact["run_id"],
                "graph_generation": artifact["graph_generation"],
                "agent": artifact["agent"],
                "profile": artifact["profile"],
                "decision": artifact["decision"],
                "eligible": len(artifact["eligible"]),
                "rejected": len(artifact["rejected"]),
            },
            indent=2,
        )
    )


@app.command("control-preflight")
@_reports_backend_outage
def control_preflight(
    plan: Path = typer.Argument(..., help="Body-free toolgraph.control-plan JSON file."),
    profile: str = typer.Option(
        selector.DEFAULT_PROFILE,
        "--profile",
        help="Named selector profile: strict / review / explore.",
    ),
    out: Path | None = typer.Option(
        None, "--out", help="Write the private canonical JSON artifact to this path."
    ),
) -> None:
    """Validate and evaluate one bounded orchestration control plan."""
    _selector_profile(profile)
    digest: str | None = None
    try:
        # Bounded read: MAX_PLAN_BYTES must limit memory, not just validation
        # cost, and a stat-then-read pair would be a TOCTOU.
        with plan.open("rb") as handle:
            raw_plan = handle.read(MAX_PLAN_BYTES + 1)
        if len(raw_plan) > MAX_PLAN_BYTES:
            raise ControlPlanError(
                f"control plan exceeds the {MAX_PLAN_BYTES}-byte input limit"
            )
        artifact = build_control_preflight(raw_plan, profile=profile)
        payload = canonical_json_bytes(artifact)
        if out is not None:
            digest = atomic_write_private(out, payload)
    except (ControlPlanError, RuntimeError, OSError, ValueError) as exc:
        typer.echo(f"ERROR: {redact_text(exc)}", err=True)
        raise typer.Exit(code=1) from exc
    if out is None:
        typer.echo(payload.decode(), nl=False)
        return
    typer.echo(
        json.dumps(
            {
                "kind": artifact["kind"],
                "output": str(out),
                # Prefixed to match the plan_digest inside the artifact and
                # SyncMill's ^sha256:[0-9a-f]{64}$ contract.
                "artifact_digest": f"sha256:{digest}",
                "plan_digest": artifact["plan_digest"],
                "run_id": artifact["run_id"],
                "graph_state": artifact["graph_state"],
                "profile": artifact["profile"],
                "decision": artifact["decision"],
                "findings": len(artifact["findings"]),
                "evaluations": len(artifact["evaluations"]),
            },
            indent=2,
        )
    )


@policy_app.command("compile")
@_reports_backend_outage
def policy_compile(
    agent: str = typer.Option(..., "--agent", help="Authored agent identity."),
    profile: str = typer.Option(
        selector.DEFAULT_PROFILE,
        "--profile",
        help="Named selector profile: strict / review / explore.",
    ),
    output: Path = typer.Option(..., "--output", help="Private policy bundle path."),
) -> None:
    """Compile a product-neutral policy bundle for an enforcement gateway."""
    _selector_profile(profile)
    try:
        artifact = build_policy_bundle(agent=agent, profile=profile)
        payload = canonical_json_bytes(artifact)
        digest = atomic_write_private(output, payload)
    except (PolicyBundleError, RuntimeError, OSError, ValueError) as exc:
        typer.echo(f"ERROR: {redact_text(exc)}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(
        json.dumps(
            {
                "kind": artifact["kind"],
                "output": str(output),
                "bundle_digest": digest,
                "graph_state": artifact["graph_state"],
                "agent": artifact["agent"],
                "profile": artifact["profile"],
                "eligible": sum(t["decision"] == "eligible" for t in artifact["tools"]),
                "rejected": sum(t["decision"] == "rejected" for t in artifact["tools"]),
            },
            indent=2,
        )
    )


@policy_app.command("review-plan")
@_reports_backend_outage
def policy_review_plan(
    report: Path = typer.Argument(..., help="Tracegraph review-candidate report."),
    agent: str = typer.Option(..., "--agent", help="Authored agent identity."),
    profile: str = typer.Option(
        selector.DEFAULT_PROFILE,
        "--profile",
        help="Named selector profile: strict / review / explore.",
    ),
    annotations: Path | None = typer.Option(
        None,
        "--annotations",
        help="Annotation sidecar (default: REPORT.toolgraph-review.json).",
    ),
    output: Path = typer.Option(..., "--output", help="Private review-plan artifact path."),
) -> None:
    """Project accepted trace evidence into a human-required policy work plan."""
    _selector_profile(profile)
    try:
        artifact = build_policy_review_plan(
            report=report,
            agent=agent,
            profile=profile,
            annotations=annotations,
        )
        digest = atomic_write_private(output, canonical_json_bytes(artifact))
    except (PolicyReviewPlanError, RuntimeError, OSError, ValueError) as exc:
        typer.echo(f"ERROR: {redact_text(exc)}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(
        json.dumps(
            {
                "kind": artifact["kind"],
                "output": str(output),
                # Prefixed like preflight/control-preflight: artifact_digest
                # follows SyncMill's ^sha256:[0-9a-f]{64}$ contract; bare hex
                # is reserved for bundle_digest (memtomem-stm).
                "artifact_digest": f"sha256:{digest}",
                "graph_state": artifact["graph_state"],
                "agent": artifact["agent"],
                "profile": artifact["profile"],
                "candidates": len(artifact["candidates"]),
                "decision_mode": artifact["decision_mode"],
            },
            indent=2,
        )
    )


def _review_candidate_failure(exc: ReviewCandidateError) -> NoReturn:
    typer.echo(f"ERROR: {redact_text(exc)}", err=True)
    raise typer.Exit(code=1)


@review_candidates_app.command("list")
def review_candidates_list(
    report: Path = typer.Argument(..., help="Tracegraph review-candidate report."),
    annotations: Path | None = typer.Option(
        None,
        "--annotations",
        help="Annotation sidecar path (default: REPORT.toolgraph-review.json).",
    ),
    status: ReviewDisposition | None = typer.Option(
        None,
        "--status",
        help="Filter by current disposition: open / accepted / dismissed.",
    ),
) -> None:
    """List validated candidates merged with Toolgraph's local review history."""
    try:
        payload = list_candidates(
            report,
            annotations_path=annotations,
            status=status.value if status is not None else None,
        )
    except ReviewCandidateError as exc:
        _review_candidate_failure(exc)
    typer.echo(json.dumps(payload, indent=2))


@review_candidates_app.command("annotate")
def review_candidates_annotate(
    report: Path = typer.Argument(..., help="Tracegraph review-candidate report."),
    candidate_id: str = typer.Argument(
        ..., help="Stable UUIDv5 candidate id printed by `review-candidates list`."
    ),
    status: ReviewDisposition = typer.Option(
        ...,
        "--status",
        help="New disposition: open / accepted / dismissed.",
    ),
    reviewer: str = typer.Option(..., "--reviewer", help="Human reviewer identity."),
    note: str | None = typer.Option(
        None,
        "--note",
        help="Optional local review note (single line, at most 500 characters).",
    ),
    annotations: Path | None = typer.Option(
        None,
        "--annotations",
        help="Annotation sidecar path (default: REPORT.toolgraph-review.json).",
    ),
) -> None:
    """Append a human disposition; never change graph policy or manifests."""
    try:
        reviewer = normalize_reviewer(reviewer)
    except ReviewCandidateError as exc:
        raise typer.BadParameter(str(exc), param_hint="--reviewer") from exc
    try:
        note = normalize_note(note)
    except ReviewCandidateError as exc:
        raise typer.BadParameter(str(exc), param_hint="--note") from exc
    try:
        payload = annotate_candidate(
            report,
            candidate_id,
            disposition=status.value,
            reviewer=reviewer,
            note=note,
            annotations_path=annotations,
        )
    except ReviewCandidateError as exc:
        _review_candidate_failure(exc)
    typer.echo(json.dumps(payload, indent=2))


if __name__ == "__main__":
    app()
