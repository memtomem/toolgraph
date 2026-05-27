"""Test fixtures: a real, isolated Neo4j via testcontainers (no Cypher mocking)."""

from __future__ import annotations

import pytest
from testcontainers.neo4j import Neo4jContainer

from toolgraph import config
from toolgraph.graph import driver, schema

NEO4J_IMAGE = "neo4j:5.26-community"
TEST_PASSWORD = "testpass"


@pytest.fixture(scope="session")
def neo4j_container():
    with Neo4jContainer(NEO4J_IMAGE, username="neo4j", password=TEST_PASSWORD) as container:
        yield container


@pytest.fixture
def graph(neo4j_container, monkeypatch):
    """Point the driver at the container, reset schema, and start from an empty graph."""
    test_settings = config.Settings(
        neo4j_uri=neo4j_container.get_connection_url(),
        neo4j_user="neo4j",
        neo4j_password=TEST_PASSWORD,
        neo4j_database="neo4j",
    )
    monkeypatch.setattr(config, "settings", test_settings)
    driver.close_driver()
    schema.init_schema()
    with driver.session() as s:
        s.run("MATCH (n) DETACH DELETE n")
    yield
    driver.close_driver()
