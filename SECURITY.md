# Security policy

## Supported versions

Toolgraph is an alpha project. Security fixes are provided for the latest
published `0.0.x` release only.

## Reporting a vulnerability

Please use GitHub's private vulnerability reporting for
[`memtomem/toolgraph`](https://github.com/memtomem/toolgraph/security/advisories/new).
If that form is unavailable, email **contact@dapada.co.kr** with `toolgraph
security` in the subject; do not treat an empty advisory page as "no channel".
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
fail-closed for duplicate keys and unknown fields.

Be precise about what is redacted, because the boundary is narrower than it
first reads. Toolgraph avoids persisting the **server endpoint** it connected
to: stdio arguments are reduced to an executable name, and a URL endpoint to
its origin, dropping path, query, userinfo and fragment. **Resource URIs are
not redacted.** A resource a server advertises is canonicalized and stored as
given, userinfo, query and fragment included, because a governed resource and a
crawled resource must canonicalize to the same graph node or a deny-governed
resource could slip past as ALLOW. Exported preflight and policy artifacts
apply their own redaction; the graph does not.

So: do not put credentials in resource URIs, server names, governance
identifiers, or evidence strings — those are stored as written.
