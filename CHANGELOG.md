# Changelog

All notable changes to Toolgraph are documented here.

## Unreleased

### Upgrading from 0.2.0

- There is no implicit backend any more (ADR-0017). 0.2.0 chose Neo4j at
  `NEO4J_URI` whenever neither `TOOLGRAPH_BACKEND` nor a `backend` field in
  the runtime config file named one; now a command that opens the graph
  exits 1 with ``ERROR: no backend configured: run `toolgraph init`, or set
  TOOLGRAPH_BACKEND to ladybug or neo4j``, `doctor` reports a Configuration
  FAIL (`backend_source: none`), and `serve` and `python -m
  toolgraph.server.app` exit 1 with that line before serving. `NEO4J_*`
  variables alone no longer select Neo4j. A deployment that relied on the
  fallback sets `TOOLGRAPH_BACKEND=neo4j` and installs `toolgraph[neo4j]`. A
  `.env` copied from `.env.example` already sets it; a hand-written `.env`
  with only `NEO4J_*` lines does not. A `--config` file without `backend`
  also meant Neo4j; it is now refused with exit 1 and a line naming the file,
  and the environment is still not consulted.
  `serve` also refuses at startup, with one line and exit 1, an unknown
  `TOOLGRAPH_BACKEND`, a backend variable that conflicts with the config file
  and an unreadable or malformed config file found through
  `TOOLGRAPH_CONFIG` or the working directory; 0.2.0 started in all four
  cases and failed each tool call instead. (A malformed `--config` file was
  already refused before serving, exit 2.) A missing `neo4j` extra or an
  unreachable backend still surfaces per tool call.
  Library callers that read `toolgraph.config.settings` with nothing
  configured get `RuntimeConfigurationError`; `Settings(backend=...)` is
  unaffected. `config.configure(path)` still reads the file at once but no
  longer builds the settings: they are built when `config.settings` is next
  read, so a file without `backend` is refused there, and a `NEO4J_*`
  variable changed in between is the one used. Read `config.settings` right
  after `configure()` to keep 0.2.0's timing.
- The Neo4j driver is now the `neo4j` extra, and Ladybug is a core
  dependency (ADR-0017). A Neo4j deployment installs `toolgraph[neo4j]`
  (`uv sync --extra neo4j` from a checkout). Without it, a command that
  opens a Neo4j graph exits 1 with one line: `ERROR: backend=neo4j requires
  the 'neo4j' extra: install with ...`. `toolgraph[ladybug]` still resolves,
  as an empty extra, so existing install recipes keep working. Installing
  Toolgraph now installs Ladybug on every platform. Ladybug 0.18.3 ships
  wheels for CPython 3.12–3.14 on macOS 15+ (arm64, x86_64), manylinux
  (x86_64 glibc 2.27+, aarch64 glibc 2.26+), musllinux 1.2 and Windows
  (amd64, arm64). Elsewhere, for example on macOS 14, pip builds Ladybug
  from its source distribution; that build has not been verified.
- Neo4j 5.7 is now the minimum server version. Toolgraph asks the server
  not to send `UNRECOGNIZED` notifications, which needs Bolt 5.2; against an
  older server every command that opens the graph exits 1 with `Neo4j
  server is older than 5.7, the minimum Toolgraph supports; upgrade the
  server` (measured on 5.6 and 4.4). Before this change the README
  walkthrough ran on 5.6, with notification noise, and on 4.4 seven of its
  eighteen commands already failed with Cypher syntax errors. Library
  callers get `BackendConfigurationError` from the same seams as below.
- `toolgraph crawl` now requires the `servers` key in `servers.yaml`. A
  blank file, a comment-only file or `{}` exits 1 with `Invalid servers
  config` before any server is contacted; 0.2.0 treated those three as an
  empty fleet and, under default pruning, retired every crawled server.
  (`servers: null`, mapping or scalar values and unknown keys were already
  rejected; a YAML set, `!!set {}`, is newly rejected instead of being
  coerced to an empty list.) To retire the whole fleet on purpose, write `servers: []`
  (library callers: `ServersConfig(servers=[])`); to crawl nothing and keep
  the graph, add `--no-prune`. See ADR-0014.
- Crawls behave differently in three smaller ways. A server whose catalog
  exceeds 10,000 records or 4 MiB of retained record JSON now fails its
  crawl instead of loading (ADR-0016); raise the limit with
  `--max-catalog-records` / `--max-catalog-bytes`, or pass
  `budget=CatalogBudget(...)` from a library call. A failed server's reason,
  printed by `crawl` and returned in the failures of `crawl_all` /
  `crawl_all_async`, names the underlying exceptions instead of
  `ExceptionGroup: unhandled errors in a TaskGroup`. And `crawl_all` /
  `crawl_all_async` raise `ValueError` for a `concurrency` that is not a
  positive `int`: `0` used to hang, while a float or `bool` was accepted.
