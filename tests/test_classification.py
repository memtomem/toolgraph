"""Classification of unsafe-tools reach: violation vs authorized_but_governed.

Codex review of the gate exhibit caught that `unsafe_callable_tools(planner)`
labeled planner's by-design grants as 'unsafe' — wrong framing for an
advisory analyzer. Operators now declare intentional DENY-zone reach in the
manifest's ``expected_exceptions`` block; matching rows surface as
``authorized_but_governed`` with the operator's stated reason, leaving real
unauthorized reach as ``violation`` so reviewers don't drown in noise.
"""

from __future__ import annotations

from tests._helpers import CSV, crawl_sample, seed_basic
from toolgraph.graph import driver, loader, queries, schema
from toolgraph.manifest.ingest import ingest_governance
from toolgraph.models import (
    AccessGrant,
    CrawlResult,
    DataAccess,
    ExpectedException,
    Governance,
    Policy,
    ResourceRecord,
    ToolRecord,
)


def _seed(graph) -> None:
    loader.load_crawl_result(
        CrawlResult(
            server_name="sample",
            transport="stdio",
            tools=[ToolRecord(name="read_file"), ToolRecord(name="write_file")],
            resources=[ResourceRecord(uri=CSV)],
        )
    )


def _gov(*exceptions: ExpectedException) -> Governance:
    return Governance(
        agents=["planner", "support-bot"],
        policies=[Policy(id="pii-deny", effect="DENY", scope="customer-pii")],
        grants=[
            AccessGrant(agent="planner", tool="sample::read_file"),
            AccessGrant(agent="planner", tool="sample::write_file"),
            AccessGrant(agent="support-bot", tool="sample::read_file"),
        ],
        data_access=[
            DataAccess(tool="sample::read_file", resource=CSV, mode="READS"),
            DataAccess(tool="sample::write_file", resource=CSV, mode="WRITES"),
        ],
        governed_by={CSV: ["pii-deny"]},
        expected_exceptions=list(exceptions),
    )


def test_no_exceptions_means_every_reach_is_violation(graph):
    _seed(graph)
    assert ingest_governance(_gov()).warnings == []
    rows = queries.unsafe_callable_tools("planner")
    assert {(r["tool"], r["classification"]) for r in rows} == {
        ("read_file", "violation"),
        ("write_file", "violation"),
    }


def test_matching_exception_marks_row_authorized_but_governed(graph):
    _seed(graph)
    assert (
        ingest_governance(
            _gov(
                ExpectedException(
                    agent="planner",
                    tool="sample::read_file",
                    policy="pii-deny",
                    resource=CSV,
                    reason="planner is the PII data owner; reads are by design",
                )
            )
        ).warnings
        == []
    )
    by_tool = {r["tool"]: r for r in queries.unsafe_callable_tools("planner")}
    assert by_tool["read_file"]["classification"] == "authorized_but_governed"
    assert by_tool["read_file"]["exception_reason"].startswith("planner is the PII")
    # write_file has no exception — stays a violation
    assert by_tool["write_file"]["classification"] == "violation"
    assert "exception_reason" not in by_tool["write_file"]


def test_exception_with_no_resource_covers_every_resource(graph):
    _seed(graph)
    assert (
        ingest_governance(
            _gov(
                ExpectedException(
                    agent="planner",
                    tool="sample::write_file",
                    policy="pii-deny",
                    # resource omitted -> matches any resource under this policy
                    reason="planner is on-call; emergency writes are authorized",
                )
            )
        ).warnings
        == []
    )
    by_tool = {r["tool"]: r for r in queries.unsafe_callable_tools("planner")}
    assert by_tool["write_file"]["classification"] == "authorized_but_governed"


