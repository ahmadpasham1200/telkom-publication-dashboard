"""Offline manual Scopus document-evidence contracts; no HTTP, no DB, no real Excel."""
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import requests
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from src.journal_index import run_journal_index as runner
from src.journal_index import scopus
from src.journal_index.init import candidates, result

MIGRATION = Path(__file__).resolve().parents[1] / 'sql/ddl/20_create_scopus_manual_verification.sql'


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


def raw_row(**overrides):
    """A RAW publication row as produced by the runner's publication_rows query."""
    row = {'raw_publication_id': 1, 'scholar_id': 'S-1', 'title': 'A Study of Testing Methods',
           'venue': 'Journal of Testing', 'detail_venue': 'Journal of Testing',
           'publication_year': 2024, 'detail_doi': '10.1000/testing.1', 'detail_url': None,
           'issn': None, 'eissn': None, 'detail_issn': None, 'detail_eissn': None,
           'authors': 'John Doe, Alice Smith', 'detail_authors': 'John Doe; Alice Smith',
           'detail_journal': 'Journal of Testing', 'detail_publication_type': 'journal',
           'detail_venue_label': None}
    row.update(overrides)
    return row


def evidence(**overrides):
    record = {'raw_publication_id': 1, 'verification_source': 'scopus_document_search',
              'title': 'A Study of Testing Methods', 'first_author': 'Doe, J.',
              'source_title': 'Journal of Testing', 'publication_year': 2024,
              'verified': True, 'doi': '10.1000/testing.1', 'eid': '2-s2.0-85123456789'}
    record.update(overrides)
    return record


def candidate(raw_id=1, title='Journal of Testing', kind='journal', **fields):
    return candidates([{'raw_publication_id': raw_id, 'title': 'Article must not be matched',
                        'detail_venue': title, 'detail_publication_type': kind, **fields}],
                      scholar_types=True)[0]


def automatic_checked(status='indexed', **values):
    return result('scopus', status, 'Unique master identity matched', source_record_id=12345,
                  source_title='Journal of Testing', issn='0378-5955',
                  matched_identity={'source_id': '12345'},
                  metadata={'master_file': 'data/scopus/ext_list_Sep_2026.xlsx',
                            'master_sha256': 'fake-digest', 'match_method': 'issn'}, **values)


class FakeMaster:
    path = Path('data/scopus/ext_list_Sep_2026.xlsx')
    digest = 'fake-digest'

    def __init__(self, answers=None):
        self.answers = answers or {}

    def check(self, candidate):
        return self.answers.get(candidate.original,
                                result('scopus', 'not_found', 'No master identity matched'))


def cli_engine(monkeypatch, database='unit_test'):
    from src.loading import postgres
    engine = MagicMock()
    engine.url.database = database
    engine.connect.return_value.__enter__.return_value.execute.return_value.scalar_one.return_value = database
    monkeypatch.setenv('POSTGRES_DB', database)
    monkeypatch.setattr(postgres, 'build_engine', lambda: engine)
    return engine


def write_jsonl(tmp_path, lines, name='evidence.jsonl'):
    path = tmp_path / name
    path.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    return path


def compile_statements(engine):
    conn = engine.begin.return_value.__enter__.return_value
    return [str(call.args[0].compile(dialect=postgresql.dialect()))
            for call in conn.execute.call_args_list]


# --- Identity status rules -------------------------------------------------


def test_doi_and_eid_present_and_doi_matches_is_indexed():
    checked = scopus.check_manual_evidence(evidence(), raw_row())
    assert checked['status'] == 'indexed'
    assert checked['source'] == 'scopus_manual'
    assert checked['metadata']['scopus_status'] == 'indexed'
    assert checked['metadata']['evidence'] == 'manual'
    assert checked['metadata']['identity_method'] == 'doi'
    assert checked['indexed'] is True


def test_only_doi_matching_is_indexed():
    # Resolver URL form must normalize to the same DOI as the RAW detail_doi.
    record = evidence(doi='https://doi.org/10.1000/testing.1', eid=None)
    checked = scopus.check_manual_evidence(record, raw_row())
    assert checked['status'] == 'indexed'
    assert checked['metadata']['identity_method'] == 'doi'
    assert checked['matched_identity'] == '10.1000/testing.1'


