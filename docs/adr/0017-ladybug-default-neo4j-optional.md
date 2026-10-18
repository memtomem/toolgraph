# 0017 — Ladybug becomes the default backend; Neo4j becomes an optional extra

Status: Accepted · Date: 2026-10-18 · Accepted: 2026-10-18

Amends [ADR-0011](0011-embedded-ladybug-backend.md).

## Context

ADR-0011 made `toolgraph init` write a Ladybug configuration. It kept Neo4j
in two places: as the backend chosen when nothing names one, and as the core
dependency, with Ladybug an optional extra "until the supported platform
matrix and full conformance suite pass".

Those two places point the opposite way from everything else. `init`, the
quickstart, the demo scripts, the notebook and every CI job that runs
Toolgraph install Ladybug. Meanwhile `config._backend_default()` returns
`neo4j` whenever neither `TOOLGRAPH_BACKEND` nor a `backend` field in the
runtime config file names a backend. That covers a command run outside an
initialized directory, and also a config file without the field. Every
install pulls in the Neo4j driver.

Neo4j-specific defects keep arriving. In this round alone: rejected
credentials printed as a traceback (#133); notification noise, which forced
the 5.7 server floor (#145); `check` passing on a missing database (#174);
and the untested 5.7 floor (#175).

Measured on 2026-10-18 at main `2ccc6fa`, with the repository's
`examples/servers.yaml` and `examples/governance.yaml`. Each command ran as
`toolgraph <command>` in a fresh directory.

- **Platform matrix.** `artifact-smoke` installs the built wheel with
  `[ladybug]` on Ubuntu (Python 3.12, 3.13, 3.14), macOS and Windows (3.12).
  It runs `example init`, `init`, `crawl`, `ingest-manifest --strict-drift`
  and `policy compile`, and it is required through `ci-required`. These
  installs also carry the core Neo4j driver, so they do not show that
  Toolgraph runs without it.
- **README walkthrough on Ladybug**, with no `TOOLGRAPH_*` or `NEO4J_*`
  variables set. This sequence gave 0 failures and 0 stderr lines:
  - `init`
  - `crawl --servers examples/servers.yaml`
  - `ingest-manifest --governance examples/governance.yaml`
  - `doctor`
  - `check`
  - `unsafe-tools public-bot`
  - `check-access public-bot read_file`
  - `blast-radius file:///private/secrets.env`
  - `unmapped-tools`
  - `orphan-policies`
  - `unbacked-edges`
  - `drift`
  - `destructive-unsafeguarded`
  - `annotation-contradictions`
  - `audit-report`
  - `rank-features public-bot filesystem::read_file`
  - `eligible-tools public-bot filesystem::read_file --profile strict`
  - `selection-explain public-bot filesystem::list_directory --profile strict`
  - `policy compile --agent public-bot --profile strict --output policy-bundle.json`
- **Output parity.** The same crawl and manifest were loaded into Neo4j
  5.26 (`init-schema`, then the same `crawl` and `ingest-manifest`) and
  into Ladybug. Then 16 query invocations ran against each: the 13 query
  commands above, plus `unsafe-tools public-bot --all`,
  `check-access ops-agent read_file` and `unbacked-edges
  --include-grants`. Stdout was compared byte for byte. It was identical
  for 15. For `audit-report`, `graph_instance_id` and
  `graph_state.instance_id` differed and nothing else did; the instance id
  is random per database.
- **Conformance gap.** Of 296 tests that start Neo4j, 132 are dual-backend
  parameters (`backend_graph`). The other 164 run on Neo4j only. They
  include `test_cli_contract.py` (21), `test_queries.py` (18),
  `test_classification.py` (15), `test_server.py` (14, the MCP server),
  `test_manifest.py` (14), `test_negative_truth.py` (13) and
  `test_selector.py` (12). ADR-0011's conformance condition is not met.

## Decision

1. **Conformance first.** Before any default changes, every Neo4j-only test
   whose behavior is backend-neutral moves to `backend_graph`. Tests that
   are Neo4j-specific by nature stay Neo4j-only, and each says why:
   - authentication and the Bolt error seam
   - server notifications and the server version floor
   - catalog permissions
   - native read-access enforcement (`test_config.py`)
   - the Neo4j fixture-isolation regression (`test_fixture_isolation.py`)

   Converting a test must not weaken its assertions. Steps 2–4 do not start
   until this lands.

   *Completed 2026-10-18* by #181 (CLI contract and MCP server), #182 (query
   layer) and #183 (write path). Of the 296 tests that start Neo4j, 3 still
   run on Neo4j only: native read-access enforcement and server notifications
   in `test_config.py`, and the fixture-isolation regression. No test needed a
   backend fix. One difference surfaced, in result shape: a cleared node
   property is absent on Neo4j and null on Ladybug, whose node dicts also
   carry `_ID` and `_LABEL`. The test helper `_tool_props` in
   `tests/test_annotations.py` now drops null values and keys beginning with
   `_` before the assertions compare node properties.
2. **Dependencies.** `ladybug` moves into the core dependencies and `neo4j`
   to a `[neo4j]` extra. `[ladybug]` stays as an empty extra, so existing
   recipes keep resolving: `toolgraph[ladybug]` in the README and guides,
   and `--extra ladybug` in the Makefile and CI.
   - The module-level driver imports in `graph/loader.py`,
     `graph/queries.py` and `manifest/ingest.py` become type-only.
   - `graph/driver.py` uses the driver at runtime: the outage exception
     tuple, `READ_ACCESS`, the notification constant, and the handlers in
     `_translating_backend_errors`, which also wraps Ladybug sessions. These
     move behind the Neo4j branch or are built lazily, so Ladybug
     acquisition, queries and failures never touch `neo4j`.
   - Selecting Neo4j without the extra fails with one `ERROR:` line that
     names the extra.
   - A new CI job, required through `ci-required`, installs the wheel with
     no extras and no dev group. It asserts that `import neo4j` fails, runs
     the Ladybug walkthrough through the CLI and an MCP tool call, and
     checks the missing-extra line. The dev group keeps
     `testcontainers[neo4j]`, so no existing job can show this.
3. **No implicit backend.** With `--config`, the backend comes from that
   file's `backend` field only, as today: the environment is not consulted,
   and a file without the field is refused, naming the file. Without
   `--config`, the backend is explicit when `TOOLGRAPH_BACKEND` is set or
   the ambient config file has a `backend` field. `NEO4J_*` variables
   alone no longer select Neo4j. Otherwise:
   - Commands that open the graph exit 1 with one line: run `toolgraph
     init`, or set `TOOLGRAPH_BACKEND` (with `--config`: add `backend` to
     that file).
   - `doctor` reports the state as a Configuration FAIL in its structured
     output.
   - `serve` and `python -m toolgraph.server.app` resolve the backend
     before serving and exit with the same line. They currently start
     without reading settings, so this is a new startup check, and the MCP
     masking described in #172 never applies to this case.
   - Commands that do not open the graph keep working: help, `--version`,
     `example init`, `review-candidates list` and `annotate`, and `init`,
     which writes the choice.
   - For library callers, reading `config.settings` raises the existing
     `RuntimeConfigurationError`. A `Settings(backend=...)` built
     explicitly is unaffected.
4. **Rollout.** The change ships as a minor release with an `Upgrading`
   entry. Deployments that relied on the fallback add
   `TOOLGRAPH_BACKEND=neo4j` (`.env.example` already sets it) and install
   `toolgraph[neo4j]`. The same change updates every repository-owned
   consumer of the fallback:
   - `scripts/ecosystem_smoke.py` and `scripts/gate_e_operational.py`,
     which pass only `NEO4J_*` to their child processes
   - `scripts/benchmark_ingest.py`, which reads ambient `config.settings`
     before it selects backends
   - the Makefile's Neo4j targets
   - the notebook's "Common pitfalls" cell
   - both beginner guides' Troubleshooting sections, which describe
     `backend_source: default`
   - the README

## Alternatives

- **Fall back to Ladybug instead of refusing.** This is quieter for new
  users. But a deployment that set only `NEO4J_*` would silently read an
  empty local graph, and a write command would create
  `.toolgraph/toolgraph.lbug` in whatever directory it ran from. Refusing
  only when `NEO4J_URI` is set would narrow the first problem and leave the
  second.
- **Keep the Neo4j fallback and only move the dependency.** That keeps
  today's confusion. The beginner guide's Troubleshooting section opens
  with "`backend_source: default` and a `Connectivity` FAIL for backend
  `neo4j`", the state a command reaches outside an initialized directory.
- **Drop Neo4j.** Ladybug allows one owning process (ADR-0011), so shared
  or multi-process deployments still need a server backend.
- **Warn for one release, then refuse.** Keep the Neo4j fallback with a
  deprecation warning for one release. That is gentler for deployments
  that set only `NEO4J_*`, but it delays the switch by a release, and a
  warning on stderr is easy to miss in automation. Chosen against on
  2026-10-18: step 3 refuses from the release that ships it, and the
  `Upgrading` entry carries the migration.

## Consequences

- A fresh `pip install toolgraph` no longer installs the Neo4j driver.
  Neo4j users opt in twice: with the extra, and with an explicit backend
  choice.
- `backend_source: default` disappears from `doctor`. The unconfigured
  state becomes an explicit failure instead of a guess.
- Neo4j-only maintenance stays, scoped to the extra: #174, #175 and the 5.7
  floor. CI keeps `test-neo4j`, and the count of Neo4j-only tests becomes
  the measure of that cost.
- #172 is not Neo4j-only. Ladybug's `BACKEND_CORRUPT` is also a
  `BackendConfigurationError` that MCP masks, so this ADR does not resolve
  it.
- There is no data migration, as in ADR-0011: crawled facts are rebuilt
  and authored facts re-ingested.
