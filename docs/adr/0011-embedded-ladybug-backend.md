# 0011 — Ladybug is the embedded local backend; Neo4j remains the shared backend

Status: Accepted · Date: 2026-07-14

## Context

Requiring a Neo4j server makes the first local policy-bundle workflow heavier
than the graph itself. The archived Kùzu project is not an appropriate new
dependency; Ladybug is its maintained embedded successor and supports the
typed property-graph model Toolgraph needs.

Ladybug has a stricter schema and a single-owner process model. Treating its
Cypher as identical to Neo4j would leak dialect and result-shape differences
through policy services.

## Decision

`toolgraph init` creates a Ladybug local configuration by default. Existing
uninitialized installations and explicit `NEO4J_*` deployments keep their
Neo4j behavior; shared or multi-process operation continues to use Neo4j.

The policy service reads through the domain-level `GraphReader` port. Backend
code owns schema DDL, transaction lifecycle, query dialect translation, and
row normalization. Both backends preserve the same authored-manifest
atomicity, reject reasons, graph-state token, and policy-bundle schema.

Ladybug is an optional install extra until the supported platform matrix and
full conformance suite pass. A second process trying to own the same database
must fail visibly rather than opening a hidden mixed-reader/writer mode.

## Consequences

- Local users can crawl, ingest, query, and compile without Docker.
- memtomem-stm never opens the Ladybug file; it consumes an immutable bundle.
- No binary Neo4j-to-Ladybug migration is provided. Crawled facts are rebuilt
  and authored facts are re-ingested from their source files.
- Vector search and full-text search are not part of this backend decision.
