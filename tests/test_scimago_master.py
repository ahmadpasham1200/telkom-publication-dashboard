"""Local SCImago master contracts; synthetic CSV fixtures, no live I/O."""
import csv
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import requests
from sqlalchemy.dialects import postgresql

from src.journal_index import scimagojr
from src.journal_index import run_journal_index as runner
from src.journal_index.init import candidates, issns

HEADERS = ['Rank', 'Sourceid', 'Title', 'Type', 'Issn', 'Publisher',
           'SJR', 'SJR Best Quartile', 'H index', 'Total Docs. (2025)',
           'Country', 'Region', 'Publisher', 'Coverage', 'Categories', 'Areas']


def row(title='Journal of Testing', source_id='123', identifiers='03785955, 03090566', **changes):
    values = ['1', source_id, title, 'journal', identifiers, 'Publisher First',
              '1,234', 'Q2', '42', '10', 'Indonesia', 'Asia', 'Publisher Second',
              '2000-2025', 'Testing (Q2); Computing (Q3)', 'Computer Science']
    for key, value in changes.items():
        values[HEADERS.index(key)] = value
    return values


def write_csv(tmp_path, rows=None, delimiter=';', encoding='utf-8-sig'):
    path = tmp_path / 'master.csv'
    with path.open('w', encoding=encoding, newline='') as stream:
        writer = csv.writer(stream, delimiter=delimiter)
        writer.writerow(HEADERS)
        writer.writerows(rows if rows is not None else [row()])
    return path


def candidate(title='Journal of Testing', kind='journal', **fields):
    return candidates([{'raw_publication_id': 1, 'title': 'Article must not be matched',
                        'detail_venue': title, 'detail_publication_type': kind, **fields}],
                      scholar_types=True)[0]


@pytest.fixture(autouse=True)
def no_http(monkeypatch):
    monkeypatch.setattr(requests.Session, 'request', MagicMock(side_effect=AssertionError('No HTTP allowed')))


@pytest.mark.parametrize('encoding,delimiter', [('utf-8-sig', ';'), ('utf-8', ','),
                                               ('utf-16', '\t'), ('cp1252', '|')])
def test_read_encoding_delimiter_and_duplicate_headers(tmp_path, encoding, delimiter):
    path = write_csv(tmp_path, [row(title='Journal Étude')], delimiter, encoding)
    original = path.read_bytes()
    master = scimagojr.load_master(path)
    assert master.delimiter == delimiter
    assert master.records[0]['publishers'] == ['Publisher First', 'Publisher Second']
    assert master.records[0]['title'] == 'Journal Étude'
    assert path.read_bytes() == original


def test_missing_file_and_wrong_edition(tmp_path):
    with pytest.raises(scimagojr.MasterExportError, match='not found'):
        scimagojr.load_master(tmp_path / 'missing.csv')
    path = write_csv(tmp_path)
    path.write_text(path.read_text(encoding='utf-8-sig').replace('Total Docs. (2025)', 'Total Docs. (2024)'), encoding='utf-8')
    with pytest.raises(scimagojr.MasterExportError, match='2025'):
        scimagojr.load_master(path)


def test_issns_multiple_and_checksum():
    assert issns('03785955 / 0309-0566, 1234-5678') == {'0378-5955', '0309-0566'}


def test_issn_first_and_reference_metadata(tmp_path):
    master = scimagojr.load_master(write_csv(tmp_path))
    checked = master.check(candidate('Renamed Journal', issn='0378-5955'))
    assert checked['status'] == 'indexed'
    assert checked['metadata']['match_method'] == 'issn'
    assert checked['metadata']['reference_year'] == 2025
    assert checked['metadata']['ranking_source'] == 'scimago'
    assert checked['metadata']['source_id'] == '123'
    assert checked['metadata']['master_record']['publishers'] == ['Publisher First', 'Publisher Second']
    observation = checked['sjr'][0]
    assert observation == {'year': 2025, 'sjr': 1.234, 'quartile': 'Q2', 'scope': 'best_quartile', 'category': None}
    assert checked['evidence_url'].endswith('q=123&tip=sid')


