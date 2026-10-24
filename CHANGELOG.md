# Changelog

All notable changes to Toolgraph are documented here.

## Unreleased

### Fixed

- A credential name at the end of a URL is caught in the forms the scrubber
  reads as an assignment, not only those the refusal rule does (#241). 0.4.0
  withheld `fetch http://h/password =secret` but printed
  `http://h/authorization=Basic secret` as `http://h secret`, and likewise
  `Authorization=Token`, `proxy_authorization=Basic`, `password :secret`,
  `password:[a, secret]` and `credential:{a: secret}`. Such a message is now
  withheld whole, and so is one where the name or its delimiter is
  percent-encoded (`http://h/pass%77ord :secret`,
  `http://h/password%3A[a, secret]`), judged against each URL where the
  rewrite finds it. More diagnostics are withheld for the same reason: a URL
  path ending in a credential name and a scheme word with text after it
  (`GET https://h/authorization=Basic returned 404`), or a bracket opening
  inside the URL after a `:` (`GET https://h/password:[id] returned 404`),
  as one after `=` already was. Not caught: an encoded delimiter that joins
  the text after a URL to the start of another one
  (`http://h/password %3Ahunter2://host`); an encoded name before a URL's
  scheme (`%70assword://hunter2`); and a repr-escaped URL followed by an
  assignment (`'http://h/password\t':hunter2`) (#246).
- Diagnostics hide everything after an `Authorization` name, not only the
  first word of its value (#245). 0.4.0 knew only the `Bearer`, `Basic` and
  `Token` schemes and took any other scheme word as the whole value, so
  `Authorization: Digest username=alice, response=hunter2` printed
  `Authorization=<redacted> username=alice, response=hunter2`; Negotiate,
  AWS4-HMAC-SHA256 `Signature=`, OAuth, SCRAM and Hawk parameters showed the
  same way, and so did a URL ending in the name
  (`http://h/authorization=Digest username=alice,response=hunter2` printed
  `http://h username=alice,response=hunter2`; it is now withheld). A name with
  a prefix is scrubbed too: `proxy_authorization:hunter2` and
  `HTTP_AUTHORIZATION:hunter2` were printed as given. Where such a value ends
  cannot be told from the text, so the rest of the message goes with it:
  `Authorization: Bearer abc; retrying in 5s` now prints
  `Authorization=<redacted>`, and a URL holding an Authorization name with
  text after it is withheld (`GET https://h/authorization=abc returned 404`).

## 0.4.0 - 2026-10-21

Fifth alpha. Read **Upgrading from 0.3.0** below before installing: it lists
eight changes an existing setup, script or embedding program can notice.
Most setups need no change, because the new refusals reject only identities
and notes that hold credential-shaped data. What most often notices is
tooling that reads Toolgraph's text: evidence paths, error lines and log
output are shown differently, and more often withheld whole.

### Upgrading from 0.3.0

- A governance manifest or `servers.yaml` holding a credential-shaped
  identity is refused at input (#217): an agent, policy id, tool ref or
  `granted_by`, a `governed_by` policy or tool-ref key, or a server's name.
  The manifest is rejected whole, so `ingest-manifest`, `policy propose` and
  `policy impact` exit 1, and so is a `servers.yaml`, before any server is
  contacted. A crawled server that reports such a tool name, or such a name
  when `servers.yaml` gives it none, fails its own crawl; the rest of the
  fleet still loads. The error names the field, never the value. Rename the
  identity. A graph keeps the values it stored earlier until they are
  re-ingested or reset.
- Identities refuse any URI with an `@` after its `://`, raw or as `%40`
  (#210, #215, #216), where 0.3.0 refused only a `user:password@` it could
  bound: agent ids, tool keys, run ids, candidates and graph instance ids in
  preflight, policy bundles, review plans, `policy impact` and `policy
  propose`, the run ids and tool keys of a Tracegraph review report, and a
  control plan's identities and keys. `policy propose` refuses to emit a
  proposal whose note would list a live-graph tool key holding userinfo.
- Review notes, reviewer names and control plans refuse a URI with a quote
  in its userinfo and an `@` after it (#210), such as
  `neo4j://u:"p w"@host`, which 0.3.0 accepted. A quoted URL followed by an
  address in the same text, `"https://h" mail a@b`, is refused too.
- Artifact evidence shows resource URIs differently (#212). A consumer that
  matches evidence text should expect `https://<redacted-uri>` when an `@`
  follows a URI's authority (`https://registry/@scope/pkg`), and
  `<redacted-uri>` for a percent-encoded scheme. A resource that cannot be
  parsed is now hidden instead of stopping the command, `export:///sink`
  keeps its three slashes, and the paths of `check-access`, `unsafe-tools`,
  `blast-radius`, `audit-report` and the matching MCP tools show resources
  and policy ids redacted. The raw `resource` and `policy` row values are
  unchanged. See the first bullets under Changed.
- Diagnostics read differently, and more of them are withheld whole (#206,
  #221, #223, #229, #232, #236). Validation errors are one line with fixed
  text and `<key>` for keys outside the schema; YAML errors read `invalid
  YAML at line L, column C: <what failed>`; MCP tool-call errors for a bad
  request no longer quote it; a CLI usage error is withheld whole when a
  token on the command line, or the tokens joined by spaces, is
  credential-shaped; and a printed path that is
  credential-shaped reads `<path withheld: credential-shaped>`. Messages
  holding an escaped credential, or a URL whose path ends in a credential
  name with text after it, are withheld: `Error accessing
  https://host/token: connection refused` now reads `details withheld (they
  contain credential-shaped data)`. Tooling
  that matches stderr or MCP error text should expect these forms; the
  entries under Fixed give each one.
- Logging changes for programs that embed Toolgraph (#206). Importing
  `toolgraph.server.app` into a process with no root log handler filters
  the root handlers the MCP SDK then creates, and running the Typer app
  in-process filters `logging.lastResort` for the rest of the process once a
  command reaches the root callback (root `--help`, `--version` and an
  unknown command do not). Every record through a
  filtered handler, the program's own included, is rebuilt: its message is
  scrubbed (`password=<redacted>`), each exception in its chain is shown as
  `<Type>: <message>` without a traceback, and `extra` attributes and `stack_info` are dropped,
  so a formatter that names an `extra` field fails with `Formatting field
  not found in record`. Configure logging before importing
  `toolgraph.server.app`, or replace the handlers it filtered. Handlers a
  program configured itself are not touched.
- Library callers: `ingest_governance`, `dry_run_ingest` and
  `load_crawl_result` re-validate the model they are given, and a model
  built with `model_construct()` or changed after validation that no longer
  validates raises `ValueError: <Model> does not validate; build it with
  <Model>.model_validate to see why` before the graph is opened.
  `build_policy_impact` already re-validated, and now raises that error
  instead of pydantic's. A YAML failure that PyYAML does not mark with a line
  and column raises `toolgraph.yaml_loader.YAMLContentError`, a `ValueError`
  (#223); marked failures still raise PyYAML's `MarkedYAMLError`.
- A durability warning escalated with `-W error` ends a command with one
  `ERROR:` line and exit 1, without a traceback or a JSON receipt (#129).
  `init` keeps the database it created, where 0.3.0 deleted it and kept the
  `config.json` naming it. The annotation warning's text now ends `do not
  retry: a retry appends a second event`.

### Changed

- Artifact evidence shows each resource URI as a whole value (#212). A
  resource is redacted before it is written into an evidence path, and
  resource fields (`resource`, `resource_uri`, `read_resource`,
  `write_resource`, `policy`, a resource `ref` and the `resources_*` lists of
  `ingest-manifest --dry-run --json`) are redacted as one value even when
  they hold whitespace. In free text, such as an exception reason, a URI ends
  at whitespace or at a `)`, as in 0.3.0. Consumers that match evidence can
  notice:
  - An `@` anywhere after a URI's authority, raw or as `%40`, hides
    everything after the scheme, since where an unquoted password ends cannot
    be told: `https://registry/@scope/pkg` is shown as
    `https://<redacted-uri>`. In free text the rest of the text goes too, as
    it already did after a quoted password, because it may hold the rest of
    the password; this includes a URI-shaped tool name inside evidence text.
  - A value whose scheme is percent-encoded (`https%3A%2F%2Fh/r`) is shown as
    `<redacted-uri>`, in free text with the rest of the text.
  - A resource value that cannot be parsed (`https://u:p@[::1/r`) is hidden
    as `<scheme>://<redacted-uri>` instead of stopping the command, and so is
    any evidence string that is nothing but such a URI, whitespace around it
    aside. A URI that cannot be parsed among other text, such as
    `see https://u:p@[::1/r` in an exception reason, still stops it, as in
    0.3.0.
  - An evidence path no longer loses the rest of its line when its resource
    holds a `)` or a quoted password: a resource `neo4j://u:p)ss@host/r` is
    shown as `(t) -READS-> (neo4j://host/r) -GOVERNED_BY-> (p:DENY)`, and a
    quoted one as `(t) -READS-> (neo4j://<redacted-uri>) -GOVERNED_BY->
    (p:DENY)`. In free text, a `)` followed by an `@` hides everything after
    the scheme.
  - `export:///sink` keeps its three slashes; it was shown as `export:/sink`.
- Paths in `check-access`, `unsafe-tools`, `blast-radius` and `audit-report`
  output, and in the matching MCP tools, show resources and policy ids the
  same way. The rows still carry the raw `resource` and `policy` values
  beside the path.
- `ingest-manifest` warnings that name a resource show it redacted.
- Identities are refused when they hold a URI with an `@` anywhere after its
  `://`, raw or percent-encoded (#210): agent ids, tool keys, run ids,
  candidates and graph instance ids in preflight, policy bundle, review plan,
  policy impact and `policy propose` input and output, and the run ids and
  tool keys of a Tracegraph review report. Before, only a `user:password@`
  that the refusal rule could bound was refused, so `neo4j://u:p/w@host/r`
  passed. An identity has no reason to hold userinfo, and an unquoted
  password may hold `/`, `)` or whitespace. Review notes, reviewer names,
  the notes of `policy propose` rows and control plans keep the rule for
  prose. The live-graph tool keys an ambiguous grant's note lists are
  checked as identities before they are joined into it, so `policy propose`
  refuses to emit a proposal whose note would show one holding userinfo
  (#216).

- The governance manifest and the crawler refuse a credential-shaped identity
  at input, with the same identity rule (#217): an agent, policy id, tool ref
  or `granted_by`, a `governed_by` policy or tool-ref key, a server's name
  (its `servers.yaml` name, else the name it reports) and the tool names it
  reports. Before, such a
  value was stored and printed by `check-access`, `unsafe-tools`,
  `blast-radius`, `unbacked-edges`, `audit-report` and `eligible-tools`. The
  error names the field, never the value. A manifest is rejected whole
  (`ingest-manifest`, `policy propose` and `policy impact` exit 1), and so
  is a `servers.yaml`, before any server is contacted. A server without a
  `servers.yaml` name that reports such a name, or any server that reports
  such a tool name, fails its own crawl, and the rest of the fleet still
  loads; a server's own reported name is never stored when `servers.yaml`
  names it. Resource URIs are not identities and are not checked. A graph
  keeps the values it stored earlier until they are re-ingested or reset.

- A control plan's identities get the identity rule at load (#215): `run_id`,
  producer name and version, principal ids, node ids and the principals a
  node lists, edge endpoints and candidate tools, so a credential URI with a
  `/` in its password or a percent-encoded scheme is refused there instead of
  being published as the result's `run_id`. Keys are field names and get the
  same rule, so a duplicate or forbidden-field error can no longer print an
  encoded credential-shaped key. String values of additive fields keep the
  prose rule. The result's `graph_state` goes through the same artifact
  check as the rest of the result.

### Fixed

- Artifact evidence no longer shows URI passwords the evidence scan could not
  bound (#212). Before, a password holding an unquoted `/`, `?` or `#`
  (`neo4j://u:p/w@host/r`, also read as a port when it starts with digits),
  a credential URI inside another URI's path, an encoded `@` (`%40`), and a
  resource whose query holds `)` (`https://h/r?k=a)b` kept `)b`) could reach
  preflight, policy bundle, control-plan and review artifacts, `policy
  impact` and `policy check` output and `ingest-manifest --dry-run --json`,
  including its warnings. Inside free text some shapes are still not
  bounded, as in 0.3.0: a password holding whitespace without quotes, a
  query or fragment holding `)` (`see https://h/r?k=a)b` still shows `)b`),
  and text after a `)` that no longer reads as a URI.
- A URI with a quote in its userinfo and an `@` after it is refused like any
  other credential URI, raw or percent-encoded (#210): in identities, review
  notes, reviewer names and control plans. Before, a quoted password holding
  whitespace (`neo4j://u:"p w"@host`), a quoted part after a `?` or `#`
  (`neo4j://u:p?"w s"@host`, `neo4j://u?x:"p w"@host`) and a quoted user
  part (`neo4j://"u v":"p w"@host`) were accepted. The refusal now matches
  what error-message scrubbing already hides, so a quoted URL followed by an
  address in the same text (`"https://h" mail a@b`, `"https://h:443" mail
  a@b`) is refused too; `see "https://x/y" and @alice`, whose quote follows
  a path, is not. Error messages hide the new shapes as well: before,
  `neo4j://u:p?"w s"@h/a` kept `"w s"@h/a`. A message whose decoded or
  scrubbed text still holds such a URI is withheld, as one holding
  `u:%22pw%22@` already was; `see "https://x/y" and @alice` as an error
  message is now withheld for that reason. Userinfo whose unquoted part
  holds whitespace (`neo4j://u v:"p w"@host`) still passes in prose and in
  error messages, where it reads as a URL followed by text; identities
  refuse it.
- Pydantic validation errors no longer print mapping keys, unknown field
  names or input values (#221). Before, an invalid `servers.yaml` or
  governance manifest (`crawl`, `ingest-manifest`, `policy impact`,
  `policy propose`), an invalid review-candidate sidecar, and a server whose
  `tools/list` reply the MCP SDK rejects printed the error location with
  every key on the way, and the SDK's errors also printed the input; only the
  credential shapes the scrubber recognizes were hidden, so
  `governed_by: {"postgres://u:prefix QXZQ@h/x": ...}` showed `QXZQ@h/x` and
  an `env` key showed as written. These errors are now one line, such as
  `1 validation error for ServersConfig: servers.0.env.<key>: expected a
  string [type=string_type]`: field names, list indexes and union member
  names that match the schema are kept, any other key is `<key>`, and the
  message is fixed text chosen by error type. A field's own refusal, such as
  `agent must not contain credential-shaped data`, is kept as written.
  Where Toolgraph cannot tell which model failed (`policy propose`, a
  rejected server reply) no location is printed.
- A config file that is not valid YAML is reported as `invalid YAML at line L,
  column C: <what failed>`, such as `could not be parsed` or `duplicate key`
  (#223). Before, the message quoted the source line, and names from the
  document: a duplicate key, an alias, anchor or tag, a character. A value
  PyYAML could not convert was reported with the value (`!!int x`,
  `!!float x`, `!!bool x`), or stopped `policy impact` and `policy propose`
  with a traceback (`!!bool x`, `!!timestamp x`, an empty `!!int`); now it is
  `invalid YAML: a value could not be read`. Invalid UTF-8 and a control
  character name only their position.
  Library callers still get PyYAML's error when it marks a line and column
  (a `MarkedYAMLError`); the other failures raise
  `toolgraph.yaml_loader.YAMLContentError`, a `ValueError`.
- Log output hides credential-shaped values and no longer prints tracebacks
  (#206). Before, a crawled server whose reply failed JSON-RPC validation
  made `crawl` print the MCP SDK's traceback with the reply's values
  (`input_value=...`), a tool that crashed under `serve` logged its
  exception message and traceback, and the SDK logged an unknown tool name
  as sent. Now the CLI's fallback output (Python's last-resort handler, used
  when no handler is configured) and the handlers the server configures
  when the process has none print each record's message through the
  credential scrubber, and each exception in its chain as
  `<Type>: <message>` without its traceback, scrubbed the same way, with a validation error's
  message rebuilt as in #221. A value the message or an exception prints
  with `repr` (`%r`, a `KeyError` key), which escapes a tab or a no-break
  space out of the scrubber's sight, is checked as it was before formatting,
  and a credential-shaped one withholds the line. Other text in a message,
  such as a tool name that is not credential-shaped, is still shown. Handlers a program
  configured itself, before or after importing Toolgraph, and uvicorn's own
  loggers under `serve --http` (#226), are not filtered.
- MCP tool-call errors for a bad request no longer quote the request (#206).
  Before, the MCP SDK answered an unknown tool with `Unknown tool: <name>` as
  sent, and invalid arguments with pydantic's text, which carried every
  input value (`input_value={'token': ...}`). The tool name now goes through
  the credential scrubber (`Unknown tool: nope` is unchanged), and invalid
  arguments read `Error executing tool <name>: 1 validation error for
  <name>Arguments: agent: expected a string [type=string_type]`, with field
  names and fixed messages as in #221. A tool that crashes keeps the SDK's
  generic `Error executing tool <name>`. Errors for resources and prompts,
  which Toolgraph does not register, and the HTTP transport's reply to a
  malformed JSON-RPC body still come from the SDK as they are.
- CLI usage errors and printed paths no longer show credential-shaped input
  (#206). Before, Click's and Typer's own usage errors quoted what was typed
  (`No such command '<arg>'`, `'<value>' is not a valid float`), as did
  Toolgraph's (`unknown profile '<value>'`), and lines such as
  `Invalid servers config at <path>` and the `--config` error printed the
  path as given. A usage error is now withheld whole when a token on the
  command line is credential-shaped, or the tokens joined by spaces are
  (`password =hunter2` typed as two arguments), checked as typed, before
  Click or Typer quotes or escapes them (`\x09`, `\xa0`) in the message;
  otherwise its
  message goes through the credential scrubber. Lines in
  which Toolgraph prints a path it was given or created (`init`, `check`,
  `crawl`, `ingest-manifest`, `example init`, `policy propose --output`, and
  the runtime-config errors) show a credential-shaped path as
  `<path withheld: credential-shaped>` and keep the rest of the line, and
  an error from the operating system prints its file names the same way
  (`[Errno 2] No such file or directory: '<path>'`) instead of quoting them
  with `repr`, which escaped a tab out of the scrubber's sight. A typo reads
  as before (`No such command 'chek'. Did you mean 'check'?`); a value the
  scrubber treats as an assignment, such as `token:v2`, is still masked
  (`'token=<redacted>`). The `"output"` path in the JSON receipts of `policy compile`,
  `policy review-plan`, `policy impact --output`, `preflight` and
  `control-preflight` is left as given: it names the file the caller asked
  for.
- Diagnostics that print a value with `repr` no longer show a credential
  whose tab or no-break space it escaped (#229). Before, `repr`, `ascii`,
  `%r`, a list or dict printed whole, a `KeyError` key and library errors
  such as `invalid literal for int() with base 10: '<value>'` or
  `could not convert string to float: b'<value>'` printed
  `password<TAB>=secret` as `password\t=secret`, which the scrubber does not
  read as an assignment: in `unknown profile` errors from the MCP tools, the
  `AGENT_NOT_FOUND` line of the selector commands, `policy propose`
  observation errors, and log records whose arguments were bytes or
  containers. Now every message that goes through the credential scrubber
  (CLI errors, MCP error text, log records, printed paths) is read as given
  for str and bytes literals that hold a backslash escape, exactly as `repr`
  or `ascii` prints them; such a literal that reads back as
  credential-shaped, alone or in place in the message, withholds the whole
  message, and so do more than 32 such literals. A literal without an
  escape is scrubbed as before (`'password=secret'` reads
  `'password=<redacted>`). Ordinary text is unchanged: a path is read only
  when quoted as `repr` would quote it, so `C:\bearer\notes.json` is still
  shown. A message that holds an escaped literal and also a credential the
  scrubber would mask is withheld instead of masked. Not read: a literal
  printed through `repr` twice, one right after an odd run of backslashes,
  and one cut without its closing quote, as `int()` prints a value whose
  repr is longer than 200 characters; the `int()` error itself is now
  rendered without its value (#236, below). Artifact and evidence text,
  which is stored rather than printed, is not affected.
- An `int()` error and a reply the MCP SDK could not parse no longer show
  a credential from the value they quote (#236, #239). Both print a
  shortened repr the scrubber cannot read back: `int()` cuts it at 200
  characters with no closing quote, and the SDK copies pydantic's error, which
  shortens `input_value` in the middle and can split an escape such as
  `\x01` or drop the quote that decided repr's quote character. A
  streamable-http server answering a crawl with a JSON string showed
  `password<TAB>=secret` in the `[error]` line, and an `int()` error
  chained under a withheld one showed it in a log record. Now a
  `ValueError` whose only argument is `int()`'s text prints
  `invalid literal for int() with base N (value not shown)`, and an MCP
  error with the JSON-RPC parse error code (-32700), including one a server
  sent itself, prints `the server's reply could not be parsed (JSON-RPC
  parse error)`, wherever Toolgraph renders an exception: crawl failures,
  log records (where an exception whose arguments are credential-shaped is
  still withheld whole, as before) and every exception `doctor` reports,
  which now renders exceptions like the other commands. Not covered: an
  `int()` error raised as a `ValueError` subclass (a `str` subclass whose
  `__repr__` raises one), and `re.Match` reprs (`%.50R`).
- A credential name at the end of a URL no longer leaves its value readable
  in a scrubbed message (#232). The scrubber rewrites each URL to its origin
  before it masks credentials, and a URL ends at whitespace or a quote, so
  `fetch http://h/password =secret` printed `fetch http://h =secret`; so did
  a name in a query (`?token =x`), with a tab or `: ` after it,
  `.../authorization: Basic x`, `.../bearer x`, and a bracketed value
  starting in the URL (`.../token=[a, x]`). Such a message is now withheld
  whole. The text cannot tell a prose colon from an assignment, so
  diagnostics whose URL path ends in a credential name with text after it
  are withheld too: `Error accessing https://host/token: connection
  refused`, `GET https://h/oauth/bearer returned 404`, `Client error for url
  'https://h/token='`, `GET https://h/password={id} returned 404`, a host
  that is a credential name (`Error accessing https://token: connection
  refused`), and a name in the host position of a malformed scheme
  (`1https://password : x`, which was masked). A value that starts inside
  the URL and continues past a quote, `<`, `>` or a tab
  (`http://h/x?token=abc"def` shows `"def`, and with an input placeholder
  `http://h/token=abc<redacted>x` shows `http://h>x`), is not caught, nor
  are forms only the scrubber reads as an assignment: `.../password :x`,
  `.../password:[a, x]`, `.../authorization=Basic x` and
  `.../proxy_authorization=Basic x` (#241).
- A command that writes a file or a review annotation no longer prints a
  traceback when the durability warning is escalated to an error (#129).
  The warning (`ArtifactDurabilityWarning`, or
  `ReviewCandidateDurabilityWarning` for `review-candidates annotate`) is
  raised after the write is published, when the directory could not be
  fsynced. With `-W error` the command still fails, as asked, but with one
  `ERROR:` line and exit 1 instead of a traceback; no JSON receipt is
  printed. Before, `init` also deleted the database it had just created
  while keeping the published `config.json` that names it; both are now
  kept. The annotation warning now ends `do not retry: a retry appends a
  second event`, since the event is already recorded. Library calls still
  raise the escalated warning after publication.

## 0.3.0 - 2026-10-19

Fourth alpha. Read **Upgrading from 0.2.0** below before installing: it lists
twelve changes an existing setup, checkout or embedding program can notice.
The one most installations meet first is that there is no implicit backend
any more: a setup that relied on Neo4j being chosen when nothing named a
backend now sets `TOOLGRAPH_BACKEND=neo4j` and installs `toolgraph[neo4j]`.

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
  Tooling that reads stderr no longer sees `Received notification from DBMS
  server` lines for `UNRECOGNIZED` notifications; warning-level ones, such
  as deprecations, still appear.
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
- More values are refused as credential-shaped (#130). Review notes, reviewer
  names, review-report run ids and tool keys, every control-plan key and string
  value, and artifact identities (now including `graph_instance_id` and
  `graph_state.instance_id`) refuse `name=value` and `name: value` assignments
  of a token, password, secret, credential, API key or authorization name,
  `Bearer` values, and credential URIs and queries, also when the name is
  percent-encoded (`?%74oken=`). A review note such as `token: rotated weekly`
  now has to be rephrased. `policy propose` reports a refused export value
  by its input line (`<file>:<line>: tool_key contains credential-shaped
  data`) instead of its position in the proposal, and refuses it before any
  other error could quote it. In the other direction, `policy impact` now
  accepts single-colon names such as `srv::get_token:v2` and
  `agent-token:blue`, which it refused before, and so does `policy propose`
  where such a name reaches its output (an observed agent, an export
  `candidate`, an ambiguous grant or a graph match in a note): 0.2.0 refused
  the proposal with `additions[0].agent contains credential-shaped data`. An
  export row's `tool_key` must still be `<server>::<tool>` with no other
  colon.
- Artifact evidence hides more around URI credentials (#204, #208, #209).
  Policy bundles, preflight and control-plan artifacts, policy review plans,
  `policy impact` and `policy check` output, and `ingest-manifest --dry-run
  --json` scrub it in three steps. A quote, `%22` or `%27` after a URI's
  scheme with an `@` anywhere after it hides everything after the scheme,
  the rest of the evidence line included. Then a URI that cannot be parsed
  (an unclosed `[`, an invalid bracketed host) stops the command as in
  0.2.0, now with `artifact evidence holds a URI that cannot be parsed; it
  is not shown` instead of the parser's message, which could quote the host
  with its password. Then a `)` with an `@` after it in the same unbroken run
  hides the rest of that run. So `https://u:"pw"@[::1/r` is shown as
  `https://<redacted-uri>`, while `https://u:p[)ss@h/r` stops the command.
  The hiding rules also hide values that hold no password. A consumer that
  matches evidence paths should expect `<scheme>://<redacted-uri>`.
- Error messages that go through Toolgraph's shared scrubber hide more (#201,
  #202): CLI errors, crawl failures and the server's
  backend-misconfiguration log record. A message drops what follows a quoted
  or bracketed credential value; one whose scrubbed or percent-decoded form
  is still credential-shaped reads `details withheld (they contain
  credential-shaped data)` after the command's own context; and a
  `<redacted` that the input itself contains is shown as `‹redacted`
  (U+2039). CLI usage errors, `--dry-run` JSON warnings, MCP SDK errors
  outside Toolgraph's own envelopes and logged tracebacks do not go through
  the scrubber (#206).

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
  Neo4j server older than 5.7, a missing Ladybug, a database file that is
  not a Ladybug database and, on Ladybug, a graph without Toolgraph's schema
  or with another schema version now return `isError: true` with
  `structuredContent`
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

- Evidence in artifacts no longer shows a quoted URI password (#204). Policy
  bundles, preflight and control-plan artifacts, policy review plans, policy
  impact and `policy check` output, and `ingest-manifest --dry-run --json`
  remove the userinfo the URI parser finds and keep the path of an ordinary
  URI. A password in quotes (`neo4j://u:"p w"@host/db`) was kept whole when
  it held a space or `)`, and partly when it held `@` and `/`. Resource URIs
  read from the graph carry the quotes percent-encoded, and some of those
  leaked too: `u:%22p@x/y%22@host` kept `x/y%22`. When a quote, `%22` or `%27`
  follows a URI's scheme and an `@` follows it, everything after the scheme
  is now hidden as `<redacted-uri>`, path and the rest of the evidence line
  included, since where such a password ends cannot be found reliably. This
  also hides values that hold no password, such as `https://host/a%22b@c`.
  Other values are redacted as before; a password the parser cannot bound,
  such as one holding an unquoted `/`, can still show (#212). Scanning a
  long line of letters is also linear now: before this fix a
  100,000-character line took 4.9 s.

- Evidence in artifacts no longer shows a URI password holding an unquoted
  `)` (#208). Evidence wraps a resource in graph notation,
  `(tool) -READS-> (uri)`, so the scan stopped at the first `)` and kept
  `neo4j://u:p)ss@host/r` whole. When a `)` follows a URI's scheme and an
  `@` follows it with no whitespace between, the rest of that run is now
  hidden as `<redacted-uri>`, the resource's closing `)` included. This also
  hides values that hold no password, such as `file:///a(b)@c`.

- Commands that write artifacts refuse evidence holding a URI that cannot be
  parsed (#209), such as one with an unclosed `[` (`https://u:p@[::1/r`), an
  invalid bracketed host or a character invalid under NFKC normalization.
  `ingest-manifest --dry-run --json` failed with a traceback, and
  `preflight` and `policy compile` printed the parser's message, which can
  quote the host with its password (`netloc 'u:pw@h＃x' contains invalid
  characters ...`). They now print `ERROR: artifact evidence holds a URI that
  cannot be parsed; it is not shown`, exit 1 and write nothing. The rest of
  the artifact is withheld as before: printing it around a hidden URI could
  show other values the scan cannot bound.

- An error message that would still show a credential after scrubbing is
  withheld (#202). Scrubbing cannot recognise every credential, for example
  a percent-encoded name such as `?%74oken=x` quoted in a validation error
  about an observed export, a selector profile or a governance key. Such a
  message now reads `details withheld (they contain credential-shaped
  data)` after the command's own context, where it used to print the
  value. A message whose percent-decoded form is credential-shaped is
  withheld before scrubbing, since the scrubber does not decode: this also
  withholds some messages it could have cleaned, such as an encoded URL with
  a `?token=` query. A placeholder that the input itself contains is shown
  as `‹redacted` (U+2039), so it cannot pass for one the scrubber inserted.
  This covers every message that goes through the shared scrubber:
  CLI errors, crawl failures and the server's backend-misconfiguration log
  record. CLI usage errors, `--dry-run` JSON warnings, MCP SDK errors
  outside Toolgraph's own envelopes and logged tracebacks do not go through
  it yet (#206). Checking for credential URIs is also linear now: a long run of
  `a:` with no `@` took seconds.

- Error text no longer shows part of a quoted credential (#201). A password
  in quotes inside a URI (`neo4j://u:"p@ss"@host`) was printed whole, and a
  quoted value with a space (`password="hunter 2"`, `NEO4J_PASSWORD="s3cr3t
  pass"`) kept its tail. A quoted or bracketed credential value, and a URI
  with a quote in its authority and an `@` anywhere after it, are now hidden
  through the end of the message: the message loses what follows them, and
  never shows part of the value. A URI that is only wrapped in quotes, such
  as `'neo4j://host:7687'` in a driver error, is shown as before unless an
  `@` follows later in the message. Scrubbing a long message is also linear
  now; before this fix a 100,000-character line of letters took 25 s.

- Values that Toolgraph refuses rather than scrubs go through one rule,
  `toolgraph.redaction.is_credential_shaped`, instead of five detectors that
  disagreed (#130). Review notes accepted `Bearer abc123`, `"token"=abc` and
  `mycredential=x`; control plans accepted `password=hunter2` and
  credential-shaped keys, and echoed a forbidden or duplicate key in the
  error; review metadata and graph instance ids were not checked; percent-encoded
  query keys passed everywhere but `policy impact`. Errors name where a refused
  value sits, never the value.

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
