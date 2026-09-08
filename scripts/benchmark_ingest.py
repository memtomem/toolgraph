#!/usr/bin/env python3
"""Compare ingest resolution against an exact Git baseline on disposable DBs.

Timing includes one complete ingest transaction, not crawl or schema creation.
Python allocation peaks exclude native engine/container memory. Measurements
are evidence for this fixture and machine, not a latency SLA or backend ranking.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
import importlib.util
import json
import logging
from pathlib import Path
import statistics
import sys
import tempfile
import time
import tracemalloc
import types

from ecosystem_smoke import exact_sha, run
from toolgraph import config
from toolgraph.graph import driver, loader, schema
from toolgraph.manifest import ingest
from toolgraph.manifest.parser import governance_digest
from toolgraph.models import AccessGrant, CrawlResult, Governance, Policy, ToolRecord

ROOT = Path(__file__).resolve().parents[1]


def baseline_module(ref: str, path: str, name: str):
    source = run(["git", "show", f"{ref}:{path}"], cwd=ROOT).stdout
    module = types.ModuleType(name)
    sys.modules[name] = module
    exec(compile(source, f"{ref}:{path}", "exec"), module.__dict__)
    return module


class Counter:
    def __init__(self, tx):
        self.tx = tx
        self.count = 0

    def run(self, query, **params):
        self.count += 1
        return self.tx.run(query, **params)


def measure(module, gov, repeats):
    durations, counts, peaks = [], [], []
    digest = governance_digest(gov)
    for attempt in range(repeats + 1):
        with driver.session() as session:
            def apply(tx):
                counted = Counter(tx)
                warnings, notices = module._ingest(counted, gov, digest=digest)
                if warnings or notices:
                    raise RuntimeError("benchmark fixture did not ingest cleanly")
                return counted.count
            tracemalloc.start()
            start = time.perf_counter()
            count = session.execute_write(apply)
            duration = (time.perf_counter() - start) * 1000
            peak = tracemalloc.get_traced_memory()[1]
            tracemalloc.stop()
        if attempt:  # one warmup per variant/size
            durations.append(duration)
            counts.append(count)
            peaks.append(peak)
    return {"query_counts": counts, "median_ms": statistics.median(durations),
            "samples_ms": durations, "python_peak_bytes": max(peaks)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-ref", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=3)
    args = parser.parse_args()
    if args.repeats < 1:
        parser.error("repeats must be positive")
    ref = exact_sha(ROOT, args.baseline_ref)
    old_queries = baseline_module(ref, "toolgraph/graph/queries.py", "benchmark_old_queries")
    old_ingest = baseline_module(ref, "toolgraph/manifest/ingest.py", "benchmark_old_ingest")
    old_ingest.resolve_tool_keys = old_queries.resolve_tool_keys
    rows = []
    original = config.settings
    try:
        for backend in ("ladybug", "neo4j"):
            with ExitStack() as stack:
                temp = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="toolgraph-bench-")))
                if backend == "ladybug":
                    if importlib.util.find_spec("ladybug") is None:
                        raise RuntimeError("Ladybug is required")
                    settings = config.Settings(backend="ladybug", db_path=temp / "bench.lbug")
                else:
                    from testcontainers.neo4j import Neo4jContainer
                    db = stack.enter_context(Neo4jContainer(
                        "neo4j:5.26-community", username="neo4j", password="testpass"
                    ))
                    settings = config.Settings(backend="neo4j", neo4j_uri=db.get_connection_url(),
                                               neo4j_user="neo4j", neo4j_password="testpass")
                driver.close_driver()
                config.settings = settings
                stack.callback(driver.close_driver)
                schema.init_schema()
                for size in (100, 1000, 4097, 10000):
                    loader.load_crawl_result(CrawlResult(
                        server_name="bench", tools=[ToolRecord(name=f"t{i}") for i in range(size)]
                    ))
                    gov = Governance(agents=["a"], policies=[Policy(id="p", effect="ALLOW")],
                                     grants=[AccessGrant(agent="a", tool=f"bench::t{i}") for i in range(size)])
                    # Duplicate the first grant 100 times: this must not add resolution queries.
                    gov.grants.extend([gov.grants[0]] * 100)
                    for variant, module in (("baseline", old_ingest), ("batched", ingest)):
                        result = {"backend": backend, "tools": size, "variant": variant,
                                  **measure(module, gov, args.repeats)}
                        rows.append(result)
                        print(json.dumps(result), flush=True)
    finally:
        driver.close_driver()
        config.settings = original
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps({"baseline_ref": ref, "rows": rows,
        "working_tree_dirty": bool(run(["git", "status", "--porcelain"], cwd=ROOT).stdout),
        "memory_scope": "tracemalloc Python allocations only; excludes native/DB memory"}, indent=2) + "\n")


if __name__ == "__main__":
    logging.getLogger("neo4j").setLevel(logging.ERROR)
    main()
