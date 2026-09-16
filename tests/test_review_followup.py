"""Regressions for the independent 2026-09-16 delivery review."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import threading

from jsonschema import Draft202012Validator, ValidationError
import pytest
from typer.testing import CliRunner

from toolgraph import cli, config, control_plan
from toolgraph.graph import driver, loader, queries, schema
from toolgraph.manifest.ingest import dry_run_ingest, ingest_governance
from toolgraph.models import AccessGrant, CrawlResult, Governance, ToolRecord

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def embedded_path(tmp_path, monkeypatch):
    path = tmp_path / 'missing-parent' / 'probe.lbug'
    monkeypatch.setattr(config, 'settings', config.Settings(backend='ladybug', db_path=path))
    monkeypatch.setenv('TOOLGRAPH_CONFIG', str(tmp_path / 'no-config.json'))
    driver.close_driver()
    yield path
    driver.close_driver()


@pytest.mark.parametrize('json_output', [False, True])
def test_doctor_missing_ladybug_does_not_create_files(embedded_path, json_output):
    result = CliRunner().invoke(cli.app, ['doctor'] + (['--json'] if json_output else []))
    assert result.exit_code == 1, result.output
    assert not embedded_path.parent.exists()
    assert 'does not exist' in result.stdout
    assert 'Traceback' not in result.output
    if json_output:
        report = json.loads(result.stdout)
        assert not report['healthy']
        assert next(c for c in report['checks'] if c['name'] == 'Connectivity')['status'] == 'FAIL'


@pytest.mark.parametrize('json_output', [False, True])
def test_uninitialized_ladybug_dry_run_is_controlled_and_unchanged(embedded_path, tmp_path, json_output):
    import ladybug
    embedded_path.parent.mkdir()
    ladybug.Database(embedded_path).close()
    before = hashlib.sha256(embedded_path.read_bytes()).hexdigest()
    gov = tmp_path / 'gov.yaml'
    gov.write_text('agents: [bot]\n')
    result = CliRunner().invoke(cli.app, ['ingest-manifest', '--governance', str(gov), '--dry-run'] + (['--json'] if json_output else []))
    assert result.exit_code == 1, result.output
    assert 'init-schema' in result.stderr
    assert 'Traceback' not in result.output
    assert driver._ladybug_database is None
    assert hashlib.sha256(embedded_path.read_bytes()).hexdigest() == before


def test_owned_readonly_sessions_never_use_writable_acquisition(embedded_path, monkeypatch):
    schema.init_schema()
    assert ingest_governance(Governance(agents=['bot'])).applied
    state = queries.graph_state()
    driver.close_driver()
    before = embedded_path.read_bytes()

    @contextmanager
    def forbidden():
        raise AssertionError('reader reopened a writable database')
        yield  # pragma: no cover

    monkeypatch.setattr(driver, 'session', forbidden)
    monkeypatch.setattr(queries, 'session', forbidden)
    result = CliRunner().invoke(cli.app, ['doctor', '--json'])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)['healthy']
    report = dry_run_ingest(Governance())
    assert report.graph_state == {'instance_id': state.instance_id, 'generation': state.generation}
    assert report.agents_deleted == ['bot']
    assert embedded_path.read_bytes() == before
    assert driver._ladybug_database is None


def test_readonly_native_open_cannot_recreate_a_disappearing_file(embedded_path, monkeypatch):
    import ladybug
    schema.init_schema()
    driver.close_driver()
    original = ladybug.Database

    def disappear(path, **kwargs):
        assert kwargs['read_only'] is True
        Path(path).unlink()
        return original(path, **kwargs)

    monkeypatch.setattr(ladybug, 'Database', disappear)
    with pytest.raises(driver.BackendUnavailableError):
        with driver.acquire_readonly_session():
            pytest.fail('must refuse missing database')
    assert not embedded_path.exists()


def test_borrowed_readonly_handle_survives_concurrent_shutdown(embedded_path):
    schema.init_schema()
    attempted = threading.Event()
    completed = threading.Event()

    def close():
        attempted.set()
        driver.close_driver()
        completed.set()

    worker = threading.Thread(target=close, daemon=True)
    try:
        with driver.acquire_readonly_session() as session:
            worker.start()
            assert attempted.wait(2)
            assert not completed.wait(0.05)
            assert session.run('RETURN 1 AS ok').single()['ok'] == 1
            with driver.acquire_readonly_session() as nested:
                assert nested.run('RETURN 1 AS ok').single()['ok'] == 1
        worker.join(2)
        assert completed.is_set()
        assert driver._ladybug_database is None
    finally:
        worker.join(2)


def test_dry_run_json_exposes_full_delta_and_rejection(backend_graph, tmp_path):
    loader.load_crawl_result(CrawlResult(server_name='sample', tools=[ToolRecord(name='read')]))
    assert ingest_governance(Governance(agents=['bot'], grants=[AccessGrant(agent='bot', tool='sample::read')])).applied
    before = queries.graph_state()
    path = tmp_path / 'gov.yaml'
    path.write_text('agents: [replacement]\n')
    runner = CliRunner()
    args = ['ingest-manifest', '--governance', str(path), '--dry-run', '--json']
    result = runner.invoke(cli.app, args)
    assert result.exit_code == 0, result.output
    delta = json.loads(result.stdout)
    assert delta['grants_revoked'] == [{'agent': 'bot', 'tool': 'sample::read'}]
    assert delta['agents_created'] == ['replacement']
    assert delta['agents_deleted'] == ['bot']
    assert queries.graph_state() == before
    path.write_text('agents: [bot]\ngrants:\n  - agent: bot\n    tool: missing::tool\n')
    rejected = runner.invoke(cli.app, args)
    assert rejected.exit_code == 1
    assert json.loads(rejected.stdout)['warnings']
    assert not json.loads(rejected.stdout)['applied']
    assert queries.graph_state() == before


def test_json_options_are_rejected_before_graph_or_file_access(monkeypatch):
    monkeypatch.setattr(cli, 'run_propose', lambda *a, **k: pytest.fail('must reject before graph access'))
    runner = CliRunner()
    result = runner.invoke(cli.app, ['policy', 'propose', 'missing.yaml', 'missing.jsonl', '--json', '--output', 'unused.md'])
    assert result.exit_code == 2
    assert 'cannot be combined' in re.sub(r'\x1b\[[0-9;]*m', '', result.output)
    result = runner.invoke(cli.app, ['ingest-manifest', '--json'])
    assert result.exit_code == 2
    assert 'requires --dry-run' in re.sub(r'\x1b\[[0-9;]*m', '', result.output)


def test_proposal_file_write_failure_has_redacted_error(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, 'run_propose', lambda *a, **k: ({}, 'proposal'))
    result = CliRunner().invoke(cli.app, ['policy', 'propose', 'gov.yaml', 'obs.jsonl', '-o', str(tmp_path)])
    assert result.exit_code == 1
    assert 'ERROR:' in result.stderr
    assert not isinstance(result.exception, OSError)
    assert 'Traceback' not in result.output


def test_dry_run_json_scrubs_uri_lists_nested_evidence_and_warnings(monkeypatch, tmp_path):
    from toolgraph.manifest.ingest import GovernanceDiffReport

    uri = 'https://user:password@host/path?token=secret'
    report = GovernanceDiffReport(
        applied=False, resources_pruned=[uri], resources_retained_crawled=[uri],
        data_access_removed=[{'resource': uri}],
        expected_exceptions_removed=[{'reason': f'see {uri}'}],
        warnings=[f'invalid {uri}'],
    )
    monkeypatch.setattr(cli, 'dry_run_ingest', lambda *a, **k: report)
    path = tmp_path / 'governance.yaml'
    path.write_text('agents: [bot]\n')
    result = CliRunner().invoke(cli.app, ['ingest-manifest', '--governance', str(path), '--dry-run', '--json'])
    assert result.exit_code == 1
    rendered = json.loads(result.stdout)
    assert rendered['resources_pruned'] == ['https://host/path']
    assert rendered['expected_exceptions_removed'] == [{'reason': 'see https://host/path'}]
    assert 'password' not in result.stdout and 'secret' not in result.stdout
    assert report.resources_pruned == [uri]  # rendering does not change the delta


@pytest.mark.parametrize('backend', ['neo4j', 'ladybug'])
def test_catalog_failure_is_not_reported_as_missing_schema(monkeypatch, backend):
    monkeypatch.setattr(schema, 'backend_name', lambda: backend)
    monkeypatch.setattr(schema, '_stored_backend_schema_version', lambda s: schema.BACKEND_SCHEMA_VERSION)

    class DeniedCatalog:
        def run(self, *_args):
            raise RuntimeError('permission denied')

    with pytest.raises(driver.BackendConfigurationError, match='catalog read permissions') as error:
        schema.check_backend_schema_version(DeniedCatalog())
    assert 'init-schema' not in str(error.value)


@pytest.mark.parametrize('native', [False, True])
def test_catalog_outage_preserves_backend_unavailable(monkeypatch, native):
    from neo4j.exceptions import ServiceUnavailable

    monkeypatch.setattr(schema, 'backend_name', lambda: 'neo4j')
    monkeypatch.setattr(schema, '_stored_backend_schema_version', lambda s: schema.BACKEND_SCHEMA_VERSION)

    class OfflineCatalog:
        def run(self, *_args):
            raise (ServiceUnavailable if native else driver.BackendUnavailableError)('offline')

    with pytest.raises(driver.BackendUnavailableError):
        schema.check_backend_schema_version(OfflineCatalog())


@pytest.mark.parametrize('dry_run', [False, True])
def test_manifest_text_warnings_are_redacted(monkeypatch, tmp_path, dry_run):
    from toolgraph.manifest.ingest import GovernanceDiffReport

    report = GovernanceDiffReport(applied=False, warnings=['resource https://user:password@host/x?token=secret'])
    monkeypatch.setattr(cli, 'dry_run_ingest' if dry_run else 'ingest_governance', lambda *a, **k: report)
    monkeypatch.setattr(cli.schema, 'init_schema', lambda: None)
    path = tmp_path / 'governance.yaml'
    path.write_text('agents: [bot]\n')
    result = CliRunner().invoke(cli.app, ['ingest-manifest', '--governance', str(path)] + (['--dry-run'] if dry_run else []))
    assert result.exit_code == 1
    assert 'password' not in result.output and 'secret' not in result.output
    assert 'https://host' in result.output


def test_corrupt_ladybug_dry_run_is_controlled(embedded_path, tmp_path):
    embedded_path.parent.mkdir()
    embedded_path.write_bytes(b'not a database')
    path = tmp_path / 'governance.yaml'
    path.write_text('agents: [bot]\n')
    result = CliRunner().invoke(cli.app, ['ingest-manifest', '--governance', str(path), '--dry-run'])
    assert result.exit_code == 1
    assert 'file integrity and format' in result.stderr
    assert 'Traceback' not in result.output
    assert embedded_path.read_bytes() == b'not a database'


def _chain(count, *, writers=True, all_readers=False):
    nodes = [{'id': f'n{i}', 'kind': 'start' if i == 0 else 'end' if i == count-1 else 'join',
              'principals': [], 'agent_call': False} for i in range(count)]
    for i in (range(1, count-1) if all_readers else [1]):
        nodes[i]['principals'] = ['reader']
    if writers:
        nodes[-2]['principals'] = ['writer']
    raw = json.dumps({
        'schema_version': 1, 'kind': 'toolgraph.control-plan', 'run_id': 'bounded',
        'producer': {'name': 'test', 'version': '1'},
        'principals': [{'id': 'reader', 'candidate_tools': ['t::read']}, {'id': 'writer', 'candidate_tools': ['t::write']}],
        'nodes': nodes,
        'edges': [{'from': f'n{i}', 'to': f'n{i+1}', 'kind': 'always', 'decided_by': 'code'} for i in range(count-1)],
        'limits': {'maximum_agent_calls': 1, 'maximum_parallelism': 1, 'maximum_cycles': 0},
    }).encode()
    return raw, control_plan.load_control_plan(raw)


EVALUATIONS = [{'principal': 'reader', 'eligible': ['t::read']}, {'principal': 'writer', 'eligible': ['t::write']}]


class ExposureSession:
    def __init__(self, reads=('file:///input',), writes=('file:///output',)):
        self.rows = [{'tool_key': 't::read', 'mode': 'READS', 'resource_uri': uri} for uri in reads]
        self.rows += [{'tool_key': 't::write', 'mode': 'WRITES', 'resource_uri': uri} for uri in writes]

    def run(self, *_args, **_kwargs):
        return self.rows


def test_golden_exposure_fixture_matches_actual_producer(monkeypatch):
    from tests.test_control_plan import _bracket

    _bracket(monkeypatch)
    fixture_dir = ROOT / 'contracts/fixtures'
    expected = json.loads((fixture_dir / 'control-preflight-exposure-v1.json').read_text())
    facts = ExposureSession()
    facts.rows = [
        {'tool_key': 'workspace::read', 'mode': 'READS', 'resource_uri': 'file:///workspace/input'},
        {'tool_key': 'workspace::write', 'mode': 'WRITES', 'resource_uri': 'file:///workspace/output'},
    ]

    @contextmanager
    def session():
        yield facts

    actual = control_plan.build_control_preflight(
        (fixture_dir / 'control-plan-exposure-v1.json').read_bytes(), profile='review',
        created_at=datetime.fromisoformat(expected['created_at']), session_factory=session,
    )
    assert actual == expected


def test_real_4096_node_reader_only_plan_skips_forward_search(monkeypatch):
    _, plan = _chain(4096, writers=False, all_readers=True)
    assert not control_plan.lint_control_plan(plan)
    monkeypatch.setattr(control_plan, 'deque', lambda *a: pytest.fail('no writer requires no traversal'))
    result = control_plan.compute_control_plan_exposures(plan, EVALUATIONS, ExposureSession(writes=()))
    assert result == {'exposures': [], 'truncated': False, 'total_candidate_pairs_found': 0}


def test_real_4096_node_witness_is_bounded():
    _, plan = _chain(4096)
    assert not control_plan.lint_control_plan(plan)
    result = control_plan.compute_control_plan_exposures(plan, EVALUATIONS, ExposureSession())
    assert not result['truncated']
    assert len(result['exposures']) == 1
    witness = result['exposures'][0]['witness_path']
    assert len(witness) == 32
    assert witness[0] == 'n1' and witness[-1] == 'n4094'
    assert '4063 intermediate nodes' in witness[16]


def test_traversal_cutoff_warns_even_without_emitted_pairs(monkeypatch):
    raw, _ = _chain(30)
    monkeypatch.setattr(control_plan, 'MAX_EXPOSURE_TRAVERSAL_STEPS', 10)
    monkeypatch.setattr(control_plan.selector, 'eligible_tools', lambda principal, candidates, **kw: {
        'agent_found': True, 'eligible': candidates, 'rejected': [],
    })
    monkeypatch.setattr(control_plan.queries, 'with_graph_state', lambda fetch, **kw: {
        **fetch(), 'graph_state': {'instance_id': 'test', 'generation': 1},
    })
    @contextmanager
    def session():
        yield ExposureSession()
    artifact = control_plan.build_control_preflight(raw, session_factory=session)
    assert artifact['potential_exposures'] == []
    assert artifact['truncated'] is True
    assert artifact['total_candidate_pairs_found'] == 0
    assert artifact['decision'] == 'advisory_warn'
    validator = Draft202012Validator(json.loads((ROOT / 'contracts/control-preflight.schema.json').read_text()))
    validator.validate(artifact)
    # Producer behavior is stricter than the backward-compatible v1 schema.
    artifact['decision'] = 'advisory_allow'
    validator.validate(artifact)


def test_sanitized_array_byte_budget_and_lower_bound(monkeypatch):
    _, plan = _chain(5)
    session = ExposureSession(reads=('https://user:secret@host/input?token=' + 'x'*70_000,))
    complete = control_plan.compute_control_plan_exposures(plan, EVALUATIONS, session)
    assert len(complete['exposures']) == 1 and not complete['truncated']
    assert complete['exposures'][0]['read_resource'] == 'https://host/input'
    exact_size = len(json.dumps(complete['exposures']).encode())
    monkeypatch.setattr(control_plan, 'MAX_AGGREGATE_EXPOSURE_BYTES', exact_size)
    assert control_plan.compute_control_plan_exposures(plan, EVALUATIONS, session) == complete
    session = ExposureSession(writes=('file:///output', 'file:///second'))
    bounded = control_plan.compute_control_plan_exposures(plan, EVALUATIONS, session)
    assert bounded['truncated']
    assert len(json.dumps(bounded['exposures']).encode()) <= exact_size
    assert bounded['total_candidate_pairs_found'] > len(bounded['exposures'])


def test_record_limit_counts_discovered_not_all_pairs(monkeypatch):
    _, plan = _chain(5)
    monkeypatch.setattr(control_plan, 'MAX_EXPOSURE_RECORDS', 2)
    result = control_plan.compute_control_plan_exposures(plan, EVALUATIONS, ExposureSession(writes=tuple(f'file:///{i}' for i in range(20))))
    assert len(result['exposures']) == 2
    assert result['truncated']
    assert result['total_candidate_pairs_found'] == 3  # discovered lower bound, not 20


@pytest.mark.parametrize('change', ['oversized_path', 'oversized_list', 'negative_count', 'missing_count'])
def test_exposure_contract_rejects_invalid_results(change):
    result = json.loads((ROOT / 'contracts/fixtures/control-preflight-exposure-v1.json').read_text())
    if change == 'oversized_path':
        result['potential_exposures'][0]['witness_path'] = ['node'] * 33
    elif change == 'oversized_list':
        result['potential_exposures'] *= 101
    elif change == 'negative_count':
        result['total_candidate_pairs_found'] = -1
    else:
        result['truncated'] = True
    validator = Draft202012Validator(json.loads((ROOT / 'contracts/control-preflight.schema.json').read_text()))
    with pytest.raises(ValidationError):
        validator.validate(result)
