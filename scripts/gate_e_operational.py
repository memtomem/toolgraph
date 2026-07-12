#!/usr/bin/env python3
"""SHA-pinned Gate E enforcement and P4 review-loop operational harness.

All mutation is confined to a temporary workspace and a fresh Neo4j container.
The retained package is body-free and never includes agent prompts or stdout.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any
from uuid import uuid4

from ecosystem_smoke import (
    NEO4J_IMAGE,
    PASSWORD,
    SmokeError,
    free_port,
    json_env,
    load_json,
    make_target,
    run,
    sha256,
    wait_for_neo4j,
)


ROOT = Path(__file__).resolve().parents[1]
GOVERNANCE_NAME = "governance-syncmill-smoke.yaml"
PROVIDERS = ("codex", "claude", "kimi", "agy")
FORBIDDEN_BODY_KEYS = {
    "prompt", "patch", "stdout", "stderr", "arguments", "input", "result", "output",
}
FORBIDDEN_TEXT = ("SENTINEL", "api_key=", "password=", "authorization:")


def resolve_sha(root: Path, ref: str) -> str:
    return run(["git", "rev-parse", f"{ref}^{{commit}}"], cwd=root).stdout.strip()


def clone_local(source: Path, target: Path, sha: str) -> None:
    run(["git", "clone", "--quiet", "--no-hardlinks", str(source), str(target)])
    run(["git", "checkout", "--quiet", "--detach", sha], cwd=target)


def write_agent_stubs(bin_dir: Path) -> None:
    bin_dir.mkdir()
    for binary, payload in (
        ("codex", '{"type":"item.completed","item":{"type":"agent_message","text":"done"}}'),
        ("kimi", '{"role":"assistant","content":"done"}'),
    ):
        path = bin_dir / binary
        path.write_text(
            "#!/bin/sh\n"
            "printf '%s\\n' invoked >> \"$GATE_E_AGENT_SENTINEL\"\n"
            f"printf '%s\\n' '{payload}'\n",
            encoding="utf-8",
        )
        path.chmod(0o755)


def syncmill_env(
    base: dict[str, str],
    *,
    toolgraph: Path,
    bin_dir: Path,
    scenario_dir: Path,
    agent: str,
    mapped_agent: str,
    candidates: list[str],
    enabled: bool = True,
    command: list[str] | None = None,
    drift_policy: str = "warn",
    unavailable_policy: str = "block",
) -> dict[str, str]:
    env = dict(base)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
    env["GATE_E_AGENT_SENTINEL"] = str(scenario_dir / "agent-invoked")
    env["SYNCMILL_INTEGRATION__REPORT__ENABLED"] = "true"
    env["SYNCMILL_INTEGRATION__REPORT__RUNS_DIR"] = str(scenario_dir / "runs")
    env["SYNCMILL_INTEGRATION__TOOLGRAPH__ENABLED"] = str(enabled).lower()
    env["SYNCMILL_INTEGRATION__TOOLGRAPH__MODE"] = "strict"
    env["SYNCMILL_INTEGRATION__TOOLGRAPH__PROFILE"] = "review"
    env["SYNCMILL_INTEGRATION__TOOLGRAPH__DRIFT_POLICY"] = drift_policy
    env["SYNCMILL_INTEGRATION__TOOLGRAPH__UNAVAILABLE_POLICY"] = unavailable_policy
    json_env(env, "SYNCMILL_AGENT_PRIORITY", [agent])
    json_env(env, "SYNCMILL_INTEGRATION__TOOLGRAPH__CANDIDATE_TOOLS", candidates)
    json_env(
        env,
        "SYNCMILL_INTEGRATION__TOOLGRAPH__AGENT_MAP",
        {agent: mapped_agent} if enabled else {},
    )
    json_env(
        env,
        "SYNCMILL_INTEGRATION__TOOLGRAPH__COMMAND",
        command
        or ["uv", "run", "--project", str(toolgraph), "toolgraph", "preflight"],
    )
    return env


def strict_scenario(
    *,
    name: str,
    expected_dispatch: bool,
    base_env: dict[str, str],
    syncmill: Path,
    toolgraph: Path,
    bin_dir: Path,
    target: Path,
    workspace: Path,
    agent: str = "codex",
    mapped_agent: str = "codex",
    candidates: list[str],
    enabled: bool = True,
    command: list[str] | None = None,
    drift_policy: str = "warn",
    unavailable_policy: str = "block",
) -> dict[str, Any]:
    scenario_dir = workspace / "strict" / name
    scenario_dir.mkdir(parents=True)
    env = syncmill_env(
        base_env,
        toolgraph=toolgraph,
        bin_dir=bin_dir,
        scenario_dir=scenario_dir,
        agent=agent,
        mapped_agent=mapped_agent,
        candidates=candidates,
        enabled=enabled,
        command=command,
        drift_policy=drift_policy,
        unavailable_policy=unavailable_policy,
    )
    proc = run(
        [
            "uv", "run", "syncmill", "run", f"gate-e-{name}",
            "--repo", str(target), "--strategy", "route", "--agent", agent,
        ],
        cwd=syncmill,
        env=env,
        check=False,
    )
    dispatched = (scenario_dir / "agent-invoked").exists()
    if dispatched != expected_dispatch:
        raise SmokeError(
            f"strict scenario {name}: dispatch={dispatched}, expected={expected_dispatch}; "
            f"exit={proc.returncode}"
        )
    if expected_dispatch and proc.returncode != 0:
        raise SmokeError(f"strict scenario {name}: dispatched run exited {proc.returncode}")
    if not expected_dispatch and proc.returncode == 0:
        raise SmokeError(f"strict scenario {name}: blocked run exited zero")
    return {
        "scenario": name,
        "expected_dispatch": expected_dispatch,
        "actual_dispatch": dispatched,
        "exit_code": proc.returncode,
    }


def crawl(syncmill: Path, toolgraph: Path, env: dict[str, str], servers: Path) -> None:
    run(["uv", "run", "toolgraph", "crawl", "--servers", str(servers)], cwd=toolgraph, env=env)


def preflight_snapshot(toolgraph: Path, env: dict[str, str], out: Path) -> dict[str, Any]:
    run(
        [
            "uv", "run", "toolgraph", "preflight", "codex", "syncmill::shared_read",
            "--profile", "review", "--run-id", "nonmutation", "--out", str(out),
        ],
        cwd=toolgraph,
        env=env,
    )
    value = load_json(out)
    value.pop("created_at", None)
    return value


def p4_pipeline(
    *,
    syncmill: Path,
    tracegraph: Path,
    toolgraph: Path,
    env: dict[str, str],
    workspace: Path,
) -> dict[str, Any]:
    p4 = workspace / "p4"
    p4.mkdir()
    raw = tracegraph / "tests" / "fixtures" / "syncmill" / "live-qualified-tool.otlp.json"
    if not raw.exists():
        raise SmokeError("Tracegraph ref has no live-qualified SyncMill fixture")
    normalized = p4 / "normalized.json"
    report = p4 / "candidates.json"
    annotations = p4 / "annotations.json"
    run(
        ["uv", "run", "tracegraph", "ingest-otlp", "--file", str(raw), "--out", str(normalized)],
        cwd=tracegraph,
        env=env,
    )
    queried = run(
        ["uv", "run", "tracegraph", "query", "tool-failure", str(normalized)],
        cwd=tracegraph,
        env=env,
    )
    if "gate_e::always_fail" not in queried.stdout or "gate:codex" in queried.stdout:
        raise SmokeError("Tracegraph query did not isolate the qualified failing tool")
    run(
        [
            "uv", "run", "tracegraph", "export-review-candidates", "tool-failure",
            str(normalized), "--out", str(report),
        ],
        cwd=tracegraph,
        env=env,
    )
    report_doc = load_json(report)
    if len(report_doc.get("candidates", [])) != 1:
        raise SmokeError("P4 export must contain exactly one candidate")

    board_env = dict(env)
    board_env["SYNCMILL_BOARD__ENABLED"] = "true"
    board_env["SYNCMILL_BOARD__DB_PATH"] = str(p4 / "board.db")
    first = run(
        ["uv", "run", "syncmill", "board", "import-review-candidates", str(report)],
        cwd=syncmill,
        env=board_env,
    )
    second = run(
        ["uv", "run", "syncmill", "board", "import-review-candidates", str(report)],
        cwd=syncmill,
        env=board_env,
    )
    if "imported 1" not in first.stdout or "skipped 1" not in second.stdout:
        raise SmokeError("SyncMill review import is not idempotent")

    listed = run(
        ["uv", "run", "toolgraph", "review-candidates", "list", str(report)],
        cwd=toolgraph,
        env=env,
    )
    listed_doc = json.loads(listed.stdout)
    candidate_id = listed_doc["candidates"][0]["candidate_id"]
    board_list = run(
        ["uv", "run", "syncmill", "board", "list"], cwd=syncmill, env=board_env
    ).stdout
    if candidate_id not in board_list or "human" not in board_list.lower():
        raise SmokeError("SyncMill board item identity/human-required state mismatch")

    source_before = report.read_bytes()
    preflight_before = preflight_snapshot(toolgraph, env, p4 / "preflight-before.json")
    blast_before = json.loads(
        run(
            ["uv", "run", "toolgraph", "blast-radius", "board-operations-deny"],
            cwd=toolgraph,
            env=env,
        ).stdout
    )
    annotated = run(
        [
            "uv", "run", "toolgraph", "review-candidates", "annotate", str(report),
            candidate_id, "--status", "dismissed", "--reviewer", "gate-e-smoke",
            "--note", "synthetic expected failure", "--annotations", str(annotations),
        ],
        cwd=toolgraph,
        env=env,
    )
    if json.loads(annotated.stdout)["disposition"] != "dismissed":
        raise SmokeError("Toolgraph synthetic disposition was not recorded")
    preflight_after = preflight_snapshot(toolgraph, env, p4 / "preflight-after.json")
    blast_after = json.loads(
        run(
            ["uv", "run", "toolgraph", "blast-radius", "board-operations-deny"],
            cwd=toolgraph,
            env=env,
        ).stdout
    )
    if source_before != report.read_bytes():
        raise SmokeError("Toolgraph annotation rewrote immutable candidate evidence")
    if preflight_before != preflight_after or blast_before != blast_after:
        raise SmokeError("review annotation mutated graph/preflight/blast-radius behavior")
    if candidate_id not in run(
        ["uv", "run", "syncmill", "board", "list"], cwd=syncmill, env=board_env
    ).stdout:
        raise SmokeError("Toolgraph annotation changed independent SyncMill board state")

    return {
        "run_id": report_doc["candidates"][0]["run_id"],
        "candidate_id": candidate_id,
        "tool_key": report_doc["candidates"][0]["tool_key"],
        "raw_trace_digest": "sha256:" + sha256(raw),
        "normalized_artifact_digest": "sha256:" + sha256(normalized),
        "candidate_report_digest": "sha256:" + sha256(report),
        "annotation_digest": "sha256:" + sha256(annotations),
        "graph_generation": preflight_before["graph_generation"],
        "board_import": {"first": 1, "second_skipped": 1},
        "disposition": "dismissed",
        "nonmutation": True,
        "retained": [raw, normalized, report, annotations],
    }


def provider_versions() -> dict[str, dict[str, Any]]:
    commands = {
        "codex": ["codex", "--version"],
        "claude": ["claude", "--version"],
        "kimi": ["kimi", "--version"],
        "agy": ["agy", "--version"],
    }
    values = {}
    for provider, command in commands.items():
        try:
            proc = run(command, check=False, timeout=20)
        except FileNotFoundError:
            values[provider] = {"available": False, "version": "missing"}
            continue
        text = (proc.stdout or proc.stderr).strip().splitlines()
        values[provider] = {
            "available": proc.returncode == 0,
            "version": text[-1] if text else "unknown",
        }
    return values


def _mcp_config(stub: Path, spool: Path) -> dict[str, Any]:
    return {
        "mcpServers": {
            "gate_e": {
                "command": sys.executable,
                "args": [str(stub)],
                "env": {"GATE_E_MCP_SPOOL": str(spool)},
            }
        }
    }


def _provider_command(provider: str, workdir: Path, spool: Path) -> list[str]:
    stub = Path(__file__).with_name("gate_e_mcp_stub.py").resolve()
    prompt = (
        "Use the gate_e MCP tool always_ok exactly once, then use always_fail "
        "exactly once. Both calls are required. Do not use any other tool. "
        "After both calls, reply only with done."
    )
    config = _mcp_config(stub, spool)
    if provider == "codex":
        entry = config["mcpServers"]["gate_e"]
        return [
            "codex", "exec", prompt, "--json", "--skip-git-repo-check",
            "--sandbox", "workspace-write", "--ignore-user-config",
            "-c", f"mcp_servers.gate_e.command={json.dumps(entry['command'])}",
            "-c", f"mcp_servers.gate_e.args={json.dumps(entry['args'])}",
            "-c", (
                "mcp_servers.gate_e.env.GATE_E_MCP_SPOOL="
                + json.dumps(entry["env"]["GATE_E_MCP_SPOOL"])
            ),
            "-c", 'mcp_servers.gate_e.default_tools_approval_mode="approve"',
        ]
    if provider == "claude":
        path = workdir / "claude-mcp.json"
        path.write_text(json.dumps(config), encoding="utf-8")
        return [
            "claude", "-p", prompt, "--output-format", "stream-json", "--verbose",
            "--mcp-config", str(path), "--strict-mcp-config",
            "--dangerously-skip-permissions", "--no-session-persistence",
        ]
    if provider == "kimi":
        path = workdir / ".mcp.json"
        path.write_text(json.dumps(config), encoding="utf-8")
        return ["kimi", "-p", prompt, "--output-format", "stream-json"]
    if provider == "agy":
        agents = workdir / ".agents"
        agents.mkdir(exist_ok=True)
        (agents / "mcp_config.json").write_text(json.dumps(config), encoding="utf-8")
        return ["agy", "--print", prompt, "--dangerously-skip-permissions"]
    raise SmokeError(f"unknown provider: {provider}")


def run_provider_canaries(workspace: Path) -> dict[str, dict[str, Any]]:
    results: dict[str, dict[str, Any]] = {}
    versions = provider_versions()
    for provider in PROVIDERS:
        root = workspace / "providers" / provider
        root.mkdir(parents=True)
        run(["git", "init", "--quiet", "-b", "main"], cwd=root)
        spool = root / "outcomes.ndjson"
        passed = False
        last_proc: subprocess.CompletedProcess[str] | None = None
        observed: list[dict[str, str]] = []
        attempts = 0
        if not versions[provider]["available"]:
            results[provider] = {
                **versions[provider],
                "live_canary": "fail",
                "attempts": attempts,
                "observed": observed,
            }
            continue
        for attempt in (1, 2):
            spool.unlink(missing_ok=True)
            command = _provider_command(provider, root, spool)
            try:
                last_proc = run(command, cwd=root, check=False, timeout=300)
            except FileNotFoundError:
                versions[provider] = {"available": False, "version": "missing"}
                break
            attempts = attempt
            observed = []
            if spool.exists():
                for line in spool.read_text(encoding="utf-8").splitlines():
                    value = json.loads(line)
                    if isinstance(value, dict):
                        observed.append(value)
            expected = {
                ("gate_e::always_ok", "succeeded"),
                ("gate_e::always_fail", "failed"),
            }
            actual = {(row.get("tool_key"), row.get("outcome")) for row in observed}
            if last_proc.returncode == 0 and expected <= actual:
                passed = True
                results[provider] = {
                    **versions[provider],
                    "live_canary": "pass",
                    "attempts": attempt,
                    "observed": [
                        {"tool_key": key, "outcome": outcome}
                        for key, outcome in sorted(expected)
                    ],
                }
                break
        if not passed:
            # Raw provider output is private failure evidence only and is never retained.
            if last_proc is not None:
                (root / "stdout.private").write_text(last_proc.stdout, encoding="utf-8")
                (root / "stderr.private").write_text(last_proc.stderr, encoding="utf-8")
            results[provider] = {
                **versions[provider],
                "live_canary": "fail",
                "attempts": attempts,
                "observed": observed,
            }
            if last_proc is not None:
                results[provider]["exit_code"] = last_proc.returncode
    return results


def assert_body_free(paths: list[Path]) -> None:
    def visit(value: Any, path: Path) -> None:
        if isinstance(value, dict):
            bad = FORBIDDEN_BODY_KEYS.intersection(key.lower() for key in value)
            if bad:
                raise SmokeError(f"body-shaped key(s) {sorted(bad)} retained in {path.name}")
            for item in value.values():
                visit(item, path)
        elif isinstance(value, list):
            for item in value:
                visit(item, path)

    for path in paths:
        raw = path.read_text(encoding="utf-8")
        lowered = raw.lower()
        if any(token.lower() in lowered for token in FORBIDDEN_TEXT):
            raise SmokeError(f"secret sentinel retained in {path.name}")
        visit(json.loads(raw), path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--toolgraph-root", type=Path, default=ROOT)
    parser.add_argument("--syncmill-root", type=Path, required=True)
    parser.add_argument("--tracegraph-root", type=Path, required=True)
    parser.add_argument("--toolgraph-ref", required=True)
    parser.add_argument("--syncmill-ref", required=True)
    parser.add_argument("--tracegraph-ref", required=True)
    parser.add_argument("--artifacts-dir", type=Path, required=True)
    args = parser.parse_args()
    if run(["docker", "info"], check=False, timeout=20).returncode:
        raise SmokeError("Docker is required; start it before any paid provider canary")

    roots = {
        "toolgraph": args.toolgraph_root.resolve(),
        "syncmill": args.syncmill_root.resolve(),
        "tracegraph": args.tracegraph_root.resolve(),
    }
    refs = {
        "toolgraph": args.toolgraph_ref,
        "syncmill": args.syncmill_ref,
        "tracegraph": args.tracegraph_ref,
    }
    shas = {name: resolve_sha(roots[name], refs[name]) for name in roots}
    tmp_root = "/private/tmp" if Path("/private/tmp").is_dir() else None
    workspace = Path(tempfile.mkdtemp(prefix="gate-e-operational-", dir=tmp_root))
    container = f"gate-e-{uuid4().hex[:12]}"
    success = False
    try:
        clones = {name: workspace / name for name in roots}
        for name in roots:
            clone_local(roots[name], clones[name], shas[name])
        port = free_port()
        run(
            [
                "docker", "run", "-d", "--name", container,
                "-p", f"127.0.0.1:{port}:7687", "-e", "NEO4J_AUTH=neo4j/" + PASSWORD,
                NEO4J_IMAGE,
            ],
            timeout=120,
        )
        env = dict(os.environ)
        env.update({
            "NEO4J_URI": f"bolt://127.0.0.1:{port}",
            "NEO4J_USER": "neo4j",
            "NEO4J_PASSWORD": PASSWORD,
            "TOOLGRAPH_ROOT": str(clones["toolgraph"]),
            "UV_CACHE_DIR": os.environ.get("UV_CACHE_DIR", "/private/tmp/gate-e-uv-cache"),
        })
        wait_for_neo4j(env)
        run(["uv", "run", "toolgraph", "init-schema"], cwd=clones["toolgraph"], env=env)
        servers = workspace / "servers.yaml"
        servers.write_text(
            "servers:\n  - name: syncmill\n    transport: stdio\n"
            f"    command: uv\n    args: [\"run\", \"--project\", \"{clones['syncmill']}\", \"syncmill\", \"serve\"]\n",
            encoding="utf-8",
        )
        crawl(clones["syncmill"], clones["toolgraph"], env, servers)
        governance = clones["toolgraph"] / "examples" / GOVERNANCE_NAME
        run(
            ["uv", "run", "toolgraph", "ingest-manifest", "--governance", str(governance)],
            cwd=clones["toolgraph"], env=env,
        )
        target = workspace / "target"
        make_target(target)
        bin_dir = workspace / "bin"
        write_agent_stubs(bin_dir)
        matrix = []
        matrix.append(strict_scenario(
            name="known-allow", expected_dispatch=True, base_env=env,
            syncmill=clones["syncmill"], toolgraph=clones["toolgraph"], bin_dir=bin_dir,
            target=target, workspace=workspace, candidates=["syncmill::shared_read"],
        ))
        matrix.append(strict_scenario(
            name="expected-exception", expected_dispatch=True, base_env=env,
            syncmill=clones["syncmill"], toolgraph=clones["toolgraph"], bin_dir=bin_dir,
            target=target, workspace=workspace, agent="kimi-code", mapped_agent="kimi",
            candidates=["syncmill::board_stats"],
        ))
        matrix.append(strict_scenario(
            name="explicit-violation", expected_dispatch=False, base_env=env,
            syncmill=clones["syncmill"], toolgraph=clones["toolgraph"], bin_dir=bin_dir,
            target=target, workspace=workspace, candidates=["syncmill::board_stats"],
        ))
        matrix.append(strict_scenario(
            name="unknown-tool", expected_dispatch=False, base_env=env,
            syncmill=clones["syncmill"], toolgraph=clones["toolgraph"], bin_dir=bin_dir,
            target=target, workspace=workspace, candidates=["ghost::missing"],
        ))
        empty_servers = workspace / "servers-empty.yaml"
        empty_servers.write_text("servers: []\n", encoding="utf-8")
        crawl(clones["syncmill"], clones["toolgraph"], env, empty_servers)
        matrix.append(strict_scenario(
            name="drift-default-warn", expected_dispatch=True, base_env=env,
            syncmill=clones["syncmill"], toolgraph=clones["toolgraph"], bin_dir=bin_dir,
            target=target, workspace=workspace, candidates=["syncmill::shared_read"],
        ))
        matrix.append(strict_scenario(
            name="drift-block-override", expected_dispatch=False, base_env=env,
            syncmill=clones["syncmill"], toolgraph=clones["toolgraph"], bin_dir=bin_dir,
            target=target, workspace=workspace, candidates=["syncmill::shared_read"],
            drift_policy="block",
        ))
        crawl(clones["syncmill"], clones["toolgraph"], env, servers)
        matrix.append(strict_scenario(
            name="unavailable-default-block", expected_dispatch=False, base_env=env,
            syncmill=clones["syncmill"], toolgraph=clones["toolgraph"], bin_dir=bin_dir,
            target=target, workspace=workspace, candidates=["syncmill::shared_read"],
            command=["/usr/bin/false"],
        ))
        matrix.append(strict_scenario(
            name="unavailable-warn-override", expected_dispatch=True, base_env=env,
            syncmill=clones["syncmill"], toolgraph=clones["toolgraph"], bin_dir=bin_dir,
            target=target, workspace=workspace, candidates=["syncmill::shared_read"],
            command=["/usr/bin/false"], unavailable_policy="warn",
        ))
        matrix.append(strict_scenario(
            name="enabled-false-rollback", expected_dispatch=True, base_env=env,
            syncmill=clones["syncmill"], toolgraph=clones["toolgraph"], bin_dir=bin_dir,
            target=target, workspace=workspace, candidates=[], enabled=False,
            command=["/usr/bin/false"],
        ))
        providers = run_provider_canaries(workspace)
        p4 = p4_pipeline(
            syncmill=clones["syncmill"], tracegraph=clones["tracegraph"],
            toolgraph=clones["toolgraph"], env=env, workspace=workspace,
        )
        retained = p4.pop("retained")
        summary = {
            "schema_version": 1,
            "refs": shas,
            "providers": providers,
            "gate_e": {
                "verdict": "go" if (
                    all(row["actual_dispatch"] == row["expected_dispatch"] for row in matrix)
                    and all(row["live_canary"] == "pass" for row in providers.values())
                ) else "no-go",
                "scenarios": matrix,
                "automatic_approval": False,
            },
            "p4": {"verdict": "go", **p4, "automatic_governance_change": False},
            "privacy_scan": "pass",
        }
        summary_source = workspace / "gate-e-p4-summary.json"
        summary_source.write_text(
            json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        assert_body_free([*retained, summary_source])
        args.artifacts_dir.mkdir(parents=True, exist_ok=True)
        for path in [*retained, summary_source]:
            shutil.copy2(path, args.artifacts_dir / path.name)
        print(json.dumps(summary, indent=2, sort_keys=True))
        success = summary["gate_e"]["verdict"] == "go" and summary["p4"]["verdict"] == "go"
        return 0 if success else 1
    finally:
        run(["docker", "rm", "-f", container], check=False, timeout=30)
        if success:
            shutil.rmtree(workspace, ignore_errors=True)
        else:
            print(f"operational workspace preserved after failure: {workspace}", file=sys.stderr)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (SmokeError, subprocess.TimeoutExpired) as exc:
        print(f"Gate E operational harness failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