def test_check_access_evidence_carries_classification_when_agent_known(graph):
    _seed(graph)
    assert (
        ingest_governance(
            _gov(
                ExpectedException(
                    agent="planner",
                    tool="sample::read_file",
                    policy="pii-deny",
                    resource=CSV,
                    reason="by design",
                )
            )
        ).warnings
        == []
    )
    res = queries.check_access("planner", "read_file")
    assert res["verdict"] == "DENY"  # verdict semantics unchanged
    [ev] = res["deny_evidence"]
    assert ev["classification"] == "authorized_but_governed"
    assert ev["exception_reason"] == "by design"

    # support-bot has no exception declared -> evidence is a plain violation
    res = queries.check_access("support-bot", "read_file")
    [ev] = res["deny_evidence"]
    assert ev["classification"] == "violation"


def test_unknown_policy_or_tool_in_exception_rejects_manifest(graph):
    _seed(graph)
    warnings = ingest_governance(
        _gov(ExpectedException(agent="planner", tool="sample::nope", policy="pii-deny", reason="x"))
    ).warnings
    assert any("nope" in w for w in warnings)
    warnings = ingest_governance(
        _gov(
            ExpectedException(
                agent="planner", tool="sample::read_file", policy="no-such-policy", reason="x"
            )
        )
    ).warnings
    assert any("no-such-policy" in w for w in warnings)


def test_exception_with_no_reason_still_classifies_authorized(graph):
    """Codex PR #1 review: classification was checking exc.reason for non-null
    as the match signal. ExpectedException.reason is OPTIONAL — a no-reason
    exception is valid and must still classify the row as
    authorized_but_governed. The match signal is now `exc IS NOT NULL`."""
    _seed(graph)
    assert (
        ingest_governance(
            _gov(
                ExpectedException(
                    agent="planner",
                    tool="sample::read_file",
                    policy="pii-deny",
                    resource=CSV,
                    # reason intentionally omitted
                )
            )
        ).warnings
        == []
    )
    rows = queries.unsafe_callable_tools("planner")
    by_tool = {r["tool"]: r for r in rows}
    assert by_tool["read_file"]["classification"] == "authorized_but_governed"
    # No exception_reason key (reason was None)
    assert "exception_reason" not in by_tool["read_file"]

    # check_access path too
    res = queries.check_access("planner", "read_file")
    [ev] = res["deny_evidence"]
    assert ev["classification"] == "authorized_but_governed"
    assert "exception_reason" not in ev
    # And all_authorized=False for read_file alone (write_file still violation)
    # because planner has both grants; assert all_authorized respects has_exception
    assert res["verdict"] == "DENY"


def test_check_access_carries_all_authorized_signal(graph):
    """When verdict=DENY, callers need a single signal for 'all paths are
    by-design'. Without it they'd have to aggregate per-row classification."""
    _seed(graph)
    # Every reach for planner is covered by an exception -> all_authorized=True
    assert (
        ingest_governance(
            _gov(
                ExpectedException(
                    agent="planner",
                    tool="sample::read_file",
                    policy="pii-deny",
                    resource=CSV,
                    reason="by design",
                ),
                ExpectedException(
                    agent="planner",
                    tool="sample::write_file",
                    policy="pii-deny",
                    resource=CSV,
                    reason="by design",
                ),
            )
        ).warnings
        == []
    )
    res = queries.check_access("planner", "read_file")
    assert res["verdict"] == "DENY"
    assert res["all_authorized"] is True

    # support-bot has no exception -> all_authorized=False
    res = queries.check_access("support-bot", "read_file")
    assert res["verdict"] == "DENY"
    assert res["all_authorized"] is False


