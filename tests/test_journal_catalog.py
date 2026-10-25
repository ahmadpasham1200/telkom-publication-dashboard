"""Offline contracts for the shared catalogue and source verification cache."""
from datetime import datetime, timedelta, timezone
from copy import deepcopy
from unittest.mock import MagicMock

import pytest
import sqlalchemy as sa
import requests
from sqlalchemy.dialects import postgresql

from src.journal_index import run_journal_index as runner
from src.journal_index.init import candidates, result


@pytest.fixture(autouse=True)
def prohibit_live_connections(monkeypatch):
    """A mistaken test setup must fail before touching any real DB or website."""
    import dotenv
    monkeypatch.setattr(dotenv, 'load_dotenv', lambda *a, **k: False)
    def forbidden(*args, **kwargs):
        raise AssertionError('Live database/HTTP access is forbidden in catalogue tests')
    monkeypatch.setattr(sa.engine.Engine, 'connect', forbidden)
    monkeypatch.setattr(sa.engine.Engine, 'raw_connection', forbidden)
    monkeypatch.setattr(requests.Session, 'request', forbidden)


@pytest.fixture
def catalog_store():
    """Stateful mock consuming real SQLAlchemy statements, never opening a socket.

    SQL assertions keep the fake from silently accepting an incorrect conflict
    target or replacement of the JSONB merge with an overwrite. PostgreSQL DDL
    and transactional behavior are not claimed to be integration-tested here.
    """
    engine = MagicMock()
    connection = engine.begin.return_value.__enter__.return_value
    engine.connect.return_value.__enter__.return_value = connection
    catalog_rows, verification_rows, statements = {}, {}, []

    def execute(statement, parameters=None):
        statements.append(statement)
        response = MagicMock()
        if isinstance(statement, sa.sql.elements.TextClause):
            assert str(statement).lstrip().upper().startswith('SELECT')
            response.scalar_one_or_none.return_value = 'raw.journal_index_verification'
            return response
        compiled = statement.compile(dialect=postgresql.dialect())
        params, sql = compiled.params, str(compiled)
        if isinstance(statement, postgresql.Insert):
            if statement.table is runner.catalog:
                assert 'ON CONFLICT ON CONSTRAINT uq_journal_catalog_title DO UPDATE' in sql
                assert 'publication_origins || excluded.publication_origins' in sql
                key = params['journal_normalized']
                row = catalog_rows.get(key)
                if row is None:
                    row = deepcopy(params)
                    row['journal_id'] = len(catalog_rows) + 1
                    catalog_rows[key] = row
                else:
                    row['publication_origins'].update(deepcopy(params['publication_origins']))
                    row['identifiers'].update(params['identifiers'])
                response.scalar_one.return_value = row['journal_id']
            elif statement.table is runner.table:
                assert 'ON CONFLICT ON CONSTRAINT uq_journal_verification_source DO UPDATE' in sql
                assert 'journal_id = excluded.journal_id' not in sql
                assert 'source = excluded.source' not in sql
                verification_rows[(params['journal_id'], params['source'])] = deepcopy(params)
            else:
                pytest.fail('Unexpected table mutation')
        else:
            assert isinstance(statement, sa.sql.Select)
            catalog_row = catalog_rows.get(params['journal_normalized_1'])
            row = catalog_row
            if 'source_1' in params:
                row = verification_rows.get((catalog_row['journal_id'], params['source_1'])) if catalog_row else None
                row = dict(row, catalog_identifiers=catalog_row['identifiers']) if row else None
            response.mappings.return_value.first.return_value = deepcopy(row)
        return response

    connection.execute.side_effect = execute
    return engine, catalog_rows, verification_rows, statements


def journals():
    return candidates([
        {'raw_publication_id': 1, 'scholar_id': 'a', 'detail_venue': 'Journal A'},
        {'raw_publication_id': 2, 'scholar_id': 'b', 'detail_venue': 'JOURNAL A'},
        {'raw_publication_id': 3, 'detail_venue': 'Journal A Extended'},
        {'raw_publication_id': 4, 'detail_venue': 'IEEE Conference'},
        {'raw_publication_id': 5, 'detail_venue': 'Telkom University'},
        {'raw_publication_id': 6, 'detail_venue': 'IEEE Access'},
    ])


def test_catalogue_summary_counts_publications_and_unique_titles():
    summary = runner.summarize_candidates(journals())
    assert summary == {'publications_with_venue': 6, 'candidate_publications': 4,
                       'unique_journals': 3, 'excluded_journals': 2, 'ambiguous_journals': 1}


