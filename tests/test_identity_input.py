"""Credential-shaped identities are refused where they enter the graph.

Agents, policy ids, tool refs and server and tool names are stored as given
and printed by every query that names them, so the manifest and the crawler
refuse one that `is_credential_shaped_identity` marks. Resource URIs are not
identities: evidence displays them through `display_resource`.
"""

from __future__ import annotations

import sys
import warnings
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError
from typer.testing import CliRunner

from toolgraph import cli
from toolgraph.crawler.crawl import crawl_all, load_servers_config
from toolgraph.graph import loader, schema
from toolgraph.manifest.ingest import dry_run_ingest, ingest_governance
from toolgraph.manifest.parser import load_governance
from toolgraph.models import CrawlResult, Governance, ServerSpec, ToolRecord
from toolgraph.policy_impact import build_policy_impact
from toolgraph.redaction import redact_text

FIXTURE = str(Path(__file__).parent / "fixtures" / "credential_named_server.py")
PLANTED = ["neo4j://u:QX/ZQ@host/r", "password=QX/ZQ"]


def _manifest() -> dict:
    return {
        "agents": ["bot"],
        "policies": [{"id": "p", "effect": "DENY"}],
        "grants": [{"agent": "bot", "tool": "srv::t", "granted_by": "team"}],
        "data_access": [{"tool": "srv::t", "resource": "file:///x", "mode": "READS"}],
        "governed_by": {"file:///x": ["p"]},
        "expected_exceptions": [{"agent": "bot", "tool": "srv::t", "policy": "p"}],
    }


def _item(key: str, field: str):
    def plant(manifest: dict, value: str) -> None:
        manifest[key][0][field] = value

    return plant


FIELDS = {
    "agents[]": lambda m, v: m["agents"].append(v),
    "policies.id": _item("policies", "id"),
    "grants.agent": _item("grants", "agent"),
    "grants.tool": _item("grants", "tool"),
    "grants.granted_by": _item("grants", "granted_by"),
    "data_access.tool": _item("data_access", "tool"),
    "expected_exceptions.agent": _item("expected_exceptions", "agent"),
    "expected_exceptions.tool": _item("expected_exceptions", "tool"),
    "expected_exceptions.policy": _item("expected_exceptions", "policy"),
    "governed_by tool-ref key": lambda m, v: m["governed_by"].update({f"srv::{v}": ["p"]}),
    "governed_by policy": lambda m, v: m["governed_by"].update({"file:///x": [v]}),
    "governed_by binding policy": lambda m, v: m["governed_by"].update(
        {"file:///x": [{"policy": v}]}
    ),
}


def _write(tmp_path: Path, manifest: dict) -> Path:
    path = tmp_path / "governance.yaml"
    path.write_text(yaml.safe_dump(manifest), encoding="utf-8")
    return path


def _assert_hidden(text: str) -> None:
    assert "QX" not in text
    assert "ZQ" not in text


@pytest.mark.parametrize("value", PLANTED)
@pytest.mark.parametrize("field", FIELDS)
def test_manifest_refuses_a_credential_shaped_identity(field, value, tmp_path):
    manifest = _manifest()
    FIELDS[field](manifest, value)

    with pytest.raises(ValidationError) as raised:
        load_governance(_write(tmp_path, manifest))

    assert "must not contain credential-shaped data" in str(raised.value)
    _assert_hidden(str(raised.value))
    _assert_hidden(redact_text(raised.value))


def test_manifest_accepts_names_and_resources_that_are_not_credentials(tmp_path):
    manifest = _manifest()
    manifest["agents"].append("alice@corp.example")
    manifest["grants"].append({"agent": "alice@corp.example", "tool": "srv::get_token:v2"})
    resource = "postgres://user@db.internal/x"
    manifest["data_access"].append({"tool": "srv::t", "resource": resource, "mode": "READS"})
    manifest["governed_by"][resource] = ["p"]
    manifest["governed_by"]["srv::t"] = ["p"]

    gov = load_governance(_write(tmp_path, manifest))

    assert resource in gov.governed_by


def test_ingest_manifest_cli_refuses_without_echoing(tmp_path):
    manifest = _manifest()
    manifest["agents"].append(PLANTED[0])

    result = CliRunner().invoke(
        cli.app, ["ingest-manifest", "--governance", str(_write(tmp_path, manifest))]
    )

    assert result.exit_code == 1
    assert "Invalid governance config" in result.output
    assert "must not contain credential-shaped data" in result.output
    _assert_hidden(result.output)