- Read commands and MCP read tools no longer create a missing Ladybug
  database: they exit 1 with one `BACKEND_UNAVAILABLE` line (MCP:
  `backend_unavailable`). A Ladybug file without Toolgraph's schema, or with
  another schema version, is refused by graph query commands, with a line
  naming `init-schema` or the version mismatch, and by MCP read tools, as
  `backend_misconfigured` with cause `schema_not_initialized` or
  `schema_version_mismatch`; `check` tests connectivity only and still
  prints `OK` for it. A script that let a read command
  create the file runs `toolgraph init` or `init-schema` first. Writers
  (`crawl`, `ingest-manifest`, `init-schema`) still create it.
- `toolgraph check` and `doctor`'s Connectivity check now look in
  `NEO4J_DATABASE`. With a database the server does not have, `check` exits
  1 where 0.2.0 printed `OK`, and Connectivity FAILs where it passed. A
  health check built on either now reports that setup as broken. Separately,
  `doctor` and `doctor --json` report exactly one `Configuration` check;
  when the settings could not be built, 0.2.0 reported a PASS or WARN
  `Configuration` followed by a FAIL with the same name.
- MCP tools report backend configuration problems as a new error kind,
  `backend_misconfigured`, with `retryable: false` and a `cause`, where 0.2.0
  returned the SDK's generic `Error executing tool <name>`. A client that
  switches on `error_kind` should handle it; `backend_unavailable` is
  unchanged. See the first bullet under Changed for the causes.
- Code that calls `driver.session()`, `acquire_readonly_session()` or
  `verify_connectivity()` and catches `neo4j.exceptions.AuthError` (or a
  `ClientError` for expired credentials, the authentication rate limit or
  `Database.DatabaseNotFound`) now gets
  `toolgraph.graph.driver.BackendConfigurationError`, with the driver error
  as `__cause__`; the CLI prints these as one `ERROR:` line, exit 1, instead
  of a traceback. `BackendConfigurationError` now takes exactly one message
  and an optional keyword `cause`, so constructing it with no arguments or
  with two positional arguments raises `TypeError`.

### Added

- **A per-server catalog budget for crawls** (ADR-0016). Each server's
  retained tools and resources are capped at 10,000 records and 4 MiB of
  record JSON (UTF-8). A server over budget fails like any other crawl
  failure, so no partial catalog is loaded and ADR-0007's failure
  protection applies. Configure it with `toolgraph crawl
  --max-catalog-bytes N --max-catalog-records N`, or `CatalogBudget` for
  `crawl_server` / `crawl_all`. It bounds retained data, not transport
  buffering or peak memory. Crawl failure messages now show the underlying
  error instead of `ExceptionGroup: unhandled errors in a TaskGroup`.

- **Read-only policy impact previews**: `toolgraph policy impact proposed.yaml
  SERVER::TOOL... --agent AGENT` compares current and hypothetical selector
  decisions with evidence. JSON reports bind graph state, governance digests,
  profile and scoped catalog; `toolgraph policy check impact.json` checks the
  proposed-side regression expectations against current state. Neither command
  applies policy. See [the impact guide](docs/policy-impact.md).

- **A first-run notebook**, `docs/notebooks/01-first-policy-bundle.ipynb`,
  walks the Beginner Guide path one command at a time inside a temporary
  directory. It goes from the quickstart to a compiled bundle, then removes and
  restores an evidence pointer and runs the audits. It is committed with its
  outputs. `tests/test_notebooks.py` checks those outputs row by row, and a new
  CI `notebooks` job re-executes the notebook from a wheel install. The job
  and the regeneration recipe install `nbclient` and `ipykernel` from a new
  `notebook` dependency group in `uv.lock`, so Dependabot updates them.

### Changed

- MCP tools report a backend configuration problem as a typed result
  instead of the SDK's generic `Error executing tool <name>`. Missing
  `neo4j` extra, rejected Neo4j credentials, a missing `NEO4J_DATABASE`, a
  Neo4j server older than 5.7, a missing Ladybug and a database file that is
  not a Ladybug database now return `isError: true` with `structuredContent`
  `{"error_kind": "backend_misconfigured", "retryable": false, "cause": ...,
  "message": ...}`. The message is fixed per cause; the path or status code
  goes to the server log as one WARNING record instead of a traceback. On
  stdio, where the SDK's log handler wraps at 80 columns, a corrupt Ladybug
  file went from 284 stderr lines to 9. A consumer that keys on
  `error_kind == "backend_unavailable"` sees no change: these failures still
  are not outages.

