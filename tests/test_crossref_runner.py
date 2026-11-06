"""New modes and cache-aware orchestration, fake DB/client only."""
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock
import json

import pytest
import requests
import sqlalchemy as sa

from src.journal_index import run_journal_index as runner
from src.journal_index import crossref_pipeline as pipeline
from src.journal_index import crossref_store as store
from src.journal_index.crossref import CrossrefResult
from test_crossref_persistence import article, decision, NOW
from test_crossref_scimago import master


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def forbidden(*a, **k):
        raise AssertionError('No live I/O')
    monkeypatch.setattr(requests.Session, 'request', forbidden)
    monkeypatch.setattr(sa.engine.Engine, 'connect', forbidden)
    monkeypatch.setattr(sa.engine.Engine, 'raw_connection', forbidden)


def options(**changes):
    values = dict(enrich_crossref=False, crossref_scimago=True, refresh_crossref=False,
        save_crossref=False, save_scimago=False, crossref_source='auto', crossref_limit=3,
        crossref_delay=1, row_limit=100, crossref_ttl=[], cache_days=30)
    values.update(changes)
    return SimpleNamespace(**values)


@pytest.fixture
def setup(monkeypatch):
    engine = MagicMock()
    monkeypatch.setattr(pipeline, 'read_publications', lambda *a, **k: [article()])
    cache = store.snapshot(article(), decision())
    monkeypatch.setattr(store, 'read_cache', lambda *a: {1: cache})
    client = MagicMock()
    client.return_value.search_bibliographic.return_value = CrossrefResult(status='ambiguous',
        lookup_method='bibliographic_search', fetched_at=NOW, reason='fixture', candidates=decision().candidates)
    monkeypatch.setattr(pipeline, 'CrossrefClient', client)
    save = MagicMock(return_value=True)
    monkeypatch.setattr(store, 'save_enrichment', save)
    monkeypatch.setattr(store, 'save_integration_audit', MagicMock(return_value=True))
    return engine, client, save, cache


def test_cache_hit_no_http_no_writes(setup, capsys):
    engine, client, save, _ = setup
    assert pipeline.run(engine, options(), 'test', master(), now=NOW) == 0
    output = json.loads(capsys.readouterr().out)
    assert output['cache_status'] == 'hit' and output['crossref_status'] == 'matched'
    assert output['scimago_status'] == 'indexed' and output['matching_method'] == 'title_exact'
    client.assert_not_called()
    save.assert_not_called()
    engine.begin.assert_not_called()


def test_expired_cache_only_does_not_use_stale_identity(setup, capsys):
    setup[3]['expires_at'] = NOW
    assert pipeline.run(setup[0], options(), 'test', master(), now=NOW) == 0
    output = json.loads(capsys.readouterr().out)
    assert output['cache_status'] == 'expired' and output['crossref_status'] == 'not_checked'
    assert output['scimago_status'] == 'unverified' and output['quartiles'] == []
    setup[1].assert_not_called()


def test_expired_enrich_requests_and_explicit_save(setup, capsys):
    setup[3]['expires_at'] = NOW
    args = options(enrich_crossref=True, crossref_scimago=False, save_crossref=True)
    assert pipeline.run(setup[0], args, 'test', now=NOW) == 0
    setup[1].return_value.search_bibliographic.assert_called_once()
    setup[1].return_value.close.assert_called_once()
    setup[2].assert_called_once()
    output = json.loads(capsys.readouterr().out)
    assert output['crossref_saved'] is True and output['scimago_status'] == 'not_requested'


def test_cache_failure_prevents_http(setup, monkeypatch):
    monkeypatch.setattr(store, 'read_cache', MagicMock(side_effect=RuntimeError('db')))
    with pytest.raises(RuntimeError):
        pipeline.run(setup[0], options(enrich_crossref=True), 'test', master(), now=NOW)
    setup[1].assert_not_called()


