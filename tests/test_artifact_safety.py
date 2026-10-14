"""Artifact identity safety: identities survive unchanged, credential-shaped
ones are refused before any graph read, evidence is redacted.
"""

from __future__ import annotations

import pytest
from toolgraph.artifact_safety import safe_artifact
from toolgraph.graph import queries
from toolgraph.policy_bundle import build_policy_bundle
from toolgraph.preflight import build_preflight


class Reader:
    def __init__(self, size):
        self.keys = [f"s::t{i:05}" for i in range(size)]
        self.calls = []
        self.generation = 1

    def state(self):
        return queries.GraphState("injected", self.generation, "a" * 64)

    def exposed_tool_contracts(self):
        return [
            {"tool_key": k, "server": "s", "name": k.split("::")[1]}
            for k in reversed(self.keys)
        ]

    def evaluate(self, agent, candidates, profile):
        self.calls.append(list(candidates))
        return {
            "agent_found": True,
            "filtered": {"eligible": candidates, "rejected": []},
            "features": [{"tool_key": k, "risk_score": 0} for k in candidates],
        }


def test_identity_preservation_and_evidence_redaction():
    identity = "https://identity.test/a?role=one#part"
    artifact = {
        "agent": identity,
        "eligible": [identity],
        "rejected": [
            {
                "candidate": identity,
                "candidates": [identity],
                "paths": ["https://user:secret@host/r?token=secret#secret"],
            }
        ],
        "run_id": identity,
    }
    result = safe_artifact(artifact)
    assert (
        result["agent"]
        == result["eligible"][0]
        == result["rejected"][0]["candidate"]
        == result["run_id"]
        == identity
    )
    assert result["rejected"][0]["paths"] == ["https://host/r"]
    assert artifact["rejected"][0]["paths"][0].startswith("https://user:")


@pytest.mark.parametrize(
    "identity",
    [
        "https://u:p@host/a",
        "s::t?token=secret",
        "https://me@corp.example:p@host/a",
        "s::t?access_token=secret",
        "s::t?x=1&credential=secret",
    ],
)
def test_credentials_rejected_before_graph_access(monkeypatch, identity):
    monkeypatch.setattr(
        queries, "with_graph_state", lambda *a, **kw: pytest.fail("graph accessed")
    )
    with pytest.raises(ValueError, match="credential"):
        build_preflight(agent="a", candidates=[identity], profile="review", run_id="r")
    with pytest.raises(ValueError, match="credential"):
        build_policy_bundle(agent=identity, store=Reader(0))