def test_name_match_and_missing_optional_values(tmp_path):
    path = write_csv(tmp_path, [row(Issn='', SJR='', **{'H index': '', 'SJR Best Quartile': ''})])
    master = scimagojr.load_master(path)
    checked = master.check(candidate(' JOURNAL — of TESTING! '))
    assert len(master.records) == 1
    assert checked['status'] == 'indexed'
    assert checked['metadata']['match_method'] == 'title_exact'
    assert checked['sjr'][0]['quartile'] is None
    assert checked['sjr'][0]['sjr'] is None


def test_fuzzy_is_only_a_suggestion(tmp_path):
    master = scimagojr.load_master(write_csv(tmp_path))
    checked = master.check(candidate('Journal of Testings'))
    assert checked['status'] == 'unverified'
    assert checked['indexed'] is None and checked['sjr'] == []
    assert checked['metadata']['candidates'][0]['source_id'] == '123'


def test_multiple_fuzzy_candidates_stay_unverified(tmp_path):
    master = scimagojr.load_master(write_csv(tmp_path, [
        row(title='Journal of Testing Systems'),
        row(title='Journal of Testing Systemics', source_id='456'),
    ]))
    checked = master.check(candidate('Journal of Testing System'))
    assert checked['status'] == 'unverified'
    assert len(checked['metadata']['candidates']) == 2
    assert checked['sjr'] == []


def test_eissn_matches_without_title_fallback(tmp_path):
    master = scimagojr.load_master(write_csv(tmp_path))
    checked = master.check(candidate('Another Name', eissn='03090566'))
    assert checked['status'] == 'indexed'
    assert checked['metadata']['match_method'] == 'issn'


def test_ambiguous_exact_and_issn_conflict(tmp_path):
    master = scimagojr.load_master(write_csv(tmp_path, [row(), row(source_id='456', identifiers='00335533')]))
    assert master.check(candidate())['status'] == 'unverified'
    assert master.check(candidate(issn='0033-5533'))['status'] == 'indexed'
    assert master.check(candidate(issn='0007-9235'))['status'] == 'unverified'
    assert master.check(candidate(issn='0378-5955', eissn='0033-5533'))['status'] == 'unverified'


def test_same_title_with_conflicting_publication_issns_is_not_merged(tmp_path):
    master = scimagojr.load_master(write_csv(tmp_path))
    selected = candidates([
        {'detail_journal': 'Journal of Testing', 'issn': '0378-5955'},
        {'detail_journal': 'Journal of Testing', 'issn': '0007-9235'},
    ], scholar_types=True)
    assert master.check(selected[0])['status'] == 'unverified'


def test_non_finite_numeric_value_is_null(tmp_path):
    master = scimagojr.load_master(write_csv(tmp_path, [row(SJR='9' * 500)]))
    assert master.records[0]['sjr'] is None


def test_not_found_without_issn(tmp_path):
    master = scimagojr.load_master(write_csv(tmp_path))
    assert master.check(candidate('A Completely Different Journal'))['status'] == 'not_found'


@pytest.mark.parametrize('kind', ['conference', 'proceedings', 'book', 'book_chapter', 'thesis', 'dissertation'])
def test_non_journals_never_get_quartiles(tmp_path, kind):
    master = scimagojr.load_master(write_csv(tmp_path))
    checked = master.check(candidate(kind=kind))
    assert checked['status'] == 'excluded'
    assert checked['sjr'] == [] and checked['indexed'] is None


def test_unknown_does_not_become_journal_from_venue_name(tmp_path):
    master = scimagojr.load_master(write_csv(tmp_path))
    assert master.check(candidate(kind='unknown'))['status'] == 'unverified'
    explicit = candidate(kind='unknown', detail_journal='Journal of Testing')
    assert master.check(explicit)['status'] == 'indexed'


def test_historical_explicit_venue_labels_are_type_evidence(tmp_path):
    master = scimagojr.load_master(write_csv(tmp_path))
    journal = candidates([{'detail_venue': 'Journal of Testing', 'detail_venue_label': 'Jurnal'}], scholar_types=True)[0]
    assert master.check(journal)['status'] == 'indexed'
    conference = candidates([{'detail_venue': 'Journal of Testing', 'detail_venue_label': 'Konferensi'}], scholar_types=True)[0]
    assert master.check(conference)['status'] == 'excluded'


