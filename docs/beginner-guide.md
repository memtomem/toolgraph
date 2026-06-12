# toolgraph Beginner Guide

This guide is for first-time users who want to run toolgraph locally, read the
results, and make a small change to the example governance files.

Korean version: [`docs/ko-beginner-guide.md`](ko-beginner-guide.md)

toolgraph is not a runtime gateway that blocks MCP tool calls. It is an
advisory analyzer. It crawls MCP servers into a Neo4j graph, combines those
facts with operator-authored grants and data-access rules, then answers:
"Which agents can reach which sensitive resources, through which tools, and
why?"

## Concepts to Know First

- `MCPServer`: an MCP server that exposes tools and resources.
- `Tool`: a callable capability exposed by an MCP server.
- `Resource`: something a tool reads or writes, such as a file, memory
  namespace, or API dataset.
- `Agent`: the caller that may be granted access to tools. The examples use
  names such as `public-bot` and `ops-agent`.
- `Policy`: a rule attached to a resource or tool. In the default example,
  `secrets-deny` marks a secret file as DENY-governed.
- `CAN_CALL`: an operator-authored grant from an agent to a tool.
- `READS` / `WRITES`: operator-authored data-flow edges from a tool to a
  resource.
- `GOVERNED_BY`: the edge that attaches a resource or tool to a policy.

The main path shape is:

```text
Agent -> Tool -> Resource -> Policy
```

For example, this path means `public-bot` can call a tool that reaches a
DENY-governed secret file:

```text
public-bot -> filesystem::read_file -> file:///private/secrets.env -> secrets-deny
```

## Prerequisites

- Python 3.12 or newer
- Docker or Docker Desktop
- `uv`
- Node.js and `npx`

The default demo crawls the filesystem MCP server from `examples/servers.yaml`.
That server is launched with `npx`, so the first run may need network access to
download the npm package.

## 1. Install and Start Neo4j

```bash
uv sync --extra dev
cp .env.example .env
docker compose up -d --wait
```

The default values in `.env.example` match `docker-compose.yml`. You only need
to edit `.env` if you are using a separate Neo4j instance.

Check connectivity and initialize constraints:

```bash
uv run toolgraph check
uv run toolgraph init-schema
```

You should see a successful connection message and the graph constraints.

## 2. Run the Demo

The fastest first run is the demo script:

```bash
bash scripts/demo.sh
```

The script does four things:

1. Clears the current graph data.
2. Crawls the MCP servers listed in `examples/servers.yaml`.
3. Loads the operator-authored rules in `examples/governance.yaml`.
4. Runs three audit questions.

## 3. Run the Same Flow Manually

Running each step yourself makes the data flow easier to understand:

```bash
uv run toolgraph reset --yes
uv run toolgraph crawl --servers examples/servers.yaml
uv run toolgraph ingest-manifest --governance examples/governance.yaml
```

`crawl` loads factual server state: which MCP server exposes which tools and
resources. `ingest-manifest` loads operator-authored governance: agents,
policies, grants, and data-access rules.

## 4. Read the Results

Find tools that `public-bot` can call and that reach a DENY-governed resource:

```bash
uv run toolgraph unsafe-tools public-bot
```

In the default example, `public-bot` has a grant to
`filesystem::read_file`. The governance file also declares that
`filesystem::read_file` reads `file:///private/secrets.env`, and that resource
is governed by `secrets-deny`. The result includes fields like:

- `tool`: the reachable tool.
- `resource`: the resource the tool reaches.
- `policy`: the DENY policy on that resource.
- `classification`: usually `violation` unless the operator declared an
  expected exception.
- `path`: the graph path used as evidence.
- `provenance`: where the relevant edge claims came from.

Check whether one agent can call one tool:

```bash
uv run toolgraph check-access public-bot read_file
```

The most important field is `verdict`:

- `ALLOW`: the agent has a grant and no DENY policy path was found.
- `DENY`: the agent has a grant, but the tool reaches a DENY-governed target.
- `NOT_GRANTED`: the agent exists but has no grant for that tool.
- `AGENT_NOT_FOUND`: the agent name is not in the graph.
- `TOOL_NOT_FOUND`: the tool name is not in the graph.
- `AMBIGUOUS_TOOL`: the bare tool name matches tools on multiple servers.

Find who would be affected if a resource changed:

```bash
uv run toolgraph blast-radius "file:///private/secrets.env"
```

If the response has `found: false`, toolgraph could not find that resource or
policy node. Check this before treating an empty impact list as safe.

## 5. Edit the Example Governance

The default rules live in `examples/governance.yaml`:

```yaml
agents:
  - ops-agent
  - public-bot

policies:
  - id: secrets-deny
    effect: DENY
    scope: secrets

grants:
  - {agent: public-bot, tool: "filesystem::read_file", granted_by: platform-team}

data_access:
  - {tool: "filesystem::read_file", resource: "file:///private/secrets.env", mode: READS}

governed_by:
  "file:///private/secrets.env": [secrets-deny]
```

Good first experiments:

- Remove the `public-bot` grant for `filesystem::read_file`, then rerun
  `unsafe-tools public-bot`.
- Change the `data_access` resource URI, then rerun `check-access`.
- Add another `Policy`, attach it under `governed_by`, and inspect the new
  evidence path.

After every change, reload the manifest:

```bash
uv run toolgraph ingest-manifest --governance examples/governance.yaml
```

If the manifest references a missing tool or policy, ingestion is rejected and
the previous graph state is preserved.

## 6. Find Missing or Weak Governance

toolgraph can also show what the graph does not know yet:

```bash
uv run toolgraph unmapped-tools
uv run toolgraph orphan-policies
uv run toolgraph unbacked-edges
uv run toolgraph drift
```

- `unmapped-tools`: granted tools with no declared `READS` or `WRITES` edges.
- `orphan-policies`: policies that no currently reachable path touches.
- `unbacked-edges`: operator-authored edges without evidence strings.
- `drift`: tools that governance still references but the latest crawl no
  longer sees.

Start with `unsafe-tools`, `check-access`, and `blast-radius`. Then use these
audit commands once you are ready to improve coverage.

## Troubleshooting

If `docker compose up -d --wait` fails, confirm Docker is running.

If `crawl` fails with an `npx` error, confirm Node.js is installed and that the
machine can download npm packages.

If you see `AGENT_NOT_FOUND`, check that the same agent name appears under
`agents:` and `grants:` in `examples/governance.yaml`.

If you see `TOOL_NOT_FOUND`, confirm that `crawl` succeeded and that you are
using the right tool name. Some cases need the server prefix, such as
`filesystem::read_file`.

If you see `DENY`, that is not a runtime block. toolgraph is reporting an
advisory finding. Runtime blocking would need to happen in a separate gateway
or MCP proxy.