def test_deny_rows_have_a_total_order_on_both_backends(backend_graph):
    """Ties on the old sort keys came back in whatever order the backend chose.

    One tool under two direct DENY policies, and one tool name on two servers,
    each tied on (agent, tool, resource). On this fixture Ladybug returned
    them out of policy-id order and Neo4j happened not to. A READS and a
    WRITES edge to one resource tie on (key, resource, policy).
    """
    for server in ("sample", "other"):
        loader.load_crawl_result(
            CrawlResult(
                server_name=server,
                transport="stdio",
                tools=[ToolRecord(name="admin")],
                resources=[ResourceRecord(uri=CSV)],
            )
        )
    assert (
        ingest_governance(
            Governance(
                agents=["planner"],
                policies=[
                    Policy(id="admin-deny", effect="DENY", scope="admin"),
                    Policy(id="pii-deny", effect="DENY", scope="customer-pii"),
                ],
                grants=[
                    AccessGrant(agent="planner", tool="sample::admin"),
                    AccessGrant(agent="planner", tool="other::admin"),
                ],
                data_access=[
                    DataAccess(tool="sample::admin", resource=CSV, mode="WRITES"),
                    DataAccess(tool="sample::admin", resource=CSV, mode="READS"),
                ],
                governed_by={
                    CSV: ["pii-deny"],
                    "sample::admin": ["pii-deny", "admin-deny"],
                    "other::admin": ["pii-deny", "admin-deny"],
                },
            )
        ).warnings
        == []
    )

    rows = queries.unsafe_callable_tools("planner")
    assert [(r["tool_key"], r["mode"], r["policy"]) for r in rows] == [
        ("sample::admin", "READS", "pii-deny"),
        ("sample::admin", "WRITES", "pii-deny"),
        ("other::admin", None, "admin-deny"),
        ("other::admin", None, "pii-deny"),
        ("sample::admin", None, "admin-deny"),
        ("sample::admin", None, "pii-deny"),
    ]
    evidence = queries.check_access("planner", "sample::admin")["deny_evidence"]
    assert [(d.get("mode"), d["policy"]) for d in evidence] == [
        ("READS", "pii-deny"),
        ("WRITES", "pii-deny"),
        (None, "admin-deny"),
        (None, "pii-deny"),
    ]


def _governance_with_both_exceptions() -> Governance:
    return Governance(
        agents=["planner"],
        policies=[Policy(id="pii-deny", effect="DENY")],
        grants=[AccessGrant(agent="planner", tool="sample::read_file")],
        data_access=[DataAccess(tool="sample::read_file", resource=CSV, mode="READS")],
        governed_by={CSV: ["pii-deny"]},
        expected_exceptions=[
            ExpectedException(
                agent="planner",
                tool="sample::read_file",
                policy="pii-deny",
                reason="wildcard: legacy blanket sign-off",
            ),
            ExpectedException(
                agent="planner",
                tool="sample::read_file",
                policy="pii-deny",
                resource=CSV,
                reason="specific: DPA ticket COMP-42",
            ),
        ],
    )


def test_exception_with_unknown_agent_warns(graph):
    """A typo'd agent in expected_exceptions can't classify any row —
    surface it at ingest, not at audit time."""
    seed_basic(graph)
    warnings = ingest_governance(
        Governance(
            agents=["planner"],
            policies=[Policy(id="pii-deny", effect="DENY")],
            grants=[AccessGrant(agent="planner", tool="sample::read_file")],
            data_access=[DataAccess(tool="sample::read_file", resource=CSV, mode="READS")],
            governed_by={CSV: ["pii-deny"]},
            expected_exceptions=[
                ExpectedException(
                    agent="planer",  # typo of 'planner'
                    tool="sample::read_file",
                    policy="pii-deny",
                    resource=CSV,
                    reason="by design",
                )
            ],
        )
    ).warnings
    assert warnings
    assert any("planer" in w and "not declared" in w for w in warnings)