def test_legacy_title_heuristic_cannot_bypass_scholar_types(tmp_path):
    master = scimagojr.load_master(write_csv(tmp_path))
    legacy = candidates([{'detail_venue': 'Journal of Testing', 'title': 'Article'}])[0]
    assert master.check(legacy)['status'] == 'unverified'


def test_library_checker_uses_explicit_types_not_legacy_name_heuristics(tmp_path):
    master = scimagojr.load_master(write_csv(tmp_path, [row(title='Nature')]))
    legacy = candidates([{'detail_venue': 'Nature', 'detail_publication_type': 'journal'}])[0]
    assert legacy.classification == 'ambiguous'
    assert master.check(legacy)['status'] == 'indexed'


def test_library_checker_prioritizes_detail_journal(tmp_path):
    master = scimagojr.load_master(write_csv(tmp_path))
    legacy = candidates([{'detail_journal': 'Journal of Testing', 'detail_venue': 'Other venue'}])[0]
    assert master.check(legacy)['status'] == 'indexed'


def test_mixed_publication_types_do_not_receive_a_shared_quartile(tmp_path):
    master = scimagojr.load_master(write_csv(tmp_path))
    legacy = candidates([{'detail_venue': 'Journal of Testing', 'detail_publication_type': 'journal'},
                         {'detail_venue': 'Journal of Testing', 'detail_publication_type': 'book'}])[0]
    assert master.check(legacy)['status'] == 'unverified'


def test_master_non_journal_source_has_no_rank(tmp_path):
    master = scimagojr.load_master(write_csv(tmp_path, [row(Type='book series')]))
    checked = master.check(candidate())
    assert checked['status'] == 'unverified' and checked['sjr'] == []


def test_invalid_optional_numbers_are_not_fabricated(tmp_path):
    master = scimagojr.load_master(write_csv(tmp_path, [row(SJR='nan', **{'H index': 'unknown', 'SJR Best Quartile': '-'})]))
    record = master.records[0]
    assert record['sjr'] is None and record['h_index'] is None and record['best_quartile'] is None
    assert master.check(candidate())['status'] == 'indexed'


def test_csv_dry_run_matches_without_ddl_writes_cache_or_http(tmp_path, monkeypatch, capsys):
    import json
    from src.loading import postgres
    engine = MagicMock()
    engine.url.database = 'unit_test'
    engine.connect.return_value.__enter__.return_value.execute.return_value.scalar_one.return_value = 'unit_test'
    monkeypatch.setenv('POSTGRES_DB', 'unit_test')
    monkeypatch.setattr(postgres, 'build_engine', lambda: engine)
    monkeypatch.setattr(runner, 'read_candidates', lambda *a, **k: [candidate(), candidate(kind='conference')])
    monkeypatch.setattr(runner, 'cached_result', MagicMock(side_effect=AssertionError('No legacy cache')))
    monkeypatch.setattr(runner, 'save_result', MagicMock(side_effect=AssertionError('No writes')))
    monkeypatch.setattr(runner, 'create_table', MagicMock(side_effect=AssertionError('No DDL')))
    assert runner.main(['--source', 'scimago', '--dry-run', '--scimago-csv', str(write_csv(tmp_path))]) == 0
    outputs = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert outputs[0]['status'] == 'indexed'
    assert outputs[1]['status'] == 'excluded'
    assert outputs[-1]['preview_only'] is True
    assert outputs[-1]['statuses'] == {'indexed': 1, 'excluded': 1}
    engine.begin.assert_not_called()


def test_missing_csv_cli_reports_clear_error_before_connect(tmp_path, monkeypatch, caplog):
    from src.loading import postgres
    build = MagicMock(side_effect=AssertionError('No connection before master validation'))
    monkeypatch.setattr(postgres, 'build_engine', build)
    assert runner.main(['--source', 'scimago', '--dry-run', '--scimago-csv', str(tmp_path / 'missing.csv')]) == 1
    assert 'not found' in caplog.text
    build.assert_not_called()


def test_unnamed_unknown_venue_does_not_create_empty_catalog_entry(tmp_path, monkeypatch):
    master = scimagojr.load_master(write_csv(tmp_path))
    unnamed = candidates([{'title': 'Article title only'}], scholar_types=True)
    save = MagicMock()
    monkeypatch.setattr(runner, 'save_result', save)
    counts, errors = runner.run_checks(None, unnamed, ['scimagojr'], local_scimago=True,
        checks={'scimagojr': master.check}, persist=True, delay=0)
    assert counts == {'unverified': 1} and errors == 0
    save.assert_not_called()


