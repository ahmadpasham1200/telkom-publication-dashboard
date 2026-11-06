"""Offline journal index contract tests: no credentials, database or HTTP required."""
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from sqlalchemy.dialects import postgresql

from src.journal_index import scopus, scimagojr, sinta
from src.journal_index.init import (candidates, classify, normalize_title, matches,
                                    issns, result)
from src.journal_index import run_journal_index as runner
from test_scopus_master import write_master
from test_scimago_master import row as scimago_row, write_csv as write_scimago_csv


@pytest.fixture(autouse=True)
def offline_journal_index_tests(monkeypatch):
    """Never load local secrets or allow live I/O, even if a mock is missed."""
    import dotenv
    import sqlalchemy as sa
    monkeypatch.setattr(dotenv, 'load_dotenv', lambda *a, **k: False)
    for name in ('SCOPUS_API_KEY', 'SCOPUS_INST_TOKEN', 'SINTA_USERNAME',
                 'SINTA_PASSWORD', 'SCIMAGO_EVIDENCE_FILE', 'SINTA_EVIDENCE_FILE'):
        monkeypatch.setenv(name, '')

    def forbidden(*args, **kwargs):
        raise AssertionError('Live database/HTTP access is forbidden in unit tests')
    monkeypatch.setattr(sa.engine.Engine, 'connect', forbidden)
    monkeypatch.setattr(sa.engine.Engine, 'raw_connection', forbidden)


def journal(name='Journal of Testing', **fields):
    return candidates([{'raw_publication_id': 1, 'detail_venue': name,
                        'publication_year': 2024, **fields}])[0]


def test_normalization_dedup_and_provenance():
    assert normalize_title(' Ｊournal — OF  Testing!') == 'journal of testing'
    rows = [{'detail_venue': 'Journal of Testing', 'venue': 'ignored', 'raw_publication_id': 1},
            {'detail_venue': ' ', 'venue': 'JOURNAL OF TESTING', 'raw_publication_id': 2}]
    selected = candidates(rows)
    assert len(selected) == 1
    assert len(selected[0].publications) == 2
    assert selected[0].original == 'Journal of Testing'
    assert len(candidates(rows, 1)) == 1


@pytest.mark.parametrize('title,kind', [
    ('Proceedings of Journal Conference', 'non_journal'),
    ('Telkom University', 'non_journal'), ('Elsevier Press', 'non_journal'),
    ('Nature', 'ambiguous'), ('Journal of Science', 'journal')])
def test_classification(title, kind):
    assert classify(title)[0] == kind


def test_matching_and_issn():
    candidate = journal()
    assert matches(candidate, 'JOURNAL OF TESTING!')
    assert not matches(candidate, 'Journal of Tests')
    assert issns('0378-5955 / 1234-5678') == {'0378-5955'}
    candidate.identifiers = {'0378-5955'}
    assert matches(candidate, 'Renamed journal', ['03785955'])
    assert not matches(candidate, candidate.original, ['0309-0566'])


def test_scopus_positive_and_no_inference(tmp_path):
    master = scopus.load_master(write_master(tmp_path))
    checked = master.check(journal())
    assert checked['status'] == 'indexed'
    assert checked['metadata']['match_method'] == 'title_exact'
    assert checked['metadata']['identity_match'] is True
    assert result('scopus', 'not_found')['indexed'] is None
    assert result('scopus', 'not_indexed')['indexed'] is False


def test_missing_credentials(tmp_path, monkeypatch):
    monkeypatch.delenv('SCOPUS_API_KEY', raising=False)
    monkeypatch.delenv('SINTA_USERNAME', raising=False)
    monkeypatch.delenv('SINTA_PASSWORD', raising=False)
    assert scopus.check(journal(), master_path=tmp_path / 'missing.xlsx')['status'] == 'error'
    assert sinta.check(journal())['status'] == 'access_unavailable'