- Toolgraph no longer chooses a backend when nothing names one (ADR-0017
  step 3). The backend comes from `TOOLGRAPH_BACKEND` or the runtime config
  file's `backend` field, or with `--config` from that file alone; otherwise
  commands that open the graph, `serve` and the MCP server module refuse with
  one line, and `doctor` reports a Configuration FAIL. Commands that do not
  open the graph (help, `--version`, `example init`, `review-candidates`,
  `init`) are unaffected.

- Ladybug moves into the core dependencies and `neo4j` to a `[neo4j]`
  extra (ADR-0017 step 2). No Ladybug path imports the Neo4j driver, so a
  Ladybug install runs without it: a new required CI job, `no-extras`,
  installs the wheel with no extras and checks that. Every Neo4j path
  imports the driver through one guarded loader, so a missing extra is a
  configuration error naming `toolgraph[neo4j]` rather than an
  `ImportError`.

- `toolgraph doctor` and `doctor --json` report exactly one
  `Configuration` check. When the settings cannot be built (for example a
  `TOOLGRAPH_BACKEND` that contradicts the config file), that check FAILs
  with the error and keeps the file details such as `backend_source`;
  previously a passing `Configuration` check was followed by a second,
  failing one with the same name.

- `servers.yaml` must declare its fleet explicitly (ADR-0014). The
  `servers` key is required and must be a list; `servers: []` is the only
  way to declare an empty fleet. A YAML set (`!!set {}`) is rejected too:
  the field is strict, so it is not coerced to an empty list. The CLI
  diagnostic, exit code and `--prune/--no-prune` semantics are unchanged.
  ADR-0007's "empty file = empty fleet" consequence is amended accordingly.

- The fleet duplicate checks (`duplicate_name_overrides`,
  `duplicate_identities`) count names in one pass, so their cost grows
  linearly with fleet size instead of quadratically. Results are unchanged.
  `scripts/benchmark_duplicate_checks.py` is a repeatable size sweep; on one
  developer machine an 8,000-server fixture went from about 300 ms to well
  under a millisecond per check.

- `SECURITY.md` now states that security fixes cover the latest published
  Toolgraph release only, with no backports, instead of naming the `0.0.x`
  line the policy was written for.

### Fixed

- Commands and MCP tools that only read the graph no longer create a missing
  Ladybug database. Pointed at a path that was never initialized, the read
  commands (`check-access`, `unmapped-tools`, `audit-report`, `drift` and
  the rest) created the file, then failed with a 72-98 line `Binder
  exception: Table ... does not exist` traceback, and `check` printed `OK`
  for that path from then on. Now a missing file gives one line, `ERROR:
  backend unavailable: BACKEND_UNAVAILABLE: Ladybug database <path> does
  not exist or is not a file`, and MCP returns `backend_unavailable`, as
  `check` already did. A Ladybug file without Toolgraph's schema gives
  `ERROR: backend schema is not initialized or incomplete — run 'toolgraph
  init-schema'`, and MCP returns `backend_misconfigured` with cause
  `schema_not_initialized` (`schema_version_mismatch` for another schema
  version). A read that opened the database and fails this way closes it,
  so `init-schema` from another process can repair it while `serve` runs
  (a process that already wrote keeps its handle).
  Neo4j is unchanged: an uninitialized Neo4j database still answers reads
  with empty results.

- `toolgraph check` and `doctor`'s Connectivity check now select
  `NEO4J_DATABASE`. With a database the Neo4j server does not have, `check`
  printed `OK` and the next graph command exited 1 with a
  `DatabaseNotFound` traceback; now `check` and every graph command exit 1
  with `ERROR: Neo4j database named by NEO4J_DATABASE does not exist on the
  server`, and `doctor` reports that line as a Connectivity FAIL
  instead of a Connectivity PASS followed by two raw driver errors.
  Connectivity is checked with a read query in the configured database,
  in READ access mode like the driver's own check (measured on Neo4j 5.7
  and 5.26).

- README's exit-code section no longer gives a rejected Neo4j password as an
  error that ends in a Python traceback; since the authentication change
  above it prints one `ERROR:` line. The exit-code table lists rejected
  credentials, a missing Neo4j database and a server older than 5.7 among
  the one-line errors, with the `init` and `serve` exceptions spelled out
  for every class.

- Neo4j no longer floods stderr with driver notifications. Every query that
  named a label, property or relationship type the graph did not hold yet
  logged a `Received notification from DBMS server` line, so a fresh
  database printed eight before `doctor`'s diagnosis and a README walkthrough
  printed 92. The driver now disables the `UNRECOGNIZED` classification.
  Warning-level notifications, such as deprecations, still reach stderr;
  performance notifications are still logged at INFO. Requires Neo4j 5.7
  (see Upgrading).