def test_only_eid_with_matching_title_is_indexed():
    record = evidence(doi=None)
    checked = scopus.check_manual_evidence(record, raw_row())
    assert checked['status'] == 'indexed'
    assert checked['metadata']['identity_method'] == 'eid_title'
    assert checked['eid'] == '2-s2.0-85123456789'


def test_eid_with_mismatched_title_is_never_indexed():
    record = evidence(doi=None, title='A Completely Different Study')
    checked = scopus.check_manual_evidence(record, raw_row())
    assert checked['status'] == 'needs_verification'
    assert 'title' in checked['reason']


def test_conflicting_doi_beats_matching_eid_and_title():
    record = evidence(doi='10.1000/other-work')
    checked = scopus.check_manual_evidence(record, raw_row())
    assert checked['status'] == 'needs_verification'
    assert 'DOI' in checked['reason']


def test_evidence_doi_with_empty_raw_doi_and_full_bibliographic_identity_is_indexed():
    record = evidence(doi='10.1109/ISITIA63062.2024.10668160')
    checked = scopus.check_manual_evidence(record, raw_row(detail_doi=None))
    assert checked['status'] == 'indexed'
    assert checked['metadata']['identity_method'] == 'bibliographic'
    assert checked['metadata']['scopus_status'] == 'indexed'
    assert checked['indexed'] is True


def test_evidence_doi_with_empty_raw_doi_and_different_title_is_never_indexed():
    record = evidence(doi='10.1109/ISITIA63062.2024.10668160', title='A Different Study Entirely')
    checked = scopus.check_manual_evidence(record, raw_row(detail_doi=None))
    assert checked['status'] == 'needs_verification'
    assert 'title' in checked['reason']


def test_evidence_doi_with_empty_raw_doi_and_different_first_author_is_never_indexed():
    record = evidence(doi='10.1109/ISITIA63062.2024.10668160', first_author='Smith, B.')
    checked = scopus.check_manual_evidence(record, raw_row(detail_doi=None))
    assert checked['status'] == 'needs_verification'
    assert 'first_author' in checked['reason']


def test_evidence_doi_with_empty_raw_doi_and_different_year_is_never_indexed():
    record = evidence(doi='10.1109/ISITIA63062.2024.10668160', publication_year=2023)
    checked = scopus.check_manual_evidence(record, raw_row(detail_doi=None))
    assert checked['status'] == 'needs_verification'
    assert 'publication_year' in checked['reason']


def test_different_raw_doi_never_falls_back_to_bibliographic_matching():
    # Even a fully matching bibliographic identity must not override a DOI conflict.
    record = evidence(doi='10.1109/ISITIA63062.2024.10668160')
    checked = scopus.check_manual_evidence(record, raw_row(detail_doi='10.1000/testing.1'))
    assert checked['status'] == 'needs_verification'
    assert 'different' in checked['reason']


def test_equal_dois_still_index_with_doi_method():
    record = evidence(doi='https://doi.org/10.1000/testing.1')
    checked = scopus.check_manual_evidence(record, raw_row(detail_doi='10.1000/testing.1'))
    assert checked['status'] == 'indexed'
    assert checked['metadata']['identity_method'] == 'doi'
    assert checked['matched_identity'] == '10.1000/testing.1'


# --- Optional alternate_titles on evidence and RAW rows ----------------------


def test_evidence_alternate_title_matching_raw_title_is_indexed():
    record = evidence(doi='10.1109/ISITIA63062.2024.10668160',
                      title='Object Detection in Multi-Camera Streaming Video',
                      alternate_titles=['Resource-Aware Framework untuk Deteksi Objek'])
    row = raw_row(detail_doi=None,
                  title='Resource-Aware Framework untuk Deteksi Objek')
    checked = scopus.check_manual_evidence(record, row)
    assert checked['status'] == 'indexed'
    assert checked['metadata']['identity_method'] == 'bibliographic'
    assert checked['metadata']['scopus_status'] == 'indexed'


