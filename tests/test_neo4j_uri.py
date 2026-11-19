"""A NEO4J_URI Toolgraph cannot use is one configuration error that never shows the URI.

The Neo4j driver's own refusals quote the whole URI, user name and password
included, and so do the warnings it gives for the parts it ignores (a path,
a fragment). Every refusal here is ``BackendConfigurationError`` with the
``neo4j_uri_invalid`` cause and fixed text.
"""

from __future__ import annotations

import sys
import warnings

import pytest
from typer.testing import CliRunner

from toolgraph import cli
from toolgraph.graph import driver
from toolgraph.graph.driver import BackendConfigurationError, ConfigurationCause

SECRET = "QXZQ"

URI_INVALID = (
    "NEO4J_URI is not a URI Toolgraph can use: use bolt:// or neo4j:// (or their "
    "+s / +ssc forms) with no path or fragment, keep the user name and password "
    "in NEO4J_USER and NEO4J_PASSWORD, and name the database in NEO4J_DATABASE"
)

# Each one measured against neo4j 6.4.1: what the driver does with it, given
# the arguments get_driver() passes.
REFUSED = {
    "unknown-scheme": "bogus://localhost:7687",  # ConfigurationError
    "no-scheme": "localhost:7687",  # ConfigurationError (scheme 'localhost')
    "empty": "",  # ConfigurationError
    "http": "http://h:7474",  # ModuleNotFoundError without aiohttp
    "https": "https://h:7473",
    "routing-userinfo": f"neo4j://u:{SECRET}@h:7687",  # ConfigurationError quoting it
    "direct-userinfo": f"bolt://u:{SECRET}@h:7687",
    "direct-path": f"bolt://h:7687/path?token={SECRET}",  # warning + error, both quote it
    "routing-path": f"neo4j://h:7687/x?token={SECRET}",  # warning quoting it, then accepted
    "encoded-slash-path": "neo4j://h:7687/%2F",  # a path that is not the root
    "fragment": f"neo4j://h:7687#{SECRET}",  # warning quoting it, then accepted
    "unclosed-ipv6": "neo4j://[::1",  # ValueError from the URL parser
}

ACCEPTED = {
    "direct": "bolt://h:7687",
    "routing": "neo4j://h:7687",
    "tls": "neo4j+s://h:7687",
    "self-signed": "bolt+ssc://h:7687",
    "root-path": "neo4j://h:7687/",
    "routing-context": "neo4j://h:7687?policy=x",
    "empty-fragment": "neo4j://h:7687?x=1#",
    "ipv6": "neo4j://[::1]:7687",
    "upper-case-scheme": "NEO4J://h:7687",
    "port-checked-on-connect": "neo4j://h:notaport",
}


@pytest.fixture
def neo4j_uri(monkeypatch, tmp_path, fresh_settings):
    """Select Neo4j with the given URI through the environment, as an
    operator would, and leave no driver behind."""
    for name in ("TOOLGRAPH_DB_PATH", "TOOLGRAPH_CONFIG", "TOOLGRAPH_SERVERS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)

    def select(uri: str) -> None:
        monkeypatch.setenv("TOOLGRAPH_BACKEND", "neo4j")
        monkeypatch.setenv("NEO4J_URI", uri)

    driver.close_driver()
    yield select
    driver.close_driver()


def _uri_parts(uri: str) -> list[str]:
    return [part for part in (uri, SECRET, "h:7687", "localhost") if part and part in uri]


@pytest.mark.parametrize("uri", REFUSED.values(), ids=REFUSED.keys())
@pytest.mark.parametrize("warning_filter", ["always", "error"])
def test_a_refused_uri_is_one_fixed_error(neo4j_uri, uri, warning_filter):
    neo4j_uri(uri)

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter(warning_filter)
        with pytest.raises(BackendConfigurationError) as raised:
            driver.get_driver()

    assert raised.value.cause is ConfigurationCause.NEO4J_URI_INVALID
    assert str(raised.value) == URI_INVALID
    assert caught == []
    for part in _uri_parts(uri):
        assert part not in str(raised.value)


@pytest.mark.parametrize("uri", REFUSED.values(), ids=REFUSED.keys())
def test_crawl_refuses_the_uri_before_launching_a_server(neo4j_uri, monkeypatch, tmp_path, uri):
    neo4j_uri(uri)
    monkeypatch.setattr(cli, "crawl_all", lambda *a, **k: pytest.fail("must not crawl"))
    servers = tmp_path / "fleet.yaml"
    servers.write_text("servers: []\n", encoding="utf-8")

    result = CliRunner().invoke(cli.app, ["crawl", "--servers", str(servers)])

    assert result.exit_code == 1
    assert result.stdout == ""
    assert result.stderr == f"ERROR: {URI_INVALID}\n"


@pytest.mark.parametrize("uri", ACCEPTED.values(), ids=ACCEPTED.keys())
def test_an_accepted_uri_builds_a_driver_without_a_warning(neo4j_uri, uri):
    neo4j_uri(uri)

    with warnings.catch_warnings():
        # A driver release that adds a warning quoting the URI fails here.
        warnings.simplefilter("error")
        built = driver.get_driver()

    assert built is driver.get_driver()


def test_a_uri_only_the_driver_refuses_is_the_same_error(neo4j_uri):
    """Routing context on a direct (bolt://) URI passes the pre-check; the
    driver refuses it while being built, quoting the URI."""
    uri = f"bolt://h:7687?token={SECRET}"
    neo4j_uri(uri)

    with pytest.raises(BackendConfigurationError) as raised:
        driver.get_driver()

    assert raised.value.cause is ConfigurationCause.NEO4J_URI_INVALID
    assert str(raised.value) == URI_INVALID
    assert SECRET in str(raised.value.__cause__)  # the reason it is never shown


@pytest.mark.parametrize(
    "uri",
    [f"neo4j://u:{SECRET}@h:7687", f"bolt://h:7687?token={SECRET}"],
    ids=["pre-check", "driver"],
)
def test_a_graph_command_prints_one_line_without_the_uri(neo4j_uri, uri):
    neo4j_uri(uri)

    result = CliRunner().invoke(cli.app, ["check"])

    assert result.exit_code == 1
    assert result.stdout == ""
    assert result.stderr == f"ERROR: {URI_INVALID}\n"


def test_doctor_reports_the_uri_as_a_fail_without_it(neo4j_uri):
    uri = f"neo4j://u:{SECRET}@h:7687"
    neo4j_uri(uri)

    result = CliRunner().invoke(cli.app, ["doctor"])

    assert result.exit_code == 1
    assert "[FAIL] Connectivity" in result.stdout
    assert URI_INVALID in result.stdout
    assert SECRET not in result.output
    assert "Traceback" not in result.output


def test_without_the_neo4j_extra_the_extra_is_reported_first(neo4j_uri, monkeypatch):
    neo4j_uri("bogus://h")
    monkeypatch.setitem(sys.modules, "neo4j", None)

    with pytest.raises(BackendConfigurationError) as raised:
        driver.check_backend_selected()

    assert raised.value.cause is ConfigurationCause.NEO4J_EXTRA_MISSING
