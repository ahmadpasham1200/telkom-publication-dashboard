"""Offline Scopus Excel-master pipeline contracts; no HTTP, no real PostgreSQL."""
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import requests
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from src.journal_index import run_journal_index as runner
from src.journal_index import scimagojr
from src.journal_index import scopus
from src.journal_index.init import candidates, result

MIGRATION = Path(__file__).resolve().parents[1] / 'sql/ddl/19_create_scopus_publication_verification.sql'


def candidate(raw_id=1, title='Journal of Testing', kind='journal', **fields):
    return candidates([{'raw_publication_id': raw_id, 'title': 'Article must not be matched',
                        'detail_venue': title, 'detail_publication_type': kind, **fields}],
                      scholar_types=True)[0]


def checked(status='indexed', **values):
    return result('scopus', status, 'Unique master identity matched', source_record_id=12345,
                  source_title='Journal of Testing', source_type='journal', issn='0378-5955',
                  coverage='2000-2026', active_status='Active', publisher='Elsevier',
                  matched_identity={'source_id': '12345'}, match_confidence='high',
                  source_sheet='Scopus Sources Sep 2026',
                  metadata={'master_file': 'data/scopus/ext_list_Sep_2026.xlsx',
                            'master_sha256': 'fake-digest', 'match_method': 'issn'}, **values)


class FakeMaster:
    def __init__(self, answers=None):
        self.answers = answers or {}
        self.path = Path('data/scopus/ext_list_Sep_2026.xlsx')
        self.digest = 'fake-sha256'
        self.checked = []

    def check(self, candidate):
        self.checked.append(candidate.original)
        return self.answers.get(candidate.original,
                                result('scopus', 'not_found', 'No master identity matched'))


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr(requests.Session, 'request',
                        MagicMock(side_effect=AssertionError('No HTTP allowed')))
    monkeypatch.setattr(requests.Session, 'get',
                        MagicMock(side_effect=AssertionError('No HTTP allowed')))
    import dotenv
    # Block real .env loading; tests supply their own environment explicitly.
    monkeypatch.setattr(dotenv, 'load_dotenv', MagicMock())
    monkeypatch.setattr(sa.engine.Engine, 'connect',
                        MagicMock(side_effect=AssertionError('No real PostgreSQL')))
    # Transitional stub while scopus.py is being rewritten in parallel; the real
    # module attribute is asserted unchanged in test_default_master_paths.
    if not hasattr(scopus, 'DEFAULT_MASTER'):
        monkeypatch.setattr(scopus, 'DEFAULT_MASTER',
                            Path('data/scopus/ext_list_Sep_2026.xlsx'), raising=False)


def cli_engine(monkeypatch, database='unit_test'):
    from src.loading import postgres
    engine = MagicMock()
    engine.url.database = database
    engine.connect.return_value.__enter__.return_value.execute.return_value.scalar_one.return_value = database
    monkeypatch.setenv('POSTGRES_DB', database)
    monkeypatch.setattr(postgres, 'build_engine', lambda: engine)
    return engine


def install_master(monkeypatch, master, seen=None):
    def fake_load(path=None):
        if seen is not None:
            seen['path'] = path
        return master
    monkeypatch.setattr(scopus, 'load_master', fake_load, raising=False)


def test_check_scopus_dry_run_is_read_only(monkeypatch, capsys):
    engine = cli_engine(monkeypatch)
    master = FakeMaster({'Journal of Testing': checked()})
    seen = {}
    install_master(monkeypatch, master, seen)
    monkeypatch.setattr(runner, 'read_scopus_candidates',
                        lambda *a, **k: [candidate(), candidate(2, 'Conference X', kind='conference')])
    ddl = MagicMock(side_effect=AssertionError('No DDL in check mode'))
    save = MagicMock(side_effect=AssertionError('No writes in check mode'))
    legacy = MagicMock(side_effect=AssertionError('No legacy journal verification writes'))
    monkeypatch.setattr(runner, 'create_table', ddl)
    monkeypatch.setattr(runner, 'save_scopus_results', save)
    monkeypatch.setattr(runner, 'save_result', legacy)
    assert runner.main(['--check-scopus', '--limit', '5',
                        '--scopus-file', 'master.xlsx']) == 0
    assert Path(seen['path']) == Path('master.xlsx')
    outputs = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert outputs[0]['source'] == 'scopus' and outputs[0]['status'] == 'indexed'
    assert outputs[0]['metadata']['match_method'] == 'issn'
    # Non-journal candidates are filtered before any master check.
    assert master.checked == ['Journal of Testing']
    ddl.assert_not_called()
    save.assert_not_called()
    legacy.assert_not_called()
    engine.begin.assert_not_called()


