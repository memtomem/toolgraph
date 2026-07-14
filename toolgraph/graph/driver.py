"""Backend lifecycle and transaction/session compatibility helpers.

The public module seam is intentionally small while graph services migrate to
domain-level ports. Neo4j remains the compatibility default; Ladybug is an
explicit embedded backend and never runs in the gateway call path.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from neo4j import Driver, GraphDatabase, Session

from toolgraph import config

_driver: Driver | None = None
_ladybug_database: Any | None = None


class BackendConfigurationError(RuntimeError):
    pass


class BackendLockedError(RuntimeError):
    pass


class _LadybugResult:
    def __init__(self, result: Any) -> None:
        self._rows: list[dict[str, Any]] = result.rows_as_dict().get_all()

    def __iter__(self):
        return iter(self._rows)

    def single(self) -> dict[str, Any] | None:
        if not self._rows:
            return None
        if len(self._rows) > 1:
            raise RuntimeError(f"expected at most one row, received {len(self._rows)}")
        return self._rows[0]


def _ladybug_query(query: str) -> str:
    """Translate the small remaining cross-dialect expression differences."""
    translated = query.replace("datetime()", "current_timestamp()")
    translated = translated.replace("type(", "label(")
    translated = translated.replace("labels(src)[0]", "label(src)")
    translated = translated.replace("labels(dst)[0]", "label(dst)")
    for field in (
        "read_only_hint",
        "destructive_hint",
        "idempotent_hint",
        "open_world_hint",
    ):
        translated = translated.replace(
            f"tool.{field}=t.{field}",
            f"tool.{field}=CAST(t.{field} AS BOOLEAN)",
        )
    return translated


class LadybugSession:
    """Minimal transaction facade used during the domain-port migration."""

    def __init__(self, database: Any) -> None:
        import ladybug as lb

        self._connection = lb.Connection(database)

    def run(self, query: str, **parameters: Any) -> _LadybugResult:
        result = self._connection.execute(_ladybug_query(query), parameters)
        return _LadybugResult(result)

    def execute_write(self, fn, *args):
        self._connection.execute("BEGIN TRANSACTION")
        try:
            value = fn(self, *args)
            self._connection.execute("COMMIT")
            return value
        except BaseException:
            # Ladybug aborts the explicit transaction immediately on some
            # binder/runtime errors; in that case there is nothing left to
            # roll back. Preserve the original exception.
            try:
                self._connection.execute("ROLLBACK")
            except RuntimeError as rollback_error:
                if "No active transaction" not in str(rollback_error):
                    raise
            raise

    def close(self) -> None:
        self._connection.close()


def backend_name() -> str:
    backend = config.settings.backend.lower().strip()
    if backend not in {"neo4j", "ladybug"}:
        raise BackendConfigurationError(
            f"unknown TOOLGRAPH_BACKEND {backend!r}; expected 'neo4j' or 'ladybug'"
        )
    return backend


def get_driver() -> Driver:
    """Return the process-wide Neo4j driver."""
    if backend_name() != "neo4j":
        raise BackendConfigurationError("Neo4j driver requested while backend=ladybug")
    global _driver
    if _driver is None:
        _driver = GraphDatabase.driver(
            config.settings.neo4j_uri,
            auth=(config.settings.neo4j_user, config.settings.neo4j_password),
        )
    return _driver


def _get_ladybug_database() -> Any:
    global _ladybug_database
    if _ladybug_database is not None:
        return _ladybug_database
    try:
        import ladybug as lb
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise BackendConfigurationError(
            "backend=ladybug requires the 'ladybug' extra: "
            "install with `uv sync --extra ladybug` or `pip install toolgraph[ladybug]`"
        ) from exc
    path = Path(config.settings.db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        _ladybug_database = lb.Database(path)
    except RuntimeError as exc:
        message = str(exc)
        if "lock" in message.lower() or "another process" in message.lower():
            raise BackendLockedError(
                f"BACKEND_LOCKED: Ladybug database {path} already has an owner"
            ) from exc
        raise
    return _ladybug_database


def verify_connectivity() -> None:
    if backend_name() == "neo4j":
        get_driver().verify_connectivity()
        return
    with session() as current:
        current.run("RETURN 1 AS ok").single()


@contextmanager
def session() -> Iterator[Session | LadybugSession]:
    if backend_name() == "neo4j":
        with get_driver().session(database=config.settings.neo4j_database) as current:
            yield current
        return
    current = LadybugSession(_get_ladybug_database())
    try:
        yield current
    finally:
        current.close()


def close_driver() -> None:
    global _driver, _ladybug_database
    if _driver is not None:
        _driver.close()
        _driver = None
    if _ladybug_database is not None:
        _ladybug_database.close()
        _ladybug_database = None
