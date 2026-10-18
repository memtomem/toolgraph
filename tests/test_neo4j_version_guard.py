"""The CI guard that proves which Neo4j server the container fixture started."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from tests._helpers import expected_neo4j_version, neo4j_server_version


def test_absent_expectation_disables_the_check():
    assert expected_neo4j_version(None) is None


@pytest.mark.parametrize("raw", ["", " ", "5", "5.x", "5.7.0", " 5.7", "abc"])
def test_malformed_expectation_fails_instead_of_disabling_the_check(raw):
    with pytest.raises(ValueError, match="MAJOR.MINOR"):
        expected_neo4j_version(raw)


@pytest.mark.parametrize(("raw", "parsed"), [("5.7", (5, 7)), ("5.26", (5, 26))])
def test_expectation_parses_major_and_minor(raw, parsed):
    assert expected_neo4j_version(raw) == parsed


def _driver_reporting(*versions: list[str]):
    records = [{"versions": v} for v in versions]
    return SimpleNamespace(execute_query=lambda _query: (records, None, None))


@pytest.mark.parametrize(
    ("served", "parsed"),
    [("5.6.0", (5, 6)), ("5.7.0", (5, 7)), ("5.26.0", (5, 26)), ("5.26", (5, 26))],
)
def test_server_version_is_major_and_minor_of_the_kernel(served, parsed):
    assert neo4j_server_version(_driver_reporting([served])) == parsed


@pytest.mark.parametrize("versions", [[], [["5.7.0"], ["5.7.0"]]], ids=["none", "two"])
def test_server_version_needs_exactly_one_kernel_row(versions):
    with pytest.raises(AssertionError, match="one Neo4j Kernel component"):
        neo4j_server_version(_driver_reporting(*versions))


def test_unrecognised_server_version_fails():
    with pytest.raises(AssertionError, match="unrecognised Neo4j version"):
        neo4j_server_version(_driver_reporting(["dev"]))