def test_scimago_save_requires_verified_master_and_explicit_flag(setup, monkeypatch):
    save = MagicMock(return_value='saved')
    monkeypatch.setattr(pipeline, 'save_master_verification', save)
    assert pipeline.run(setup[0], options(save_scimago=True), 'test', master(), now=NOW) == 0
    save.assert_called_once()
    setup[2].assert_not_called()


@pytest.mark.parametrize('status', ['error', 'ambiguous', 'not_found'])
def test_nonmatched_cache_never_saves_scimago(status, setup, monkeypatch, capsys):
    setup[3].update(store.snapshot(article(), decision(status)))
    save = MagicMock()
    monkeypatch.setattr(pipeline, 'save_master_verification', save)
    code = pipeline.run(setup[0], options(save_scimago=True), 'test', master(), now=NOW)
    assert code == (1 if status == 'error' else 0)
    save.assert_not_called()
    output = json.loads(capsys.readouterr().out)
    assert output['crossref_status'] == status and output['scimago_status'] == 'unverified'


@pytest.mark.parametrize('args', [
    ['--check-crossref', '--save-crossref'], ['--dry-run', '--save-crossref'],
    ['--dry-run', '--crossref-scimago'], ['--crossref-scimago', '--save'],
    ['--enrich-crossref', '--save-scimago'],
    ['--check-crossref', '--refresh-crossref'], ['--save-scimago'],
    ['--enrich-crossref', '--crossref-ttl', 'unknown=1'],
    ['--enrich-crossref', '--crossref-ttl', 'matched=nan'],
    ['--crossref-scimago', '--refresh-crossref', '--save-scimago'],
    ['--fill-raw-crossref'], ['--fill-raw-crossref', '--crossref-scimago'],
    ['--scholar-id', 'X'], ['--scholar-id', '   '],
])
def test_cli_rejects_dangerous_combinations_before_db(args, monkeypatch):
    import src.loading.postgres as postgres
    build = MagicMock(side_effect=AssertionError('No DB'))
    monkeypatch.setattr(postgres, 'build_engine', build)
    with pytest.raises(SystemExit) as exc:
        runner.main(args)
    assert exc.value.code == 2
    build.assert_not_called()


@pytest.mark.parametrize('mode', ['--enrich-crossref', '--crossref-scimago'])
def test_cli_dispatch_and_no_ddl(mode, monkeypatch):
    import src.loading.postgres as postgres
    engine = MagicMock()
    engine.url.database = 'publication_dashboard'
    monkeypatch.setenv('POSTGRES_DB', 'publication_dashboard')
    monkeypatch.setattr(postgres, 'build_engine', lambda: engine)
    execute = MagicMock(return_value=0)
    monkeypatch.setattr(pipeline, 'run', execute)
    monkeypatch.setattr(runner.scimagojr, 'load_master', lambda *a: master())
    monkeypatch.setattr(runner, 'create_table', MagicMock(side_effect=AssertionError('No DDL')))
    assert runner.main([mode]) == 0
    execute.assert_called_once()
    engine.dispose.assert_called_once()


def test_unsaved_refresh_cannot_create_index_provenance(setup, monkeypatch, capsys):
    setup[3]['expires_at'] = NOW
    setup[2].return_value = False
    save = MagicMock()
    monkeypatch.setattr(pipeline, 'save_master_verification', save)
    args = options(refresh_crossref=True, save_crossref=True, save_scimago=True)
    assert pipeline.run(setup[0], args, 'test', master(), now=NOW) == 1
    save.assert_not_called()
    assert json.loads(capsys.readouterr().out)['cache_status'] == 'protected_unusable'


def test_cache_only_can_explicitly_save_integration_audit_without_http(setup, capsys):
    assert pipeline.run(setup[0], options(save_crossref=True), 'test', master(), now=NOW) == 0
    report = json.loads(capsys.readouterr().out)
    assert report['integration_audit_saved'] is True
    setup[1].assert_not_called()
    setup[2].assert_not_called()


