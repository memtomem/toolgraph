"""Enumerate one MCP server's factual capabilities into a CrawlResult."""

from __future__ import annotations

from dataclasses import dataclass, field

from toolgraph.crawler.transports import open_session
from toolgraph.models import CrawlResult, ResourceRecord, ServerSpec, ToolRecord
from toolgraph.redaction import endpoint_label
from toolgraph.uris import normalize_resource_uri

# A crawled server controls its own cursors, so an unbounded drain loop lets a
# buggy or malicious server that keeps returning a cursor pin the crawler and
# grow memory without limit. The per-server wall-clock timeout only applies on
# the CLI path (crawl_all), not to library callers of crawl_server. Real
# catalogs are a handful of pages; hitting this cap means the server is broken.
MAX_LIST_PAGES = 1000


class PaginationLimitError(RuntimeError):
    """A server kept returning continuation cursors past MAX_LIST_PAGES."""


class CatalogBudgetError(RuntimeError):
    """A server's catalog exceeded its retained-data budget (ADR-0016)."""


DEFAULT_MAX_CATALOG_RECORDS = 10_000
DEFAULT_MAX_CATALOG_BYTES = 4 * 1024 * 1024


def _positive_int(name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be a positive integer, got {value!r}")
    return value


@dataclass(frozen=True)
class CatalogBudget:
    """Per-server cap on the catalog Toolgraph retains from one crawl (ADR-0016).

    Counted over the records Toolgraph keeps (tools and resources together):
    ``max_records`` records and ``max_bytes`` UTF-8 bytes of each record's
    ``model_dump_json()``. A catalog at the limit is accepted; one record past
    it fails the server, and no partial catalog is returned. This bounds
    retained data only. It does not bound what the MCP SDK buffers or parses
    before handing a page over, nor peak process memory.
    """

    max_records: int = DEFAULT_MAX_CATALOG_RECORDS
    max_bytes: int = DEFAULT_MAX_CATALOG_BYTES

    def __post_init__(self) -> None:
        _positive_int("max_records", self.max_records)
        _positive_int("max_bytes", self.max_bytes)


@dataclass
class _BudgetMeter:
    """Running totals for one server's crawl; shared by tools and resources."""

    budget: CatalogBudget
    records: int = 0
    bytes: int = field(default=0)

    def admit(self, record) -> None:
        """Account for ``record`` before it is retained; raise past the budget."""
        size = len(record.model_dump_json().encode("utf-8"))
        records = self.records + 1
        total = self.bytes + size
        if records > self.budget.max_records:
            raise CatalogBudgetError(
                f"catalog exceeds {self.budget.max_records} records"
                " (tools and resources combined); the server was not loaded"
            )
        if total > self.budget.max_bytes:
            raise CatalogBudgetError(
                f"catalog exceeds {self.budget.max_bytes} bytes of retained records"
                f" (reached {total}); the server was not loaded"
            )
        self.records, self.bytes = records, total


def _page(cursor: str | None):
    """Pagination params for a list call, or None for the first page.

    mcp 2.x replaced the ``cursor=`` keyword with a params object. The SDK
    treats "no params" and "params with a null cursor" as equivalent when
    deciding a listing is complete, so returning None here is a matter of
    sending the smaller request, not of correctness. The distinction that DOES
    decide the loop is null versus ``""`` in the RESPONSE cursor, which the
    caller below handles.
    """
    if cursor is None:
        return None
    from mcp.types import PaginatedRequestParams

    return PaginatedRequestParams(cursor=cursor)


def _annotation_fields(tool) -> dict:
    """Tool annotations as ToolRecord fields (ADR-0006).

    Annotations are optional in the protocol; absent ones stay None so the
    loader can clear node properties the server stopped sending (SET to null
    removes the property — same reconciliation lifecycle as description).
    """
    ann = getattr(tool, "annotations", None)
    if ann is None:
        return {}
    return {
        "read_only_hint": ann.read_only_hint,
        "destructive_hint": ann.destructive_hint,
        "idempotent_hint": ann.idempotent_hint,
        "open_world_hint": ann.open_world_hint,
    }


async def _list_all_tools(session, meter: _BudgetMeter | None = None) -> list[ToolRecord]:
    """Drain ``list_tools`` pages until ``next_cursor`` is exhausted.

    Without pagination, a server with more than one page silently drops
    tools on later pages — governance refs to them would then reject at
    ingest and reachability analysis would miss the tools entirely. Each
    record is charged to ``meter`` before it is kept, so an over-budget
    catalog stops the drain without requesting another page.
    """
    meter = meter or _BudgetMeter(CatalogBudget())
    out: list[ToolRecord] = []
    cursor: str | None = None
    for _ in range(MAX_LIST_PAGES):
        listed = await session.list_tools(params=_page(cursor))
        for t in listed.tools:
            record = ToolRecord(
                name=t.name,
                description=t.description,
                input_schema=t.input_schema,
                **_annotation_fields(t),
            )
            meter.admit(record)
            out.append(record)
        cursor = getattr(listed, "next_cursor", None)
        if cursor is None:
            # Stop ONLY on None: an empty-string cursor is a valid opaque
            # continuation token in MCP and the SDK distinguishes "" from
            # None when sending the next request. `if not cursor` would
            # silently truncate after page 1 against such a server.
            return out
    raise PaginationLimitError(
        f"list_tools returned a continuation cursor on {MAX_LIST_PAGES}"
        " consecutive pages — refusing to drain further"
    )


async def _list_all_resources(session, meter: _BudgetMeter | None = None) -> list[ResourceRecord]:
    """Drain ``list_resources`` pages until ``next_cursor`` is exhausted."""
    meter = meter or _BudgetMeter(CatalogBudget())
    out: list[ResourceRecord] = []
    cursor: str | None = None
    for _ in range(MAX_LIST_PAGES):
        listed = await session.list_resources(params=_page(cursor))
        for r in listed.resources:
            record = ResourceRecord(
                uri=normalize_resource_uri(str(r.uri)),
                name=r.name,
                mime_type=r.mime_type,
                description=r.description,
            )
            meter.admit(record)
            out.append(record)
        cursor = getattr(listed, "next_cursor", None)
        if cursor is None:
            # Stop ONLY on None: an empty-string cursor is a valid opaque
            # continuation token in MCP and the SDK distinguishes "" from
            # None when sending the next request. `if not cursor` would
            # silently truncate after page 1 against such a server.
            return out
    raise PaginationLimitError(
        f"list_resources returned a continuation cursor on {MAX_LIST_PAGES}"
        " consecutive pages — refusing to drain further"
    )


async def crawl_server(spec: ServerSpec, budget: CatalogBudget | None = None) -> CrawlResult:
    """Connect, initialize, and list tools/resources the server actually advertises.

    ``budget`` defaults to ``CatalogBudget()``; one meter spans tools and
    resources, so the limits apply to the server's whole catalog. Exceeding
    it raises ``CatalogBudgetError`` and no result is returned.
    """
    meter = _BudgetMeter(budget or CatalogBudget())
    async with open_session(spec) as session:
        init = await session.initialize()
        caps = init.capabilities

        tools = await _list_all_tools(session, meter) if caps.tools is not None else []
        resources = await _list_all_resources(session, meter) if caps.resources is not None else []

        return CrawlResult(
            server_name=spec.name or init.server_info.name,
            server_version=init.server_info.version,
            transport=spec.transport,
            endpoint=endpoint_label(spec),
            tools=tools,
            resources=resources,
        )
