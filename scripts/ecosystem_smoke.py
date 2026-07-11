#!/usr/bin/env python3
"""Destructive-isolation E2E smoke for Toolgraph, SyncMill, and Tracegraph.

The only destructive operations happen in a private temporary workspace and a
fresh Neo4j container. Source repositories are read-only inputs: fetched refs
are cloned by SHA, so live branches and worktrees are never reset or reused.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

import jsonschema


ROOT = Path(__file__).resolve().parents[1]
GOVERNANCE = ROOT / "examples" / "governance-syncmill-smoke.yaml"
NEO4J_IMAGE = "neo4j:5.26"
PASSWORD = "toolgraph-smoke-password"
FORBIDDEN = ("prompt", "patch", "output", "completion", "stdout", "stderr")


class SmokeError(RuntimeError):
    pass


def run(
    argv: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    timeout: int = 300,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        argv,
        cwd=cwd,
        env=env,
        text=True,
        capture_output=True,
        timeout=timeout,
    )
    if check and proc.returncode:
        detail = (proc.stderr or proc.stdout).strip()[-2000:]
        raise SmokeError(f"command exited {proc.returncode}: {' '.join(argv)}\n{detail}")
    return proc


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def fetched_sha(root: Path, ref: str) -> str:
    if not (root / ".git").exists():
        raise SmokeError(f"not a Git checkout: {root}")
    run(["git", "fetch", "origin"], cwd=root)
    return run(["git", "rev-parse", f"{ref}^{{commit}}"], cwd=root).stdout.strip()


def clone_at(source: Path, target: Path, sha: str) -> None:
    run(["git", "clone", "--no-local", "--quiet", str(source), str(target)])
    run(["git", "checkout", "--quiet", "--detach", sha], cwd=target)


def wait_for_neo4j(env: dict[str, str]) -> None:
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        probe = run(
            ["uv", "run", "toolgraph", "check"],
            cwd=Path(env["TOOLGRAPH_ROOT"]),
            env=env,
            check=False,
            timeout=20,
        )
        if probe.returncode == 0:
            return
        time.sleep(1)
    raise SmokeError("isolated Neo4j did not become ready within 90 seconds")


def write_stubs(bin_dir: Path) -> None:
    bin_dir.mkdir()
    codex = bin_dir / "codex"
    codex.write_text(
        "#!/bin/sh\n"
        "sleep 1\n"
        "printf 'codex\\n' > winner.txt\n"
        "printf '%s\\n' '{\"type\":\"item.completed\",\"item\":{\"type\":\"agent_message\",\"text\":\"done\"}}'\n",
        encoding="utf-8",
    )
    kimi = bin_dir / "kimi"
    kimi.write_text(
        "#!/bin/sh\n"
        "printf 'kimi\\n' > winner.txt\n"
        "printf '%s\\n' '{\"role\":\"assistant\",\"content\":\"done\"}'\n",
        encoding="utf-8",
    )
    codex.chmod(0o755)
    kimi.chmod(0o755)


def make_target(path: Path) -> str:
    path.mkdir()
    run(["git", "init", "--quiet", "-b", "main"], cwd=path)
    run(["git", "config", "user.email", "smoke@example.invalid"], cwd=path)
    run(["git", "config", "user.name", "ecosystem smoke"], cwd=path)
    (path / "README.md").write_text("disposable smoke target\n", encoding="utf-8")
    run(["git", "add", "README.md"], cwd=path)
    run(["git", "commit", "--quiet", "-m", "initial"], cwd=path)
    return run(["git", "rev-parse", "HEAD"], cwd=path).stdout.strip()


def json_env(base: dict[str, str], key: str, value: Any) -> None:
    base[key] = json.dumps(value, separators=(",", ":"))


def syncmill_env(
    base: dict[str, str], *, runs_dir: Path, toolgraph_root: Path, bin_dir: Path
) -> dict[str, str]:
    env = dict(base)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
    json_env(env, "SYNCMILL_AGENT_PRIORITY", ["codex", "kimi-code"])
    env["SYNCMILL_INTEGRATION__REPORT__ENABLED"] = "true"
    env["SYNCMILL_INTEGRATION__REPORT__RUNS_DIR"] = str(runs_dir)
    env["SYNCMILL_INTEGRATION__TRACE__ENABLED"] = "true"
    env["SYNCMILL_INTEGRATION__TRACE__DIR"] = str(runs_dir)
    env["SYNCMILL_INTEGRATION__TOOLGRAPH__ENABLED"] = "true"
    env["SYNCMILL_INTEGRATION__TOOLGRAPH__PROFILE"] = "review"
    json_env(
        env,
        "SYNCMILL_INTEGRATION__TOOLGRAPH__COMMAND",
        ["uv", "run", "--project", str(toolgraph_root), "toolgraph", "preflight"],
    )
    json_env(
        env,
        "SYNCMILL_INTEGRATION__TOOLGRAPH__CANDIDATE_TOOLS",
        ["syncmill::shared_read", "syncmill::board_stats"],
    )
    json_env(
        env,
        "SYNCMILL_INTEGRATION__TOOLGRAPH__AGENT_MAP",
        {"codex": "codex", "kimi-code": "kimi"},
    )
    return env


def one_new_run(runs_dir: Path, before: set[str]) -> str:
    after = {item.name for item in runs_dir.iterdir() if item.is_dir()}
    created = after - before
    if len(created) != 1:
        raise SmokeError(f"expected exactly one new run directory, found {sorted(created)}")
    return created.pop()


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SmokeError(f"invalid JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise SmokeError(f"expected JSON object: {path}")
    return value


def nested_values(value: Any, key: str) -> list[Any]:
    found: list[Any] = []
    if isinstance(value, dict):
        for item_key, item in value.items():
            if item_key == key:
                found.append(item)
            found.extend(nested_values(item, key))
    elif isinstance(value, list):
        for item in value:
            found.extend(nested_values(item, key))
    return found


def validate_run(
    run_dir: Path,
    expected_agent: str,
    expected_decision: str,
    *,
    envelope_schema: Path,
    preflight_schema: Path,
) -> dict[str, Any]:
    run_id = run_dir.name
    envelope_path = run_dir / "envelope.json"
    preflight_path = run_dir / "preflight.json"
    trace_path = run_dir / "trace.otlp.json"
    envelope = load_json(envelope_path)
    preflight = load_json(preflight_path)
    trace = load_json(trace_path)
    jsonschema.validate(envelope, load_json(envelope_schema))
    jsonschema.validate(preflight, load_json(preflight_schema))
    if envelope.get("run_id") != run_id or preflight.get("run_id") != run_id:
        raise SmokeError(f"canonical run_id mismatch in {run_dir}")
    # OTLP attributes encode keys and values as adjacent objects, so also use a
    # deterministic serialized check for every span below.
    spans = nested_values(trace, "spans")
    flat_spans = [span for group in spans for span in group if isinstance(span, dict)]
    if not flat_spans or any(run_id not in json.dumps(span, sort_keys=True) for span in flat_spans):
        raise SmokeError(f"not every OTLP span carries run_id {run_id}")
    if preflight.get("agent") != expected_agent or preflight.get("decision") != expected_decision:
        raise SmokeError(
            f"unexpected preflight identity/decision: {preflight.get('agent')}/"
            f"{preflight.get('decision')}"
        )
    for name, path in (
        ("result", run_dir / "result.json"),
        ("trace", trace_path),
        ("preflight", preflight_path),
    ):
        block = envelope.get(name, {})
        digest = block.get("digest") if isinstance(block, dict) else None
        if digest != "sha256:" + sha256(path):
            raise SmokeError(f"{name} digest mismatch for {run_id}")
    return {"envelope": envelope, "preflight": preflight, "trace": trace, "trace_path": trace_path}


def assert_clean_target(target: Path, initial_head: str) -> None:
    if run(["git", "rev-parse", "HEAD"], cwd=target).stdout.strip() != initial_head:
        raise SmokeError("disposable target HEAD changed")
    if run(["git", "status", "--porcelain"], cwd=target).stdout.strip():
        raise SmokeError("disposable target worktree changed")
    branches = run(["git", "branch", "--list", "syncmill/*"], cwd=target).stdout.strip()
    if branches:
        raise SmokeError(f"SyncMill branches survived cleanup: {branches}")


def tracegraph_probe(
    tracegraph: Path, trace: Path, artifact_path: Path, *, compete: bool = False
) -> None:
    run(
        [
            "uv",
            "run",
            "tracegraph",
            "ingest-otlp",
            "--file",
            str(trace),
            "--out",
            str(artifact_path),
        ],
        cwd=tracegraph,
    )
    run(["uv", "run", "tracegraph", "inspect", str(artifact_path)], cwd=tracegraph)
    if not compete:
        return
    normalized = load_json(artifact_path)["trace"]
    steps = normalized["steps"]
    if len(steps) != 4:
        raise SmokeError(f"compete trace has {len(steps)} steps, expected 4")
    names = {step["step_id"]: step["name"] for step in steps}
    if sorted(names.values()) != ["attempt:codex", "attempt:kimi-code", "select", "syncmill.run"]:
        raise SmokeError(f"unexpected compete steps: {sorted(names.values())}")
    select = next(step for step in steps if step["name"] == "select")
    if select.get("projection_lossy") is not True:
        raise SmokeError("compete select is not marked projection_lossy")
    attempt_ids = {step["step_id"] for step in steps if step["name"].startswith("attempt:")}
    causal = [edge for edge in normalized["edges"] if edge["type"] == "CAUSED_BY"]
    if any(edge["src"] in attempt_ids and edge["dst"] in attempt_ids for edge in causal):
        raise SmokeError("compete attempts incorrectly have a sibling causal edge")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--syncmill-root", type=Path, required=True)
    parser.add_argument("--tracegraph-root", type=Path, required=True)
    parser.add_argument("--artifacts-dir", type=Path, required=True)
    parser.add_argument("--export-compete-trace", action="store_true")
    args = parser.parse_args()
    if run(["docker", "info"], check=False, timeout=20).returncode:
        raise SmokeError("Docker is required; start the daemon and rerun the smoke")

    roots = {"toolgraph": ROOT, "syncmill": args.syncmill_root.resolve(), "tracegraph": args.tracegraph_root.resolve()}
    refs = {"toolgraph": "origin/main", "syncmill": "origin/main", "tracegraph": "origin/tracegraph-mvp"}
    shas = {name: fetched_sha(root, refs[name]) for name, root in roots.items()}
    workspace = Path(tempfile.mkdtemp(prefix="toolgraph-ecosystem-smoke-", dir="/private/tmp"))
    container = f"toolgraph-smoke-{uuid4().hex[:12]}"
    success = False
    try:
        clones = {name: workspace / name for name in roots}
        for name, source in roots.items():
            clone_at(source, clones[name], shas[name])
        # The harness under test may be newer than fetched origin/main. Overlay
        # only its two smoke inputs; production packages still come from SHAs.
        shutil.copy2(GOVERNANCE, clones["toolgraph"] / "examples" / GOVERNANCE.name)
        port = free_port()
        run([
            "docker", "run", "-d", "--name", container,
            "-p", f"127.0.0.1:{port}:7687",
            "-e", "NEO4J_AUTH=neo4j/" + PASSWORD,
            NEO4J_IMAGE,
        ], timeout=120)
        base_env = dict(os.environ)
        base_env.update({
            "NEO4J_URI": f"bolt://127.0.0.1:{port}",
            "NEO4J_USER": "neo4j",
            "NEO4J_PASSWORD": PASSWORD,
            "TOOLGRAPH_ROOT": str(clones["toolgraph"]),
        })
        wait_for_neo4j(base_env)
        run(["uv", "run", "toolgraph", "init-schema"], cwd=clones["toolgraph"], env=base_env)
        servers = workspace / "servers.yaml"
        servers.write_text(
            "servers:\n  - name: syncmill\n    transport: stdio\n"
            f"    command: uv\n    args: [\"run\", \"--project\", \"{clones['syncmill']}\", \"syncmill\", \"serve\"]\n",
            encoding="utf-8",
        )
        run(["uv", "run", "toolgraph", "crawl", "--servers", str(servers)], cwd=clones["toolgraph"], env=base_env)
        run(["uv", "run", "toolgraph", "ingest-manifest", "--governance", str(clones["toolgraph"] / "examples" / GOVERNANCE.name)], cwd=clones["toolgraph"], env=base_env)

        target = workspace / "target"
        initial_head = make_target(target)
        bin_dir = workspace / "bin"
        write_stubs(bin_dir)
        runs_dir = workspace / "runs"
        env = syncmill_env(base_env, runs_dir=runs_dir, toolgraph_root=clones["toolgraph"], bin_dir=bin_dir)

        before = set()
        run(["uv", "run", "syncmill", "run", "smoke", "--repo", str(target), "--strategy", "route", "--agent", "kimi-code"], cwd=clones["syncmill"], env=env)
        route_id = one_new_run(runs_dir, before)
        route_data = validate_run(
            runs_dir / route_id,
            "kimi",
            "advisory_allow",
            envelope_schema=clones["syncmill"] / "contracts" / "run-envelope.schema.json",
            preflight_schema=clones["toolgraph"] / "contracts" / "preflight.schema.json",
        )
        assert_clean_target(target, initial_head)

        before = {item.name for item in runs_dir.iterdir() if item.is_dir()}
        run(["uv", "run", "syncmill", "run", "smoke", "--repo", str(target), "--strategy", "compete"], cwd=clones["syncmill"], env=env)
        compete_id = one_new_run(runs_dir, before)
        compete_data = validate_run(
            runs_dir / compete_id,
            "codex",
            "advisory_warn",
            envelope_schema=clones["syncmill"] / "contracts" / "run-envelope.schema.json",
            preflight_schema=clones["toolgraph"] / "contracts" / "preflight.schema.json",
        )
        envelope = compete_data["envelope"]
        if (envelope.get("result") or {}).get("agent_id") != "codex":
            raise SmokeError("compete winner was not priority-first Codex")
        rejected = compete_data["preflight"].get("rejected", [])
        board = next(
            (row for row in rejected if row.get("candidate") == "syncmill::board_stats"),
            None,
        )
        if not board or board.get("reason") != "DENY_VIOLATION" or not board.get("paths"):
            raise SmokeError("Codex board_stats rejection lacks DENY_VIOLATION evidence")
        assert_clean_target(target, initial_head)

        tracegraph_probe(
            clones["tracegraph"], route_data["trace_path"], workspace / "route.tracegraph.json"
        )
        tracegraph_probe(
            clones["tracegraph"],
            compete_data["trace_path"],
            workspace / "compete.tracegraph.json",
            compete=True,
        )

        args.artifacts_dir.mkdir(parents=True, exist_ok=True)
        summary = {
            "schema_version": 1,
            "refs": shas,
            "route": {"run_id": route_id, "agent": "kimi", "decision": "advisory_allow"},
            "compete": {"run_id": compete_id, "agent": "codex", "decision": "advisory_warn", "winner": "codex", "rejected": ["syncmill::board_stats:DENY_VIOLATION"]},
        }
        summary_path = args.artifacts_dir / "ecosystem-smoke-summary.json"
        summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        shutil.copy2(runs_dir / route_id / "preflight.json", args.artifacts_dir / "route-preflight.json")
        shutil.copy2(runs_dir / compete_id / "preflight.json", args.artifacts_dir / "compete-preflight.json")
        if args.export_compete_trace:
            shutil.copy2(compete_data["trace_path"], args.artifacts_dir / "compete.otlp.json")
        success = True
        print(json.dumps(summary, indent=2, sort_keys=True))
        return 0
    finally:
        run(["docker", "rm", "-f", container], check=False, timeout=30)
        if success:
            shutil.rmtree(workspace, ignore_errors=True)
        else:
            print(f"smoke workspace preserved after failure: {workspace}", file=sys.stderr)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (SmokeError, subprocess.TimeoutExpired) as exc:
        print(f"ecosystem smoke failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