def test_row_alternate_titles_are_title_candidates():
    record = evidence(doi='10.1109/ISITIA63062.2024.10668160',
                      title='Object Detection in Multi-Camera Streaming Video')
    row = raw_row(detail_doi=None, title='Resource-Aware Framework untuk Deteksi Objek',
                  alternate_titles=['Object Detection in Multi-Camera Streaming Video'])
    checked = scopus.check_manual_evidence(record, row)
    assert checked['status'] == 'indexed'
    assert checked['metadata']['identity_method'] == 'bibliographic'


def test_alternate_titles_with_no_title_match_is_never_indexed():
    record = evidence(doi='10.1109/ISITIA63062.2024.10668160',
                      title='Main Title Without Match',
                      alternate_titles=['Alternate A', 'Alternate B'])
    checked = scopus.check_manual_evidence(record, raw_row(detail_doi=None))
    assert checked['status'] == 'needs_verification'
    assert 'title' in checked['reason']
    assert 'no title candidate' in checked['reason']


def test_empty_alternate_titles_behave_as_absent():
    record = evidence(doi='10.1109/ISITIA63062.2024.10668160', alternate_titles=[])
    checked = scopus.check_manual_evidence(record, raw_row(detail_doi=None))
    assert checked['status'] == 'indexed'
    assert checked['metadata']['identity_method'] == 'bibliographic'


def test_alternate_titles_do_not_loosen_mandatory_fields():
    # An alternate-title match never compensates for a mandatory-field conflict.
    record = evidence(doi='10.1109/ISITIA63062.2024.10668160',
                      alternate_titles=['A Study of Testing Methods'],
                      first_author='Smith, B.')
    checked = scopus.check_manual_evidence(record, raw_row(detail_doi=None))
    assert checked['status'] == 'needs_verification'
    assert 'first_author' in checked['reason']


def test_loader_tolerates_absent_and_null_alternate_titles(tmp_path):
    path = write_jsonl(tmp_path, [
        json.dumps(evidence(raw_publication_id=1)),
        json.dumps(evidence(raw_publication_id=2, alternate_titles=None)),
        json.dumps(evidence(raw_publication_id=3, alternate_titles=[' Variant Title '])),
    ])
    by_id, _duplicates = scopus.load_manual_evidence(path)
    assert by_id[1]['alternate_titles'] == []
    assert by_id[2]['alternate_titles'] == []
    assert by_id[3]['alternate_titles'] == ['Variant Title']


def test_alternate_titles_must_be_list_of_nonempty_strings(tmp_path):
    cases = [
        ('notalist', evidence(raw_publication_id=2,
                              alternate_titles='Resource-Aware Framework')),
        ('intitem', evidence(raw_publication_id=3, alternate_titles=['ok', 42])),
        ('emptyitem', evidence(raw_publication_id=4, alternate_titles=['ok', '   '])),
    ]
    for name, record in cases:
        path = write_jsonl(tmp_path, [json.dumps(evidence(raw_publication_id=1)),
                                      json.dumps(record)], name=f'bad_alt_{name}.jsonl')
        with pytest.raises(scopus.ScopusEvidenceError, match='line 2.*alternate_titles'):
            scopus.load_manual_evidence(path)


def test_no_doi_eid_full_identity_match_is_indexed():
    record = evidence(doi=None, eid=None)
    checked = scopus.check_manual_evidence(record, raw_row())
    assert checked['status'] == 'indexed'
    assert checked['metadata']['identity_method'] == 'title_author_year_source'


def test_matching_title_but_different_first_author_is_never_indexed():
    record = evidence(doi=None, eid=None, first_author='Smith, B.')
    checked = scopus.check_manual_evidence(record, raw_row())
    assert checked['status'] == 'needs_verification'
    assert 'first_author' in checked['reason']
    assert checked['metadata']['scopus_status'] == 'needs_verification'


def test_different_year_is_never_indexed():
    record = evidence(doi=None, eid=None, publication_year=2023)
    checked = scopus.check_manual_evidence(record, raw_row())
    assert checked['status'] == 'needs_verification'
    assert 'publication_year' in checked['reason']


def test_different_source_title_is_never_indexed():
    record = evidence(doi=None, eid=None, source_title='Another Journal')
    checked = scopus.check_manual_evidence(record, raw_row())
    assert checked['status'] == 'needs_verification'
    assert 'source_title' in checked['reason']


