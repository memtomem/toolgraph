"""Bounded, read-only comparison of observed and hypothetical governance."""

from __future__ import annotations

import html
import json
import re
import unicodedata
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from toolgraph.artifact_safety import safe_artifact
from toolgraph.artifacts import canonical_json_bytes, sha256_bytes
from toolgraph.graph import queries, selector
from toolgraph.graph.driver import acquire_readonly_session
from toolgraph.graph.schema import check_backend_schema_version
from toolgraph.manifest.ingest import _validate_manifest
from toolgraph.manifest.parser import governance_digest
from toolgraph.models import Governance
from toolgraph.policy_bundle import tool_contract_digest
from toolgraph.policy_impact_models import (
    UNRESOLVED,
    ImpactDecision,
    ImpactReport,
    ImpactRow,
    change_kind,
    clean_identity,
)
from toolgraph.redaction import redact_credentials, redact_text

MAX_PROJECTION_ROWS = 100_000
MAX_IMPACT_BYTES = 50_000_000


class PolicyImpactError(ValueError):
    """No complete, trustworthy impact comparison can be produced."""


class _Rows(list):
    def single(self):
        if len(self) > 1:
            raise PolicyImpactError("expected at most one projection row")
        return self[0] if self else None


class _Projection:
    """Count retained rows/bytes; never expose a write or transaction interface.

    LIMIT stops oversized result sets, not aggregate fields or driver buffering.
    The byte budget is checked after decoding; it is not a process RSS limit.
    """

    def __init__(self, session):
        self.session = session
        self.rows = 0
        self.bytes = 0

    def account(self, row):
        self.rows += 1
        self.bytes += len(canonical_json_bytes(row))
        if self.rows > MAX_PROJECTION_ROWS or self.bytes > MAX_IMPACT_BYTES:
            raise PolicyImpactError("impact projection exceeds its row/byte budget; narrow scope")

    def run(self, query, **parameters):
        limit = MAX_PROJECTION_ROWS - self.rows + 1
        result = self.session.run(
            query + " LIMIT $impact_row_limit", **parameters, impact_row_limit=limit
        )
        out = _Rows()
        for row in result:
            item = dict(row)
            self.account(item)
            out.append(item)
        return out


def _scope(agent: str, candidates: list[str], profile: str) -> list[str]:
    clean_identity(agent)
    if problem := selector.profile_error(profile):
        raise PolicyImpactError(problem)
    if not candidates or len(candidates) > selector.MAX_CANDIDATES:
        raise PolicyImpactError(f"select 1–{selector.MAX_CANDIDATES} candidate tools")
    for ref in candidates:
        clean_identity(ref)
    return sorted(set(candidates))


def _state(s):
    state = queries.graph_state(s)
    if not state.instance_id or not state.governance_digest:
        raise PolicyImpactError("graph needs an instance id and ingested governance digest")
    clean_identity(state.instance_id)
    if not re.fullmatch(r"[a-f0-9]{64}", state.governance_digest):
        raise PolicyImpactError("graph governance digest is invalid")
    if (
        isinstance(state.generation, bool)
        or not isinstance(state.generation, int)
        or state.generation < 0
    ):
        raise PolicyImpactError("graph generation must be a non-negative integer")
    return state


def _catalog(s, resolved):
    keys = sorted({key for matches in resolved.values() for key in matches})
    contracts = []
    for row in s.run(
        "MATCH (t:Tool) WHERE t.key IN $keys "
        "RETURN t.key AS tool_key, t.server AS server, t.name AS name, "
        "t.description AS description, t.input_schema AS input_schema, "
        "t.read_only_hint AS read_only_hint, t.destructive_hint AS destructive_hint, "
        "t.idempotent_hint AS idempotent_hint, t.open_world_hint AS open_world_hint, "
        "EXISTS { MATCH (:MCPServer)-[:EXPOSES]->(t) } AS exposed ORDER BY tool_key",
        keys=keys,
    ):
        clean_identity(row["tool_key"])
        row["input_schema"] = json.loads(row["input_schema"]) if row["input_schema"] else None
        contracts.append(
            {
                "tool_key": row["tool_key"],
                "exposed": row["exposed"],
                "contract_digest": tool_contract_digest(row),
            }
        )
    return sha256_bytes(canonical_json_bytes({"resolution": resolved, "contracts": contracts}))