- `.env.example` no longer presents `TOOLGRAPH_SERVERS=servers.yaml` and
  `TOOLGRAPH_GOVERNANCE=governance.yaml` as configured paths. Those values
  are the built-in defaults, and the repository root has neither file, so
  after `cp .env.example .env` a bare `toolgraph crawl` still exits 1 with
  `Invalid servers config at servers.yaml` (`ingest-manifest`: `Invalid
  governance config at governance.yaml`). The lines are now commented out
  with a note to pass `--servers examples/servers.yaml` explicitly. They are
  not pointed at `examples/`: a bare `crawl` would then reconcile the graph
  against the example fleet and retire every other server.

- A Neo4j that rejects the credentials no longer makes CLI commands print a
  native traceback. Wrong, expired or rate-limited credentials and an
  expired auth-provider token are typed at the driver seam as a
  configuration error and reported as one line, `ERROR: Neo4j refused
  authentication (<Neo4j status code>); check NEO4J_USER, NEO4J_PASSWORD
  and the account's auth provider`, exit 1. The driver's own message is not
  echoed. It is not treated as a retryable outage, and `doctor`'s schema
  check keeps that message instead of rewording it as a catalog permission
  problem. Query-level `Forbidden` errors are unchanged.

- `crawl_all` and `crawl_all_async` now reject a `concurrency` that is not a
  positive integer (`0`, negatives, `bool`, floats, strings, `None`) with a
  `ValueError` before any server is contacted. Previously `concurrency=0` made
  every crawl of a non-empty fleet wait forever, because the per-server
  timeout only starts once the semaphore is held. The CLI always used the
  default of 5 and is unaffected.

## 0.2.0 - 2026-10-15

Third alpha. Read **Upgrading from 0.1.0** below before installing: it lists
seven changes an existing setup, checkout or embedding program can notice.
The one most installations meet first is that commands now refuse a backend
variable that disagrees with the config file.

### Upgrading from 0.1.0

- Commands stop with `ERROR: conflicting backend configuration (...)` when
  `TOOLGRAPH_BACKEND` or `TOOLGRAPH_DB_PATH` disagrees with the config file;
  0.1.0 silently let the variable win. Pass `toolgraph --config <file>
  <command>`, or unset the variable; when it comes from `.env`, remove it
  there, since the next command loads `.env` again. See the first bullet
  under Changed.
- `control-preflight` can now return `advisory_warn` for a plan that 0.1.0
  answered with `advisory_allow`. See the `control-preflight` bullet under
  Changed before relying on `advisory_allow` as a gate.
- A program that imports Toolgraph no longer gets `./.env` loaded for it.
- `toolgraph check` no longer creates a missing Ladybug database; it exits 1.
  A script that used `check` to initialize should run `toolgraph init` or
  `init-schema` first. See the `check` bullet under Changed.
- Identities, control-plan strings and review notes that 0.1.0 accepted can
  now be refused as credential-shaped (for example `?access_token=` or `?credential=`). See
  the credential bullet under Changed.
- `GraphState.token()` and `queries.with_generation()` are gone; use the
  `graph_state` token results carry and `queries.with_graph_state()`. See
  Removed.
- The repository's `examples/policy_propose_from_observed.py`, which 0.1.0
  introduced, is gone; use `toolgraph policy propose`, which reads the live
  graph. See its entry under Removed for the provenance flags.

### Added

- **`toolgraph doctor [--json]`** checks the configuration file (present,
  valid JSON, mode 0600, and `backend_source`: which of `--config`, the
  environment, the config file or the built-in default decided the backend),
  backend connectivity, the physical schema and the graph state. It opens the
  database read-only — on Neo4j in READ access mode — and redacts what it
  reports. It exits 1 when any check fails; a warning alone exits 0.
- **`toolgraph ingest-manifest --dry-run [--json]`** reports the full delta a
  manifest would apply without changing the database: agents, policies,
  grants and `granted_by`, data access, `GOVERNED_BY`, expected exceptions,
  provenance, and the resources and tools that would be pruned for having no
  edges left. It exits 1 when the manifest would be rejected; with `--json` it
  then prints a rejection report with the warnings, not a delta (the delta
  lists stay empty because no comparison was made). A missing database file is refused and none is
  created; an existing but uninitialized one is refused with `init-schema`
  guidance. On Neo4j the preview
  session opens in READ access mode, so the server rejects an auto-commit write
  issued through it (verified on Neo4j 5.26 over `bolt://`); that is an access
  mode, not access control, and does not replace a read-only role. `--json`
  requires `--dry-run`. Plain `ingest-manifest` behaves as before.