def test_catalog_sync_ignores_unnamed_rows(monkeypatch):
    unnamed = candidates([{'title': 'Article title only'}], scholar_types=True)
    upsert = MagicMock()
    monkeypatch.setattr(runner, 'upsert_catalog', upsert)
    assert runner.sync_catalog(MagicMock(), unnamed) == (0, 0)
    upsert.assert_not_called()


def test_detail_journal_priority_and_article_title_not_used():
    selected = candidates([{'title': 'Journal of Testing', 'detail_journal': 'Actual Journal',
                           'detail_venue': 'Other Venue', 'venue': 'Profile Venue'}], scholar_types=True)
    assert selected[0].original == 'Actual Journal'
    empty = candidates([{'title': 'Journal of Testing'}], scholar_types=True)[0]
    assert not empty.original and empty.classification == 'ambiguous'


def test_local_match_can_be_saved_in_existing_schema(tmp_path, monkeypatch):
    checked = scimagojr.load_master(write_csv(tmp_path)).check(candidate())
    assert runner.verification_status(checked) == 'verified'
    engine = MagicMock()
    conn = engine.begin.return_value.__enter__.return_value
    monkeypatch.setattr(runner, 'upsert_catalog', lambda *a: 7)
    runner.save_result(engine, candidate(), checked)
    statement = conn.execute.call_args.args[0]
    params = statement.compile(dialect=postgresql.dialect()).params
    assert params['source'] == 'scimagojr'  # Preserve database source constraint.
    assert params['status'] == 'verified' and params['indexed'] is True
    assert params['matched_identity']['source_id'] == '123'
    assert params['metadata']['reference_year'] == 2025
    assert params['sjr'][0]['quartile'] == 'Q2'


def test_failed_csv_match_preserves_prior_verified_snapshot(tmp_path, monkeypatch):
    checked = scimagojr.load_master(write_csv(tmp_path)).check(candidate('Unknown Journal'))
    engine = MagicMock()
    monkeypatch.setattr(runner, 'upsert_catalog', lambda *a: 7)
    runner.save_result(engine, candidate(), checked)
    stmt = engine.begin.return_value.__enter__.return_value.execute.call_args.args[0]
    sql = str(stmt.compile(dialect=postgresql.dialect()))
    assert 'WHERE raw.journal_index_verification.status !=' in sql
    assert 'source = excluded.source' not in sql


def test_unexpected_local_processing_error_also_protects_verified_snapshot(monkeypatch):
    engine = MagicMock()
    monkeypatch.setattr(runner, 'upsert_catalog', lambda *a: 7)
    def broken_check(selected):
        raise RuntimeError('technical problem')
    counts, errors = runner.run_checks(engine, [candidate()], ['scimagojr'], delay=0,
        persist=True, local_scimago=True, checks={'scimagojr': broken_check})
    assert counts == {'error': 1} and errors == 0
    stmt = engine.begin.return_value.__enter__.return_value.execute.call_args.args[0]
    assert 'WHERE raw.journal_index_verification.status !=' in str(stmt.compile(dialect=postgresql.dialect()))


def test_all_sources_use_separate_strict_scimago_candidates(tmp_path, monkeypatch):
    from src.loading import postgres
    from src.journal_index.init import result
    engine = MagicMock()
    engine.url.database = 'publication_dashboard'
    engine.connect.return_value.__enter__.return_value.execute.return_value.scalar_one.return_value = 'publication_dashboard'
    monkeypatch.setenv('POSTGRES_DB', 'publication_dashboard')
    monkeypatch.setattr(postgres, 'build_engine', lambda: engine)
    raw = [{'detail_venue': 'Nature', 'detail_journal': 'Nature', 'detail_publication_type': 'journal'}]
    def read(*args, scholar_types=False):
        return candidates(raw, scholar_types=scholar_types)
    monkeypatch.setattr(runner, 'read_candidates', read)
    monkeypatch.setattr(runner, 'cached_result', lambda *args: None)
    monkeypatch.setattr(runner, 'create_table', lambda *args, **kwargs: None)
    save = MagicMock()
    monkeypatch.setattr(runner, 'save_result', save)
    monkeypatch.setattr(runner, 'SOURCES', {'scimagojr': scimagojr.check,
        'scopus': lambda c: result('scopus', 'not_found'), 'sinta': lambda c: result('sinta', 'unverified')})
    path = write_csv(tmp_path, [row(title='Nature')])
    assert runner.main(['--source', 'all', '--save', '--scimago-csv', str(path)]) == 0
    saved = {call.args[2]['source']: call.args[2] for call in save.call_args_list}
    assert saved['scimagojr']['status'] == 'indexed'
    assert saved['scopus']['status'] == 'not_found'
    assert saved['sinta']['status'] == 'unverified'