@pytest.mark.parametrize(
    "build",
    [
        lambda: Governance.model_construct(agents=[PLANTED[0]]),
        lambda: Governance.model_construct(agents=PLANTED[0]),
        # An error inside the mapping would carry the key in its location.
        lambda: Governance.model_construct(
            governed_by={"neo4j://u:QX ZQ@host/r": [{"policy": 42}]}
        ),
    ],
    ids=["planted-list", "wrong-type", "mapping-key"],
)
@pytest.mark.parametrize("write", [ingest_governance, dry_run_ingest])
def test_ingest_revalidates_a_model_built_around_validation(backend_graph, build, write):
    schema.init_schema()
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        with pytest.raises(ValueError) as raised:
            write(build())

    assert str(raised.value) == (
        "Governance does not validate; build it with Governance.model_validate to see why"
    )
    _assert_hidden(" ".join(str(w.message) for w in caught))


def test_ingest_revalidates_a_list_mutated_after_validation(backend_graph, tmp_path):
    schema.init_schema()
    gov = load_governance(_write(tmp_path, _manifest()))
    gov.agents.append(PLANTED[0])

    with pytest.raises(ValueError) as raised:
        ingest_governance(gov)

    assert str(raised.value) == (
        "Governance does not validate; build it with Governance.model_validate to see why"
    )


@pytest.mark.parametrize("value", PLANTED)
def test_crawl_records_refuse_credential_shaped_names(value):
    for build in (
        lambda: CrawlResult(server_name=value),
        lambda: ToolRecord(name=value),
        lambda: ServerSpec(name=value),
    ):
        with pytest.raises(ValidationError) as raised:
            build()
        assert "must not contain credential-shaped data" in str(raised.value)
        _assert_hidden(str(raised.value))


def test_load_crawl_result_revalidates(backend_graph):
    schema.init_schema()
    with pytest.raises(ValueError) as raised:
        loader.load_crawl_result(CrawlResult.model_construct(server_name=PLANTED[0], tools=[]))

    assert str(raised.value) == (
        "CrawlResult does not validate; build it with CrawlResult.model_validate to see why"
    )
    assert loader.graph_counts()["mcpserver"] == 0


@pytest.mark.parametrize("which", ["server", "tool"])
def test_crawl_fails_only_the_server_that_reports_a_credential_shaped_name(which):
    planted = ServerSpec(transport="stdio", command=sys.executable, args=[FIXTURE, which])
    plain = ServerSpec(name="plain", transport="stdio", command=sys.executable, args=[FIXTURE, "x"])

    results, failures = crawl_all([planted, plain], timeout=60)

    assert [r.server_name for r in results] == ["plain"]
    assert len(failures) == 1
    assert "must not contain credential-shaped data" in failures[0][1]
    _assert_hidden(failures[0][1])


def test_a_servers_yaml_name_replaces_the_name_a_server_reports():
    # The graph stores the configured name, so the reported one is never kept.
    named = ServerSpec(
        name="named", transport="stdio", command=sys.executable, args=[FIXTURE, "server"]
    )

    results, failures = crawl_all([named], timeout=60)

    assert failures == []
    assert [r.server_name for r in results] == ["named"]


@pytest.mark.parametrize(
    "build",
    [
        lambda: Governance.model_construct(agents=[PLANTED[0]]),
        lambda: Governance.model_construct(
            governed_by={"neo4j://u:QX ZQ@host/r": [{"policy": 42}]}
        ),
    ],
    ids=["planted-list", "mapping-key"],
)
def test_policy_impact_revalidates_before_opening_the_graph(backend_graph, build):
    # A configured graph, so a missing check would get past the boundary rather
    # than stop at "no backend configured".
    schema.init_schema()
    with pytest.raises(ValueError) as raised:
        build_policy_impact(build(), agent="bot", candidates=["srv::t"])

    assert str(raised.value) == (
        "Governance does not validate; build it with Governance.model_validate to see why"
    )


def test_servers_yaml_with_a_credential_shaped_name_is_rejected_whole(tmp_path, monkeypatch):
    path = tmp_path / "servers.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "servers": [
                    {"name": "plain", "command": "unused"},
                    {"name": PLANTED[0], "command": "unused"},
                ]
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValidationError) as raised:
        load_servers_config(path)
    _assert_hidden(str(raised.value))

    def never(*_a, **_k):
        raise AssertionError("crawl_all must not be reached")

    monkeypatch.setattr(cli, "crawl_all", never)
    result = CliRunner().invoke(cli.app, ["crawl", "--servers", str(path)])

    assert result.exit_code == 1
    assert "Invalid servers config" in result.output
    _assert_hidden(result.output)