- **`toolgraph policy propose GOVERNANCE OBSERVED`** proposes grant changes to
  `governance.yaml` from an observation-window JSONL export
  (`contracts/observation-window.schema.json`), resolving grants against the
  live graph. It never writes to the graph and never edits the manifest. The
  export's envelope must match the manifest and the live graph;
  `--revalidate-current` accepts a moved generation and re-checks additions
  against the current strict catalog, and `--unverified-provenance` accepts a
  missing envelope or identity and labels the result unverified. Other
  options: `--min-would-block` (default 3), `--window-label`, `--json`, and
  `-o/--output`, which cannot be combined with `--json` and writes the file
  atomically with mode 0600 (a failure before the replacement leaves an
  existing file as it was). It refuses, with a redacted `ERROR:` diagnostic
  and exit 1 rather than a traceback: an export over 50,000,000 bytes,
  malformed YAML, and any proposal row it would emit whose agent, tool or note
  is credential-shaped — that error names the position (`additions[0].tool`),
  never the value. See
  `docs/policy-propose.md`.
- **`control-preflight` reports potential exposures.** Each record in the new
  `potential_exposures` field joins an eligible reader tool on one plan node to
  an eligible writer tool on a distinct node downstream of it, with the
  resources involved (redacted) and a shortest witness path. It marks a
  potential path, not proof that data moved. The search is bounded: at most 100 records,
  64,000 bytes of records and 65,536 traversal steps; when it stops early the
  result also carries `truncated: true` and `total_candidate_pairs_found`,
  which is then a lower bound. The fields are additive to the v1 schema. See
  `docs/adr/0013-bounded-control-plan-preflight.md`.
- `toolgraph audit-report` JSON now carries the graph state it was read at
  (`graph_generation`, `graph_instance_id`, `graph_state`,
  `graph_state_verified`), as the MCP `audit_report` tool already did. The
  Markdown format is unchanged.
- `toolgraph example init` now writes a `README.md` into the generated
  directory. `docs/` is excluded from the sdist, so a user who installed from
  PyPI previously landed in a directory of three unexplained files with only
  the CLI's one-line hint to go on. The file lists the commands in order,
  states the expected outcome, and links the full guide.
- `examples/README.md` maps each `servers-*.yaml` / `governance-*.yaml` input
  to the script or workflow that consumes it, and to what it needs to run.
- A `Documentation` URL in the project metadata, so the beginner guide is
  reachable from the PyPI sidebar.

### Changed

- **An environment variable can no longer silently override `toolgraph
  init`'s config.** Without `--config`, `TOOLGRAPH_BACKEND` and
  `TOOLGRAPH_DB_PATH` used to win over `.toolgraph/config.json` (or the file
  `TOOLGRAPH_CONFIG` names). Copying `.env.example` (which sets
  `TOOLGRAPH_BACKEND=neo4j`) into a directory where `toolgraph init` had
  written a Ladybug config sent every command to Neo4j with no warning. When
  the two disagree on the backend or on the resolved database path, commands
  now stop with `ERROR: conflicting backend configuration (...)`. To migrate,
  pass `--config <file>` to choose the file, or unset the variable (remove it
  from `.env` if it is set there). Agreeing
  values, or only one of the two being set, behave as before. `--help`,
  `toolgraph init` and `toolgraph example init` are not blocked, so `init` can
  still be used to rewrite the file. `toolgraph doctor` reports which input
  decides the backend as `backend_source`.
- **Only the CLI reads `./.env`.** Importing `toolgraph` as a library used to
  load `.env` from the current directory into `os.environ`. The CLI, including
  `toolgraph serve`, still does; a program that embeds Toolgraph and relied on
  that should call `toolgraph.config.load_dotenv_file()` or set its own
  environment.
- **`toolgraph check` no longer creates a database.** It opens the configured
  Ladybug file read-only, so a mistyped `db_path` exits 1 with
  `BACKEND_UNAVAILABLE ... does not exist` instead of creating an empty
  database and printing `OK`. Run `toolgraph init` or `init-schema` first.
- **`control-preflight` can now warn where 0.1.0 allowed.** The decision is
  `advisory_warn`, not `advisory_allow`, when the plan has a potential
  exposure (see Added) or the exposure search was truncated. The exit code is
  still 0 for both. A consumer that gates on `advisory_allow` will see more
  warnings and should review them rather than treat them as failures of the
  tool. A result without the exposure fields, from 0.1.0, means "not
  checked", not "none found"; `total_candidate_pairs_found` on a truncated
  result is a lower bound. ADR-0013 states the decision rules.