def test_isbn_or_proceedings_title_corroboration_is_never_indexed():
    # Venue-level corroboration (ISBN / proceedings title) never proves the article.
    record = evidence(doi=None, eid=None, first_author='Smith, B.',
                      isbn='978-0-123456-78-9',
                      proceedings_title='Journal of Testing Proceedings')
    checked = scopus.check_manual_evidence(record, raw_row())
    assert checked['status'] == 'needs_verification'
    assert checked['metadata']['scopus_status'] == 'needs_verification'
    assert checked['indexed'] is not True


def test_verified_false_is_never_indexed():
    checked = scopus.check_manual_evidence(evidence(verified=False), raw_row())
    assert checked['status'] == 'needs_verification'
    assert 'not verified' in checked['reason']


def test_person_matching_tolerates_doe_j_versus_john_doe():
    assert scopus.person_matches('Doe, J.', 'John Doe')
    assert scopus.person_matches('John Doe', 'Doe, J.')
    assert scopus.person_matches('Doe, J.', 'Jane Doe')
    assert not scopus.person_matches('Doe, J.', 'Smith, J.')
    assert not scopus.person_matches('Doe, J.', 'Doe, A.')
    assert scopus.normalize_person('  José  García-López! ') == 'josé garcía lópez'


def test_unmatched_manual_evidence_is_reported_not_dropped():
    checked = scopus.unmatched_manual_evidence(evidence(raw_publication_id=99))
    assert checked['status'] == 'needs_verification'
    assert checked['reason'] == 'raw_publication_not_in_window'
    assert checked['source'] == 'scopus_manual'
    assert checked['metadata']['scopus_status'] == 'needs_verification'


# --- JSONL evidence loading -------------------------------------------------


def test_loads_multi_line_jsonl_and_skips_blank_lines(tmp_path):
    path = write_jsonl(tmp_path, [
        json.dumps(evidence(raw_publication_id=1)),
        '',
        '   ',
        json.dumps(evidence(raw_publication_id=2, title='Second Study')),
    ])
    by_id, duplicates = scopus.load_manual_evidence(path)
    assert set(by_id) == {1, 2}
    assert by_id[2]['title'] == 'Second Study'
    assert duplicates == 0


def test_doi_and_eid_may_be_null_empty_or_absent(tmp_path):
    path = write_jsonl(tmp_path, [
        json.dumps(evidence(raw_publication_id=1, doi=None, eid=None)),
        json.dumps(evidence(raw_publication_id=2, doi='', eid='')),
        json.dumps({key: value for key, value in evidence(raw_publication_id=3).items()
                    if key not in ('doi', 'eid')}),
    ])
    by_id, _duplicates = scopus.load_manual_evidence(path)
    assert by_id[1]['doi'] is None and by_id[1]['eid'] is None
    assert by_id[2]['doi'] is None and by_id[2]['eid'] is None
    assert by_id[3]['doi'] is None and by_id[3]['eid'] is None


def test_duplicate_raw_publication_id_keeps_first_and_counts_rest(tmp_path):
    path = write_jsonl(tmp_path, [
        json.dumps(evidence(raw_publication_id=1, title='First Title')),
        json.dumps(evidence(raw_publication_id=1, title='Second Title')),
        json.dumps(evidence(raw_publication_id=1, title='Third Title')),
    ])
    by_id, duplicates = scopus.load_manual_evidence(path)
    assert by_id[1]['title'] == 'First Title'
    assert duplicates == 2


def test_malformed_json_line_raises_with_line_number(tmp_path):
    path = write_jsonl(tmp_path, [
        json.dumps(evidence(raw_publication_id=1)),
        '{not valid json',
    ])
    with pytest.raises(scopus.ScopusEvidenceError, match='line 2'):
        scopus.load_manual_evidence(path)


def test_missing_required_field_raises_with_line_number(tmp_path):
    broken = {key: value for key, value in evidence(raw_publication_id=2).items()
              if key != 'source_title'}
    path = write_jsonl(tmp_path, [json.dumps(evidence(raw_publication_id=1)),
                                  json.dumps(broken)])
    with pytest.raises(scopus.ScopusEvidenceError, match='line 2.*source_title'):
        scopus.load_manual_evidence(path)