def _current(s, agent, candidates, resolved):
    keys = sorted({key for matches in resolved.values() for key in matches})
    facts = selector._tool_facts(s, keys, agent)
    deny = queries._deny_evidence_batch(s, keys, agent=agent)
    found = queries._agent_present(s, agent)
    ranked = selector.assemble_features(agent, candidates, resolved, facts, deny)
    ranked["agent_found"] = found
    return ranked, facts


def _backed(provenance):
    return bool(provenance.get("evidence") and provenance["evidence"].strip())


def _add_route(projection, routes, route):
    projection.account(route)
    routes.append(route)


def _hypothetical(s, gov, normalized, agent, candidates, resolved, observed_facts):
    """Replace authored edges using ingest's normalized rows; retain crawl facts."""
    grants = {(a, key) for a, key, *_ in normalized["resolved_grants"]}
    access = defaultdict(list)
    for key, mode, resource, prov in normalized["resolved_access"]:
        access[key].append((mode, resource, prov))
    bindings = {(kind, ref): entries for kind, ref, entries in normalized["resolved_governed"]}
    policies = {p.id: p.effect for p in gov.policies}
    exceptions = defaultdict(list)
    for a, key, resource, policy, reason in normalized["resolved_exceptions"]:
        if a == agent:
            exceptions[(key, policy)].append({"resource": resource, "reason": reason})
    authored = {key for _, key in grants} | set(access)
    authored |= {ref for (kind, ref), entries in bindings.items() if kind == "tool" and entries}
    # Ingest prunes degree-zero tools after replacing authored edges. Only
    # EXPOSES is a crawl-owned Tool edge in the supported graph schema.
    facts, deny = {}, {}
    for key, original in observed_facts.items():
        if original["is_drifted"] and key not in authored:
            continue
        f = {
            **original,
            "permitted": (agent, key) in grants,
            "is_unmapped": not access[key],
            "has_unbacked_edges": False,
        }
        paths = []

        routes = []

        for mode, resource, prov in access[key]:
            if not _backed(prov):
                f["has_unbacked_edges"] = True
            for pid, policy_prov in bindings.get(("resource", resource), []):
                if not _backed(policy_prov):
                    f["has_unbacked_edges"] = True
                if policies[pid] == "DENY":
                    _add_route(s, routes, ("resource", mode, resource, pid, prov, policy_prov))
        for pid, prov in bindings.get(("tool", key), []):
            if not _backed(prov):
                f["has_unbacked_edges"] = True
            if policies[pid] == "DENY":
                _add_route(s, routes, ("tool", None, None, pid, prov, None))
        # Match the database route ordering and resource-specific exception priority.
        routes.sort(key=lambda x: (x[0], x[2] or "", x[3], x[1] or ""))
        for via, mode, resource, pid, prov, policy_prov in routes:
            row = {
                **prov,
                "mode": mode,
                "resource": resource,
                "policy": pid,
                "exceptions": [
                    e
                    for e in exceptions[(key, pid)]
                    if e["resource"] is None or (via == "resource" and e["resource"] == resource)
                ],
            }
            if policy_prov is not None:
                row.update({f"gov_{k}": v for k, v in policy_prov.items()})
            evidence = {"via": via, "policy": pid}
            if via == "resource":
                evidence.update(mode=mode, resource=resource)
            evidence.update(queries._deny_fields(via, row, queries._name(key), classify=True))
            s.account(evidence)
            paths.append(evidence)
        facts[key], deny[key] = f, paths
    after_resolution = {
        ref: [key for key in keys if key in facts] for ref, keys in resolved.items()
    }
    ranked = selector.assemble_features(agent, candidates, after_resolution, facts, deny)
    ranked["agent_found"] = agent in normalized["known_agents"]
    return ranked


