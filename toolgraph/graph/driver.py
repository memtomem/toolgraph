"""Neo4j driver lifecycle and session helpers."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from neo4j import Driver, GraphDatabase, Session

from toolgraph import config

_driver: Driver | None = None


def get_driver() -> Driver:
    """Return the process-wide driver, creating it on first use."""
    global _driver
    if _driver is None:
        _driver = GraphDatabase.driver(
            config.settings.neo4j_uri,
            auth=(config.settings.neo4j_user, config.settings.neo4j_password),
        )
    return _driver


def verify_connectivity() -> None:
    """Raise if the database is unreachable or auth is wrong."""
    get_driver().verify_connectivity()


@contextmanager
def session() -> Iterator[Session]:
    """Yield a session bound to the configured database."""
    with get_driver().session(database=config.settings.neo4j_database) as s:
        yield s


def close_driver() -> None:
    global _driver
    if _driver is not None:
        _driver.close()
        _driver = None