def test_manual_quartiles_and_ranks(tmp_path, monkeypatch):
    path = tmp_path / 'evidence.json'
    evidence = {'title': 'Journal of Testing',
                'evidence_url': 'https://www.scimagojr.com/journalsearch.php?q=123&tip=sid',
                'sjr': [{'quartile': 'Q1', 'year': 2024, 'category': 'A'},
                        {'quartile': 'Q3', 'year': 2024, 'category': 'B'},
                        {'quartile': 'S1', 'year': 2024}]}
    path.write_text(json.dumps([evidence]))
    assert len(scimagojr.check(journal(), path)['sjr']) == 2
    assert scimagojr.check(journal('Other Journal'), path)['status'] == 'not_found'
    evidence['evidence_url'] = 'https://evil.example/journal'
    path.write_text(json.dumps([evidence]))
    assert scimagojr.check(journal(), path)['status'] == 'unverified'
    monkeypatch.setenv('SINTA_USERNAME', 'local-user')
    monkeypatch.setenv('SINTA_PASSWORD', 'secret-sentinel')
    evidence.update(evidence_url='https://sinta.kemdiktisaintek.go.id/journals/profile/1',
                    sinta_rank='SINTA 2', sinta_period='2022–2026')
    path.write_text(json.dumps([evidence]))
    checked = sinta.check(journal(), path)
    assert checked['sinta_rank'] == 'S2'
    assert checked['sinta_period'] == '2022–2026'
    assert 'secret-sentinel' not in str(checked)
    assert sinta.parse_rank('Q1') is None
    assert sinta.parse_rank('S7') is None


def test_upsert_and_create_table():
    engine = MagicMock()
    runner.create_table(engine)
    ddl = engine.begin.return_value.__enter__.return_value.execute.call_args.args[0]
    assert 'CREATE TABLE IF NOT EXISTS raw.journal_index_verification' in str(ddl.compile(dialect=postgresql.dialect()))
    runner.save_result(engine, journal(), result('scopus', 'unverified'))
    stmt = engine.begin.return_value.__enter__.return_value.execute.call_args.args[0]
    sql = str(stmt.compile(dialect=postgresql.dialect()))
    assert 'ON CONFLICT ON CONSTRAINT uq_journal_verification_source DO UPDATE' in sql
    assert 'raw.scholar_publication ' not in sql


def test_sources_and_writes_are_isolated(monkeypatch, caplog):
    def fail(candidate):
        raise RuntimeError('secret-sentinel')
    monkeypatch.setattr(runner, 'SOURCES', {'scopus': fail, 'sinta': lambda c: result('sinta', 'access_unavailable')})
    save = MagicMock(side_effect=[RuntimeError('secret-sentinel'), None])
    monkeypatch.setattr(runner, 'save_result', save)
    counts, errors = runner.run_checks(None, [journal()], ['scopus', 'sinta'], delay=0)
    assert counts == {'error': 1, 'access_unavailable': 1}
    assert errors == 1
    assert save.call_count == 2
    assert 'secret-sentinel' not in caplog.text


def test_help_does_not_connect_or_authenticate(monkeypatch):
    import src.loading.postgres as postgres
    build = MagicMock(side_effect=AssertionError)
    monkeypatch.setattr(postgres, 'build_engine', build)
    with pytest.raises(SystemExit) as exc:
        runner.main(['--help'])
    assert exc.value.code == 0
    build.assert_not_called()


@pytest.mark.parametrize('flag,reader,saver,source', [
    ('--scimago', 'read_candidates', 'save_result', 'scimagojr'),
    ('--scopus', 'read_scopus_candidates', 'save_scopus_results', 'scopus'),
])
def test_automatic_modes_read_all_and_save_per_unique_journal(
        monkeypatch, capsys, flag, reader, saver, source):
    import src.loading.postgres as postgres
    engine = MagicMock()
    engine.url.database = 'publication_dashboard'
    engine.connect.return_value.__enter__.return_value.execute.return_value.scalar_one.return_value = 'publication_dashboard'
    monkeypatch.setenv('POSTGRES_DB', 'publication_dashboard')
    monkeypatch.setattr(postgres, 'build_engine', lambda: engine)
    item = journal()
    item.publications.append(dict(item.publications[0], raw_publication_id=2, scholar_id='different'))
    selected = [item]
    read = MagicMock(return_value=selected)
    monkeypatch.setattr(runner, reader, read)
    checker = MagicMock(return_value=result(source, 'indexed', metadata={
        'method': 'scimago_master_csv', 'identity_match': True, 'source_id': '1',
        'reference_year': 2025, 'match_method': 'title_exact'}))
    master = SimpleNamespace(path='fixture-master', records=[], check=checker)
    if source == 'scimagojr':
        monkeypatch.setattr(runner.scimagojr, 'load_master', lambda *a: master)
        monkeypatch.setattr(runner, 'create_table', MagicMock())
    else:
        monkeypatch.setattr(runner.scopus, 'load_master', lambda *a: master)
    save = MagicMock(return_value=2)
    monkeypatch.setattr(runner, saver, save)
    assert runner.main([flag]) == 0
    assert read.call_args.args[1:] == (None, None)
    checker.assert_called_once_with(item)
    assert save.call_count == 1
    assert 'Total publikasi RAW diperiksa : 2' in capsys.readouterr().out