def test_wrong_types_raise_with_line_number(tmp_path):
    cases = [
        ('verified', evidence(raw_publication_id=2, verified='yes'), r'line 2.*verified'),
        ('publication_year', evidence(raw_publication_id=3, publication_year='2024'),
         r'line 2.*publication_year'),
        ('raw_publication_id', evidence(raw_publication_id='4'), r'line 2.*raw_publication_id'),
        ('doi', evidence(raw_publication_id=5, doi=12345), r'line 2.*doi'),
    ]
    for name, record, pattern in cases:
        # A valid first line keeps the reported line number meaningful.
        path = write_jsonl(tmp_path, [json.dumps(evidence(raw_publication_id=1)),
                                      json.dumps(record)], name=f'bad_{name}.jsonl')
        with pytest.raises(scopus.ScopusEvidenceError, match=pattern):
            scopus.load_manual_evidence(path)


def test_missing_evidence_file_names_the_path(tmp_path):
    missing = tmp_path / 'missing.jsonl'
    with pytest.raises(scopus.ScopusEvidenceError, match='missing.jsonl'):
        scopus.load_manual_evidence(missing)


# --- Persistence: manual and automatic tables stay separate ------------------


def test_manual_save_writes_only_manual_table():
    engine = MagicMock()
    checked = scopus.check_manual_evidence(evidence(), raw_row())
    assert runner.save_scopus_manual_result(engine, evidence(), checked) == 1
    statements = compile_statements(engine)
    assert 'pg_advisory_xact_lock' in statements[0]
    sql = statements[1]
    assert 'INSERT INTO raw.scopus_manual_verification' in sql
    assert 'ON CONFLICT' in sql and 'DO UPDATE' in sql
    assert 'uq_scopus_manual_verification_raw' in sql
    params = engine.begin.return_value.__enter__.return_value.execute.call_args_list[1]\
        .args[0].compile(dialect=postgresql.dialect()).params
    assert params['raw_publication_id'] == 1
    assert params['scopus_status'] == 'indexed'
    assert params['verified'] is True
    assert params['verification_source'] == 'scopus_document_search'
    assert params['doi'] == '10.1000/testing.1'
    for statement in statements:
        assert 'DELETE' not in statement.upper()
        assert 'MERGE' not in statement.upper()
        assert 'scholar_publication' not in statement
        assert 'scopus_publication_verification' not in statement


def test_automatic_save_writes_only_automatic_table():
    engine = MagicMock()
    assert runner.save_scopus_result(engine, candidate(), automatic_checked()) == 1
    for statement in compile_statements(engine):
        assert 'scopus_manual_verification' not in statement
        assert 'scholar_publication' not in statement
        assert 'DELETE' not in statement.upper()


def test_manual_save_uses_distinct_advisory_namespace():
    engine = MagicMock()
    checked = scopus.check_manual_evidence(evidence(), raw_row())
    runner.save_scopus_manual_result(engine, evidence(), checked)
    lock_params = engine.begin.return_value.__enter__.return_value\
        .execute.call_args_list[0].args[1]
    assert lock_params == {'namespace': 166846, 'publication_id': 1}


def test_manual_save_skips_unpersistable_status_and_missing_raw_id():
    engine = MagicMock()
    # needs_verification is a persistable internal status.
    not_verified = scopus.check_manual_evidence(evidence(verified=False), raw_row())
    assert not_verified['status'] == 'needs_verification'
    assert runner.save_scopus_manual_result(engine, evidence(verified=False),
                                            not_verified) == 1
    # A missing raw_publication_id is never persistable.
    fresh = MagicMock()
    assert runner.save_scopus_manual_result(fresh, {'raw_publication_id': None},
                                            automatic_checked()) == 0
    fresh.begin.assert_not_called()


def test_save_scopus_manual_results_requires_reviewed_schema():
    engine = MagicMock()
    engine.connect.return_value.__enter__.return_value.execute.return_value\
        .scalar_one_or_none.return_value = None
    with pytest.raises(runner.ScopusManualVerificationSchemaError,
                       match='20_create_scopus_manual_verification'):
        runner.save_scopus_manual_results(
            engine, [(evidence(), scopus.check_manual_evidence(evidence(), raw_row()))])
    engine.begin.assert_not_called()


