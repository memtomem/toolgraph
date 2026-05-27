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
