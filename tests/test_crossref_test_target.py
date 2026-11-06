"""Explicit test target opt-in is verified without opening a real connection."""
import json
from unittest.mock import MagicMock

import pytest
from sqlalchemy.engine import URL

from src.journal_index import crossref_target as guard
from src.journal_index import run_journal_index as runner
from test_crossref_persistence import offline


def target():
    return dict(host='127.0.0.1', port=55439, database='crossref_test_stage56', user='crossref_test_writer',
                server_address='127.0.0.1', server_port=55439, cluster_name='crossref-test-' + 'a' * 32)


@pytest.fixture
def configured(tmp_path, monkeypatch):
    entry = target()
    path = tmp_path / 'allowlist.json'
    path.write_text(json.dumps({'version': 1, 'targets': [entry]}), encoding='utf-8')
    for key, field in [('POSTGRES_HOST', 'host'), ('POSTGRES_PORT', 'port'),
                       ('POSTGRES_DB', 'database'), ('POSTGRES_USER', 'user')]:
        monkeypatch.setenv(key, str(entry[field]))
    return path


def test_explicit_allowlisted_test_target(configured):
    assert guard.load_target(configured, target()['database']) == target()


@pytest.mark.parametrize('key,value', [('POSTGRES_HOST', 'localhost'), ('POSTGRES_PORT', '5432'),
    ('POSTGRES_DB', 'publication_dashboard'), ('POSTGRES_USER', 'etl_writer'),
    ('POSTGRES_DB', 'crossref_test_unlisted')])
def test_configuration_mismatch_is_rejected(configured, monkeypatch, key, value):
    monkeypatch.setenv(key, value)
    with pytest.raises(ValueError):
        guard.load_target(configured, target()['database'])


@pytest.mark.parametrize('key', ['POSTGRES_HOST', 'POSTGRES_PORT', 'POSTGRES_DB', 'POSTGRES_USER'])
def test_missing_environment_never_falls_back_to_dotenv(configured, monkeypatch, key):
    monkeypatch.delenv(key)
    with pytest.raises(ValueError):
        guard.load_target(configured, target()['database'])


@pytest.mark.parametrize('field,value', [('database', 'publication_dashboard'), ('database', 'other_database'),
    ('host', 'production.example.test'), ('port', 5432), ('user', 'postgres'), ('cluster_name', '')])
def test_unsafe_allowlist_entry_rejected(configured, field, value):
    entry = dict(target(), **{field: value})
    configured.write_text(json.dumps({'version': 1, 'targets': [entry]}))
    with pytest.raises(ValueError):
        guard.load_target(configured, entry['database'])


def fake_engine():
    engine = MagicMock()
    t = target()
    engine.url = URL.create('postgresql+psycopg', username=t['user'], host=t['host'], port=t['port'], database=t['database'])
    return engine


def server():
    t = target()
    return dict(database=t['database'], session_user=t['user'], current_user=t['user'],
                server_address=t['server_address'], server_port=t['server_port'],
                cluster_name=t['cluster_name'], superuser=False)


def test_engine_and_server_target_verified_readonly():
    engine = fake_engine()
    conn = engine.connect.return_value.__enter__.return_value
    conn.execute.side_effect = [MagicMock(), MagicMock(mappings=lambda: MagicMock(one=server))]
    guard.verify_target(engine, target())
    assert str(conn.execute.call_args_list[0].args[0]) == 'SET TRANSACTION READ ONLY'
    engine.begin.assert_not_called()


@pytest.mark.parametrize('field,value', [('database', 'publication_dashboard'), ('session_user', 'postgres'),
    ('current_user', 'postgres'), ('server_address', '192.0.2.1'), ('server_port', 5432),
    ('cluster_name', 'production'), ('superuser', True)])
def test_server_mismatch_prevents_pipeline(field, value):
    engine = fake_engine()
    answer = dict(server(), **{field: value})
    conn = engine.connect.return_value.__enter__.return_value
    conn.execute.side_effect = [MagicMock(), MagicMock(mappings=lambda: MagicMock(one=lambda: answer))]
    with pytest.raises(ValueError):
        guard.verify_target(engine, target())


@pytest.mark.parametrize('field,value', [('port', 5432), ('host', '192.0.2.1'),
    ('username', 'etl_writer'), ('database', 'publication_dashboard')])
def test_engine_mismatch_rejected_before_connection(field, value):
    engine = fake_engine()
    engine.url = engine.url.set(**{field: value})
    with pytest.raises(ValueError):
        guard.verify_target(engine, target())
    engine.connect.assert_not_called()


def test_cli_test_write_dispatches_only_after_verification(configured, monkeypatch):
    import src.loading.postgres as postgres
    from src.journal_index import crossref_pipeline
    engine = fake_engine()
    monkeypatch.setattr(postgres, 'build_engine', lambda: engine)
    calls = []
    monkeypatch.setattr(guard, 'verify_target', lambda *a: calls.append('verify'))
    monkeypatch.setattr(crossref_pipeline, 'run', lambda *a: calls.append('run') or 0)
    assert runner.main(['--enrich-crossref', '--save-crossref', '--test-db-allowlist', str(configured),
                       '--confirm-test-db', target()['database']]) == 0
    assert calls == ['verify', 'run']


@pytest.mark.parametrize('args', [
    ['--enrich-crossref', '--test-db-allowlist', 'missing.json'],
    ['--enrich-crossref', '--confirm-test-db', 'crossref_test_stage56'],
    ['--save', '--test-db-allowlist', 'missing.json', '--confirm-test-db', 'crossref_test_stage56'],
    ['--dry-run', '--confirm-test-db', 'crossref_test_stage56'],
])
def test_invalid_optin_fails_before_engine(args, monkeypatch):
    import src.loading.postgres as postgres
    build = MagicMock(side_effect=AssertionError('No connection'))
    monkeypatch.setattr(postgres, 'build_engine', build)
    with pytest.raises(SystemExit):
        runner.main(args)
    build.assert_not_called()


def test_default_production_write_policy_is_unchanged():
    runner.validate_database_name('publication_dashboard', writing=True)
    with pytest.raises(ValueError):
        runner.validate_database_name('crossref_test_stage56', writing=True)


def test_production_name_cannot_enter_test_cli(configured, monkeypatch):
    import src.loading.postgres as postgres
    build = MagicMock(side_effect=AssertionError('No DB'))
    monkeypatch.setattr(postgres, 'build_engine', build)
    with pytest.raises(SystemExit) as exc:
        runner.main(['--enrich-crossref', '--save-crossref', '--test-db-allowlist', str(configured),
                     '--confirm-test-db', 'publication_dashboard'])
    assert exc.value.code == 2
    build.assert_not_called()
