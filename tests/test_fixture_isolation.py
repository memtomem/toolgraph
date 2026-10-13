"""The shared graph fixtures must never reach a developer's own database."""

from __future__ import annotations

import json

import pytest

from toolgraph import config
from toolgraph.graph import driver, schema


@pytest.mark.neo4j  # reaches the container through getfixturevalue
def test_graph_fixture_ignores_ambient_ladybug_configuration(
    tmp_path, monkeypatch, request
):
    import ladybug  # noqa: F401  # the dual-backend suite requires the extra

    # A checkout where someone ran `toolgraph init` and also exported the
    # backend: both ambient sources point at a local Ladybug database that
    # holds real work.
    state_dir = tmp_path / ".toolgraph"
    db_path = state_dir / "toolgraph.lbug"
    monkeypatch.setattr(
        config, "settings", config.Settings(backend="ladybug", db_path=db_path)
    )
    driver.close_driver()
    schema.init_schema()
    with driver.session() as s:
        s.run("CREATE (:Agent {id: 'developer-work'})")
    driver.close_driver()
    (state_dir / "config.json").write_text(
        json.dumps({"schema_version": 1, "backend": "ladybug", "db_path": str(db_path)})
    )
    before = db_path.read_bytes()

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TOOLGRAPH_BACKEND", "ladybug")
    monkeypatch.setenv("TOOLGRAPH_DB_PATH", str(db_path))
    monkeypatch.setenv("TOOLGRAPH_CONFIG", str(state_dir / "config.json"))

    # The fixture wipes its graph with MATCH (n) DETACH DELETE n.
    request.getfixturevalue("graph")

    assert config.settings.backend == "neo4j"
    assert driver.backend_name() == "neo4j"
    assert db_path.read_bytes() == before

    monkeypatch.setattr(
        config, "settings", config.Settings(backend="ladybug", db_path=db_path)
    )
    driver.close_driver()
    with driver.acquire_readonly_session() as s:
        rows = [row["id"] for row in s.run("MATCH (a:Agent) RETURN a.id AS id")]
    driver.close_driver()
    assert rows == ["developer-work"]