def test_save_scopus_manual_results_persists_each_pair():
    engine = MagicMock()
    engine.connect.return_value.__enter__.return_value.execute.return_value\
        .scalar_one_or_none.return_value = 'raw.scopus_manual_verification'
    pairs = [(evidence(raw_publication_id=1), scopus.check_manual_evidence(evidence(), raw_row())),
             (evidence(raw_publication_id=2, verified=False),
              scopus.check_manual_evidence(evidence(raw_publication_id=2, verified=False),
                                           raw_row(raw_publication_id=2)))]
    assert runner.save_scopus_manual_results(engine, pairs) == 2


# --- Migration 20 contract ---------------------------------------------------


def test_migration_sql_matches_reviewed_contract():
    text = MIGRATION.read_text(encoding='utf-8')
    assert text.lstrip().startswith('--') and 'BEGIN;' in text and 'COMMIT;' in text
    assert 'CREATE TABLE IF NOT EXISTS raw.scopus_manual_verification' in text
    for column in ('verification_id BIGSERIAL PRIMARY KEY',
                   'raw_publication_id INTEGER NOT NULL REFERENCES '
                   'raw.scholar_publication(raw_publication_id) ON DELETE CASCADE',
                   'verification_source VARCHAR(64) NOT NULL', 'title TEXT',
                   'first_author TEXT', 'source_title TEXT', 'publication_year INTEGER',
                   'doi TEXT', 'eid TEXT', 'verified BOOLEAN NOT NULL',
                   'scopus_status VARCHAR(32) NOT NULL', 'matched_identity JSONB',
                   'reason TEXT', 'metadata JSONB', 'checked_at TIMESTAMPTZ NOT NULL',
                   'created_at TIMESTAMPTZ NOT NULL DEFAULT now()',
                   'updated_at TIMESTAMPTZ NOT NULL DEFAULT now()'):
        assert column in text, column
    assert "CONSTRAINT uq_scopus_manual_verification_raw UNIQUE (raw_publication_id)" in text
    assert "CHECK (scopus_status IN ('indexed','indicated','needs_verification','not_found'))" in text
    assert 'CREATE INDEX IF NOT EXISTS ix_scopus_manual_verification_status' in text
    # Additive only: no destructive statements, no auto-apply hooks.
    assert 'DROP ' not in text and 'ALTER ' not in text and 'TRUNCATE' not in text


# --- CLI integration ---------------------------------------------------------


def test_scopus_evidence_file_default_is_none():
    args = runner.parser().parse_args([])
    assert args.scopus_evidence_file is None


def test_scopus_evidence_file_requires_check_scopus():
    with pytest.raises(SystemExit) as exc:
        runner.main(['--scopus-evidence-file', 'evidence.jsonl'])
    assert exc.value.code == 2


def test_dry_run_with_evidence_is_read_only_and_marks_provenance(monkeypatch, capsys, tmp_path):
    engine = cli_engine(monkeypatch)
    master = FakeMaster()
    monkeypatch.setattr(scopus, 'load_master', lambda path=None: master)
    monkeypatch.setattr(runner, 'read_scopus_candidates', lambda *a, **k: [])
    monkeypatch.setattr(runner, 'scopus_rows_with_enrichment',
                        lambda *a, **k: [raw_row()])
    save_auto = MagicMock(side_effect=AssertionError('No automatic writes in dry-run'))
    save_manual = MagicMock(side_effect=AssertionError('No manual writes in dry-run'))
    monkeypatch.setattr(runner, 'save_scopus_results', save_auto)
    monkeypatch.setattr(runner, 'save_scopus_manual_results', save_manual)
    path = write_jsonl(tmp_path, [json.dumps(evidence())])
    assert runner.main(['--check-scopus', '--scopus-evidence-file', str(path)]) == 0
    outputs = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    manual = [o for o in outputs if o.get('evidence') == 'manual']
    assert manual and manual[0]['source'] == 'scopus_manual'
    assert manual[0]['raw_publication_id'] == 1
    assert manual[0]['verification_source'] == 'scopus_document_search'
    assert manual[0]['status'] == 'indexed'
    summary = [o for o in outputs if o.get('source') == 'scopus_manual' and 'statuses' in o]
    assert summary and summary[0]['dry_run'] is True and summary[0]['saved_rows'] == 0
    save_auto.assert_not_called()
    save_manual.assert_not_called()
    engine.begin.assert_not_called()


