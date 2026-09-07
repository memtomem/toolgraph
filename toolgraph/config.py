"""Runtime settings, sourced from the environment (and an optional .env file)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
import json
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(dotenv_path=Path.cwd() / ".env", override=False)

_explicit_config_path: Path | None = None


def runtime_config_path() -> Path:
    value = _explicit_config_path or os.getenv("TOOLGRAPH_CONFIG", ".toolgraph/config.json")
    return Path(value).expanduser()


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