- **Credential checks are shared and refuse more.** Agent and tool identities
  (preflight, `policy compile`, `policy review-plan`, `policy propose`) and
  strings in control plans are now also refused when they carry
  `?access_token=` or `?credential=`, a password URI whose username is an
  email address, or an `@` in a query after a port
  (`https://user@host:443?x=a@b`). `review-candidates annotate` notes that
  assign `secret=` or `access_token=` are now refused too. Error text
  now also hides `credential=...` and a bare `Bearer <token>`. Nothing that
  was refused or hidden before is accepted or shown now.
- Control-plan validation errors give the location and the reason, never the
  rejected value: `invalid control plan at nodes.0.principals.1: ...`, and a
  bad candidate tool is reported by its index.
- A file that is not a Ladybug database is now a non-retryable
  `BACKEND_CORRUPT` configuration error, printed as one `ERROR:` line. In
  0.1.0 it surfaced as a Python traceback from Ladybug.
- `config.configure()` closes the cached backend connection, so a library
  caller switching configs no longer keeps talking to the previous database.
- Deny-path rows (`check-access` evidence, `unsafe-tools`, `audit-report`,
  selector deny paths) now have a fixed order when rows used to tie: one
  tool under several DENY policies, one tool name on several servers, or a
  READS and a WRITES edge to the same resource. Ties are broken by tool key,
  policy and mode; before, the backend chose, and Ladybug and Neo4j could
  disagree. Rows that did not tie keep their order.

### Removed

- **`examples/policy_propose_from_observed.py`, the offline proposal script.**
  There is no longer a graph-free way to produce a proposal. Its replacement,
  `toolgraph policy propose` (see Added), applies the same row and proposal
  rules but resolves grants against the live graph, and checks the envelope
  against it too: an envelope the script accepted is refused when it lacks a
  graph instance id, or when its generation, digest or instance differs from
  the live graph. Re-export from the current graph, or pass
  `--revalidate-current` (a moved generation) or `--unverified-provenance`
  (missing identity) to proceed. The script was repository-only and never
  shipped in the distribution. Its tests now drive `toolgraph.policy_propose`.
- `GraphState.token()`, which nothing called and which returned a different
  set of keys than the `graph_state` token that results actually carry.
- `toolgraph.graph.queries.with_generation()`, which had no production
  caller; use `with_graph_state()`. A strict read that sees the graph state
  change on every retry now raises `queries.GraphStateChurnError`, a
  `RuntimeError` subclass with the same message, so CLI output and exit codes
  are unchanged.

### Fixed

- Backend configuration errors print one `ERROR:` line and exit 1 instead of
  a Python traceback: an unknown `TOOLGRAPH_BACKEND`, a missing `ladybug`
  extra, and a schema-version mismatch. `toolgraph doctor` is the exception
  by design: it reports the same failure as a FAIL check (exit 1) in its text
  or JSON report.
- An outage or other backend error while reading the schema version is no
  longer taken for a fresh database, so `init`, `init-schema`, `crawl` and
  `ingest-manifest` stop before running any DDL.
- `toolgraph check` no longer prints credentials embedded in `NEO4J_URI`, and
  `ingest-manifest` warnings and notices are redacted.
- `toolgraph init` with an unusable `--state-dir` prints `ERROR: cannot
  initialize ...` and exits 1 instead of a traceback raised from its cleanup;
  a partial database file it cannot remove is named in a warning.
- `policy review-plan` refuses a graph with no instance id instead of writing
  a plan its own schema rejects.
- `policy compile` (and `build_policy_bundle()`) refuses a graph state whose
  generation is not a non-negative integer — a boolean, float, string, null
  or negative value — instead of writing it into the bundle. Toolgraph's own
  graph always yields one; the check matters for a hand-edited graph or a
  library caller's own graph reader.
- Writing a private artifact — `init`'s config file, `preflight --out`,
  `control-preflight --out`, `policy compile --output`, `policy review-plan
  --output` — no longer reports failure when the new file was already in
  place and only re-applying its 0600 mode afterwards failed. The file is
  created 0600 before it is written, so that second step was redundant.
- `crawl` no longer prints `vNone` for a server that reports no version.
- The packaged quickstart governance manifest now carries `provenance` on
  every authored edge. The beginner guide claimed every authored edge had an
  evidence pointer and then told the reader to run `unbacked-edges`, which
  listed all three of them. The audit now comes back empty, and both the
  manifest and the guide say what that does and does not prove: the demo tools
  return strings and touch no file, so the READS/WRITES effects are synthetic
  and their evidence cites the fixture that defines them. An empty audit means
  every edge has a pointer, not that a claim is true. As a result `read_note`'s
  risk in the quickstart drops from 0.4 to 0.0 and the compiled bundle's
  digest changes.