def test_evidence_for_raw_id_outside_window_is_reported_not_dropped(monkeypatch, capsys,
                                                                    tmp_path):
    cli_engine(monkeypatch)
    monkeypatch.setattr(scopus, 'load_master', lambda path=None: FakeMaster())
    monkeypatch.setattr(runner, 'read_scopus_candidates', lambda *a, **k: [])
    monkeypatch.setattr(runner, 'scopus_rows_with_enrichment',
                        lambda *a, **k: [raw_row()])
    path = write_jsonl(tmp_path, [json.dumps(evidence(raw_publication_id=99))])
    assert runner.main(['--check-scopus', '--scopus-evidence-file', str(path)]) == 0
    outputs = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    unmatched = [o for o in outputs if o.get('evidence') == 'manual'
                 and o.get('raw_publication_id') == 99]
    assert unmatched and unmatched[0]['status'] == 'needs_verification'
    assert unmatched[0]['reason'] == 'raw_publication_not_in_window'
    summary = [o for o in outputs if o.get('source') == 'scopus_manual' and 'unmatched' in o]
    assert summary and summary[0]['unmatched'] == 1


def test_missing_evidence_file_fails_before_connect(monkeypatch, caplog, tmp_path):
    from src.loading import postgres
    build = MagicMock(side_effect=AssertionError('No connection before evidence validation'))
    monkeypatch.setattr(postgres, 'build_engine', build)
    missing = tmp_path / 'absent.jsonl'
    assert runner.main(['--check-scopus', '--scopus-evidence-file', str(missing)]) == 1
    assert 'absent.jsonl' in caplog.text
    build.assert_not_called()


def test_save_scopus_persists_automatic_and_manual_results(monkeypatch, capsys, tmp_path):
    engine = cli_engine(monkeypatch, 'publication_dashboard')
    master = FakeMaster({'Journal of Testing': automatic_checked()})
    monkeypatch.setattr(scopus, 'load_master', lambda path=None: master)
    monkeypatch.setattr(runner, 'read_scopus_candidates',
                        lambda *a, **k: [candidate()])
    monkeypatch.setattr(runner, 'scopus_rows_with_enrichment',
                        lambda *a, **k: [raw_row()])
    save_auto = MagicMock(return_value=1)
    save_manual = MagicMock(return_value=1)
    monkeypatch.setattr(runner, 'save_scopus_results', save_auto)
    monkeypatch.setattr(runner, 'save_scopus_manual_results', save_manual)
    path = write_jsonl(tmp_path, [json.dumps(evidence())])
    assert runner.main(['--check-scopus', '--save-scopus',
                        '--scopus-evidence-file', str(path)]) == 0
    save_auto.assert_called_once()
    save_manual.assert_called_once()
    saved_evidence, saved_checked = save_manual.call_args.args[1][0]
    assert saved_evidence['raw_publication_id'] == 1
    assert saved_checked['status'] == 'indexed'
    outputs = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    manual_report = [o for o in outputs if o.get('source') == 'scopus_manual'
                     and 'saved_rows' in o]
    assert manual_report and manual_report[0]['saved_rows'] == 1
    assert manual_report[0]['dry_run'] is False


def test_save_scopus_without_evidence_file_keeps_legacy_behavior(monkeypatch, capsys):
    engine = cli_engine(monkeypatch, 'publication_dashboard')
    monkeypatch.setattr(scopus, 'load_master', lambda path=None: FakeMaster())
    monkeypatch.setattr(runner, 'read_scopus_candidates',
                        lambda *a, **k: [candidate()])
    save_auto = MagicMock(return_value=1)
    save_manual = MagicMock(side_effect=AssertionError('No manual save without evidence file'))
    monkeypatch.setattr(runner, 'save_scopus_results', save_auto)
    monkeypatch.setattr(runner, 'save_scopus_manual_results', save_manual)
    assert runner.main(['--check-scopus', '--save-scopus']) == 0
    save_auto.assert_called_once()
    save_manual.assert_not_called()
    outputs = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert not [o for o in outputs if o.get('evidence') == 'manual']
