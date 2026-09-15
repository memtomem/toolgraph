"""Regression evidence for the v12 handoff's six review findings."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch

import pytest
from typer.testing import CliRunner
import yaml

from toolgraph import cli, config
from toolgraph.graph import driver, loader, queries, schema, selector
from toolgraph.health import run_doctor
from toolgraph.manifest.ingest import dry_run_ingest, ingest_governance
from toolgraph.models import CrawlResult, Governance, ResourceRecord, ToolRecord
from toolgraph.policy_propose import ProvenanceError, StateMismatchError, run_propose


@pytest.fixture
def proposal_case(backend_graph, tmp_path):
    keys = ['clean', 'unmapped', 'denied', 'retired', 'deleted', 'already']
    loader.load_crawl_result(CrawlResult(
        server_name='sample', tools=[ToolRecord(name=key) for key in keys],
        resources=[ResourceRecord(uri='file:///data')],
    ))
    gov = Governance.model_validate({
        'agents': ['bot'],
        'policies': [{'id': 'deny', 'effect': 'DENY'}],
        'data_access': [
            {'tool': f'sample::{key}', 'resource': 'file:///data', 'mode': 'READS'}
            for key in keys if key != 'unmapped'
        ],
        'governed_by': {'sample::denied': [{'policy': 'deny'}]},
    })
    assert ingest_governance(gov).applied
    governance = tmp_path / 'governance.yaml'
    governance.write_text(yaml.safe_dump(gov.model_dump(mode='json')))
    state = queries.graph_state()
    envelope = {
        'graph_instance_id': state.instance_id,
        'graph_generation': state.generation,
        'governance_digest': state.governance_digest,
        'profile': 'strict',
        'window_start': '2026-09-01T00:00:00Z',
        'window_end': '2026-09-02T00:00:00Z',
    }
    observed = tmp_path / 'observed.jsonl'

    def export(env=envelope, tools=('clean',)):
        lines = [] if env is None else [json.dumps({'envelope': env})]
        lines += [json.dumps({
            'agent': 'bot', 'tool_key': f'sample::{key}', 'decision': 'rejected',
            'calls': 5, 'would_block_calls': 5, 'reject_reasons': ['NOT_GRANTED'],
        }) for key in tools]
        observed.write_text('\n'.join(lines) + '\n')
        return governance, observed

    export()
    return envelope, export


@pytest.mark.parametrize('missing', ['envelope', 'observed.instance', 'current.instance', 'current.digest'])
def test_incomplete_identity_requires_opt_in_and_stays_unverified(proposal_case, missing):
    envelope, export = proposal_case
    envelope = dict(envelope)
    if missing == 'envelope':
        envelope = None
    elif missing == 'observed.instance':
        envelope.pop('graph_instance_id')
    else:
        field = 'instance_id' if missing == 'current.instance' else 'governance_digest'
        with driver.session() as session:
            session.run(f"MATCH (m:GraphMeta {{id:'singleton'}}) SET m.{field}=NULL")
    args = export(envelope)
    for revalidate in (False, True):
        with pytest.raises(ProvenanceError):
            run_propose(*args, revalidate_current=revalidate)
        proposal, rendered = run_propose(
            *args, unverified_provenance=True, revalidate_current=revalidate,
        )
        assert proposal['provenance']['status'] == 'unverified'
        assert proposal['provenance']['missing_fields']
        assert proposal['provenance']['current_revalidated'] is revalidate
        assert 'UNVERIFIED PROVENANCE' in rendered
        assert 'exact composite token matched' not in rendered
        assert len(proposal['additions']) == 1
    result = CliRunner().invoke(cli.app, [
        'policy', 'propose', *map(str, args), '--unverified-provenance', '--json',
    ])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)['provenance']['status'] == 'unverified'


@pytest.mark.parametrize('field,value', [
    ('graph_instance_id', 1), ('graph_instance_id', True),
    ('graph_instance_id', 'bad\nidentity'), ('graph_instance_id', ' spaced '),
    ('instance_id', ['bad']),
])
def test_invalid_identity_is_not_an_opt_in(proposal_case, field, value):
    envelope, export = proposal_case
    args = export({**envelope, field: value})
    with pytest.raises(ProvenanceError):
        run_propose(*args, unverified_provenance=True, revalidate_current=True)


def test_alias_compatibility_and_conflict(proposal_case):
    envelope, export = proposal_case
    alias = dict(envelope)
    alias['instance_id'] = alias.pop('graph_instance_id')
    for env in (alias, {**envelope, **alias}):
        proposal, rendered = run_propose(*export(env))
        assert proposal['provenance']['status'] == 'exact'
        assert 'exact composite token matched' in rendered
    with pytest.raises(ProvenanceError, match='conflict'):
        run_propose(*export({**envelope, 'instance_id': 'different'}), unverified_provenance=True)
    for value in (None, '', '   '):
        args = export({**envelope, 'graph_instance_id': value})
        with pytest.raises(ProvenanceError, match='incomplete provenance'):
            run_propose(*args)
        proposal, _ = run_propose(*args, unverified_provenance=True)
        assert proposal['provenance']['status'] == 'unverified'


@pytest.mark.parametrize('mismatch', ['instance', 'live_digest', 'manifest_digest', 'generation'])
def test_mismatches_cannot_be_bypassed(proposal_case, mismatch):
    envelope, export = proposal_case
    env = dict(envelope)
    if mismatch == 'instance':
        env['graph_instance_id'] = 'different-instance'
    elif mismatch == 'manifest_digest':
        env['governance_digest'] = 'a' * 64
    elif mismatch == 'generation':
        env['graph_generation'] -= 1
    else:
        with driver.session() as session:
            session.run("MATCH (m:GraphMeta {id:'singleton'}) SET m.governance_digest=$d", d='b' * 64)
    args = export(env)
    for unverified in (False, True):
        if mismatch == 'generation':
            with pytest.raises(StateMismatchError):
                run_propose(*args, unverified_provenance=unverified)
            proposal, _ = run_propose(*args, revalidate_current=True, unverified_provenance=unverified)
            assert proposal['provenance']['status'] == 'revalidated'
        else:
            with pytest.raises((ProvenanceError, StateMismatchError)):
                run_propose(*args, unverified_provenance=unverified, revalidate_current=True)


@pytest.mark.parametrize('profile', ['strict', 'review', 'explore'])
def test_current_revalidation_checks_all_strict_blockers_even_same_generation(proposal_case, profile):
    envelope, export = proposal_case
    with driver.session() as session:
        session.run("MATCH (:MCPServer)-[e:EXPOSES]->(:Tool {key:'sample::retired'}) DELETE e")
        session.run("MATCH (t:Tool {key:'sample::deleted'}) DETACH DELETE t")
        session.run("MATCH (a:Agent {id:'bot'}), (t:Tool {key:'sample::already'}) CREATE (a)-[:CAN_CALL]->(t)")
    args = export({**envelope, 'profile': profile}, ('clean', 'unmapped', 'denied', 'retired', 'deleted', 'already'))
    proposal, rendered = run_propose(*args, revalidate_current=True)
    assert [row['tool'] for row in proposal['additions']] == ['sample::clean']
    assert {row['reason'] for row in proposal['not_grant_shaped']} == {'UNMAPPED', 'DRIFTED', 'TOOL_NOT_FOUND'}
    assert [row['reason'] for row in proposal['conflicts']] == ['DENY_VIOLATION']
    assert [row['reason'] for row in proposal['insufficient_evidence']] == ['ALREADY_GRANTED']
    assert proposal['provenance']['revalidation_profile'] == 'strict'
    assert 'profile=strict' in rendered
    # Hypothetical grants are never persisted.
    with driver.session() as session:
        assert session.run("MATCH (:Agent {id:'bot'})-[:CAN_CALL]->(t:Tool) RETURN t.key AS k").single()['k'] == 'sample::already'


def test_missing_current_agent_is_insufficient_evidence(proposal_case):
    _, export = proposal_case
    with driver.session() as session:
        session.run("MATCH (a:Agent {id:'bot'}) DETACH DELETE a")
    proposal, _ = run_propose(*export(), revalidate_current=True)
    assert not proposal['additions']
    assert proposal['insufficient_evidence'][0]['reason'] == 'AGENT_NOT_FOUND'


def test_revalidation_retries_all_reads_after_catalog_change(proposal_case, monkeypatch):
    _, export = proposal_case
    original = selector.rank_features
    calls = []

    def change_after_read(agent, candidates):
        ranked = original(agent, candidates)
        calls.append(candidates)
        if len(calls) == 1:
            with driver.session() as session:
                session.run("MATCH (:MCPServer)-[e:EXPOSES]->(:Tool {key:'sample::clean'}) DELETE e")
                session.run("MATCH (m:GraphMeta {id:'singleton'}) SET m.generation=m.generation+1")
        return ranked

    monkeypatch.setattr(selector, 'rank_features', change_after_read)
    monkeypatch.setattr(queries, '_bracket_backoff', lambda _: None)
    proposal, _ = run_propose(*export(), revalidate_current=True)
    assert len(calls) == 2
    assert not proposal['additions']
    assert proposal['not_grant_shaped'][0]['reason'] == 'DRIFTED'


def test_revalidation_chunks_candidates_without_mutating_features(tmp_path, monkeypatch):
    # Exercise the public workflow with more than the selector's real limit;
    # bounded calls are instrumented, graph semantics are covered above.
    count = selector.MAX_CANDIDATES + 1
    governance = tmp_path / 'gov.yaml'
    governance.write_text('agents: [bot]\n')
    observed = tmp_path / 'observed.jsonl'
    observed.write_text('\n'.join(json.dumps({
        'agent': 'bot', 'tool_key': f'sample::t{i}', 'decision': 'rejected',
        'calls': 5, 'would_block_calls': 5, 'reject_reasons': ['NOT_GRANTED'],
    }) for i in range(count)))
    monkeypatch.setattr(queries, 'graph_state', lambda: queries.GraphState('instance', 1))
    from contextlib import nullcontext
    monkeypatch.setattr(driver, 'session', lambda: nullcontext(None))
    batches, originals = [], []

    def ranked(agent, candidates):
        batches.append(len(candidates))
        rows = [{'candidate': key, 'tool_key': key, 'found': True, 'ambiguous': False,
                 'permitted': False, 'classification': None, 'is_drifted': False,
                 'is_unmapped': False} for key in candidates]
        originals.extend(rows)
        return {'agent': agent, 'agent_found': True, 'features': rows}

    monkeypatch.setattr(selector, 'rank_features', ranked)
    proposal, _ = run_propose(governance, observed, unverified_provenance=True, revalidate_current=True)
    assert batches == [selector.MAX_CANDIDATES, 1]
    assert len(proposal['additions']) == count
    assert all(not row['permitted'] for row in originals)


def test_nullable_exceptions_and_empty_bindings_match_real_ingest(backend_graph):
    loader.load_crawl_result(CrawlResult(server_name='sample', tools=[ToolRecord(name='read')]))

    def gov(reason):
        return Governance.model_validate({
            'agents': ['bot'], 'policies': [{'id': 'deny', 'effect': 'DENY'}],
            'grants': [{'agent': 'bot', 'tool': 'sample::read'}],
            'data_access': [{'tool': 'sample::read', 'resource': 'file:///data', 'mode': 'READS'}],
            'governed_by': {'file:///data': [{'policy': 'deny'}]},
            'expected_exceptions': [
                {'agent': 'bot', 'tool': 'sample::read', 'resource': resource, 'policy': 'deny', 'reason': reason}
                for resource in (None, 'file:///data')
            ],
        })

    def snapshot():
        with driver.session() as session:
            return (
                queries.graph_state(),
                [dict(row) for row in session.run('MATCH (e:ExpectedException) RETURN e.resource AS resource, e.reason AS reason')],
                sorted(row['uri'] for row in session.run('MATCH (r:Resource) RETURN r.uri AS uri')),
            )

    def dry_unchanged(manifest):
        before = snapshot()
        diff = dry_run_ingest(manifest)
        assert diff.applied, diff.warnings
        assert snapshot() == before
        return diff

    first = gov('before')
    assert [row['resource'] for row in dry_unchanged(first).expected_exceptions_added] == [None, 'file:///data']
    assert ingest_governance(first).applied
    updated = gov('after')
    assert len(dry_unchanged(updated).expected_exceptions_reason_updated) == 2
    assert ingest_governance(updated).applied
    assert {row['reason'] for row in snapshot()[1]} == {'after'}
    empty = Governance(governed_by={'file:///unused': [], 'file:///data': []})
    with driver.session() as session:
        session.run("CREATE (:Resource {uri:'file:///unused'})")
    diff = dry_unchanged(empty)
    assert len(diff.expected_exceptions_removed) == 2
    assert diff.resources_created == []
    assert set(diff.resources_pruned) == {'file:///data', 'file:///unused'}
    assert ingest_governance(empty).applied
    assert snapshot()[1:] == ([], [])
    assert dry_unchanged(empty).resources_created == []


@pytest.mark.parametrize('phase', ['inspection', 'settings', 'connectivity', 'schema', 'state'])
def test_doctor_redacts_all_exception_reports(monkeypatch, phase):
    info = {'path': 'probe.json', 'exists': True, 'valid_json': True,
            'permissions_ok': True, 'permissions': '0o600', 'healthy': True, 'detail': 'ok'}
    monkeypatch.setattr(config, 'check_config_health', lambda: info)
    monkeypatch.setattr(driver, 'verify_connectivity', lambda: None)
    monkeypatch.setattr(schema, 'inspect_backend_schema', lambda: {
        'healthy': True, 'schema_version': 1, 'backend': 'ladybug', 'live_tables': [],
    })
    monkeypatch.setattr(queries, 'graph_state', lambda: queries.GraphState('probe', 1))
    error = RuntimeError('bolt://user:synthetic-password@localhost:7687 token=synthetic-token')
    targets = {
        'inspection': (config, 'check_config_health'),
        'settings': (config, '__getattr__'),
        'connectivity': (driver, 'verify_connectivity'),
        'schema': (schema, 'inspect_backend_schema'),
        'state': (queries, 'graph_state'),
    }
    if phase == 'settings':
        monkeypatch.delattr(config, 'settings', raising=False)
    with patch.object(*targets[phase], side_effect=error):
        report = run_doctor()
    assert not report.healthy
    for text in (report.summary_text(), json.dumps(report.to_dict())):
        assert 'synthetic-password' not in text
        assert 'synthetic-token' not in text
        assert '<redacted>' in text


def test_doctor_invalid_config_in_fresh_process(tmp_path):
    config_path = tmp_path / 'broken.json'
    config_path.write_text('{invalid json')
    result = subprocess.run([
        sys.executable, '-c', 'from toolgraph.cli import app; app()', 'doctor', '--json',
    ], cwd=Path(__file__).resolve().parents[1], env={**os.environ, 'TOOLGRAPH_CONFIG': str(config_path)},
       text=True, capture_output=True)
    assert result.returncode == 1, result.stderr
    report = json.loads(result.stdout)
    assert report['healthy'] is False
    assert [check['name'] for check in report['checks']] == ['Configuration']
    assert 'Traceback' not in result.stderr


def test_no_envelope_does_not_waive_known_live_manifest_mismatch(proposal_case):
    _, export = proposal_case
    with driver.session() as session:
        session.run("MATCH (m:GraphMeta {id:'singleton'}) SET m.governance_digest=$d", d='a' * 64)
    with pytest.raises(StateMismatchError, match='digest mismatch'):
        run_propose(*export(None), unverified_provenance=True, revalidate_current=True)


@pytest.mark.parametrize('value', ['invalid-digest', 'a' * 63, 'A' * 64])
def test_invalid_live_digest_cannot_be_bypassed(proposal_case, value):
    _, export = proposal_case
    with driver.session() as session:
        session.run("MATCH (m:GraphMeta {id:'singleton'}) SET m.governance_digest=$d", d=value)
    with pytest.raises(ProvenanceError, match='live governance_digest'):
        run_propose(*export(), unverified_provenance=True, revalidate_current=True)


def test_current_expected_exception_remains_deny_governed(proposal_case):
    envelope, export = proposal_case
    with driver.session() as session:
        session.run("CREATE (:ExpectedException {key:'probe', agent:'bot', "
                    "tool_key:'sample::denied', policy:'deny', reason:'operator exception'})")
    proposal, _ = run_propose(*export({**envelope, 'profile': 'explore'}, ('denied',)), revalidate_current=True)
    assert not proposal['additions']
    assert proposal['conflicts'][0]['reason'] == 'DENY_GOVERNED'


def test_revalidation_churn_exhausts_five_complete_attempts(proposal_case, monkeypatch):
    _, export = proposal_case
    original = selector.rank_features
    attempts = []

    def unstable(agent, candidates):
        ranked = original(agent, candidates)
        attempts.append(agent)
        with driver.session() as session:
            session.run("MATCH (m:GraphMeta {id:'singleton'}) SET m.generation=m.generation+1")
        return ranked

    monkeypatch.setattr(selector, 'rank_features', unstable)
    monkeypatch.setattr(queries, '_bracket_backoff', lambda _: None)
    from toolgraph.policy_propose import StateChurnError
    with pytest.raises(StateChurnError, match='exhausted 5 read attempts'):
        run_propose(*export(), revalidate_current=True)
    assert len(attempts) == 5


def test_unverified_flag_alone_does_not_waive_generation_change(proposal_case):
    envelope, export = proposal_case
    env = dict(envelope)
    env.pop('graph_instance_id')
    env['graph_generation'] -= 1
    args = export(env)
    with pytest.raises(StateMismatchError):
        run_propose(*args, unverified_provenance=True)
    with pytest.raises(ProvenanceError):
        run_propose(*args, revalidate_current=True)
    proposal, rendered = run_propose(*args, unverified_provenance=True, revalidate_current=True)
    assert proposal['provenance']['status'] == 'unverified'
    assert proposal['provenance']['current_revalidated'] is True
    assert 'UNVERIFIED PROVENANCE' in rendered


def test_doctor_redacts_configuration_details_before_early_return(monkeypatch):
    monkeypatch.setattr(config, 'check_config_health', lambda: {
        'path': 'probe.json', 'exists': True, 'valid_json': False,
        'permissions_ok': True, 'permissions': '0o600', 'healthy': False,
        'detail': 'could not load bolt://user:synthetic-secret@localhost:7687',
    })
    with patch.object(driver, 'verify_connectivity') as connect:
        report = run_doctor()
    connect.assert_not_called()
    assert not report.healthy
    assert 'synthetic-secret' not in report.summary_text()
    assert 'synthetic-secret' not in json.dumps(report.to_dict())