def test_auto_modes_are_mutually_exclusive_and_do_not_take_scholar_id():
    with pytest.raises(SystemExit) as exc:
        runner.main(['--scimago', '--scopus'])
    assert exc.value.code == 2
    with pytest.raises(SystemExit) as exc:
        runner.main(['--scimago', '--scholar-id', 'one'])
    assert exc.value.code == 2


def test_dry_run_never_writes_or_checks(monkeypatch, capsys):
    import src.loading.postgres as postgres
    engine = MagicMock()
    engine.url.database = 'test_db'
    engine.connect.return_value.__enter__.return_value.execute.return_value.scalar_one.return_value = 'test_db'
    monkeypatch.setenv('POSTGRES_DB', 'test_db')
    monkeypatch.setattr(postgres, 'build_engine', lambda: engine)
    monkeypatch.setattr(runner, 'read_candidates', lambda *a: [
        journal(), journal('IEEE Access'), journal('Telkom University')])
    ddl = MagicMock(side_effect=AssertionError)
    checks = MagicMock(side_effect=AssertionError)
    monkeypatch.setattr(runner, 'create_table', ddl)
    monkeypatch.setattr(runner, 'run_checks', checks)
    assert runner.main(['--dry-run']) == 0
    ddl.assert_not_called()
    checks.assert_not_called()
    engine.begin.assert_not_called()
    preview = [json.loads(line) for line in capsys.readouterr().out.splitlines()[:-1]]
    assert preview[0]['action'] == 'check_sources'
    assert preview[1]['classification'] == 'ambiguous'
    assert preview[1]['verification_required'] is True
    assert preview[1]['action'] == 'verify_identity'
    assert preview[2]['action'] == 'excluded_non_journal'
    assert preview[2]['sources'] == []


def test_database_mismatch_prevents_reads(monkeypatch, caplog):
    import src.loading.postgres as postgres
    engine = MagicMock()
    engine.url.database = 'wrong'
    monkeypatch.setenv('POSTGRES_DB', 'expected')
    monkeypatch.setattr(postgres, 'build_engine', lambda: engine)
    assert runner.main(['--dry-run']) == 1
    engine.connect.assert_not_called()


@pytest.mark.parametrize('title', ['IEEE Access', 'Nature', 'Unresolved Venue'])
def test_ambiguous_candidate_is_sent_for_identity_verification(monkeypatch, title):
    source = MagicMock(return_value=result('scopus', 'unverified', 'Identity not verified'))
    monkeypatch.setattr(runner, 'SOURCES', {'scopus': source})
    save = MagicMock()
    monkeypatch.setattr(runner, 'save_result', save)
    candidate = journal(title)
    assert candidate.classification == 'ambiguous'
    counts, errors = runner.run_checks(None, [candidate], ['scopus'], delay=0)
    assert counts == {'unverified': 1} and errors == 0
    source.assert_called_once_with(candidate)
    assert save.call_args.args[2]['indexed'] is None


@pytest.mark.parametrize('title', [
    'IEEE International Conference on Computing',
    'Proceedings of Journal Conference', 'Telkom University', 'Elsevier Press'])
