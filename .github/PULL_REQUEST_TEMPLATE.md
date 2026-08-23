## Summary

<!-- What changes, and why. One or two sentences is usually enough. -->

<!-- If this closes an issue, say so: Closes #N -->

## Validation

<!--
Say what you actually ran and what it printed. A reviewer should not have to
guess which parts you exercised locally.

This is the local release gate from docs/releasing.md, which is what to run
when CI cannot (Actions does not run on this repository while it is private
with unpaid minutes):

    uv lock --check
    uv sync --frozen --group dev --extra ladybug
    uv run ruff check .
    uv run pytest -q                    # needs a Docker daemon: testcontainers
    scripts/audit-dependencies.sh runtime
    scripts/audit-dependencies.sh extras
    uv build && uv run twine check dist/*
    scripts/verify-artifacts.sh dist <VERSION>

`ci-required` covers more than this once CI runs -- the artifact quickstart
across the OS/Python matrix, and the minimum-MCP install. Neither is something
a single machine reproduces, so say what you could not check rather than
implying the gate above is equivalent.
-->

## Notes for the reviewer

<!--
Optional. Useful things to put here:
- a behavior change, and what depends on the old behavior
- a decision you made that could reasonably have gone the other way
- what you deliberately left out of scope
-->

---

<!--
Before you open this:

- **Contract or schema change?** `contracts/` is consumed by other repositories.
  A test that only reads a fixture does not catch producer drift — drive the
  producer. Record the decision in an ADR under `docs/adr/` if it changes the
  boundary.
- **Advisory boundary.** toolgraph explains and compiles; an independent runtime
  gateway enforces. If a change moves that line, say so explicitly.
- **First contribution?** A bot will ask you to sign the CLA on this pull
  request. See CONTRIBUTING.md.
- **Release?** `docs/releasing.md` is the runbook. The README and CHANGELOG must
  be final *in* the tagged commit — pyproject.toml embeds the README as package
  metadata.
-->