- Beginner guide corrections (both languages): the shared-backend section
  needs a repository clone, since `docker-compose.yml` and `.env.example` ship
  with neither the package nor the generated quickstart; `policy compile`
  prints metadata describing the bundle, not the bundle; and the gateway step
  is labelled optional, matching the README's statement that memtomem-stm is
  not a required dependency.

### Dependencies

- The `pyjwt` security floor rises from `>=2.13.0` to `>=2.15.0`. 2.13.0
  carries 14 advisories published after it was set: PYSEC-2026-4140 through
  PYSEC-2026-4152 and PYSEC-2026-4183. PYSEC-2026-4141 and PYSEC-2026-4183
  are fixed in 2.15.0, PYSEC-2026-4146 lists 2.13.0 as its last affected
  version, and the rest are fixed in 2.14.0. The floor is what keeps an
  install from PyPI from resolving an older version; the lock already resolves
  a newer one. Toolgraph does not import `pyjwt` itself; it is pinned for the
  MCP SDK's dependency tree.

### Testing

- The `sse` crawl transport and auth-header forwarding on both HTTP
  transports are now exercised end to end, at the lockfile version and at the
  declared MCP SDK floor. Neither had been run before; no runtime behaviour
  changed.

## 0.1.0 - 2026-09-10

Second alpha. Upgrading from `0.0.1` takes three things: the MCP SDK 2.x, one
new field for anything caching MCP responses, and — if you generated a
quickstart with `0.0.1` — a regenerated server script. Read **Upgrading from
0.0.1** below before installing.

### Upgrading from 0.0.1

`toolgraph example init` copies a quickstart out of the package, so a
quickstart generated by `0.0.1` holds its own copy of the demo MCP server —
one that imports `mcp.server.fastmcp`. Upgrading that environment installs MCP
2.x and the copied server stops importing, so the next `toolgraph crawl`
fails. Updating the package does not touch a directory it already wrote, and
`example init` refuses an existing destination on purpose.

To migrate one, generate a fresh quickstart beside it and move your own work
across:

```bash
toolgraph example init quickstart-0.1.0
# Keep your authored governance and your database; take the new server script.
cp quickstart/governance.yaml quickstart-0.1.0/governance.yaml
cp -R quickstart/.toolgraph   quickstart-0.1.0/.toolgraph
```

`servers.yaml` records the interpreter that generated it, so regenerating from
the upgraded environment already points at the right one; check it only if you
generated from somewhere else. Re-run `toolgraph crawl`, then
`toolgraph ingest-manifest`, to confirm.

Verified end to end against the published `0.0.1`: a quickstart it generated
crawls cleanly on `0.0.1`, fails on the next crawl once the environment holds
`0.1.0`, and returns the same eligibility verdicts after these steps.

### Added

- `examples/policy_propose_from_observed.py` reads a window of observed gateway
  usage and prints a reviewable `governance.yaml` grant proposal. It never
  writes governance. Repository-only — it is not part of the distribution — and
  deliberately narrow: it requires an envelope declaring the governance digest
  it was recorded against and refuses a mismatch, refuses an export with no
  envelope unless `--unverified-provenance` is given, proposes a removal only
  from an explicit zero-call row, and accepts only fully qualified timestamps
  with an explicit UTC offset. Digest equality shows what the export declared,
  not where its rows came from; issue #89 tracks what a real binding needs.

### Changed

- **Breaking:** requires the MCP SDK 2.x (`mcp>=2.1.1,<3`). Environments pinned
  to `mcp` 1.x can no longer install this package, because 2.x removed
  `mcp.server.fastmcp` and renamed every model field from camelCase to
  snake_case. The MCP *wire* format is unchanged, so a client that only speaks
  the protocol needs no change; that is not the same as no change for anything
  written against the Python SDK, caching responses, or reading exception text.
- An unexpected exception raised by a toolgraph tool body no longer puts its
  message on the wire. Only errors raised deliberately as `ContractError` (for
  example an unknown selector profile) are forwarded; the rest keep the SDK's
  generic text, so an internal failure cannot disclose filesystem paths, query
  bodies or credential-shaped values. Argument-validation failures still echo
  the caller's own input, as the MCP SDK intends.