def _scrub_evidence(value):
    # Use the artifact URI policy first, then handle free-text credentials.
    if isinstance(value, dict):
        return {k: _scrub_evidence(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub_evidence(v) for v in value]
    return redact_credentials(value) if isinstance(value, str) else value


def _decisions(ranked, profile):
    filtered = selector.filter_features(ranked, profile)
    rejected = {row["candidate"]: row for row in filtered["rejected"]}
    result = []
    for feature in ranked["features"]:
        reason = rejected.get(feature["candidate"], {}).get("reason")
        if not ranked["agent_found"]:
            reason = "AGENT_NOT_FOUND"
        unresolved = reason in UNRESOLVED
        result.append(
            ImpactDecision(
                decision="unresolved" if unresolved else "rejected" if reason else "eligible",
                reason=reason,
                tool_key=None if unresolved else feature["tool_key"],
                agent_found=ranked["agent_found"],
                risk_score=feature["risk_score"],
                evidence=_scrub_evidence(
                    safe_artifact({"deny_paths": feature["deny_paths"]})["deny_paths"]
                ),
                is_drifted=feature["is_drifted"],
                is_unmapped=feature["is_unmapped"],
                has_unbacked_edges=feature["has_unbacked_edges"],
            )
        )
    return result


def build_policy_impact(
    governance: Governance,
    *,
    agent: str,
    candidates: list[str],
    profile: str = selector.DEFAULT_PROFILE,
    strict_drift: bool = False,
    created_at: datetime | None = None,
) -> dict[str, Any]:
    candidates = _scope(agent, candidates, profile)
    governance = Governance.model_validate(governance.model_dump(mode="json"))
    with acquire_readonly_session() as session:
        check_backend_schema_version(session)

        def fetch():
            s = _Projection(session)
            state = _state(s)
            warnings, notices, normalized = _validate_manifest(s, governance, strict_drift)
            if warnings:
                raise PolicyImpactError(
                    "candidate manifest rejected: " + "; ".join(map(redact_text, warnings))
                )
            resolved = queries.resolve_tool_refs(s, candidates)
            catalog = _catalog(s, resolved)
            current, facts = _current(s, agent, candidates, resolved)
            proposed = _hypothetical(s, governance, normalized, agent, candidates, resolved, facts)
            before, after = _decisions(current, profile), _decisions(proposed, profile)
            rows = [
                ImpactRow(
                    candidate=ref,
                    before=b,
                    after=a,
                    change=change_kind(b, a),
                    details_changed=b != a,
                )
                for ref, b, a in zip(candidates, before, after, strict=True)
            ]
            report = ImpactReport(
                schema_version=1,
                kind="toolgraph.policy-impact",
                mode="advisory",
                complete=True,
                created_at=(created_at or datetime.now(UTC)).isoformat(),
                graph_state={"instance_id": state.instance_id, "generation": state.generation},
                baseline_governance_digest=state.governance_digest,
                proposed_governance_digest=governance_digest(governance),
                catalog_digest=catalog,
                agent=agent,
                profile=profile,
                candidates=candidates,
                rows=rows,
                regression_ready=all(row.change != "unresolved" for row in rows),
                notices=[redact_text(n) for n in notices],
            ).model_dump(mode="json")
            if len(canonical_json_bytes(report)) > MAX_IMPACT_BYTES:
                raise PolicyImpactError("impact artifact exceeds byte budget; narrow scope")
            return {"report": report}

        result = queries.with_graph_state(fetch, strict=True, state_reader=lambda: _state(session))
    return result["report"]


def load_impact(path: Path) -> ImpactReport:
    with path.open("rb") as handle:
        raw = handle.read(MAX_IMPACT_BYTES + 1)
    if len(raw) > MAX_IMPACT_BYTES:
        raise PolicyImpactError("impact artifact exceeds byte budget")

    # Duplicate keys must not silently select different expectations for consumers.
    def unique(pairs):
        obj = {}
        for key, value in pairs:
            if key in obj:
                raise PolicyImpactError("duplicate key in impact artifact")
            obj[key] = value
        return obj

    try:

        def refuse_constant(value):
            raise PolicyImpactError("non-finite number in impact artifact")

        value = json.loads(raw, object_pairs_hook=unique, parse_constant=refuse_constant)
        return ImpactReport.model_validate(value)
    except (ValueError, RecursionError) as exc:
        raise PolicyImpactError("invalid policy impact artifact") from exc


def check_policy_impact(report: ImpactReport) -> dict[str, Any]:
    # Revalidate library inputs too: model_copy/model_construct can bypass validators.
    report = ImpactReport.model_validate(report.model_dump(mode="json"))
    if not report.regression_ready:
        raise PolicyImpactError(
            "impact contains unresolved comparisons; regenerate reviewed expectations"
        )
    candidates = _scope(report.agent, report.candidates, report.profile)
    with acquire_readonly_session() as session:
        check_backend_schema_version(session)

        def fetch():
            s = _Projection(session)
            state = _state(s)
            resolved = queries.resolve_tool_refs(s, candidates)
            catalog = _catalog(s, resolved)
            if state.instance_id != report.graph_state.instance_id:
                raise PolicyImpactError(
                    "incompatible graph instance; regenerate reviewed expectations"
                )
            if catalog != report.catalog_digest:
                raise PolicyImpactError(
                    "incompatible scoped catalog: tool resolution, exposure, or contract changed; "
                    "check crawl/server status and restore unintended changes before retrying. "
                    "For intended catalog changes, regenerate and review expectations"
                )
            ranked, _ = _current(s, report.agent, candidates, resolved)
            actual = _decisions(ranked, report.profile)
            rows = []
            for row, current in zip(report.rows, actual, strict=True):
                expected = row.after
                matches = (current.decision, current.reason, current.tool_key) == (
                    expected.decision,
                    expected.reason,
                    expected.tool_key,
                )
                rows.append(
                    {
                        "candidate": row.candidate,
                        "matches": matches,
                        "expected": {
                            **expected.model_dump(mode="json"),
                            "evidence": _scrub_evidence(
                                safe_artifact({"deny_paths": expected.evidence})["deny_paths"]
                            ),
                        },
                        "actual": current.model_dump(mode="json"),
                    }
                )
            return {
                "passed": all(row["matches"] for row in rows),
                "rows": rows,
                "expected_governance_digest": report.proposed_governance_digest,
                "current_governance_digest": state.governance_digest,
                "catalog_digest": catalog,
                "agent": report.agent,
                "profile": report.profile,
            }

        result = queries.with_graph_state(fetch, strict=True, state_reader=lambda: _state(session))
    if len(canonical_json_bytes(result)) > MAX_IMPACT_BYTES:
        raise PolicyImpactError("check result exceeds byte budget")
    return result


def _visible_controls(text: str) -> str:
    """Make control/format characters visible without rewriting artifact identities."""
    return "".join(
        json.dumps(char, ensure_ascii=True)[1:-1]
        if unicodedata.category(char) in {"Cc", "Cf"}
        else char
        for char in text
    )


def _display(value):
    """Escape untrusted identifiers for passive Markdown display only."""
    return html.escape(
        re.sub(r"([\\`*_{}\[\]()#+.!|~-])", r"\\\1", _visible_controls(str(value))), quote=False
    )


def impact_markdown(report: dict) -> str:
    lines = [
        "# Policy impact (advisory)",
        "",
        f"Agent: {_display(report['agent'])}; profile: {report['profile']}",
        "",
        "Compared only the explicitly selected tools. No policy was applied.",
        "",
        "| Candidate | Before | Proposed | Change | Details changed |",
        "| --- | --- | --- | --- | --- |",
    ]
    for row in report["rows"]:

        def label(side):
            return side["decision"] + (f" ({side['reason']})" if side["reason"] else "")

        lines.append(
            f"| {_display(row['candidate'])} | {label(row['before'])} | "
            f"{label(row['after'])} | {row['change']} | {row['details_changed']} |"
        )
    for row in report["rows"]:
        lines.extend(["", f"## {_display(row['candidate'])}"])
        for name in ("before", "after"):
            lines.append(f"\n{name.title()} evidence:")
            evidence = row[name]["evidence"]
            if not evidence:
                lines.append("\nNo DENY path evidence for this scoped decision.")
            for path in evidence:
                lines.append("\n" + _json_block(path))
    if report["notices"]:
        lines.extend(["", "## Notices", *["- " + _display(n) for n in report["notices"]]])
    lines.extend(["", f"Regression expectations ready: {report['regression_ready']}."])
    return "\n".join(lines) + "\n"


def _json_block(value):
    """Keep JSON readable while preventing embedded text from closing its fence."""
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2)
    payload = "\n".join(_visible_controls(line) for line in payload.split("\n"))
    longest = max((len(run) for run in re.findall(r"`+", payload)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}json\n{payload}\n{fence}"


def check_markdown(result: dict) -> str:
    lines = [
        "# Policy regression check: " + ("PASS" if result["passed"] else "FAIL"),
        "",
        f"Expected governance digest: `{result['expected_governance_digest']}`",
        f"Current governance digest: `{result['current_governance_digest']}`",
        "",
        "| Candidate | Matches | Expected | Actual |",
        "| --- | --- | --- | --- |",
    ]
    for row in result["rows"]:
        expected = row["expected"]["reason"] or row["expected"]["decision"]
        actual = row["actual"]["reason"] or row["actual"]["decision"]
        lines.append(f"| {_display(row['candidate'])} | {row['matches']} | {expected} | {actual} |")
    return "\n".join(lines) + "\n"
