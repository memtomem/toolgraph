# Security policy

## Supported versions

Toolgraph is an alpha project. Security fixes are provided for the latest
published `0.1.x` release only.

## Reporting a vulnerability

Please use GitHub's private vulnerability reporting for
[`memtomem/toolgraph`](https://github.com/memtomem/toolgraph/security/advisories/new).
Do not open a public issue containing credentials, private endpoint details, or
an unpatched exploit. Include affected versions, reproduction steps, impact,
and any suggested mitigation.

## Security boundary

Toolgraph analyzes governance and compiles policy artifacts. It does not sit in
the MCP traffic path and does not enforce runtime decisions. A gateway must
consume the compiled artifact to enforce them.

The bundled Ladybug backend is intended for a single local Toolgraph process.
The repository's Neo4j compose file is development-only, uses a known password,
and binds to loopback; it is not a production authentication configuration.

Operator-authored YAML is treated as trusted configuration but is parsed
fail-closed for duplicate keys and unknown fields. Toolgraph avoids persisting
stdio arguments or URL paths, queries, userinfo, and fragments. Do not put
credentials in server names, governance identifiers, or evidence strings.
