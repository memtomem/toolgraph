"""Crawl many servers; each server fails independently (partial success)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import yaml

from toolgraph.crawler.client import crawl_server
from toolgraph.models import CrawlResult, ServerSpec, ServersConfig


def load_servers_config(path: Path) -> list[ServerSpec]:
    data = yaml.safe_load(path.read_text()) or {}
    return ServersConfig.model_validate(data).servers


def spec_label(spec: ServerSpec) -> str:
    """Display label for a spec — NOT a graph identity (see fleet_keep_names)."""
    return spec.name or spec.command or spec.url or "<unnamed>"


DEFAULT_TIMEOUT = 30.0
DEFAULT_CONCURRENCY = 5

# A failure carries its ServerSpec, not a display label: fleet reconciliation
# must know whether the failed spec had a ``name`` override, and inferring
# that from a label is unsound (an unnamed spec's command can collide with
# another spec's name — Codex review of this branch caught it).
CrawlFailure = tuple[ServerSpec, str]


async def _crawl_one(
    spec: ServerSpec, timeout: float | None, sem: asyncio.Semaphore
) -> tuple[CrawlResult | None, CrawlFailure | None]:
    async with sem:
        try:
            return await asyncio.wait_for(crawl_server(spec), timeout), None
        except TimeoutError:  # a hanging server must not block the crawl
            return None, (spec, f"timeout after {timeout}s")
        except Exception as exc:  # noqa: BLE001 - one bad server must not abort the crawl
            return None, (spec, f"{type(exc).__name__}: {exc}")


async def crawl_all_async(
    specs: list[ServerSpec],
    timeout: float | None = DEFAULT_TIMEOUT,
    concurrency: int = DEFAULT_CONCURRENCY,
) -> tuple[list[CrawlResult], list[CrawlFailure]]:
    sem = asyncio.Semaphore(concurrency)
    outcomes = await asyncio.gather(*(_crawl_one(s, timeout, sem) for s in specs))
    results = [r for r, _ in outcomes if r is not None]
    failures = [f for _, f in outcomes if f is not None]
    return results, failures


def crawl_all(
    specs: list[ServerSpec],
    timeout: float | None = DEFAULT_TIMEOUT,
    concurrency: int = DEFAULT_CONCURRENCY,
) -> tuple[list[CrawlResult], list[CrawlFailure]]:
    return asyncio.run(crawl_all_async(specs, timeout, concurrency))


def fleet_keep_names(
    specs: list[ServerSpec],
    results: list[CrawlResult],
    failures: list[CrawlFailure],
) -> tuple[set[str], list[str]]:
    """Graph names that must survive fleet reconciliation, plus blockers.

    A failed spec with a ``name`` override is protected by that name. A failed
    spec WITHOUT one cannot be mapped to a graph identity (node names come
    from ``serverInfo.name`` at crawl time, which a failed crawl never saw) —
    its label is returned as a blocker and the caller must skip the prune:
    a server that failed to crawl is not a server that was removed.
    """
    keep = {s.name for s in specs if s.name} | {r.server_name for r in results}
    blockers = [spec_label(spec) for spec, _ in failures if not spec.name]
    return keep, blockers


def duplicate_name_overrides(specs: list[ServerSpec]) -> set[str]:
    """``name:`` overrides claimed by more than one spec — a config error."""
    names = [s.name for s in specs if s.name]
    return {n for n in names if names.count(n) > 1}


def duplicate_identities(
    results: list[CrawlResult], failures: list[CrawlFailure]
) -> set[str]:
    """Graph names claimed by more than one server this pass.

    Two specs resolving to the same ``server_name`` would silently MERGE into
    one MCPServer node and last-load-wins reconcile each other's tools away —
    and give fleet reconciliation an ambiguous identity. The caller must
    reject the pass before any graph mutation. Failed named specs count too:
    an unnamed server self-reporting a failed spec's name is the same
    collision, one crawl later.
    """
    names = [r.server_name for r in results] + [s.name for s, _ in failures if s.name]
    return {n for n in names if names.count(n) > 1}
