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


def _label(spec: ServerSpec) -> str:
    return spec.name or spec.command or spec.url or "<unnamed>"


DEFAULT_TIMEOUT = 30.0
DEFAULT_CONCURRENCY = 5


async def _crawl_one(
    spec: ServerSpec, timeout: float | None, sem: asyncio.Semaphore
) -> tuple[CrawlResult | None, tuple[str, str] | None]:
    async with sem:
        try:
            return await asyncio.wait_for(crawl_server(spec), timeout), None
        except TimeoutError:  # a hanging server must not block the crawl
            return None, (_label(spec), f"timeout after {timeout}s")
        except Exception as exc:  # noqa: BLE001 - one bad server must not abort the crawl
            return None, (_label(spec), f"{type(exc).__name__}: {exc}")


async def crawl_all_async(
    specs: list[ServerSpec],
    timeout: float | None = DEFAULT_TIMEOUT,
    concurrency: int = DEFAULT_CONCURRENCY,
) -> tuple[list[CrawlResult], list[tuple[str, str]]]:
    sem = asyncio.Semaphore(concurrency)
    outcomes = await asyncio.gather(*(_crawl_one(s, timeout, sem) for s in specs))
    results = [r for r, _ in outcomes if r is not None]
    failures = [f for _, f in outcomes if f is not None]
    return results, failures


def crawl_all(
    specs: list[ServerSpec],
    timeout: float | None = DEFAULT_TIMEOUT,
    concurrency: int = DEFAULT_CONCURRENCY,
) -> tuple[list[CrawlResult], list[tuple[str, str]]]:
    return asyncio.run(crawl_all_async(specs, timeout, concurrency))


def fleet_keep_names(
    specs: list[ServerSpec],
    results: list[CrawlResult],
    failures: list[tuple[str, str]],
) -> tuple[set[str], list[str]]:
    """Graph names that must survive fleet reconciliation, plus blockers.

    A failed spec with a ``name`` override is protected by that name. A failed
    spec WITHOUT one cannot be mapped to a graph identity (node names come
    from ``serverInfo.name`` at crawl time, which a failed crawl never saw) —
    its label is returned as a blocker and the caller must skip the prune:
    a server that failed to crawl is not a server that was removed.

    Blocker detection rides on the failure label: a named spec's label IS its
    name (see ``_label``), so any failure label outside the named set must
    come from an unnamed spec.
    """
    named = {s.name for s in specs if s.name}
    keep = named | {r.server_name for r in results}
    blockers = [label for label, _ in failures if label not in named]
    return keep, blockers