@pytest.mark.parametrize('retry_failed', [False, True])
def test_non_journal_is_excluded_entirely(monkeypatch, title, retry_failed):
    sources = {name: MagicMock(side_effect=AssertionError('Excluded venue'))
               for name in ('scopus', 'scimagojr', 'sinta')}
    monkeypatch.setattr(runner, 'SOURCES', sources)
    save = MagicMock()
    monkeypatch.setattr(runner, 'save_result', save)
    engine = MagicMock()
    counts, errors = runner.run_checks(engine, [journal(title)], list(sources),
                                       retry_failed=retry_failed, delay=0)
    assert counts == {} and errors == 0
    for check in sources.values():
        check.assert_not_called()
    save.assert_not_called()
    engine.connect.assert_not_called()


@pytest.mark.parametrize('title', ['IEEE Access', 'Nature'])
def test_no_keyword_identity_matching_uses_source_evidence(tmp_path, monkeypatch, title):
    candidate = journal(title)
    # Synthetic master fixture only; this is not a live index assertion about these titles.
    master = scopus.load_master(write_master(tmp_path, rows={scopus.SHEET_SOURCES: [
        {'Sourcerecord ID': 'test-source', 'Source Title': title, 'ISSN': '0378-5955',
         'EISSN': '', 'Active or Inactive': 'Active', 'Coverage': '2010-2026',
         'Source Type': 'Journal'}]}))
    positive = master.check(candidate)
    assert positive['status'] == 'indexed'
    assert master.check(journal(title + ' Different'))['status'] == 'not_found'
    monkeypatch.setattr(runner, 'SOURCES', {
        'scopus': lambda c: positive,
        'scimagojr': lambda c: result('scimagojr', 'not_found'),
        'sinta': lambda c: result('sinta', 'access_unavailable')})
    save = MagicMock()
    monkeypatch.setattr(runner, 'save_result', save)
    counts, errors = runner.run_checks(None, [candidate], list(runner.SOURCES), delay=0)
    assert counts == {'indexed': 1, 'not_found': 1, 'access_unavailable': 1}
    assert errors == 0
    saved = {call.args[2]['source']: call.args[2] for call in save.call_args_list}
    assert saved['scopus']['status'] == 'indexed'
    assert saved['scimagojr']['indexed'] is None
    assert saved['sinta']['indexed'] is None


def test_all_sources_continue_without_credentials(monkeypatch):
    for key in ('SCOPUS_API_KEY', 'SCIMAGO_EVIDENCE_FILE', 'SINTA_USERNAME', 'SINTA_PASSWORD'):
        monkeypatch.delenv(key, raising=False)
    save = MagicMock()
    monkeypatch.setattr(runner, 'save_result', save)
    def missing_master(*args):
        raise scimagojr.MasterExportError('SCImagoJR 2025 master CSV not found: fixture.csv')
    monkeypatch.setattr(scimagojr, 'load_master', missing_master)
    stub = SimpleNamespace(check=lambda c: result('scopus', 'not_found'))
    monkeypatch.setattr(scopus, 'load_master', lambda *a, **k: stub)
    scopus._MASTER_CACHE.clear()
    counts, errors = runner.run_checks(None, [journal()], list(runner.SOURCES), delay=0)
    assert counts == {'not_found': 1, 'error': 1, 'access_unavailable': 1} and errors == 0
    assert save.call_count == 3


def test_retry_only_existing_failed_results(monkeypatch):
    engine = MagicMock()
    engine.connect.return_value.__enter__.return_value.execute.return_value.scalar_one_or_none.side_effect = [
        'indexed', None, 'error']
    save = MagicMock()
    monkeypatch.setattr(runner, 'save_result', save)
    check = MagicMock(return_value=result('sinta', 'access_unavailable'))
    monkeypatch.setattr(runner, 'SOURCES', {'scopus': check, 'scimagojr': check, 'sinta': check})
    runner.run_checks(engine, [journal()], list(runner.SOURCES), retry_failed=True, delay=0)
    assert check.call_count == 1
    assert save.call_count == 1


