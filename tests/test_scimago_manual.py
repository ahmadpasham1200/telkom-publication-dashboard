"""Manual SCImago CLI tests; database calls are mocked and no migrations run."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from src.journal_index import run_journal_index as runner
from src.journal_index import scimago_manual as manual
from src.journal_index.init import result


def item():
    return dict(journal_id=7, journal_original='Journal of Testing',
                issn='0378-5955', eissn='0309-0566', reason='needs review')


def entry(**overrides):
    values = dict(confirmed_title='Journal of Testing', issn='0378-5955',
                  eissn='0309-0566', decision='verified', sjr=None,
                  quartile=None, evidence_url='https://example.org/evidence',
                  notes='Confirmed manually')
    values.update(overrides)
    return values


@pytest.mark.parametrize('value,expected', [
    ('0378-5955', '0378-5955'), ('03090566', '0309-0566'), ('', None),
])
def test_issn_validation(value, expected):
    assert manual.validate_issn(value) == expected


@pytest.mark.parametrize('value', ['1234-5678', '0378-595', '0378/5955'])
def test_invalid_issn_is_rejected(value):
    with pytest.raises(ValueError):
        manual.validate_issn(value)


@pytest.mark.parametrize('value,expected', [
    ('', None), ('1.25', '1.25'), ('0', None), ('-0.1', None),
    ('nan', None), ('text', None),
])
def test_sjr_validation(value, expected):
    if expected is None and value:
        with pytest.raises(ValueError):
            manual.validate_sjr(value)
    else:
        parsed = manual.validate_sjr(value)
        assert (str(parsed) if parsed is not None else None) == expected


@pytest.mark.parametrize('value,expected', [
    ('', None), ('-', None), ('kosong', None), ('KOSONG', None),
    ('empty', None), ('EmPtY', None), ('none', None),
    ('q1', 'Q1'), ('Q2', 'Q2'), ('q3', 'Q3'), ('Q4', 'Q4'),
])
def test_quartile_validation(value, expected):
    assert manual.validate_quartile(value) == expected


@pytest.mark.parametrize('value', ['Q0', 'Q5', 'A1', 'Q1.5'])
def test_invalid_quartile_is_rejected(value):
    with pytest.raises(ValueError):
        manual.validate_quartile(value)


@pytest.mark.parametrize('value,expected', [
    ('', None), ('https://www.scimagojr.com/journalsearch.php?q=1',
                 'https://www.scimagojr.com/journalsearch.php?q=1'),
])
def test_url_validation(value, expected):
    assert manual.validate_url(value) == expected


@pytest.mark.parametrize('value', ['www.scimagojr.com', 'ftp://example.org', 'https://bad host'])
def test_invalid_url_is_rejected(value):
    with pytest.raises(ValueError):
        manual.validate_url(value)


def test_lists_only_scimago_unverified_and_requires_manual_schema():
    engine = MagicMock()
    conn = engine.connect.return_value.__enter__.return_value
    conn.execute.side_effect = [
        SimpleNamespace(scalar_one_or_none=lambda: 'raw.scimago_manual_verification'),
        SimpleNamespace(mappings=lambda: [item()]),
    ]
    assert manual.list_unverified(engine) == [item()]
    sql = str(conn.execute.call_args_list[1].args[0].compile(dialect=postgresql.dialect()))
    assert "source = %(source_1)s" in sql and "status = %(status_1)s" in sql

    missing = MagicMock()
    missing.connect.return_value.__enter__.return_value.execute.return_value.scalar_one_or_none.return_value = None
    with pytest.raises(manual.ManualScimagoSchemaError, match='21_create_scimago_manual_verification'):
        manual.list_unverified(missing)


def test_cli_selects_journal_and_saves_after_confirmation(monkeypatch):
    monkeypatch.setattr(manual, 'list_unverified', lambda engine: [item()])
    save = MagicMock(return_value=42)
    monkeypatch.setattr(manual, 'save_manual', save)
    answers = iter(['7', 'Journal of Testing', '0378-5955', '', 'verified',
                    '0.125', 'kosong', 'https://example.org/evidence', 'Confirmed', 'y', ''])
    output = []
    assert manual.run(MagicMock(), lambda _: next(answers), output.append) == 0
    save.assert_called_once()
    assert save.call_args.args[1] == 7
    assert save.call_args.args[2]['decision'] == 'verified'
    assert save.call_args.args[2]['quartile'] is None
    assert any('audit id: 42' in line for line in output)


@pytest.mark.parametrize('choice', ['', 'selesai', 'EXIT', 'q'])
def test_menu_exit_words_return_normally(monkeypatch, choice):
    monkeypatch.setattr(manual, 'list_unverified', lambda engine: [item()])
    output = []
    assert manual.run(MagicMock(), lambda _: choice, output.append) == 0
    assert 'Kembali ke menu utama.' in output


def test_invalid_journal_id_is_rejected_then_prompt_repeats(monkeypatch):
    monkeypatch.setattr(manual, 'list_unverified', lambda engine: [item()])
    answers = iter(['abc', ''])
    output = []
    assert manual.run(MagicMock(), lambda _: next(answers), output.append) == 0
    assert any('masukkan angka journal_id' in line for line in output)


def test_entry_retries_invalid_name_status_quartile_and_url():
    answers = iter(['', 'Journal Confirmed', '', '', 'other', 'verified', '',
                    'Q5', 'q2', 'ftp://bad', 'https://example.org', '', 'n'])
    output = []
    collected = manual.collect_entry(item(), lambda _: next(answers), output.append)
    assert collected['confirmed_title'] == 'Journal Confirmed'
    assert collected['decision'] == 'verified' and collected['quartile'] == 'Q2'
    assert collected['evidence_url'] == 'https://example.org'
    assert not collected['confirmed']
    assert sum('Input tidak valid:' in line for line in output) == 4
    assert any('INPUT VERIFIKASI MANUAL SCIMAGO' in line for line in output)
    assert any('Contoh: Journal of Information Systems' in line for line in output)


def test_verified_without_url_warns_without_fabricating_values():
    answers = iter(['Verified Journal', '', '', 'verified', '', 'none', '', '', 'y'])
    output = []
    collected = manual.collect_entry(item(), lambda _: next(answers), output.append)
    assert collected['confirmed'] is True
    assert collected['sjr'] is None and collected['quartile'] is None
    assert collected['evidence_url'] is None
    assert any('verified dipilih tanpa URL bukti' in line for line in output)
    assert any('Quartile: tidak tersedia (NULL)' in line for line in output)


def test_manual_scimago_flag_routes_to_interactive_runner(monkeypatch):
    from src.loading import postgres

    engine = MagicMock()
    engine.url.database = 'publication_dashboard'
    engine.connect.return_value.__enter__.return_value.execute.return_value.scalar_one.return_value = 'publication_dashboard'
    monkeypatch.setenv('POSTGRES_DB', 'publication_dashboard')
    monkeypatch.setattr(postgres, 'build_engine', lambda: engine)
    run = MagicMock(return_value=0)
    monkeypatch.setattr(manual, 'run', run)
    assert runner.main(['--manual-scimago']) == 0
    run.assert_called_once_with(engine)


def test_confirmation_cancel_does_not_save(monkeypatch):
    monkeypatch.setattr(manual, 'list_unverified', lambda engine: [item()])
    save = MagicMock()
    monkeypatch.setattr(manual, 'save_manual', save)
    answers = iter(['7', 'Journal of Testing', '', '', 'unverified', '', '', '', '', 'n', ''])
    assert manual.run(MagicMock(), lambda _: next(answers), lambda _: None) == 0
    save.assert_not_called()


def test_manual_save_appends_history_and_projects_current_status(monkeypatch):
    engine = MagicMock()
    conn = engine.begin.return_value.__enter__.return_value
    audit_ids = iter([11, 12, 13])
    statements = []

    def execute(statement, *args, **kwargs):
        statements.append(statement)
        sql = str(statement)
        if 'to_regclass' in sql:
            return SimpleNamespace(scalar_one_or_none=lambda: 'raw.scimago_manual_verification')
        if isinstance(statement, sa.sql.Select):
            return SimpleNamespace(scalar_one_or_none=lambda: 7)
        if 'INSERT INTO raw.scimago_manual_verification' in sql:
            return SimpleNamespace(scalar_one=lambda: next(audit_ids))
        return SimpleNamespace()

    conn.execute.side_effect = execute
    assert manual.save_manual(engine, 7, entry()) == 11
    assert manual.save_manual(engine, 7, entry(decision='unverified')) == 12
    assert manual.save_manual(engine, 7, entry(decision='not_journal')) == 13
    history_inserts = [stmt for stmt in statements
                       if 'INSERT INTO raw.scimago_manual_verification' in str(stmt.compile(
                           dialect=postgresql.dialect()))]
    assert len(history_inserts) == 3
    assert history_inserts[0].compile(dialect=postgresql.dialect()).params['sjr'] is None
    assert all('ON CONFLICT' not in str(stmt.compile(dialect=postgresql.dialect()))
               for stmt in history_inserts)
    projections = [stmt.compile(dialect=postgresql.dialect()) for stmt in statements
                   if 'journal_index_verification' in str(stmt)]
    assert [stmt.params['status'] for stmt in projections] == [
        'verified', 'unverified', 'not_found']
    assert projections[0].params['metadata']['verification_type'] == 'manual'
    assert projections[2].params['metadata']['manual_status'] == 'not_journal'


def test_automatic_scimago_save_does_not_overwrite_manual(monkeypatch):
    engine = MagicMock()
    conn = engine.begin.return_value.__enter__.return_value
    conn.execute.return_value.scalar_one_or_none.side_effect = [
        'raw.scimago_manual_verification', 11]
    upsert = MagicMock()
    monkeypatch.setattr(runner, 'upsert_catalog', upsert)
    runner.save_result(engine, SimpleNamespace(normalized='journal of testing', original='Journal of Testing',
                         identifiers=set(), publications=[]),
                       result('scimagojr', 'indexed', evidence_url='https://example.org',
                              metadata={'method': 'scimago_master_csv', 'identity_match': True,
                                        'source_id': '1', 'reference_year': 2025,
                                        'match_method': 'title_exact'}))
    upsert.assert_called_once()
    verification_writes = [call.args[0] for call in conn.execute.call_args_list
                           if isinstance(call.args[0], sa.sql.dml.Insert)
                           and call.args[0].table is runner.table]
    assert not verification_writes


def test_manual_migration_is_append_only_and_allows_manual_verified_without_url():
    migration = Path(__file__).resolve().parents[1] / 'sql/ddl/21_create_scimago_manual_verification.sql'
    sql = migration.read_text()
    assert 'PRIMARY KEY' in sql and 'UNIQUE (journal_id' not in sql
    assert "metadata->>'verification_type' = 'manual'" in sql
    assert 'decision IN (\'verified\', \'unverified\', \'not_journal\')' in sql