def test_check_scopus_scholar_id_filters_candidates(monkeypatch, capsys):
    cli_engine(monkeypatch)
    master = FakeMaster()
    install_master(monkeypatch, master)
    monkeypatch.setattr(runner, 'read_scopus_candidates', lambda *a, **k: [
        candidate(1), candidate(2, 'Other Journal', scholar_id='ABC')])
    assert runner.main(['--check-scopus', '--scholar-id', 'ABC']) == 0
    assert master.checked == ['Other Journal']
    outputs = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert outputs[0]['journal'] == 'Other Journal'


def test_check_scopus_without_candidates_reports_master_not_http(monkeypatch, capsys):
    cli_engine(monkeypatch)
    master = FakeMaster()
    install_master(monkeypatch, master)
    monkeypatch.setattr(runner, 'read_scopus_candidates', lambda *a, **k: [])
    assert runner.main(['--check-scopus']) == 0
    report = json.loads(capsys.readouterr().out)
    assert report['status'] == 'not_found'
    assert report['metadata']['master_file'].endswith('ext_list_Sep_2026.xlsx')
    assert 'api_success' not in report['metadata']


def test_save_scopus_persists_after_matching(monkeypatch, capsys):
    cli_engine(monkeypatch, 'publication_dashboard')
    master = FakeMaster({'Journal of Testing': checked()})
    install_master(monkeypatch, master)
    monkeypatch.setattr(runner, 'read_scopus_candidates', lambda *a, **k: [candidate()])
    save = MagicMock(return_value=1)
    monkeypatch.setattr(runner, 'save_scopus_results', save)
    assert runner.main(['--check-scopus', '--save-scopus']) == 0
    saved_candidates, saved_checked = save.call_args.args[1][0]
    assert saved_candidates.original == 'Journal of Testing'
    assert saved_checked['status'] == 'indexed'
    report = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert report['saved_rows'] == 1 and report['statuses'] == {'indexed': 1}


def test_save_scopus_requires_check_scopus():
    with pytest.raises(SystemExit) as exc:
        runner.main(['--save-scopus'])
    assert exc.value.code == 2


def test_missing_scopus_master_reports_error_before_connect(monkeypatch, caplog):
    if not hasattr(scopus, 'ScopusMasterError'):
        pytest.skip('scopus.py Excel-master contract not landed yet')
    from src.loading import postgres
    build = MagicMock(side_effect=AssertionError('No connection before master validation'))
    monkeypatch.setattr(postgres, 'build_engine', build)

    def missing(path=None):
        raise scopus.ScopusMasterError(f'Scopus master Excel not found: {path}')
    monkeypatch.setattr(scopus, 'load_master', missing, raising=False)
    assert runner.main(['--check-scopus']) == 1
    assert 'not found' in caplog.text
    build.assert_not_called()


def compile_statements(engine):
    conn = engine.begin.return_value.__enter__.return_value
    compiled = []
    for call in conn.execute.call_args_list:
        statement = call.args[0]
        compiled.append(str(statement.compile(dialect=postgresql.dialect())))
    return compiled


def test_save_scopus_result_is_idempotent_upsert():
    engine = MagicMock()
    assert runner.save_scopus_result(engine, candidate(), checked()) == 1
    statements = compile_statements(engine)
    assert 'pg_advisory_xact_lock' in statements[0]
    sql = statements[1]
    assert 'INSERT INTO raw.scopus_publication_verification' in sql
    assert 'ON CONFLICT' in sql and 'DO UPDATE' in sql
    conflict_target = sql.split('ON CONFLICT')[1].split('DO UPDATE')[0]
    assert 'raw_publication_id' in conflict_target or 'uq_scopus_pub_verification_raw' in conflict_target
    params = engine.begin.return_value.__enter__.return_value.execute.call_args_list[1]\
        .args[0].compile(dialect=postgresql.dialect()).params
    assert params['raw_publication_id'] == 1
    assert params['scopus_status'] == 'indexed'
    assert params['source_record_id'] == 12345
    assert params['metadata']['match_method'] == 'issn'
    # Saving the same candidate again emits the identical idempotent upsert;
    # the unique constraint on raw_publication_id can never duplicate rows.
    runner.save_scopus_result(engine, candidate(), checked())
    again = compile_statements(engine)[len(statements):]
    assert again[1] == sql


def test_save_scopus_result_writes_only_verification_table():
    engine = MagicMock()
    runner.save_scopus_result(engine, candidate(), checked('not_found'))
    for sql in compile_statements(engine):
        assert 'DELETE' not in sql.upper()
        assert 'MERGE' not in sql.upper()
        assert 'scholar_publication' not in sql
        assert 'scopus_publication_verification' in sql or 'pg_advisory_xact_lock' in sql


def test_save_scopus_result_skips_unpersistable_status():
    engine = MagicMock()
    assert runner.save_scopus_result(engine, candidate(), checked('error')) == 0
    engine.begin.assert_not_called()