def test_scopus_duplicate_identity_and_fuzzy_stay_unverified(tmp_path):
    master = scopus.load_master(write_master(tmp_path, rows={scopus.SHEET_SOURCES: [
        {'Sourcerecord ID': '123', 'Source Title': 'Journal of Testing', 'ISSN': '0378-5955',
         'EISSN': '', 'Active or Inactive': 'Active', 'Coverage': '2000-2026'},
        {'Sourcerecord ID': '456', 'Source Title': 'Journal of Testing', 'ISSN': '0309-0566',
         'EISSN': '', 'Active or Inactive': 'Active', 'Coverage': '2000-2026'}]}))
    checked = master.check(journal('Journal of Testing'))
    assert checked['status'] == 'needs_verification'
    assert len(checked['metadata']['candidates']) == 2
    fuzzy = master.check(journal('Journal of Testings'))
    assert fuzzy['status'] == 'needs_verification'
    assert fuzzy['metadata']['match_method'] == 'fuzzy_candidates'


def test_scopus_issn_match_takes_precedence_over_changed_title(tmp_path):
    candidate = journal()
    candidate.identifiers = {'0378-5955'}
    master = scopus.load_master(write_master(tmp_path, rows={scopus.SHEET_SOURCES: [
        {'Sourcerecord ID': '123', 'Source Title': 'Changed title', 'ISSN': '0378-5955',
         'EISSN': '', 'Active or Inactive': 'Active', 'Coverage': '2000-2026'}]}))
    checked = master.check(candidate)
    assert checked['status'] == 'indexed'
    assert checked['metadata']['match_method'] == 'issn'
    assert checked['metadata']['identity_match'] is True
    assert checked['indexed'] is True


def test_check_scopus_cannot_enable_save_or_dry_run():
    for mode in ('--save', '--dry-run'):
        with pytest.raises(SystemExit) as exc:
            runner.main(['--check-scopus', mode])
        assert exc.value.code == 2


@pytest.mark.parametrize('adapter', [scimagojr, sinta], ids=['scimagojr', 'sinta'])
@pytest.mark.parametrize('title,has_issn,expected', [
    ('Different Journal', True, 'unverified'),
    (' JOURNAL — of  Testing! ', True, 'indexed'),
    (' JOURNAL — of  Testing! ', False, 'indexed'),
    ('Different Journal', False, 'not_found'),
])
def test_manual_evidence_requires_title_and_available_issn(adapter, title, has_issn, expected, monkeypatch):
    candidate = journal()
    if has_issn:
        candidate.identifiers = {'0378-5955'}
    evidence = {'title': title, 'issn': '0378-5955',
                'evidence_url': 'https://www.scimagojr.com/journalsearch.php?q=123&tip=sid',
                'sjr': [{'quartile': 'Q2', 'year': 2024, 'category': 'Testing'}],
                'sinta_rank': 'S3', 'sinta_period': '2023-2027'}
    if adapter is sinta:
        monkeypatch.setenv('SINTA_USERNAME', 'fixture-user')
        monkeypatch.setenv('SINTA_PASSWORD', 'fixture-password')
        evidence['evidence_url'] = 'https://sinta.kemdiktisaintek.go.id/journals/profile/123'
    monkeypatch.setattr(adapter, 'read_evidence', lambda *a: [evidence])
    checked = (adapter.check_manual_evidence(candidate) if adapter is scimagojr else adapter.check(candidate))
    assert checked['status'] == expected
    assert checked['indexed'] is (True if expected == 'indexed' else None)
    if expected == 'indexed':
        if adapter is scimagojr:
            assert checked['sjr'] == [{'quartile': 'Q2', 'year': 2024, 'category': 'Testing'}]
        else:
            assert checked['sinta_rank'] == 'S3'
            assert checked['sinta_period'] == '2023-2027'


def test_manual_title_rule_does_not_change_general_or_scopus_issn_matching(tmp_path):
    candidate = journal()
    candidate.identifiers = {'0378-5955'}
    assert matches(candidate, 'Different Journal', ['0378-5955']) is True
    master = scopus.load_master(write_master(tmp_path, rows={scopus.SHEET_SOURCES: [
        {'Sourcerecord ID': '123', 'Source Title': 'Different Journal', 'ISSN': '0378-5955',
         'EISSN': '', 'Active or Inactive': 'Active', 'Coverage': '2000-2026'}]}))
    checked = master.check(candidate)
    assert checked['status'] == 'indexed'
    assert checked['metadata']['identity_match'] is True
