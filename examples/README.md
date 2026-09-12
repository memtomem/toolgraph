# Example inputs

Crawl targets (`servers*.yaml`) and operator-authored governance
(`governance*.yaml`) for the repository demos. These ship with the clone only,
not with the PyPI package.

If you are here to try Toolgraph for the first time, do not start with these.
Run `toolgraph example init`, which writes an offline, self-contained
quickstart with its own README, and follow the
[beginner guide](../docs/beginner-guide.md)
([Korean](../docs/ko-beginner-guide.md)).

All paths below are relative to the repository root; the demos expect to be run
from there.

| Input | Used by | Needs |
| :--- | :--- | :--- |
| `servers.yaml` + `governance.yaml` | `scripts/demo.sh`, and the `Makefile` defaults (`SERVERS` / `GOVERNANCE`) | `npx` and network on first run |
| `servers-public.yaml` + `governance-public.yaml` | `scripts/demo-public.sh` | `npx`, `uvx`, network; downloads several real public servers |
| `servers-gate.yaml` + `governance-gate.yaml` | The proxy/surfacing wedge in the main [README](../README.md) | The two companion servers installed separately |
| `servers-fleet.yaml` + `governance-fleet.yaml` | Fleet-scale authoring exhibit, 7 servers | `npx`, `uvx`, network, the companion servers, and `mkdir -p /tmp/toolgraph-fleet-sandbox` for the filesystem server |
| `servers-policy-gateway.yaml` + `governance-policy-gateway.yaml` | Manual policy-gateway walkthrough against `policy_gateway_server.py` | Nothing external; runs offline |
| `governance-syncmill-smoke.yaml` | `scripts/ecosystem_smoke.py` and `scripts/gate_e_operational.py` | Those harnesses supply their own crawl input |
| `policy_gateway_server.py` | The pair above, and `scripts/policy_bundle_gateway_smoke.py`, which writes its own `servers.yaml` at run time | Nothing external |
| `policy_propose_from_observed.py` | Run directly; reads a governance file plus a window of observed gateway usage you supply | No fixed pair. `--self-test` runs it standalone |

Not every file has a partner: the two harness entries above bring their own
crawl input, and the proposal script takes whatever governance and observation
files you point it at.

`governance-*.yaml` files are operator assertions, not crawled facts. The
richer ones carry per-edge `provenance` with an evidence pointer;
`toolgraph unbacked-edges` is what finds the ones that do not.
