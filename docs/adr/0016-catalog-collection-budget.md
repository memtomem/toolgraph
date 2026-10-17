# 0016 — Per-server catalog collection budget

Status: Accepted · Date: 2026-10-18

Tracking: [issue #153](https://github.com/memtomem/toolgraph/issues/153).

## Context

`MAX_LIST_PAGES` stops a server that never exhausts its cursor, but it does
not bound how much a server's catalog can make Toolgraph retain: every tool
and resource record, descriptions and input schemas included, is kept until
loading, for the whole fleet. A synthetic page with one 1,000,000-character
description was accepted intact.

Measured on 2026-10-18 by crawling the repository's example fleets in memory
(nine servers that ran on the development machine), in UTF-8 bytes of each
server's whole `CrawlResult.model_dump_json()` (records plus the result
envelope, so slightly more than the budget's record-only unit): the largest
catalog was 21,713 bytes (20 tools), the longest description 3,225
characters, the largest input schema 1,370 bytes, and the nine together
72,204 bytes.

The MCP SDK (inspected at mcp 2.3.0) bounds some input before parsing and
not other input: SSE events, on both the legacy SSE client and streamable
HTTP, are capped at 1 MiB each; a stdio line and a streamable-HTTP JSON
response body have no size limit. Toolgraph receives pages only after the
SDK has parsed them.

## Decision

1. Each crawled server gets a `CatalogBudget`: at most `max_records`
   records (tools and resources combined, default 10,000) and `max_bytes`
   bytes (default 4 MiB). The byte unit is the UTF-8 length of each
   retained record's `model_dump_json()`; one meter spans the server's
   tools and resources.
2. Every record is charged before it is retained. A catalog exactly at the
   limit is accepted; the first record past it raises `CatalogBudgetError`,
   the drain stops without requesting another page, and no partial catalog
   is returned.
3. A budget overrun is an ordinary crawl failure. ADR-0007's failure rules
   apply unchanged: a named failed server keeps its previous graph state,
   an unnamed failure skips fleet pruning, other servers load normally.
4. The budget is on by default for library callers (`crawl_server`,
   `crawl_all`, `crawl_all_async` take `budget=None`) and configurable on
   the CLI with `--max-catalog-bytes` and `--max-catalog-records`.
5. Crawl failure text unwraps exception groups, because the SDK runs each
   session in a task group and an in-session error otherwise surfaces only
   as `ExceptionGroup: unhandled errors in a TaskGroup`.

## Consequences

- The defaults are about 190x the largest measured catalog. A server over
  them fails visibly and an operator raises the limit for that run.
- This bounds retained record data. It does not bound SDK or transport
  buffering, parse-time allocation, Python object overhead, or peak process
  memory; a stdio server can still send an arbitrarily long line before the
  budget sees anything. The fleet-wide figure the defaults imply,
  servers × 4 MiB of record JSON, is a payload figure, not a memory bound.
- There is no fleet-wide budget and no per-server override yet. Both can be
  added later without changing what a per-server overrun means.
