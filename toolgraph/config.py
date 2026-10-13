"""Runtime settings, sourced from the environment and a runtime config file.

The CLI also loads a ``.env`` file from the working directory before it
resolves settings (``load_dotenv_file``). Importing this module does not:
a library caller decides its own environment.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

_explicit_config_path: Path | None = None


def load_dotenv_file() -> None:
    """Load ``./.env`` without overriding variables already set (CLI only)."""
    load_dotenv(dotenv_path=Path.cwd() / ".env", override=False)


def runtime_config_path() -> Path:
    value = _explicit_config_path or os.getenv("TOOLGRAPH_CONFIG", ".toolgraph/config.json")
    return Path(value).expanduser()


def check_config_health() -> dict[str, Any]:
    """Inspect active configuration file existence, schema validity, and permissions."""
    path = runtime_config_path()
    exists = path.is_file()
    if not exists:
        return {
            "path": str(path),
            "exists": False,
            "valid_json": False,
            "permissions_ok": None,
            "permissions": None,
            "healthy": False,
            "detail": f"configuration file not found at {path}",
            "backend_source": backend_source(),
        }

    permissions_ok = True
    perms_octal = None
    if os.name != "nt":
        mode = path.stat().st_mode & 0o777
        perms_octal = oct(mode)
        permissions_ok = (mode == 0o600)

    valid_json = False
    detail = "ok"
    try:
        cfg = _runtime_config()
        valid_json = bool(cfg)
    except Exception as exc:
        detail = str(exc)
        valid_json = False

    healthy = exists and valid_json and (permissions_ok is not False)
    return {
        "path": str(path),
        "exists": exists,
        "valid_json": valid_json,
        "permissions_ok": permissions_ok,
        "permissions": perms_octal,
        "healthy": healthy,
        "detail": detail,
        "backend_source": backend_source(),
    }


class RuntimeConfigurationError(RuntimeError):
    """The selected runtime configuration cannot be loaded."""


_MAX_CONFIG_BYTES = 1_000_000


def _runtime_config() -> dict:
    path = runtime_config_path()
    if not path.exists():
        return {}
    try:
        # Bounded read from one open descriptor (stat-then-read is a TOCTOU).
        with path.open("rb") as handle:
            raw = handle.read(_MAX_CONFIG_BYTES + 1)
        if len(raw) > _MAX_CONFIG_BYTES:
            raise RuntimeConfigurationError(
                f"invalid Toolgraph runtime config at {path}: exceeds"
                f" the {_MAX_CONFIG_BYTES}-byte limit"
            )
        value = json.loads(raw.decode("utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise RuntimeConfigurationError(f"invalid Toolgraph runtime config at {path}: {exc}") from exc
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise RuntimeConfigurationError(f"unsupported Toolgraph runtime config at {path}")
    return value


def backend_source() -> str:
    """Which input decides the backend: config-flag, env, config-file or default."""
    if _explicit_config_path is not None:
        return "config-flag"
    if os.getenv("TOOLGRAPH_BACKEND"):
        return "env"
    try:
        if "backend" in _runtime_config():
            return "config-file"
    except RuntimeConfigurationError:
        return "config-file"
    return "default"


def _same_path(left: str, right: str) -> bool:
    return Path(left).expanduser().resolve() == Path(right).expanduser().resolve()


def ambient_conflict() -> str | None:
    """Describe a disagreement between the runtime config file and the env.

    Without ``--config``, ``TOOLGRAPH_BACKEND`` / ``TOOLGRAPH_DB_PATH`` used to
    win over the file ``toolgraph init`` wrote, silently. A command then ran
    against a different database than the one the operator initialized.
    Agreement, or only one source being set, is not a conflict.
    """
    if _explicit_config_path is not None:
        return None
    runtime = _runtime_config()
    problems = []
    env_backend = os.getenv("TOOLGRAPH_BACKEND")
    file_backend = runtime.get("backend")
    if (
        env_backend
        and file_backend
        # driver.backend_name() normalizes the same way, so "LADYBUG" and
        # " ladybug " select the same backend and are not a conflict.
        and env_backend.lower().strip() != str(file_backend).lower().strip()
    ):
        problems.append(f"TOOLGRAPH_BACKEND={env_backend} but the config file says {file_backend}")
    env_db = os.getenv("TOOLGRAPH_DB_PATH")
    file_db = runtime.get("db_path")
    if env_db and file_db and not _same_path(env_db, str(file_db)):
        problems.append(f"TOOLGRAPH_DB_PATH={env_db} but the config file says {file_db}")
    if not problems:
        return None
    return (
        f"conflicting backend configuration ({runtime_config_path()}): "
        + "; ".join(problems)
        + ". Pass --config to choose the file, or unset the variable."
    )


def _backend_default() -> str:
    runtime = _runtime_config()
    if _explicit_config_path is not None:
        return str(runtime.get("backend", "neo4j"))
    return os.getenv("TOOLGRAPH_BACKEND") or str(runtime.get("backend", "neo4j"))


def _db_path_default() -> Path:
    runtime = _runtime_config()
    if _explicit_config_path is not None:
        value = runtime.get("db_path", ".toolgraph/toolgraph.lbug")
    else:
        value = os.getenv("TOOLGRAPH_DB_PATH") or runtime.get(
            "db_path", ".toolgraph/toolgraph.lbug"
        )
    return Path(str(value)).expanduser()


@dataclass(frozen=True)
class Settings:
    # default_factory so each Settings() re-reads the environment (tests build
    # their own Settings; the module-level singleton reads it once on first access).
    neo4j_uri: str = field(default_factory=lambda: os.getenv("NEO4J_URI", "bolt://localhost:7687"))
    neo4j_user: str = field(default_factory=lambda: os.getenv("NEO4J_USER", "neo4j"))
    neo4j_password: str = field(default_factory=lambda: os.getenv("NEO4J_PASSWORD", "toolgraph-dev"))
    neo4j_database: str = field(default_factory=lambda: os.getenv("NEO4J_DATABASE", "neo4j"))
    backend: str = field(default_factory=_backend_default)
    db_path: Path = field(default_factory=_db_path_default)
    servers_config: Path = field(
        default_factory=lambda: Path(os.getenv("TOOLGRAPH_SERVERS", "servers.yaml"))
    )
    governance_config: Path = field(
        default_factory=lambda: Path(os.getenv("TOOLGRAPH_GOVERNANCE", "governance.yaml"))
    )


def __getattr__(name: str):
    if name == "settings":
        # The implicit singleton is the only settings object built from
        # ambient sources, so it is where a conflict between them is refused.
        # Settings(...) built with explicit fields is the caller's choice.
        conflict = ambient_conflict()
        if conflict:
            raise RuntimeConfigurationError(conflict)
        value = Settings()
        globals()[name] = value
        return value
    raise AttributeError(name)


def configure(config_path: Path) -> None:
    """Select an explicit runtime config (CLI > env > cwd .env > default)."""
    global _explicit_config_path, settings
    selected = config_path.expanduser().resolve()
    if not selected.is_file():
        raise RuntimeConfigurationError(f"Toolgraph runtime config does not exist: {selected}")
    previous = _explicit_config_path
    _explicit_config_path = selected
    try:
        settings = Settings()
    except Exception:
        _explicit_config_path = previous
        raise
    # A cached connection still points at the previous database.
    from toolgraph.graph import driver

    driver.close_driver()
