"""Which node a governed_by key governs, and blast_radius over the same names.

``governed_by`` picks the target kind from the key's shape: a URI scheme means
a resource, anything else a tool ref. The same string can name both, and
binding the policy to the one the shape picks left the other ungoverned, so a
DENY read as ALLOW. A collision now rejects the manifest; ``governed_tools``
and ``governed_resources`` say the kind explicitly.
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from tests._helpers import control_plan_raw
from toolgraph.cli import app as cli_app
from toolgraph.control_plan import build_control_preflight
from toolgraph.errors import ContractError
from toolgraph.graph import loader, queries
from toolgraph.manifest.ingest import dry_run_ingest, governance_counts, ingest_governance
from toolgraph.manifest.parser import governance_digest, load_governance
from toolgraph.models import CrawlResult, Governance, ResourceRecord, ToolRecord
from toolgraph.policy_bundle import build_policy_bundle
from toolgraph.policy_impact import PolicyImpactError, build_policy_impact

ROOT = Path(__file__).resolve().parents[1]

# governance_digest of toolgraph/quickstart/governance.yaml at 4c762ad, before
# the explicit fields existed (computed from `git archive 4c762ad toolgraph`).
QUICKSTART_DIGEST_BEFORE_EXPLICIT_FIELDS = (
    "7d9e4f60d6cd58ddaabb0c374547190b18c54e0b21b1f0a1bab728b88b5ccf16"
)

MOVE_HINT = "move it to governed_tools or governed_resources to say which one the policy governs"


def _crawl(server: str, *tools: str, resources: tuple[str, ...] = ()) -> None:
    loader.load_crawl_result(
        CrawlResult(
            server_name=server,
            tools=[ToolRecord(name=t) for t in tools],
            resources=[ResourceRecord(uri=r) for r in resources],
        )
    )


def _gov(**fields) -> Governance:
    base = {
        "agents": ["bot"],
        "policies": [{"id": "no-go", "effect": "DENY"}],
    }
    return Governance.model_validate(base | fields)


def _verdict(agent: str, tool: str) -> str:
    return queries.check_access(agent, tool)["verdict"]


# --- collision A: a resource-shaped key that also names a tool ---------------


def test_a_resource_shaped_key_that_names_a_tool_rejects_the_manifest(backend_graph):
    _crawl("gh", "repo:delete")
    gov = _gov(
        grants=[{"agent": "bot", "tool": "repo:delete"}], governed_by={"repo:delete": ["no-go"]}
    )
    before = governance_counts()

    report = ingest_governance(gov)

    assert report.warnings == [
        "GOVERNED_BY: 'repo:delete' reads as a resource URI but also names "
        f"1 crawled tool(s) — {MOVE_HINT}"
    ]
    assert governance_counts() == before
    assert dry_run_ingest(gov).warnings == report.warnings


def test_the_same_policy_in_governed_tools_denies_that_tool(backend_graph):
    _crawl("gh", "repo:delete")
    gov = _gov(
        grants=[{"agent": "bot", "tool": "repo:delete"}],
        governed_tools={"repo:delete": ["no-go"]},
    )

    assert ingest_governance(gov).warnings == []
    assert _verdict("bot", "repo:delete") == "DENY"
    radius = queries.blast_radius("no-go")
    assert [(r["via"], r["tool_key"]) for r in radius["impacted"]] == [("tool", "gh::repo:delete")]


def test_a_qualified_key_whose_server_name_holds_a_colon_rejects_then_governs(backend_graph):
    # `gh:prod::delete` reads as a URI (scheme `gh`): the tool-ref prefix
    # cannot hold a colon. It used to bind to a Resource and leave the tool
    # ALLOW; now it is a collision, and governed_tools says what it is.
    _crawl("gh:prod", "delete")
    grants = [{"agent": "bot", "tool": "gh:prod::delete"}]

    rejected = ingest_governance(_gov(grants=grants, governed_by={"gh:prod::delete": ["no-go"]}))
    assert rejected.warnings == [
        "GOVERNED_BY: 'gh:prod::delete' reads as a resource URI but also names "
        f"1 crawled tool(s) — {MOVE_HINT}"
    ]

    gov = _gov(grants=grants, governed_tools={"gh:prod::delete": ["no-go"]})
    assert ingest_governance(gov).warnings == []
    assert _verdict("bot", "gh:prod::delete") == "DENY"


def test_a_tool_and_a_resource_with_the_same_uri_are_each_governable(backend_graph):
    # A tool named like a resource URI: governed_by cannot say which one is
    # meant, so it rejects; each explicit field governs its own node.
    _crawl("s", "file:///x", "reader", resources=("file:///x",))
    grants = [
        {"agent": "bot", "tool": "s::file:///x"},
        {"agent": "bot", "tool": "s::reader"},
    ]
    access = [{"tool": "s::reader", "resource": "file:///x", "mode": "READS"}]

    rejected = ingest_governance(
        _gov(grants=grants, data_access=access, governed_by={"file:///x": ["no-go"]})
    )
    assert rejected.warnings == [
        "GOVERNED_BY: 'file:///x' reads as a resource URI but also names "
        f"1 crawled tool(s) — {MOVE_HINT}"
    ]

    as_resource = _gov(
        grants=grants, data_access=access, governed_resources={"file:///x": ["no-go"]}
    )
    assert ingest_governance(as_resource).warnings == []
    assert (_verdict("bot", "s::reader"), _verdict("bot", "s::file:///x")) == ("DENY", "ALLOW")

    as_tool = _gov(grants=grants, data_access=access, governed_tools={"s::file:///x": ["no-go"]})
    assert ingest_governance(as_tool).warnings == []
    assert (_verdict("bot", "s::reader"), _verdict("bot", "s::file:///x")) == ("ALLOW", "DENY")


def test_an_ipv6_resource_with_no_matching_tool_is_governed_as_before(backend_graph):
    # Holds "::" but no tool has that key: no collision, a resource as on main.
    _crawl("s", "reader")
    gov = _gov(
        grants=[{"agent": "bot", "tool": "s::reader"}],
        data_access=[{"tool": "s::reader", "resource": "http://[::1]/x", "mode": "READS"}],
        governed_by={"http://[::1]/x": ["no-go"]},
    )

    assert ingest_governance(gov).warnings == []
    assert _verdict("bot", "s::reader") == "DENY"


def test_an_ipv6_resource_that_also_names_a_tool_key_rejects(backend_graph):
    # Server "http://[" + tool "1]/x" has the key "http://[::1]/x".
    _crawl("http://[", "1]/x", "reader")
    gov = _gov(
        grants=[{"agent": "bot", "tool": "http://[::reader"}],
        data_access=[{"tool": "http://[::reader", "resource": "http://[::1]/x", "mode": "READS"}],
        governed_by={"http://[::1]/x": ["no-go"]},
    )

    assert ingest_governance(gov).warnings == [
        "GOVERNED_BY: 'http://[::1]/x' reads as a resource URI but also names "
        f"1 crawled tool(s) — {MOVE_HINT}"
    ]


def test_the_collision_warning_never_prints_the_matched_tool_keys(backend_graph):
    # Each name passes the identity check alone; their join reads as a URL
    # with a password, so the warning counts the tools instead of naming them.
    _crawl("https://u:QXZQ7", "urn:x@host")
    _crawl("https://v:QXZQ8", "urn:x@host")
    gov = _gov(governed_by={"urn:x@host": ["no-go"]})

    assert ingest_governance(gov).warnings == [
        f"GOVERNED_BY: 'urn:x@host' reads as a resource URI but also names 2 crawled tool(s) — "
        f"{MOVE_HINT}"
    ]


def test_an_explicit_target_resolves_the_same_after_its_server_retires(backend_graph):
    _crawl("gh:prod", "delete")
    gov = _gov(
        grants=[{"agent": "bot", "tool": "gh:prod::delete"}],
        governed_tools={"gh:prod::delete": ["no-go"]},
    )
    assert ingest_governance(gov).warnings == []

    loader.retire_unlisted_servers(set())
    again = ingest_governance(gov)

    assert again.warnings == []
    assert any(n.startswith("DRIFT: tool 'gh:prod::delete'") for n in again.notices)
    assert _verdict("bot", "gh:prod::delete") == "DENY"


# --- collision B: a tool-shaped key that is also a manifest resource ---------


def test_a_bare_key_that_is_also_a_data_access_resource_rejects(backend_graph):
    # The resource `customers_db` has no scheme, so governed_by reads it as a
    # tool, and a tool of that name took the DENY while `read` stayed ALLOW.
    _crawl("s", "read", "customers_db")
    grants = [{"agent": "bot", "tool": "s::read"}]
    access = [{"tool": "s::read", "resource": "customers_db", "mode": "READS"}]

    rejected = ingest_governance(
        _gov(grants=grants, data_access=access, governed_by={"customers_db": ["no-go"]})
    )
    assert rejected.warnings == [
        "GOVERNED_BY: 'customers_db' reads as a tool but is also a resource in data_access "
        f"or expected_exceptions — {MOVE_HINT}"
    ]

    gov = _gov(grants=grants, data_access=access, governed_resources={"customers_db": ["no-go"]})
    assert ingest_governance(gov).warnings == []
    assert _verdict("bot", "s::read") == "DENY"
    assert queries.blast_radius("customers_db")["kind"] == "resource"


def test_the_collision_compares_canonical_forms(backend_graph):
    # `URN::x` reads as a tool ref (the `URN::` prefix); canonical it is the
    # resource `urn::x` that data_access names.
    _crawl("s", "read")
    gov = _gov(
        grants=[{"agent": "bot", "tool": "s::read"}],
        data_access=[{"tool": "s::read", "resource": "urn::x", "mode": "READS"}],
        governed_by={"URN::x": ["no-go"]},
    )

    assert ingest_governance(gov).warnings == [
        "GOVERNED_BY: 'URN::x' reads as a tool but is also a resource in data_access "
        f"or expected_exceptions — {MOVE_HINT}"
    ]


def test_a_key_named_only_as_an_exception_resource_also_collides(backend_graph):
    _crawl("s", "read", "vault")
    gov = _gov(
        grants=[{"agent": "bot", "tool": "s::read"}],
        expected_exceptions=[
            {"agent": "bot", "tool": "s::read", "policy": "no-go", "resource": "vault"}
        ],
        governed_by={"vault": ["no-go"]},
    )

    warnings = ingest_governance(gov).warnings
    assert (
        "GOVERNED_BY: 'vault' reads as a tool but is also a resource in data_access "
        f"or expected_exceptions — {MOVE_HINT}"
    ) in warnings


def test_policy_impact_rejects_the_same_collision(backend_graph):
    _crawl("gh", "repo:delete")
    assert ingest_governance(_gov(grants=[{"agent": "bot", "tool": "repo:delete"}])).warnings == []
    gov = _gov(
        grants=[{"agent": "bot", "tool": "repo:delete"}], governed_by={"repo:delete": ["no-go"]}
    )

    with pytest.raises(PolicyImpactError) as exc:
        build_policy_impact(gov, agent="bot", candidates=["gh::repo:delete"])
    assert str(exc.value) == (
        "candidate manifest rejected: GOVERNED_BY: 'repo:delete' reads as a resource URI but "
        f"also names 1 crawled tool(s) — {MOVE_HINT}"
    )


# --- explicit fields ---------------------------------------------------------


def test_one_binding_in_two_fields_is_rejected(backend_graph):
    _crawl("s", "reader")
    gov = _gov(
        grants=[{"agent": "bot", "tool": "s::reader"}],
        data_access=[{"tool": "s::reader", "resource": "file:///x", "mode": "READS"}],
        governed_by={"file:///x": ["no-go"]},
        governed_resources={"FILE:///x": ["no-go"]},
    )

    assert ingest_governance(gov).warnings == [
        "GOVERNED_RESOURCES: policy 'no-go' is already bound to resource 'file:///x' in "
        "governed_by — bind it in one field only"
    ]


def test_an_explicit_tool_that_does_not_resolve_is_reported_under_its_field(backend_graph):
    _crawl("s", "reader")
    gov = _gov(governed_tools={"s::nope": ["no-go"]})

    assert ingest_governance(gov).warnings == ["GOVERNED_TOOLS: tool 's::nope' not found"]


def test_empty_explicit_fields_keep_the_digest_a_manifest_had_before():
    gov = load_governance(ROOT / "toolgraph" / "quickstart" / "governance.yaml")
    assert gov.governed_tools == {} and gov.governed_resources == {}

    assert governance_digest(gov) == QUICKSTART_DIGEST_BEFORE_EXPLICIT_FIELDS
    assert governance_digest(gov.model_copy(update={"governed_tools": {"s::t": ["p"]}})) != (
        QUICKSTART_DIGEST_BEFORE_EXPLICIT_FIELDS
    )


@pytest.mark.parametrize(
    ("field", "key"),
    [
        ("governed_tools", "password=QXZQ1"),
        # A resource-shaped key is exempt in governed_by, never in governed_tools.
        ("governed_tools", "https://u:QXZQ2@h/x"),
        ("governed_by", "token=QXZQ3"),
    ],
)
def test_a_credential_shaped_tool_ref_is_refused_without_its_value(field, key):
    with pytest.raises(ValidationError) as exc:
        Governance.model_validate({field: {key: ["p"]}})
    assert f"{field} tool ref must not contain credential-shaped data" in str(exc.value)
    assert "QXZQ" not in str(exc.value)


@pytest.mark.parametrize("field", ["governed_by", "governed_tools", "governed_resources"])
def test_a_credential_shaped_policy_id_is_refused_in_every_field(field):
    key = "file:///x" if field != "governed_tools" else "s::t"
    with pytest.raises(ValidationError) as exc:
        Governance.model_validate({field: {key: ["password=QXZQ4"]}})
    assert f"{field} policy id must not contain credential-shaped data" in str(exc.value)
    assert "QXZQ" not in str(exc.value)


def test_a_governed_resource_key_is_a_resource_not_an_identity():
    # A resource is shown through display_resource, never refused as an
    # identity: the same key in governed_tools is refused (above).
    gov = Governance.model_validate({"governed_resources": {"password=QXZQ5": ["p"]}})
    assert list(gov.governed_resources) == ["password=QXZQ5"]


@pytest.mark.parametrize("field", ["governed_tools", "governed_resources"])
def test_a_constructed_manifest_is_checked_again_at_ingest(backend_graph, field):
    # model_construct skips the validators; ingest revalidates before reading.
    gov = Governance.model_construct(
        agents=[],
        policies=[],
        grants=[],
        data_access=[],
        governed_by={},
        governed_tools={"s::t": ["password=QXZQ6"]} if field == "governed_tools" else {},
        governed_resources={"file:///x": ["password=QXZQ6"]}
        if field == "governed_resources"
        else {},
        expected_exceptions=[],
    )

    with pytest.raises(ValueError) as exc:
        ingest_governance(gov)
    assert str(exc.value) == (
        "Governance does not validate; build it with Governance.model_validate to see why"
    )


def test_explicit_targets_reach_preview_bundle_and_control_preflight(backend_graph):
    # The consumers read the graph (and policy impact the resolved manifest),
    # never the manifest fields: an explicit binding must reach each of them.
    _crawl("workspace", "read", "write")
    tools = ["workspace::read", "workspace::write"]
    base = {
        "agents": ["codex", "claude"],
        "policies": [{"id": "no-go", "effect": "DENY"}],
        "grants": [{"agent": a, "tool": t} for a in ("codex", "claude") for t in tools],
        "data_access": [{"tool": "workspace::read", "resource": "customers_db", "mode": "READS"}],
    }
    assert ingest_governance(Governance.model_validate(base)).warnings == []
    gov = Governance.model_validate(
        base
        | {
            "governed_resources": {"customers_db": ["no-go"]},
            "governed_tools": {"workspace::write": ["no-go"]},
        }
    )
    deny_paths = {
        "workspace::read": ["(read) -READS-> (customers_db) -GOVERNED_BY-> (no-go:DENY)"],
        "workspace::write": ["(write) -GOVERNED_BY-> (no-go:DENY)"],
    }

    preview = dry_run_ingest(gov)
    assert preview.warnings == []
    assert [(b["kind"], b["ref"], b["policy"]) for b in preview.governed_by_added] == [
        ("resource", "customers_db", "no-go"),
        ("tool", "workspace::write", "no-go"),
    ]
    impact = build_policy_impact(gov, agent="codex", candidates=tools, profile="strict")
    assert [(r["candidate"], r["after"]["reason"]) for r in impact["rows"]] == [
        ("workspace::read", "DENY_VIOLATION"),
        ("workspace::write", "DENY_VIOLATION"),
    ]
    assert [[e["path"] for e in r["after"]["evidence"]] for r in impact["rows"]] == [
        deny_paths["workspace::read"],
        deny_paths["workspace::write"],
    ]

    assert ingest_governance(gov).warnings == []
    now = datetime(2026, 11, 14, tzinfo=UTC)
    bundle = build_policy_bundle(agent="codex", profile="strict", created_at=now)
    assert [(t["tool_key"], t["decision"], t["reason"]) for t in bundle["tools"]] == [
        ("workspace::read", "rejected", "DENY_VIOLATION"),
        ("workspace::write", "rejected", "DENY_VIOLATION"),
    ]
    preflight = build_control_preflight(control_plan_raw(), profile="strict", created_at=now)
    assert preflight["decision"] == "advisory_warn"
    assert [
        (e["principal"], e["eligible"], {r["tool_key"]: r["paths"] for r in e["rejected"]})
        for e in preflight["evaluations"]
    ] == [("codex", [], deny_paths), ("claude", [], deny_paths)]


# --- blast_radius over a name that is both a resource and a policy -----------


def _seed_resource_and_policy_named_alike() -> None:
    _crawl("s", "reader", "writer")
    gov = Governance.model_validate(
        {
            "agents": ["bot"],
            "policies": [{"id": "urn:x", "effect": "DENY"}, {"id": "other", "effect": "DENY"}],
            "grants": [
                {"agent": "bot", "tool": "s::reader"},
                {"agent": "bot", "tool": "s::writer"},
            ],
            "data_access": [{"tool": "s::reader", "resource": "urn:x", "mode": "READS"}],
            "governed_by": {"urn:x": ["other"]},
            "governed_tools": {"s::writer": ["urn:x"]},
        }
    )
    assert ingest_governance(gov).warnings == []


def test_blast_radius_refuses_a_node_that_names_a_resource_and_a_policy(backend_graph):
    _seed_resource_and_policy_named_alike()

    with pytest.raises(ContractError) as exc:
        queries.blast_radius("urn:x")
    assert str(exc.value) == queries.BLAST_RADIUS_AMBIGUOUS

    as_policy = queries.blast_radius("urn:x", "policy")
    assert (as_policy["kind"], [r["tool_key"] for r in as_policy["impacted"]]) == (
        "policy",
        ["s::writer"],
    )
    as_resource = queries.blast_radius("urn:x", "resource")
    assert (as_resource["kind"], [r["tool_key"] for r in as_resource["impacted"]]) == (
        "resource",
        ["s::reader"],
    )


def test_blast_radius_kind_finds_only_that_kind(backend_graph):
    _seed_resource_and_policy_named_alike()

    assert queries.blast_radius("other", "resource") == {
        "kind": "resource",
        "node": "other",
        "found": False,
        "impacted": [],
    }
    assert queries.blast_radius("s::reader", "policy")["found"] is False
    with pytest.raises(ContractError) as exc:
        queries.blast_radius("urn:x", "tool")
    assert str(exc.value) == "kind must be resource or policy"


def test_blast_radius_not_found_keeps_its_kind_by_shape(backend_graph):
    _crawl("s", "reader")
    assert queries.blast_radius("urn:none") == {
        "kind": "resource",
        "node": "urn:none",
        "found": False,
        "impacted": [],
    }
    assert queries.blast_radius("none")["kind"] == "policy"


def test_blast_radius_cli_reports_the_ambiguity_as_a_usage_error(backend_graph):
    _seed_resource_and_policy_named_alike()
    runner = CliRunner()

    ambiguous = runner.invoke(cli_app, ["blast-radius", "urn:x"])
    assert ambiguous.exit_code == 2
    assert ambiguous.stdout == ""
    # Typer colors and boxes the usage error (on GitHub Actions too) and Rich
    # wraps it at any character, so compare the text without codes, box edges
    # or whitespace.
    shown = re.sub(r"[\s│]", "", re.sub(r"\x1b\[[0-9;]*m", "", ambiguous.stderr))
    assert "Invalidvaluefor'--kind':" in shown
    assert re.sub(r"\s", "", queries.BLAST_RADIUS_AMBIGUOUS) in shown

    chosen = runner.invoke(cli_app, ["blast-radius", "urn:x", "--kind", "policy"])
    assert chosen.exit_code == 0
    assert json.loads(chosen.stdout)["kind"] == "policy"

    invalid = runner.invoke(cli_app, ["blast-radius", "urn:x", "--kind", "tool"])
    assert invalid.exit_code == 2
    assert invalid.stdout == ""


def test_blast_radius_mcp_forwards_the_ambiguity_and_accepts_kind(backend_graph):
    from tests.test_server_errors import _wire_call

    _seed_resource_and_policy_named_alike()

    ambiguous = asyncio.run(_wire_call("blast_radius", {"node": "urn:x"}))
    assert ambiguous.is_error is True
    assert ambiguous.content[0].text == queries.BLAST_RADIUS_AMBIGUOUS

    chosen = asyncio.run(_wire_call("blast_radius", {"node": "urn:x", "kind": "resource"}))
    assert chosen.is_error is False
    assert json.loads(chosen.content[0].text)["kind"] == "resource"

    invalid = asyncio.run(_wire_call("blast_radius", {"node": "urn:x", "kind": "tool"}))
    assert invalid.is_error is True
    assert "kind" in invalid.content[0].text
