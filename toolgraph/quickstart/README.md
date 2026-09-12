# Toolgraph quickstart

You are inside a generated directory. It is self-contained and offline: the
three files here are a demo MCP server, a crawl target list, and an
operator-authored governance manifest.

| File | What it is |
| :--- | :--- |
| `policy_gateway_server.py` | A local MCP server exposing `read_note` (read-only) and `publish_note` (destructive). |
| `servers.yaml` | Points the crawler at that server, named `policy-gateway`. |
| `governance.yaml` | Who may call what, what each tool touches, and a DENY policy on the draft resource. |

## Run it

```bash
toolgraph init
toolgraph crawl --servers servers.yaml
toolgraph ingest-manifest --governance governance.yaml --strict-drift
toolgraph eligible-tools vibe-coder \
  policy-gateway::read_note policy-gateway::publish_note --profile review
toolgraph selection-explain vibe-coder policy-gateway::publish_note
toolgraph policy compile --agent vibe-coder --profile review \
  --output .toolgraph/policy-bundle.json
```

`read_note` comes back eligible. `publish_note` comes back rejected, because it
writes to a resource under a DENY policy, and `selection-explain` prints that
path. The last command writes the bundle and prints its path, byte digest, and
eligible/rejected counts.

Toolgraph decided nothing at runtime here. It analyzed and compiled; a gateway
that consumes the bundle is what would actually block a call.

## Then

Try `toolgraph unbacked-edges`, `unsafe-tools vibe-coder`, or
`blast-radius draft-publish-deny`. Edit `governance.yaml`, rerun
`ingest-manifest`, and recompile to see a decision change.

These files are a copy, not a link, so upgrading Toolgraph does not update
them. After an upgrade, generate a fresh directory alongside this one
(`toolgraph example init toolgraph-quickstart-new`) and move your edits across:
`example init` refuses to write into a directory that already exists. The
CHANGELOG says when a release changed these files.

Full walkthrough, including handing the bundle to a gateway:
<https://github.com/memtomem/toolgraph/blob/main/docs/beginner-guide.md>
(Korean: `docs/ko-beginner-guide.md`).
