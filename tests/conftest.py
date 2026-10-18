"""Test fixtures: a real, isolated Neo4j via testcontainers (no Cypher mocking)."""

from __future__ import annotations

import os

import pytest
from testcontainers.neo4j import Neo4jContainer

from tests._helpers import NEO4J_EXPECT_VERSION_ENV, expected_neo4j_version, neo4j_server_version
from toolgraph import config
from toolgraph.graph import driver, schema

# CI runs the suite at the supported floor by overriding the image; the
# version check in ``neo4j_container`` proves the override reached Docker.
NEO4J_IMAGE = os.environ.get("TOOLGRAPH_TEST_NEO4J_IMAGE", "neo4j:5.26-community")
TEST_PASSWORD = "testpass"
NO_DOCKER_ENV = "TOOLGRAPH_TEST_NO_DOCKER"


def pytest_collection_modifyitems(items):
    """Mark tests that depend on ``neo4j_container``, so ``-m "not neo4j"`` runs without Docker.

    The fixture closure covers tests that request ``graph`` or
    ``neo4j_container`` by name. A test that pulls them in through
    ``request.getfixturevalue`` is invisible here and must carry the marker
    itself; ``NO_DOCKER_ENV`` turns a missed one into a failure. Neither check
    sees a container started anywhere but ``neo4j_container``, so start it there.
    """
    for item in items:
        if "neo4j_container" in getattr(item, "fixturenames", ()):
            item.add_marker(pytest.mark.neo4j)


@pytest.fixture(scope="session")
def neo4j_container():
    if os.environ.get(NO_DOCKER_ENV):
        pytest.fail(
            f"{NO_DOCKER_ENV} is set but this test starts the Neo4j container; "
            "mark it with pytest.mark.neo4j"
        )
    expected = expected_neo4j_version(os.environ.get(NEO4J_EXPECT_VERSION_ENV))
    with Neo4jContainer(NEO4J_IMAGE, username="neo4j", password=TEST_PASSWORD) as container:
        if expected is not None:
            with container.get_driver() as probe:
                served = neo4j_server_version(probe)
            if served != expected:
                pytest.fail(
                    f"{NEO4J_IMAGE} serves Neo4j {served[0]}.{served[1]}, but "
                    f"{NEO4J_EXPECT_VERSION_ENV} expects {expected[0]}.{expected[1]}"
                )
        yield container


@pytest.fixture
def graph(neo4j_container, monkeypatch):
    """Point the driver at the container, reset schema, and start from an empty graph.

    The backend is pinned rather than left to ``Settings``' defaults. Those read
    ``TOOLGRAPH_BACKEND`` / ``TOOLGRAPH_DB_PATH`` and ``.toolgraph/config.json``
    in the working directory, so an unpinned fixture run from a checkout where
    someone ran ``toolgraph init`` would aim the wipe below at that Ladybug
    database instead of the container.
    """
    for name in ("TOOLGRAPH_BACKEND", "TOOLGRAPH_DB_PATH", "TOOLGRAPH_CONFIG"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(config, "_explicit_config_path", None)
    test_settings = config.Settings(
        backend="neo4j",
        neo4j_uri=neo4j_container.get_connection_url(),
        neo4j_user="neo4j",
        neo4j_password=TEST_PASSWORD,
        neo4j_database="neo4j",
    )
    monkeypatch.setitem(vars(config), "settings", test_settings)
    driver.close_driver()
    schema.init_schema()
    with driver.session() as s:
        s.run("MATCH (n) DETACH DELETE n")
    yield
    driver.close_driver()


@pytest.fixture(params=[pytest.param("neo4j", marks=pytest.mark.neo4j), "ladybug"])
def backend_graph(request, tmp_path, monkeypatch):
    """Required dual-backend regression fixture; missing Ladybug is an error."""
    if request.param == "neo4j":
        request.getfixturevalue("graph")
        yield "neo4j"
        return
    import ladybug  # noqa: F401

    monkeypatch.setitem(
        vars(config),
        "settings",
        config.Settings(backend="ladybug", db_path=tmp_path / "regression.lbug"),
    )
    driver.close_driver()
    schema.init_schema()
    try:
        yield "ladybug"
    finally:
        driver.close_driver()


@pytest.fixture
def fresh_settings():
    """No cached ``config.settings`` during the test, and none left behind.

    A command can build or install the singleton (``--config``, ``init``).
    Left cached, it hides a missing backend from every later test.
    ``monkeypatch.delitem(..., raising=False)`` cannot do this: on an absent
    key it records nothing to restore.
    """
    saved = vars(config).pop("settings", None)
    yield
    vars(config).pop("settings", None)
    if saved is not None:
        vars(config)["settings"] = saved


@pytest.fixture(scope="session")
def artifact_format_checker():
    from jsonschema import FormatChecker

    checker = FormatChecker()
    assert "date-time" in checker.checkers, "install the dev dependency group with formats"
    return checker


@pytest.fixture
def embedded_path(tmp_path, monkeypatch):
    path = tmp_path / "missing-parent" / "probe.lbug"
    monkeypatch.setitem(vars(config), "settings", config.Settings(backend="ladybug", db_path=path))
    monkeypatch.setenv("TOOLGRAPH_CONFIG", str(tmp_path / "no-config.json"))
    driver.close_driver()
    yield path
    driver.close_driver()