def test_exception_narrowed_to_missing_resource_warns(graph):
    """If the (tool, resource) combination isn't authored in this manifest's
    data_access, the exception can't match any deny_evidence row — warn."""
    seed_basic(graph)
    warnings = ingest_governance(
        Governance(
            agents=["planner"],
            policies=[Policy(id="pii-deny", effect="DENY")],
            grants=[AccessGrant(agent="planner", tool="sample::read_file")],
            data_access=[DataAccess(tool="sample::read_file", resource=CSV, mode="READS")],
            governed_by={CSV: ["pii-deny"]},
            expected_exceptions=[
                ExpectedException(
                    agent="planner",
                    tool="sample::read_file",
                    policy="pii-deny",
                    resource="file:///wrong",  # not in data_access
                    reason="x",
                )
            ],
        )
    ).warnings
    assert warnings
    assert any("file:///wrong" in w and "no READS/WRITES" in w for w in warnings)


def test_exception_with_no_grant_for_tool_warns(graph):
    """Codex R3 critique: an exception that names an agent without a CAN_CALL
    grant on the tool is dead-on-arrival — no unsafe-tools row can ever
    match it. Warn at ingest, not at audit."""
    seed_basic(graph)
    warnings = ingest_governance(
        Governance(
            agents=["planner", "loner"],  # loner declared, but no grants
            policies=[Policy(id="pii-deny", effect="DENY")],
            grants=[AccessGrant(agent="planner", tool="sample::read_file")],
            data_access=[DataAccess(tool="sample::read_file", resource=CSV, mode="READS")],
            governed_by={CSV: ["pii-deny"]},
            expected_exceptions=[
                ExpectedException(
                    agent="loner",
                    tool="sample::read_file",
                    policy="pii-deny",
                    resource=CSV,
                    reason="will never match",
                )
            ],
        )
    ).warnings
    assert warnings
    assert any("loner" in w and "no CAN_CALL grant" in w for w in warnings)


def test_exception_with_wrong_policy_warns(graph):
    """Codex R3 critique: when exc.resource is set but the resource is NOT
    governed_by exc.policy, the exception can never match. Previous round
    only checked (tool, resource) ∈ data_access — missing the (resource,
    policy) binding check."""
    seed_basic(graph)
    warnings = ingest_governance(
        Governance(
            agents=["planner"],
            policies=[
                Policy(id="pii-deny", effect="DENY"),
                Policy(id="other-deny", effect="DENY"),
            ],
            grants=[AccessGrant(agent="planner", tool="sample::read_file")],
            data_access=[DataAccess(tool="sample::read_file", resource=CSV, mode="READS")],
            governed_by={CSV: ["pii-deny"]},  # NOT other-deny
            expected_exceptions=[
                ExpectedException(
                    agent="planner",
                    tool="sample::read_file",
                    policy="other-deny",  # wrong policy
                    resource=CSV,
                    reason="dead-on-arrival",
                )
            ],
        )
    ).warnings
    assert warnings
    assert any("other-deny" in w and "not governed_by" in w for w in warnings)


def test_wildcard_exception_with_no_reachable_path_warns(graph):
    """Wildcard exception (resource=None) must still verify the tool reaches
    the policy via SOME path — either a touched resource governed by the
    policy, or tool-level GOVERNED_BY."""
    seed_basic(graph)
    warnings = ingest_governance(
        Governance(
            agents=["planner"],
            policies=[
                Policy(id="pii-deny", effect="DENY"),
                Policy(id="unrelated-deny", effect="DENY"),
            ],
            grants=[AccessGrant(agent="planner", tool="sample::read_file")],
            data_access=[DataAccess(tool="sample::read_file", resource=CSV, mode="READS")],
            governed_by={CSV: ["pii-deny"]},  # only pii-deny; unrelated-deny has no path
            expected_exceptions=[
                ExpectedException(
                    agent="planner",
                    tool="sample::read_file",
                    policy="unrelated-deny",
                    # resource omitted -> wildcard
                    reason="will never fire",
                )
            ],
        )
    ).warnings
    assert warnings
    assert any("dead-on-arrival" in w and "unrelated-deny" in w for w in warnings)