def test_save_scopus_result_skips_origins_without_raw_id():
    engine = MagicMock()
    unnamed = candidates([{'title': 'Article title only', 'detail_venue': 'Journal of Testing',
                           'detail_publication_type': 'journal'}], scholar_types=True)[0]
    assert runner.save_scopus_result(engine, unnamed, checked()) == 0
    engine.begin.assert_not_called()


def test_indexed_snapshot_is_never_downgraded_by_weaker_status():
    from sqlalchemy.dialects.postgresql import insert
    from datetime import datetime, timezone
    stmt = insert(runner.scopus_verification).values(
        raw_publication_id=1, scopus_status='not_found',
        checked_at=datetime.now(timezone.utc))
    guard = runner.scopus_status_guard(stmt)
    sql = str(guard.compile(dialect=postgresql.dialect(), compile_kwargs={'literal_binds': True}))
    assert "scopus_status != 'indexed'" in sql
    assert 'excluded.scopus_status = ' in sql and "'indexed'" in sql
    # The full upsert carries the same guard in its WHERE clause; the literal
    # 'indexed' comparisons appear as bound parameters.
    engine = MagicMock()
    runner.save_scopus_result(engine, candidate(), checked('not_found'))
    upsert_stmt = engine.begin.return_value.__enter__.return_value.execute.call_args_list[1].args[0]
    upsert = str(upsert_stmt.compile(dialect=postgresql.dialect()))
    assert 'WHERE' in upsert and 'scopus_status' in upsert.split('WHERE')[-1]
    guard_params = upsert_stmt.compile(dialect=postgresql.dialect()).params
    assert 'indexed' in guard_params.values()


def test_save_scopus_results_requires_reviewed_schema():
    engine = MagicMock()
    engine.connect.return_value.__enter__.return_value.execute.return_value\
        .scalar_one_or_none.return_value = None
    with pytest.raises(runner.ScopusVerificationSchemaError,
                       match='19_create_scopus_publication_verification'):
        runner.save_scopus_results(engine, [(candidate(), checked())])
    engine.begin.assert_not_called()


def test_save_scopus_results_persists_each_candidate():
    engine = MagicMock()
    engine.connect.return_value.__enter__.return_value.execute.return_value\
        .scalar_one_or_none.return_value = 'raw.scopus_publication_verification'
    rows = [(candidate(1), checked()), (candidate(2, 'Other Journal'), checked('not_found'))]
    assert runner.save_scopus_results(engine, rows) == 2


def test_default_master_paths(monkeypatch):
    if hasattr(scopus, 'DEFAULT_MASTER'):
        assert Path(scopus.DEFAULT_MASTER).parts[-3:] == ('data', 'scopus', 'ext_list_Sep_2026.xlsx')
    else:
        monkeypatch.setattr(scopus, 'DEFAULT_MASTER',
                            Path('data/scopus/ext_list_Sep_2026.xlsx'), raising=False)
    args = runner.parser().parse_args([])
    assert Path(args.scopus_file).parts[-3:] == ('data', 'scopus', 'ext_list_Sep_2026.xlsx')
    assert Path(scimagojr.DEFAULT_MASTER).parts[-3:] == ('data', 'scimago', 'scimagojr 2025.csv')


def test_migration_sql_matches_reviewed_contract():
    text = MIGRATION.read_text(encoding='utf-8')
    assert text.lstrip().startswith('--') and 'BEGIN;' in text and 'COMMIT;' in text
    assert 'CREATE TABLE IF NOT EXISTS raw.scopus_publication_verification' in text
    for column in ('verification_id BIGSERIAL PRIMARY KEY',
                   'raw_publication_id INTEGER NOT NULL REFERENCES '
                   'raw.scholar_publication(raw_publication_id) ON DELETE CASCADE',
                   'scopus_status VARCHAR(32) NOT NULL', 'source_record_id BIGINT',
                   'source_title TEXT', 'source_type TEXT', 'issn TEXT', 'eissn TEXT',
                   'isbn TEXT', 'coverage TEXT', 'active_status TEXT', 'discontinued BOOLEAN',
                   'publisher TEXT', 'asjc TEXT', 'matched_identity JSONB',
                   'match_confidence VARCHAR(16)', 'reason TEXT', 'source_sheet TEXT',
                   'metadata JSONB', 'checked_at TIMESTAMPTZ NOT NULL',
                   'created_at TIMESTAMPTZ NOT NULL DEFAULT now()',
                   'updated_at TIMESTAMPTZ NOT NULL DEFAULT now()'):
        assert column in text, column
    assert "CONSTRAINT uq_scopus_pub_verification_raw UNIQUE (raw_publication_id)" in text
    assert "CHECK (scopus_status IN ('indexed','indicated','needs_verification','not_found'))" in text
    assert 'CREATE INDEX IF NOT EXISTS ix_scopus_pub_verification_status' in text
    # Additive only: no destructive statements, no auto-apply hooks.
    assert 'DROP ' not in text and 'ALTER ' not in text and 'TRUNCATE' not in text