def test_catalogue_and_verification_ddl_are_separate_and_idempotent():
    engine = MagicMock()
    runner.create_table(engine)
    statements = [str(call.args[0].compile(dialect=postgresql.dialect()))
                  for call in engine.begin.return_value.__enter__.return_value.execute.call_args_list]
    assert any('CREATE TABLE IF NOT EXISTS raw.journal_catalog' in sql for sql in statements)
    assert any('CREATE TABLE IF NOT EXISTS raw.journal_index_verification' in sql for sql in statements)
    assert any('FOREIGN KEY(journal_id) REFERENCES raw.journal_catalog' in sql for sql in statements)
    assert not any('scholar_publication' in sql for sql in statements)


def test_catalogue_upsert_preserves_origins_and_reuses_id():
    conn = MagicMock()
    conn.execute.return_value.scalar_one.return_value = 7
    candidate = journals()[0]
    assert runner.upsert_catalog(conn, candidate) == 7
    stmt = conn.execute.call_args.args[0]
    sql = str(stmt.compile(dialect=postgresql.dialect()))
    assert 'ON CONFLICT ON CONSTRAINT uq_journal_catalog_title DO UPDATE' in sql
    assert 'publication_origins || excluded.publication_origins' in sql
    assert 'RETURNING raw.journal_catalog.journal_id' in sql


def test_verified_requires_positive_source_evidence():
    assert runner.verification_status(result('scopus', 'indexed')) == 'unverified'
    assert runner.verification_status(result('scopus', 'indexed', evidence_url='https://api.elsevier.com/content/serial/title',
        metadata={'identity_match': True, 'api_success': True, 'source_id': '123'})) == 'verified'
    assert runner.verification_status(result('scopus', 'indexed', evidence_url='https://api.elsevier.com/content/serial/title',
        metadata={'identity_match': False, 'api_success': True})) == 'unverified'
    assert runner.verification_status(result('scopus', 'not_found')) == 'not_found'


def test_cache_is_per_source_and_expiry_is_enforced(monkeypatch):
    now = datetime.now(timezone.utc)
    row = dict(source='scopus', status='not_found', indexed=None, checked_at=now,
               expires_at=now + timedelta(days=1), metadata={}, matched_identity={},
               evidence_url=None, reason='Empty bounded query', sjr=[], sinta_rank=None, sinta_period=None)
    engine = MagicMock()
    connection = engine.connect.return_value.__enter__.return_value
    connection.execute.return_value.scalar_one_or_none.return_value = 'raw.journal_index_verification'
    connection.execute.return_value.mappings.return_value.first.return_value = row
    assert runner.cached_result(engine, journals()[0], 'scopus', now=now)['metadata']['reused'] is True
    row['expires_at'] = now - timedelta(seconds=1)
    assert runner.cached_result(engine, journals()[0], 'scopus', now=now) is None


def test_cached_journal_is_not_requested_or_saved_again(monkeypatch):
    source = MagicMock(side_effect=AssertionError('cache must avoid HTTP'))
    monkeypatch.setattr(runner, 'SOURCES', {'scopus': source})
    monkeypatch.setattr(runner, 'cached_result', lambda *a, **k: result('scopus', 'not_found', metadata={'reused': True}))
    save = MagicMock()
    monkeypatch.setattr(runner, 'save_result', save)
    candidate = journals()[0]
    counts, errors = runner.run_checks(None, [candidate, candidate], ['scopus'], delay=0)
    assert counts == {'not_found': 1} and errors == 0
    source.assert_not_called()
    save.assert_not_called()


def test_read_only_checks_never_save(monkeypatch):
    monkeypatch.setattr(runner, 'cached_result', lambda *a, **k: None)
    monkeypatch.setattr(runner, 'SOURCES', {'scimagojr': lambda c: result('scimagojr', 'access_unavailable')})
    save = MagicMock()
    monkeypatch.setattr(runner, 'save_result', save)
    counts, errors = runner.run_checks(None, journals()[:1], ['scimagojr'], persist=False, delay=0)
    assert counts == {'access_unavailable': 1} and errors == 0
    save.assert_not_called()


def test_explicit_source_issns_are_used_without_merging_similar_titles():
    selected = candidates([
        {'raw_publication_id': 1, 'detail_venue': 'Journal A', 'issn': '0378-5955'},
        {'raw_publication_id': 2, 'detail_venue': 'Journal A Extended', 'eissn': '0309-0566'},
    ])
    assert len(selected) == 2
    assert selected[0].identifiers == {'0378-5955'}
    assert selected[1].identifiers == {'0309-0566'}
    from src.journal_index.init import matches
    assert not matches(selected[0], 'Journal A', [])


