#!/usr/bin/env python3
"""Size sweep for the fleet duplicate checks in ``toolgraph.crawler.crawl``.

Times ``duplicate_name_overrides`` and ``duplicate_identities`` on prebuilt
fleets of growing size. Fixtures are built outside the timed region, each
size gets a warmup call, and every sample times a batch of calls so the small
post-fix timings are not dominated by timer resolution. The growth ratio
between adjacent sizes is printed as an observation (quadratic scans grow
about 4x per doubling, linear counting about 2x); it is not a pass criterion.
Numbers are evidence for this machine and fixture shape, not a latency SLA.
"""

from __future__ import annotations

import argparse
import functools
import json
import platform
import statistics
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from toolgraph.crawler.crawl import duplicate_identities, duplicate_name_overrides
from toolgraph.models import CrawlResult, ServerSpec


def _fleet(size: int, shape: str) -> tuple[list[ServerSpec], list[CrawlResult], list]:
    """``unique``: every name distinct. ``dupes``: every name shared by ten specs."""
    names = [f"s{i}" if shape == "unique" else f"s{i // 10}" for i in range(size)]
    specs = [ServerSpec(name=n, command="unused") for n in names]
    results = [CrawlResult(server_name=n) for n in names[: size // 2]]
    failures = [(s, "synthetic") for s in specs[size // 2 :]]
    return specs, results, failures


def _median_ms(fn: Callable[[], object], repeats: int, batch: int) -> float:
    fn()  # warmup
    samples = []
    for _ in range(repeats):
        start = time.perf_counter()
        for _ in range(batch):
            fn()
        samples.append((time.perf_counter() - start) * 1000 / batch)
    return statistics.median(samples)


_REPO_ROOT = Path(__file__).resolve().parents[1]


def _revision() -> str:
    """Short HEAD of the repository this script lives in, plus ``-dirty`` when
    the working tree differs from it, so a run over staged or unsaved edits is
    not mistaken for a run over the committed revision."""
    git = ["git", "-C", str(_REPO_ROOT)]
    try:
        head = subprocess.check_output([*git, "rev-parse", "--short", "HEAD"], text=True).strip()
        dirty = subprocess.check_output([*git, "status", "--porcelain"], text=True).strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"
    return f"{head}-dirty" if dirty else head


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--sizes", type=int, nargs="+", default=[1000, 2000, 4000, 8000])
    parser.add_argument("--repeats", type=int, default=5, help="samples per size (median)")
    parser.add_argument("--batch", type=int, default=5, help="calls per sample")
    parser.add_argument("--json", action="store_true", help="emit one JSON document")
    args = parser.parse_args(argv)
    if any(n < 1 for n in args.sizes) or args.repeats < 1 or args.batch < 1:
        parser.error("--sizes, --repeats and --batch must be positive integers")

    header = {
        "revision": _revision(),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "repeats": args.repeats,
        "batch": args.batch,
        "fixture": "unique: s0..sN; dupes: ten specs per name; "
        "identities = results for the first half + named failures for the second half",
    }
    rows = []
    for shape in ("unique", "dupes"):
        prev: dict[str, float] = {}
        for size in args.sizes:
            specs, results, failures = _fleet(size, shape)
            row = {
                "shape": shape,
                "size": size,
                "overrides_ms": _median_ms(
                    functools.partial(duplicate_name_overrides, specs), args.repeats, args.batch
                ),
                "identities_ms": _median_ms(
                    functools.partial(duplicate_identities, results, failures),
                    args.repeats,
                    args.batch,
                ),
            }
            for key in ("overrides_ms", "identities_ms"):
                row[key.replace("_ms", "_growth")] = (
                    round(row[key] / prev[key], 2) if prev.get(key) else None
                )
            prev = {k: row[k] for k in ("overrides_ms", "identities_ms")}
            rows.append(row)

    if args.json:
        print(json.dumps({"header": header, "rows": rows}, indent=2))
        return 0
    for k, v in header.items():
        print(f"{k}: {v}")
    print(
        f"{'shape':<7}{'size':>7}{'overrides ms':>14}{'growth':>8}"
        f"{'identities ms':>15}{'growth':>8}"
    )
    for r in rows:
        g1 = "-" if r["overrides_growth"] is None else f"{r['overrides_growth']:.2f}x"
        g2 = "-" if r["identities_growth"] is None else f"{r['identities_growth']:.2f}x"
        print(
            f"{r['shape']:<7}{r['size']:>7}{r['overrides_ms']:>14.3f}{g1:>8}"
            f"{r['identities_ms']:>15.3f}{g2:>8}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