- **Breaking:** an MCP response whose read could not be bracketed by a stable
  graph state now carries `graph_generation: null` and
  `graph_state_verified: false` instead of the generation read afterwards.
  Labelling such a result with a generation it was never read at let a verdict
  from before a permission change be cached under the generation that changed
  it, while the tool documentation tells callers to cache per generation.
  Successful reads gain `graph_state_verified: true`, so the absence of a
  guarantee is never inferred from a missing field. Null is not self-enforcing
  — `None` is a usable cache key — so the obligation to skip the cache and to
  attribute nothing to a graph state is the consumer's; ADR-0004 and the README
  state it, and a consumer that turns eligibility into an authorization
  decision should reject or retry such a response.

## 0.0.1 - 2026-09-09

Initial public alpha release.

- Crawl MCP servers into a graph and combine factual capability data with
  operator-authored governance.
- Explain access, reachability, drift, blast radius, and tool selection.
- Compile deterministic policy bundles for an independent runtime gateway.
- Support local Ladybug and shared Neo4j backends.
- Reject unknown manifest fields and duplicate YAML keys before graph mutation.
- Persist only credential-free endpoint labels and redact failure diagnostics.
- Ship an install-only, offline quickstart through `toolgraph example init`.
- Give MCP consumers a typed `backend_unavailable` error envelope for transient
  graph outages while keeping contract and internal errors fail-loud. Outages
  are typed at the backend seam, and `retryable` follows the tool's advertised
  read-only annotation instead of being assumed, failing closed to `false`
  whenever that annotation cannot be read.
- Add a body-free `toolgraph.control-plan` v1 contract and advisory
  `control-preflight` command that lints bounded DAG topology and evaluates all
  principal/tool surfaces under one collision-safe graph-state bracket.

### Stabilization (2026-09-08)

- Resolve ingest references in bounded batches shared with the selector,
  preserving atomic application, diagnostic order and ambiguous identities.
- Require explicit commit SHAs for ecosystem smokes and clean matching HEADs
  for gateway smokes; record dependencies and artifact evidence.
- Include Ladybug in `make install` and refresh the pip audit dependency lock.
- Reconcile historical integration and selection roadmaps with shipped code.

### Implementation review fixes (2026-09-07)

- Handle empty crawl and manifest collections on both backends while retaining
  stale-edge reconciliation, revocation, and transactional generation updates.
- Preserve opaque artifact identities, including ordinary URI queries/fragments.
  Reject credential-shaped identities and redact only evidence fields, including
  policy review paths. Existing v1 schemas and decision vocabularies are unchanged.
- Use collision-safe `v2:` exception keys and deterministic last-entry-wins
  metadata for duplicate logical exceptions. No physical schema migration is
  required: existing keys remain readable. Re-ingest the original governance
  manifest to rebuild keys and restore entries lost to old key collisions, then
  recompile policy bundles. Old artifacts cannot reconstruct lost manifest rows.
- Require control-plan fields exactly as published: missing `maximum_cycles`,
  explicit null bounds, string/boolean coercions are rejected. JSON integer
  numbers such as `2.0` remain accepted; additive same-major fields remain valid.
- Compile catalogs in chunks of at most 4,096 candidates under one strict state
  bracket; reject incomplete or conflicting evaluations. Injected readers own
  all state reads, and adapters without the optional `evaluate` capability work.
- Load runtime settings lazily so help/version and explicit configuration remain
  usable when the default config is broken. Invalid selected configs fail clearly.
- Reject naive and sub-minute timestamps before preflight/review I/O; development
  validation now installs and checks the RFC 3339 format validator.

### Upgrade note for pre-release users (historical)

`driver.session()` and `driver.verify_connectivity()` now raise
`BackendUnavailableError` where they previously surfaced the raw `neo4j`
`ServiceUnavailable`, `SessionExpired`, `ConnectionAcquisitionTimeoutError`, or
`DatabaseUnavailable`. Code embedding toolgraph as a library and catching those
types directly must catch `BackendUnavailableError` (or its `BackendLockedError`
subclass); the original exception is preserved as `__cause__`. Configuration,
authentication, and Cypher/client errors are unaffected, as is `get_driver()`.
Successful CLI output and query result shapes are unchanged; an unhandled
outage now names `BackendUnavailableError` in the traceback.

`driver.is_backend_unavailable()` narrowed to match: it now recognizes only
`BackendUnavailableError` (anywhere in the cause chain), not the raw `neo4j`
types it previously classified. Code that pairs the still-raw `get_driver()`
escape hatch with `is_backend_unavailable()` to decide whether to retry will
silently stop matching — catch `BackendUnavailableError`, or route the call
through `driver.session()` so the seam types it.

Earlier snapshots could persist stdio arguments or complete HTTP URLs in graph
endpoint/evidence properties. Reset and rebuild pre-release graphs after
upgrading. If any argument or URL contained a credential, rotate it as well.