def test_sync_catalog_never_checks_sources_and_excludes_venues(monkeypatch):
    engine = MagicMock()
    upsert = MagicMock(return_value=7)
    monkeypatch.setattr(runner, 'upsert_catalog', upsert)
    source = MagicMock(side_effect=AssertionError('sync must not request sources'))
    monkeypatch.setattr(runner, 'SOURCES', {'scopus': source})
    count, errors = runner.sync_catalog(engine, journals())
    assert count == 3 and errors == 0
    assert upsert.call_count == 3
    source.assert_not_called()


def test_verification_upsert_is_keyed_by_journal_and_source_and_has_expiry():
    engine = MagicMock()
    connection = engine.begin.return_value.__enter__.return_value
    connection.execute.return_value.scalar_one.return_value = 7
    checked = result('scopus', 'not_found')
    runner.save_result(engine, journals()[0], checked)
    stmt = connection.execute.call_args.args[0]
    sql = str(stmt.compile(dialect=postgresql.dialect()))
    params = stmt.compile().params
    assert 'ON CONFLICT ON CONSTRAINT uq_journal_verification_source DO UPDATE' in sql
    assert params['journal_id'] == 7 and params['source'] == 'scopus'
    assert params['status'] == 'not_found' and params['indexed'] is None
    assert params['expires_at'] - params['checked_at'] == timedelta(days=1)


def test_failed_catalog_upsert_does_not_cancel_next_journal(monkeypatch):
    upsert = MagicMock(side_effect=[RuntimeError('secret-sentinel'), 7, 8])
    monkeypatch.setattr(runner, 'upsert_catalog', upsert)
    count, errors = runner.sync_catalog(MagicMock(), journals())
    assert count == 2 and errors == 1


def test_conflicting_issns_are_not_silently_merged():
    conn = MagicMock()
    conn.execute.return_value.mappings.return_value.first.return_value = {'identifiers': {'0309-0566': True}}
    candidate = journals()[0]
    candidate.identifiers = {'0378-5955'}
    import pytest
    with pytest.raises(ValueError, match='Conflicting ISSNs'):
        runner.upsert_catalog(conn, candidate)
    assert conn.execute.call_count == 1


def test_dry_run_cannot_enable_writing_modes():
    import pytest
    for option in ('--sync-catalog', '--save', '--save-verification'):
        with pytest.raises(SystemExit) as exc:
            runner.main(['--dry-run', option])
        assert exc.value.code == 2


def test_scopus_cache_does_not_leak_to_another_source():
    now = datetime.now(timezone.utc)
    engine = MagicMock()
    connection = engine.connect.return_value.__enter__.return_value
    connection.execute.return_value.scalar_one_or_none.return_value = 'raw.journal_index_verification'
    connection.execute.return_value.mappings.return_value.first.return_value = {
        'source': 'scopus', 'expires_at': now + timedelta(days=1)}
    assert runner.cached_result(engine, journals()[0], 'sinta', now=now) is None


def test_verified_cache_reused_without_credentials(monkeypatch):
    monkeypatch.delenv('SCOPUS_API_KEY', raising=False)
    checked = result('scopus', 'indexed', evidence_url='https://api.elsevier.com/content/serial/title',
                     metadata={'identity_match': True, 'api_success': True, 'source_id': '123', 'reused': True})
    monkeypatch.setattr(runner, 'cached_result', lambda *a, **k: checked)
    source = MagicMock(side_effect=AssertionError('No HTTP when cache valid'))
    monkeypatch.setattr(runner, 'SOURCES', {'scopus': source})
    counts, errors = runner.run_checks(None, journals()[:1], ['scopus'], persist=False, delay=0)
    assert counts == {'indexed': 1} and errors == 0
    source.assert_not_called()


def test_two_publications_and_repeat_sync_preserve_one_catalogue_entry(catalog_store):
    engine, catalog_rows, verification_rows, _ = catalog_store
    first = candidates([{'raw_publication_id': 101, 'scholar_id': 'lecturer-a',
                         'detail_venue': 'Journal A'}])
    second = candidates([{'raw_publication_id': 202, 'scholar_id': 'lecturer-b',
                          'detail_venue': 'JOURNAL A'}])
    # Different batches also exercise the JSONB merge, not only Python deduplication.
    assert runner.sync_catalog(engine, first) == (1, 0)
    original_id = catalog_rows['journal a']['journal_id']
    assert runner.sync_catalog(engine, second) == (1, 0)
    assert runner.sync_catalog(engine, first + second) == (2, 0)
    assert len(catalog_rows) == 1
    stored = catalog_rows['journal a']
    assert stored['journal_id'] == original_id
    assert set(stored['publication_origins']) == {'101', '202'}
    assert stored['publication_origins']['101']['scholar_id'] == 'lecturer-a'
    assert stored['publication_origins']['202']['scholar_id'] == 'lecturer-b'
    assert stored['publication_origins']['202']['detail_venue'] == 'JOURNAL A'
    assert verification_rows == {}