def test_clock_rechecked_for_each_article(setup, monkeypatch, capsys):
    row1, row2 = article(), dict(article(), raw_publication_id=2, detail_title='Another scientific metadata publication')
    time = [NOW]
    caches = {r['raw_publication_id']: store.snapshot(r, decision(), {'matched': 1}) for r in (row1, row2)}
    monkeypatch.setattr(pipeline, 'read_publications', lambda *a, **k: [row1, row2])
    monkeypatch.setattr(store, 'read_cache', lambda *a: caches)
    original = pipeline.match_serial
    def check(*args):
        answer = original(*args)
        time[0] += timedelta(seconds=2)
        return answer
    monkeypatch.setattr(pipeline, 'match_serial', check)
    pipeline.run(setup[0], options(), 'test', master(), clock=lambda: time[0])
    reports = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert reports[0]['cache_status'] == 'expired_during_processing'
    assert reports[0]['quartiles'] == []
    assert reports[1]['cache_status'] == 'expired'
    setup[1].assert_not_called()


def test_conflict_recovery_uses_fresh_clock(setup, monkeypatch, capsys):
    time = [NOW]
    setup[3]['expires_at'] = NOW
    def rejected(*args):
        setup[3]['expires_at'] = NOW + timedelta(seconds=1)
        time[0] = NOW + timedelta(seconds=2)
        return False
    monkeypatch.setattr(store, 'save_enrichment', rejected)
    assert pipeline.run(setup[0], options(enrich_crossref=True, save_crossref=True),
        'test', master(), clock=lambda: time[0]) == 1
    assert json.loads(capsys.readouterr().out)['cache_status'] == 'protected_unusable'


def test_initial_cache_hit_with_injected_clock(setup, capsys):
    assert pipeline.run(setup[0], options(), 'test', master(), clock=lambda: NOW) == 0
    assert json.loads(capsys.readouterr().out)['cache_status'] == 'hit'
    setup[1].assert_not_called()


# --------------------------------------------------------------------------- #
# Filter satu profil (--scholar-id) dan pengisian RAW opsional.                #
# --------------------------------------------------------------------------- #

def test_scholar_id_forwarded_to_read_publications(setup, monkeypatch, capsys):
    captured = {}
    def fake_read(engine, row_limit, target, scholar_id=None):
        captured['scholar_id'] = scholar_id
        return [article()]
    monkeypatch.setattr(pipeline, 'read_publications', fake_read)
    pipeline.run(setup[0], options(scholar_id='ID_P'), 'test', master(), now=NOW)
    assert captured['scholar_id'] == 'ID_P'
    captured.clear()
    pipeline.run(setup[0], options(), 'test', master(), now=NOW)
    assert captured['scholar_id'] is None


def test_fill_raw_runs_for_validated_matched_cache_hit(setup, monkeypatch, capsys):
    fill = MagicMock(return_value={'detail_doi': '10.1234/test'})
    monkeypatch.setattr(store, 'fill_raw_from_matched', fill)
    assert pipeline.run(setup[0], options(fill_raw_crossref=True), 'test', master(), now=NOW) == 0
    fill.assert_called_once()
    output = json.loads(capsys.readouterr().out)
    assert output['raw_fill'] == {'detail_doi': '10.1234/test'}


def test_fill_raw_skipped_for_nonmatched_and_without_flag(setup, monkeypatch, capsys):
    fill = MagicMock()
    monkeypatch.setattr(store, 'fill_raw_from_matched', fill)
    setup[3].update(store.snapshot(article(), decision('ambiguous')))
    pipeline.run(setup[0], options(fill_raw_crossref=True), 'test', master(), now=NOW)
    fill.assert_not_called()
    assert json.loads(capsys.readouterr().out)['raw_fill'] is None
    setup[3].update(store.snapshot(article(), decision()))
    pipeline.run(setup[0], options(), 'test', master(), now=NOW)
    fill.assert_not_called()


def test_fill_raw_skipped_when_cache_expired(setup, monkeypatch):
    fill = MagicMock()
    monkeypatch.setattr(store, 'fill_raw_from_matched', fill)
    setup[3]['expires_at'] = NOW
    pipeline.run(setup[0], options(fill_raw_crossref=True), 'test', master(), now=NOW)
    fill.assert_not_called()