def test_exception_against_allow_policy_warns(graph):
    """Codex R3 final critique: classification only joins to DENY policies,
    so an exception against an ALLOW policy can never fire. Reject at ingest."""
    seed_basic(graph)
    warnings = ingest_governance(
        Governance(
            agents=["planner"],
            policies=[
                Policy(id="pii-deny", effect="DENY"),
                Policy(id="legacy-allow", effect="ALLOW"),
            ],
            grants=[AccessGrant(agent="planner", tool="sample::read_file")],
            data_access=[DataAccess(tool="sample::read_file", resource=CSV, mode="READS")],
            governed_by={CSV: ["pii-deny", "legacy-allow"]},
            expected_exceptions=[
                ExpectedException(
                    agent="planner",
                    tool="sample::read_file",
                    policy="legacy-allow",  # ALLOW policy — can't ever fire
                    resource=CSV,
                    reason="dead-on-arrival",
                )
            ],
        )
    ).warnings
    assert warnings
    assert any("legacy-allow" in w and "ALLOW" in w for w in warnings)


def test_unsafe_tools_one_row_when_wildcard_and_specific_both_match(graph):
    crawl_sample(graph)
    assert ingest_governance(_governance_with_both_exceptions()).warnings == []

    rows = queries.unsafe_callable_tools("planner")
    assert len(rows) == 1
    row = rows[0]
    assert row["classification"] == "authorized_but_governed"
    # The resource-specific exception outranks the wildcard for the reason.
    assert row["exception_reason"] == "specific: DPA ticket COMP-42"


def test_check_access_one_deny_row_when_wildcard_and_specific_both_match(graph):
    crawl_sample(graph)
    assert ingest_governance(_governance_with_both_exceptions()).warnings == []

    res = queries.check_access("planner", "read_file")
    assert res["verdict"] == "DENY"
    assert len(res["deny_evidence"]) == 1
    assert res["deny_evidence"][0]["exception_reason"] == "specific: DPA ticket COMP-42"
    assert res["all_authorized"] is True


def test_exception_keys_cannot_collide():
    assert schema.exception_key("a|b", "c", None, "p") != schema.exception_key(
        "a", "b|c", None, "p"
    )
    assert schema.exception_key("a", "s::t", None, "p") != schema.exception_key(
        "a", "s::t", "*", "p"
    )
    assert schema.exception_key("a", "s::t", None, "p").startswith("v2:")


def test_colliding_exception_tuples_and_legacy_reingest(backend_graph):
    from toolgraph.models import AccessGrant, ExpectedException, Policy

    for server in ("s", "b|s"):
        loader.load_crawl_result(CrawlResult(server_name=server, tools=[ToolRecord(name="t")]))
    pairs = [("a|b", "s::t"), ("a", "b|s::t")]
    gov = Governance(
        policies=[Policy(id="p", effect="DENY")],
        grants=[AccessGrant(agent=a, tool=k) for a, k in pairs],
        governed_by={k: ["p"] for _, k in pairs},
        expected_exceptions=[
            ExpectedException(agent=a, tool=k, policy="p", reason="first") for a, k in pairs
        ]
        + [ExpectedException(agent="a", tool="b|s::t", policy="p", reason="last")],
    )
    assert ingest_governance(gov).applied
    with driver.session() as session:
        rows = list(
            session.run(
                "MATCH (e:ExpectedException) RETURN e.key AS key, e.agent AS agent, e.reason AS reason"
            )
        )
        assert len(rows) == 2
        assert {r["agent"]: r["reason"] for r in rows} == {"a|b": "first", "a": "last"}
        session.run("MATCH (e:ExpectedException {agent:'a'}) DELETE e")
        session.run(
            "CREATE (:ExpectedException {key:'legacy|key', agent:'a', tool_key:'b|s::t', policy:'p', reason:'last'})"
        )
    assert ingest_governance(gov).applied
    with driver.session() as session:
        rows = list(session.run("MATCH (e:ExpectedException) RETURN e.key AS key"))
        assert len(rows) == 2 and all(r["key"].startswith("v2:") for r in rows)
