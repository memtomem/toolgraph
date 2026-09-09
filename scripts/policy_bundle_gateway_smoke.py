#!/usr/bin/env python3
"""Black-box Toolgraph policy-bundle -> memtomem-stm gateway smoke.

Both source checkouts are read-only inputs. Graph state, proxy configuration,
cache files, invocation sentinels, and evidence are isolated in a temporary
workspace. The resulting summary deliberately contains no tool arguments or
response bodies.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
import yaml

from ecosystem_smoke import SmokeError, dependency_evidence, require_clean_checkout


ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "examples" / "policy_gateway_server.py"


def run(
    argv: list[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
    check: bool = True,
    timeout: int = 180,
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


def run_json(
    argv: list[str], *, cwd: Path, env: dict[str, str] | None = None
) -> dict[str, Any]:
    proc = run(argv, cwd=cwd, env=env)
    try:
        value = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise SmokeError(f"command did not emit JSON: {' '.join(argv)}") from exc
    if not isinstance(value, dict):
        raise SmokeError(f"command emitted non-object JSON: {' '.join(argv)}")
    return value


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def git_state(root: Path) -> dict[str, Any]:
    sha = run(["git", "rev-parse", "HEAD"], cwd=root).stdout.strip()
    dirty = bool(run(["git", "status", "--porcelain"], cwd=root).stdout.strip())
    return {"sha": sha, "dirty": dirty}


def venv_python(root: Path) -> Path:
    candidates = (
        root / ".venv" / "bin" / "python",
        root / ".venv" / "Scripts" / "python.exe",
    )
    for candidate in candidates:
        if candidate.is_file():
            # Do not resolve the venv's python symlink to the base interpreter;
            # that would drop the venv site-packages on some uv installations.
            return candidate.absolute()
    raise SmokeError(f"missing synced virtualenv under {root}")


def toolgraph_cli() -> Path:
    candidates = (
        ROOT / ".venv" / "bin" / "toolgraph",
        ROOT / ".venv" / "Scripts" / "toolgraph.exe",
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    raise SmokeError("missing Toolgraph console script; run uv sync first")


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def write_yaml(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(value, sort_keys=False), encoding="utf-8")


def governance(*, allow_read: bool) -> dict[str, Any]:
    grants: list[dict[str, Any]] = [
        {
            "agent": "vibe-coder",
            "tool": "policy-gateway::publish_note",
            "granted_by": "policy-gateway-smoke",
            "provenance": {
                "source": "operator_asserted",
                "confidence": "high",
                "evidence": "policy-gateway-smoke:publish-grant",
            },
        }
    ]
    if allow_read:
        grants.insert(
            0,
            {
                "agent": "vibe-coder",
                "tool": "policy-gateway::read_note",
                "granted_by": "policy-gateway-smoke",
                "provenance": {
                    "source": "operator_asserted",
                    "confidence": "high",
                    "evidence": "policy-gateway-smoke:read-grant",
                },
            },
        )
    return {
        "agents": ["vibe-coder"],
        "policies": [
            {
                "id": "draft-publish-deny",
                "effect": "DENY",
                "scope": "demo-drafts",
                "description": "Publishing demo drafts requires review.",
            }
        ],
        "grants": grants,
        "data_access": [
            {
                "tool": "policy-gateway::read_note",
                "resource": "file:///demo/notes",
                "mode": "READS",
                "provenance": {
                    "source": "operator_asserted",
                    "confidence": "high",
                    "evidence": "policy-gateway-smoke:read-effect",
                },
            },
            {
                "tool": "policy-gateway::publish_note",
                "resource": "file:///demo/drafts",
                "mode": "WRITES",
                "provenance": {
                    "source": "operator_asserted",
                    "confidence": "high",
                    "evidence": "policy-gateway-smoke:publish-effect",
                },
            },
        ],
        "governed_by": {
            "file:///demo/drafts": [
                {
                    "policy": "draft-publish-deny",
                    "provenance": {
                        "source": "operator_asserted",
                        "confidence": "high",
                        "evidence": "policy-gateway-smoke:draft-policy",
                    },
                }
            ]
        },
    }


def proxy_config(
    *, bundle: Path, cache: Path, spool: Path, schema_version: str = "1"
) -> dict[str, Any]:
    return {
        "enabled": True,
        "upstream_servers": {
            "policy-gateway": {
                "command": sys.executable,
                "args": [str(SERVER)],
                "env": {
                    "POLICY_GATEWAY_SCHEMA_VERSION": schema_version,
                    "POLICY_GATEWAY_SPOOL": str(spool),
                },
                "prefix": "demo",
                "compression": "none",
                "cache": True,
                "surfacing_enabled": False,
            }
        },
        "cache": {
            "enabled": True,
            "db_path": str(cache),
            "tool_annotation_policy": "strict",
        },
        "metrics": {"enabled": False},
        "exposure": {"profile": "strict"},
        "toolgraph": {
            "enabled": True,
            "source": "bundle",
            "bundle_path": str(bundle),
            "agent_id": "vibe-coder",
            "query_profile": "strict",
            "server_name_map": {},
        },
    }


def mms(
    stm_python: Path,
    stm_root: Path,
    args: list[str],
    *,
    env: dict[str, str],
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    return run(
        [str(stm_python), "-m", "memtomem_stm", *args],
        cwd=stm_root,
        env=env,
        check=check,
    )


def mms_json(
    stm_python: Path, stm_root: Path, args: list[str], *, env: dict[str, str]
) -> dict[str, Any]:
    proc = mms(stm_python, stm_root, args, env=env)
    try:
        value = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise SmokeError(f"mms did not emit JSON: {' '.join(args)}") from exc
    if not isinstance(value, dict):
        raise SmokeError(f"mms emitted non-object JSON: {' '.join(args)}")
    return value


def compile_bundle(
    *,
    tg: Path,
    env: dict[str, str],
    governance_path: Path,
    profile: str,
    bundle: Path,
) -> dict[str, Any]:
    run(
        [str(tg), "ingest-manifest", "--governance", str(governance_path), "--strict-drift"],
        cwd=ROOT,
        env=env,
    )
    summary = run_json(
        [
            str(tg),
            "policy",
            "compile",
            "--agent",
            "vibe-coder",
            "--profile",
            profile,
            "--output",
            str(bundle),
        ],
        cwd=ROOT,
        env=env,
    )
    if summary.get("bundle_digest") != sha256(bundle):
        raise SmokeError("producer summary digest does not match bundle bytes")
    return summary


def spool_count(path: Path, tool: str) -> int:
    if not path.exists():
        return 0
    count = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise SmokeError("invalid invocation sentinel") from exc
        if value == {"tool": tool}:
            count += 1
    return count


def stm_process_env(config: Path, *, observability: bool) -> dict[str, str]:
    env = dict(os.environ)
    home = config.parent / "home"
    runtime = home / "run"
    runtime.mkdir(parents=True, exist_ok=True, mode=0o700)
    runtime.chmod(0o700)
    env.update(
        {
            "HOME": str(home),
            "XDG_RUNTIME_DIR": str(runtime),
            "MEMTOMEM_STM_PROXY__CONFIG_PATH": str(config),
            "MEMTOMEM_STM_SURFACING__ENABLED": "false",
            "MEMTOMEM_STM_ADVERTISE_OBSERVABILITY_TOOLS": (
                "true" if observability else "false"
            ),
        }
    )
    for key in list(env):
        if key.startswith("TOOLGRAPH_") or key.startswith("NEO4J_"):
            env.pop(key)
    return env


def stm_params(stm_python: Path, config: Path, *, observability: bool) -> StdioServerParameters:
    return StdioServerParameters(
        command=str(stm_python),
        args=["-m", "memtomem_stm"],
        env=stm_process_env(config, observability=observability),
        cwd=str(stm_python.parents[2]),
    )


async def strict_live_reload(
    *,
    stm_python: Path,
    stm_root: Path,
    config: Path,
    errlog: Path,
    spool: Path,
    publish_deny: Any,
) -> None:
    with errlog.open("w", encoding="utf-8") as errors:
        async with stdio_client(stm_params(stm_python, config, observability=False), errlog=errors) as (
            read_stream,
            write_stream,
        ):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                names = {tool.name for tool in (await session.list_tools()).tools}
                if "demo__read_note" not in names or "demo__publish_note" in names:
                    raise SmokeError("strict startup advertised the wrong policy-gated tools")
                first = await session.call_tool("demo__read_note", {"note_id": "first"})
                if first.is_error:
                    raise SmokeError("eligible strict call failed")
                if spool_count(spool, "read_note") != 1:
                    raise SmokeError("eligible call did not reach the upstream exactly once")

                publish_deny()
                explained = mms_json(
                    stm_python,
                    stm_root,
                    [
                        "gateway",
                        "explain",
                        "policy-gateway::read_note",
                        "--config",
                        str(config),
                        "--json",
                    ],
                    env=stm_process_env(config, observability=False),
                )
                if (explained.get("decision"), explained.get("reason")) != (
                    "rejected",
                    "NOT_GRANTED",
                ):
                    raise SmokeError("gateway explain did not expose the fresh denial")
                denied = await session.call_tool("demo__read_note", {"note_id": "first"})
                if not denied.is_error:
                    raise SmokeError("fresh strict denial did not beat the warm cache")
                if spool_count(spool, "read_note") != 1:
                    raise SmokeError("strict denial reached the upstream")


async def assert_list(
    *,
    stm_python: Path,
    config: Path,
    errlog: Path,
    expected_present: bool,
    review_call: bool = False,
    spool: Path | None = None,
    expected_would_block: int | None = 1,
) -> int:
    with errlog.open("w", encoding="utf-8") as errors:
        async with stdio_client(
            stm_params(stm_python, config, observability=review_call), errlog=errors
        ) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                names = {tool.name for tool in (await session.list_tools()).tools}
                if ("demo__read_note" in names) is not expected_present:
                    raise SmokeError("restart did not apply the expected tools/list policy")
                if not review_call:
                    return 0
                if spool is None:
                    raise SmokeError("review call requires an invocation sentinel")
                before = spool_count(spool, "read_note")
                called = await session.call_tool("demo__read_note", {"note_id": "review" if expected_would_block else "disabled"})
                if called.is_error:
                    raise SmokeError("review mode blocked a would-block call")
                if spool_count(spool, "read_note") != before + 1:
                    raise SmokeError("review call did not reach the upstream exactly once")
                health = await session.call_tool("stm_proxy_health", {})
                if health.is_error:
                    raise SmokeError("review health surface was not callable")
                if expected_would_block is None:
                    return 0
                health_text = "\n".join(c.text for c in health.content if c.type == "text")
                return review_counter(health_text, expected_would_block)


def review_counter(health_text: str, expected: int) -> int:
    # stm_proxy_health's public contract is a human-readable text report.
    counts = re.findall(r"^  review would-block calls: ([0-9]+)$", health_text, re.MULTILINE)
    if counts != [str(expected)]:
        raise SmokeError("review health did not record the expected would-block count")
    return int(counts[0])



async def expect_start_failure(
    *, stm_python: Path, config: Path, errlog: Path
) -> None:
    async def initialize() -> None:
        with errlog.open("w", encoding="utf-8") as errors:
            async with stdio_client(
                stm_params(stm_python, config, observability=False), errlog=errors
            ) as (read_stream, write_stream):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()

    failed = False
    try:
        # Bound process launch, stdio setup, and initialization as one operation.
        await asyncio.wait_for(initialize(), timeout=12)
    except (Exception, BaseExceptionGroup):
        failed = True
    if not failed:
        raise SmokeError("strict gateway initialized with an unusable bundle")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--memtomem-stm-root", type=Path, required=True)
    parser.add_argument("--artifacts-dir", type=Path, required=True)
    parser.add_argument("--keep-workspace", action="store_true")
    parser.add_argument("--toolgraph-ref", required=True, help="Full clean Toolgraph HEAD SHA")
    parser.add_argument("--memtomem-stm-ref", required=True, help="Full clean STM HEAD SHA")
    args = parser.parse_args()

    stm_root = args.memtomem_stm_root.expanduser().resolve()
    artifacts = args.artifacts_dir.expanduser().resolve()
    artifacts.mkdir(parents=True, exist_ok=True)
    workspace = Path(tempfile.mkdtemp(prefix="toolgraph-policy-gateway-smoke-"))
    summary_path = artifacts / "policy-bundle-gateway-summary.json"
    refs: dict[str, dict[str, Any]] = {}
    success = False
    try:
        # Keep ref discovery inside the artifact-producing failure boundary.
        refs = {"toolgraph": git_state(ROOT), "memtomem_stm": git_state(stm_root)}
        require_clean_checkout(ROOT, args.toolgraph_ref)
        require_clean_checkout(stm_root, args.memtomem_stm_ref)
        dependencies = {
            "toolgraph": dependency_evidence(ROOT),
            "memtomem_stm": dependency_evidence(stm_root),
        }
        stm_python = venv_python(stm_root)
        tg = toolgraph_cli()
        stm_toolgraph_import_probe = run(
            [
                str(stm_python),
                "-c",
                "import importlib.util; raise SystemExit(importlib.util.find_spec('toolgraph') is not None)",
            ],
            cwd=stm_root,
            check=False,
        )
        # The probe exits 0 only when Toolgraph is absent from the STM environment.
        if stm_toolgraph_import_probe.returncode != 0:
            raise SmokeError("STM environment unexpectedly imports Toolgraph")

        state = workspace / "graph"
        config_path = workspace / "stm_proxy.json"
        active_bundle = workspace / "policy-bundle.json"
        cache_path = workspace / "proxy-cache.db"
        spool = workspace / "upstream-invocations.jsonl"
        servers = workspace / "servers.yaml"
        allow_manifest = workspace / "governance-allow.yaml"
        deny_manifest = workspace / "governance-deny.yaml"
        write_yaml(
            servers,
            {
                "servers": [
                    {
                        "name": "policy-gateway",
                        "transport": "stdio",
                        "command": sys.executable,
                        "args": [str(SERVER)],
                        "env": {"POLICY_GATEWAY_SCHEMA_VERSION": "1"},
                    }
                ]
            },
        )
        write_yaml(allow_manifest, governance(allow_read=True))
        write_yaml(deny_manifest, governance(allow_read=False))
        tg_env = {key: value for key, value in os.environ.items()
                  if not key.startswith(("TOOLGRAPH_", "NEO4J_", "GIT_"))}
        tg_env["TOOLGRAPH_CONFIG"] = str(state / "config.json")
        run([str(tg), "init", "--state-dir", str(state)], cwd=ROOT, env=tg_env)
        run([str(tg), "crawl", "--servers", str(servers)], cwd=ROOT, env=tg_env)

        initial = compile_bundle(
            tg=tg,
            env=tg_env,
            governance_path=allow_manifest,
            profile="strict",
            bundle=active_bundle,
        )
        write_json(
            config_path,
            proxy_config(bundle=active_bundle, cache=cache_path, spool=spool),
        )
        stm_env = stm_process_env(config_path, observability=False)
        mms(
            stm_python,
            stm_root,
            ["config", "validate", "--config", str(config_path)],
            env=stm_env,
        )
        mms(
            stm_python,
            stm_root,
            [
                "gateway",
                "mode",
                "strict",
                "--config",
                str(config_path),
                "--bundle",
                str(active_bundle),
                "--apply",
            ],
            env=stm_env,
        )
        status = mms_json(
            stm_python,
            stm_root,
            ["gateway", "status", "--config", str(config_path), "--json"],
            env=stm_env,
        )
        if status.get("valid") is not True:
            raise SmokeError("STM rejected the producer bundle")
        if status.get("bundle_digest") != initial["bundle_digest"]:
            raise SmokeError("STM status digest differs from the producer digest")
        if (status.get("eligible"), status.get("rejected")) != (1, 1):
            raise SmokeError("unexpected initial strict decision counts")
        explained = mms_json(
            stm_python,
            stm_root,
            [
                "gateway",
                "explain",
                "policy-gateway::read_note",
                "--config",
                str(config_path),
                "--json",
            ],
            env=stm_env,
        )
        if explained.get("decision") != "eligible":
            raise SmokeError("gateway explain disagreed with the producer")

        deny_summary: dict[str, Any] = {}

        def publish_deny() -> None:
            nonlocal deny_summary
            deny_summary = compile_bundle(
                tg=tg,
                env=tg_env,
                governance_path=deny_manifest,
                profile="strict",
                bundle=active_bundle,
            )

        asyncio.run(
            strict_live_reload(
                stm_python=stm_python,
                stm_root=stm_root,
                config=config_path,
                errlog=workspace / "strict-live.stderr.log",
                spool=spool,
                publish_deny=publish_deny,
            )
        )
        asyncio.run(
            assert_list(
                stm_python=stm_python,
                config=config_path,
                errlog=workspace / "strict-restart.stderr.log",
                expected_present=False,
            )
        )

        review = compile_bundle(
            tg=tg,
            env=tg_env,
            governance_path=deny_manifest,
            profile="review",
            bundle=active_bundle,
        )
        mms(
            stm_python,
            stm_root,
            [
                "gateway",
                "mode",
                "review",
                "--config",
                str(config_path),
                "--bundle",
                str(active_bundle),
                "--apply",
            ],
            env=stm_env,
        )
        would_block = asyncio.run(
            assert_list(
                stm_python=stm_python,
                config=config_path,
                errlog=workspace / "review.stderr.log",
                expected_present=True,
                review_call=True,
                spool=spool,
            )
        )

        drift_source = compile_bundle(
            tg=tg,
            env=tg_env,
            governance_path=allow_manifest,
            profile="strict",
            bundle=active_bundle,
        )
        drift_config = proxy_config(
            bundle=active_bundle,
            cache=cache_path,
            spool=spool,
            schema_version="2",
        )
        write_json(config_path, drift_config)
        asyncio.run(
            assert_list(
                stm_python=stm_python,
                config=config_path,
                errlog=workspace / "drift.stderr.log",
                expected_present=False,
            )
        )

        write_json(
            config_path,
            proxy_config(bundle=active_bundle, cache=cache_path, spool=spool),
        )
        active_bundle.write_text("not json\n", encoding="utf-8")
        invalid = mms_json(
            stm_python,
            stm_root,
            ["gateway", "status", "--config", str(config_path), "--json"],
            env=stm_env,
        )
        if invalid.get("valid") is not False:
            raise SmokeError("gateway status did not identify a corrupt bundle")
        asyncio.run(
            expect_start_failure(
                stm_python=stm_python,
                config=config_path,
                errlog=workspace / "corrupt.stderr.log",
            )
        )

        missing_bundle = workspace / "missing-policy-bundle.json"
        missing_config = proxy_config(bundle=missing_bundle, cache=cache_path, spool=spool)
        write_json(config_path, missing_config)
        asyncio.run(
            expect_start_failure(
                stm_python=stm_python,
                config=config_path,
                errlog=workspace / "missing.stderr.log",
            )
        )

        # The one-field rollback must bypass even a missing policy artifact.
        missing_config["toolgraph"]["enabled"] = False
        write_json(config_path, missing_config)
        asyncio.run(assert_list(
            stm_python=stm_python, config=config_path,
            errlog=workspace / "disabled.stderr.log", expected_present=True,
            review_call=True, spool=spool, expected_would_block=None,
        ))

        require_clean_checkout(ROOT, args.toolgraph_ref)
        require_clean_checkout(stm_root, args.memtomem_stm_ref)
        summary = {
            "schema_version": 1,
            "dependencies": dependencies,
            "status": "pass",
            "refs": refs,
            "bundles": {
                "strict_allow": initial["bundle_digest"],
                "strict_deny": deny_summary["bundle_digest"],
                "review": review["bundle_digest"],
                "drift_source": drift_source["bundle_digest"],
            },
            "graph_state": initial["graph_state"],
            "strict": {
                "initial_list_filtered": True,
                "warm_cache_call_blocked": True,
                "upstream_not_invoked": True,
                "restart_list_hidden": True,
                "contract_drift_hidden": True,
                "corrupt_bundle_failed_start": True,
                "missing_bundle_failed_start": True,
            },
            "review": {
                "rejected_tool_visible": True,
                "call_allowed": True,
                "would_block_calls": would_block,
            },
            "disabled_missing_bundle_call_allowed": True,
            "toolgraph_imported_by_stm": False,
        }
        write_json(summary_path, summary)
        print(json.dumps(summary, indent=2, sort_keys=True))
        success = True
        return 0
    except Exception as exc:
        write_json(
            summary_path,
            {
                "schema_version": 1,
                "status": "failed",
                "refs": refs,
                "error_type": type(exc).__name__,
            },
        )
        raise
    finally:
        if success and not args.keep_workspace:
            shutil.rmtree(workspace, ignore_errors=True)
        else:
            print(f"policy gateway smoke workspace: {workspace}", file=sys.stderr)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (SmokeError, subprocess.TimeoutExpired, OSError) as exc:
        print(f"policy gateway smoke failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