def test_source_upserts_replace_only_the_same_journal_and_source(catalog_store):
    engine, catalog_rows, verification_rows, _ = catalog_store
    candidate = journals()[0]
    runner.save_result(engine, candidate, result('scopus', 'not_found'))
    runner.save_result(engine, candidate, result('scimagojr', 'access_unavailable'))
    runner.save_result(engine, candidate, result('sinta', 'unverified'))
    before = deepcopy(verification_rows)
    runner.save_result(engine, candidate, result('scopus', 'error', 'New Scopus attempt'))
    assert len(catalog_rows) == 1 and len(verification_rows) == 3
    journal_id = catalog_rows['journal a']['journal_id']
    assert verification_rows[(journal_id, 'scopus')]['status'] == 'error'
    assert verification_rows[(journal_id, 'scimagojr')] == before[(journal_id, 'scimagojr')]
    assert verification_rows[(journal_id, 'sinta')] == before[(journal_id, 'sinta')]
    other = journals()[1]
    runner.save_result(engine, other, result('scopus', 'not_found'))
    assert len(catalog_rows) == 2 and len(verification_rows) == 4
    assert verification_rows[(journal_id, 'scopus')]['reason'] == 'New Scopus attempt'


@pytest.mark.parametrize('adapter_status,stored_status,lifetime', [
    ('indexed', 'verified', timedelta(days=30)),
    ('not_found', 'not_found', timedelta(days=1)),
    ('error', 'error', timedelta(hours=1)),
    ('access_unavailable', 'access_unavailable', timedelta(hours=1)),
    ('unverified', 'unverified', timedelta(hours=1)),
])
@pytest.mark.parametrize('offset,valid', [
    (-timedelta(microseconds=1), True),
    (timedelta(0), False),
    (timedelta(microseconds=1), False),
])
def test_saved_cache_ttl_before_at_and_after_expiry(
        catalog_store, adapter_status, stored_status, lifetime, offset, valid):
    engine, catalog_rows, verification_rows, _ = catalog_store
    checked_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    candidate = journals()[0]
    checked = result('scopus', adapter_status, checked_at=checked_at)
    if adapter_status == 'indexed':
        # Synthetic identity evidence; no claim of live Scopus verification.
        checked.update(evidence_url='https://api.elsevier.com/content/serial/title',
                       metadata={'identity_match': True, 'api_success': True, 'source_id': '123'})
    runner.save_result(engine, candidate, checked)
    row = verification_rows[(catalog_rows['journal a']['journal_id'], 'scopus')]
    assert row['status'] == stored_status
    assert row['expires_at'] == checked_at + lifetime
    previous_row = deepcopy(row)
    cached = runner.cached_result(engine, candidate, 'scopus', now=row['expires_at'] + offset)
    assert (cached is not None) is valid
    if valid:
        assert cached['metadata']['reused'] is True
        assert cached['checked_at'] == checked_at
    assert row == previous_row  # Reading the cache never renews its lifetime.


def test_cached_result_adds_new_publication_without_renewing_verification(catalog_store, monkeypatch):
    engine, catalog_rows, verification_rows, _ = catalog_store
    first = candidates([{'raw_publication_id': 101, 'detail_venue': 'Journal A'}])[0]
    second = candidates([{'raw_publication_id': 202, 'detail_venue': 'JOURNAL A'}])[0]
    runner.save_result(engine, first, result('scopus', 'not_found'))
    snapshot = deepcopy(verification_rows)
    source = MagicMock(side_effect=AssertionError('Cache must avoid repeated request'))
    monkeypatch.setattr(runner, 'SOURCES', {'scopus': source})
    counts, errors = runner.run_checks(engine, [second], ['scopus'], delay=0)
    assert counts == {'not_found': 1} and errors == 0
    assert set(catalog_rows['journal a']['publication_origins']) == {'101', '202'}
    assert verification_rows == snapshot
    source.assert_not_called()


