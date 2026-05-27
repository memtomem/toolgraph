"""Runtime settings, sourced from the environment (and an optional .env file)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Settings:
    # default_factory so each Settings() re-reads the environment (tests build
    # their own Settings; the module-level singleton reads it once at import).
    neo4j_uri: str = field(default_factory=lambda: os.getenv("NEO4J_URI", "bolt://localhost:7687"))
    neo4j_user: str = field(default_factory=lambda: os.getenv("NEO4J_USER", "neo4j"))
    neo4j_password: str = field(default_factory=lambda: os.getenv("NEO4J_PASSWORD", "toolgraph-dev"))
    neo4j_database: str = field(default_factory=lambda: os.getenv("NEO4J_DATABASE", "neo4j"))
    servers_config: Path = field(
        default_factory=lambda: Path(os.getenv("TOOLGRAPH_SERVERS", "servers.yaml"))
    )
    governance_config: Path = field(
        default_factory=lambda: Path(os.getenv("TOOLGRAPH_GOVERNANCE", "governance.yaml"))
    )


settings = Settings()