@pytest.fixture(scope='module')
def isolated_scimago_db():
    """Opt-in PostgreSQL test: create a unique database, never change an existing one."""
    import os
    import uuid
    import sqlalchemy as sa
    from src.loading.postgres import build_engine
    if os.getenv('SCIMAGO_MASTER_INTEGRATION') != '1':
        pytest.skip('Set SCIMAGO_MASTER_INTEGRATION=1 to use a new isolated PostgreSQL database')
    admin = build_engine()
    name = 'scimago_master_test_' + uuid.uuid4().hex[:12]
    with admin.connect().execution_options(isolation_level='AUTOCOMMIT') as conn:
        conn.exec_driver_sql(f'CREATE DATABASE "{name}"')
    engine = sa.create_engine(admin.url.set(database=name))
    admin.dispose()
    runner.create_table(engine)
    print(f'Isolated SCImago test database retained: {name}')
    yield engine
    engine.dispose()


def test_postgres_matched_only_upsert_and_source_protection(isolated_scimago_db, tmp_path):
    import sqlalchemy as sa
    from src.journal_index.init import result
    master = scimagojr.load_master(write_csv(tmp_path, [row(), row(title='Unrelated Master Journal', source_id='999')]))
    selected = candidate()
    checked = master.check(selected)
    runner.save_result(isolated_scimago_db, selected, result('scopus', 'not_found', 'Existing other-source snapshot'))
    runner.save_result(isolated_scimago_db, selected, checked)
    with isolated_scimago_db.connect() as conn:
        original = conn.execute(sa.select(runner.table).where(runner.table.c.source == 'scimagojr')).mappings().one()
        other = conn.execute(sa.select(runner.table).where(runner.table.c.source == 'scopus')).mappings().one()
        identity = conn.execute(sa.select(runner.catalog)).mappings().one()
        assert set(identity['identifiers']) == {'0378-5955', '0309-0566'}
        assert original['matched_identity']['source_id'] == '123'
        assert original['metadata']['master_record']['h_index'] == 42
        assert original['metadata']['master_record']['publishers'] == ['Publisher First', 'Publisher Second']
        assert original['sjr'][0]['year'] == 2025
        assert conn.execute(sa.select(sa.func.count()).select_from(runner.catalog)).scalar_one() == 1
        assert conn.execute(sa.text("SELECT to_regclass('raw.scholar_publication')")).scalar_one() is None
    # Repeat matching updates one relevant source snapshot, not the entire master.
    runner.save_result(isolated_scimago_db, selected, checked)
    def broken_check(candidate):
        raise RuntimeError('simulated adapter failure')
    assert runner.run_checks(isolated_scimago_db, [selected], ['scimagojr'],
        checks={'scimagojr': broken_check}, local_scimago=True, persist=True, delay=0) == ({'error': 1}, 0)
    failed = result('scimagojr', 'error', 'New CSV processing failure', metadata={'method': 'scimago_master_csv'})
    runner.save_result(isolated_scimago_db, selected, failed)
    with isolated_scimago_db.connect() as conn:
        current = conn.execute(sa.select(runner.table).where(runner.table.c.source == 'scimagojr')).mappings().one()
        assert dict(current) == dict(original)
        assert dict(conn.execute(sa.select(runner.table).where(runner.table.c.source == 'scopus')).mappings().one()) == dict(other)
        assert conn.execute(sa.select(sa.func.count()).select_from(runner.table)).scalar_one() == 2