def test_dry_run_real_read_path_only_executes_select(monkeypatch, capsys):
    import json
    from src.loading import postgres
    engine = MagicMock()
    engine.url.database = 'journal_catalog_unit_test'
    statements = []
    rows = [
        {'raw_publication_id': 1, 'detail_venue': 'Journal A'},
        {'raw_publication_id': 2, 'detail_venue': 'JOURNAL A'},
        {'raw_publication_id': 3, 'detail_venue': 'Proceedings of Computing'},
        {'raw_publication_id': 4, 'detail_venue': 'Telkom University'},
        {'raw_publication_id': 5, 'detail_venue': 'IEEE Access'},
    ]

    def execute(statement, parameters=None):
        sql = str(statement).strip()
        statements.append(sql)
        assert sql.upper().startswith('SELECT'), 'Dry-run attempted a non-SELECT statement'
        response = MagicMock()
        if 'current_database()' in sql:
            response.scalar_one.return_value = 'journal_catalog_unit_test'
        else:
            assert 'FROM raw.scholar_publication' in sql
            assert parameters == {'row_limit': 1000}
            response.mappings.return_value = iter(rows)
        return response

    engine.connect.return_value.__enter__.return_value.execute.side_effect = execute
    engine.begin.side_effect = AssertionError('No write transaction allowed')
    monkeypatch.setenv('POSTGRES_DB', 'journal_catalog_unit_test')
    monkeypatch.setattr(postgres, 'build_engine', lambda: engine)
    # read_candidates is deliberately not mocked: exercise the actual CLI-to-SQL path.
    assert runner.main(['--dry-run']) == 0
    assert len(statements) == 2
    engine.begin.assert_not_called()
    output = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert output[-1]['summary'] == {
        'publications_with_venue': 5, 'candidate_publications': 3,
        'unique_journals': 2, 'excluded_journals': 2, 'ambiguous_journals': 1}
    assert output[-1]['preview_only'] is True


@pytest.mark.parametrize('venue,kind', [
    ('International Conference on Computing', 'non_journal'),
    ('Prosiding Informatika', 'non_journal'),
    ('Proceedings of Computing', 'non_journal'),
    ('Telkom University', 'non_journal'),
    ('IEEE Access', 'ambiguous'),
    ('Journal of Testing', 'journal'),
])
def test_classification_controls_catalogue_and_source_checks(catalog_store, monkeypatch, venue, kind):
    engine, catalog_rows, verification_rows, _ = catalog_store
    candidate = candidates([{'raw_publication_id': 1, 'detail_venue': venue}])[0]
    assert candidate.classification == kind
    source = MagicMock(return_value=result('scopus', 'unverified', 'Identity not confirmed'))
    monkeypatch.setattr(runner, 'SOURCES', {'scopus': source})
    runner.sync_catalog(engine, [candidate])
    counts, errors = runner.run_checks(engine, [candidate], ['scopus'], delay=0)
    assert errors == 0
    if kind == 'non_journal':
        assert catalog_rows == {} and verification_rows == {} and counts == {}
        source.assert_not_called()
    else:
        assert len(catalog_rows) == 1
        assert next(iter(catalog_rows.values()))['classification'] == kind
        assert next(iter(verification_rows.values()))['status'] == 'unverified'
        assert next(iter(verification_rows.values()))['indexed'] is None
        source.assert_called_once_with(candidate)


@pytest.mark.parametrize('database', ['', 'another_database', 'publication_dashboard_test'])
def test_write_database_name_must_be_publication_dashboard(database):
    with pytest.raises(ValueError):
        runner.validate_database_name(database, writing=True)


def test_database_name_validation_allows_exact_write_target_and_read_only_test_db():
    runner.validate_database_name('publication_dashboard', writing=True)
    runner.validate_database_name('isolated_unit_test', writing=False)


def test_catalogue_only_ddl_contains_no_other_table_or_destructive_statement():
    engine = MagicMock()
    runner.create_table(engine, catalog_only=True)
    executed = engine.begin.return_value.__enter__.return_value.execute.call_args_list
    sql = [str(call.args[0].compile(dialect=postgresql.dialect())).strip() for call in executed]
    assert len(sql) == 2
    assert sql[0] == 'CREATE SCHEMA IF NOT EXISTS raw'
    assert sql[1].startswith('CREATE TABLE IF NOT EXISTS raw.journal_catalog')
    for statement in sql:
        assert not any(word in statement.upper().split() for word in ('TRUNCATE', 'DELETE', 'DROP', 'ALTER'))
        assert 'scholar_publication' not in statement
        assert 'journal_index_verification' not in statement
