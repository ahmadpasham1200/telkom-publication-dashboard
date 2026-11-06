"""Limited inspection orchestration; all transport and database calls are fake."""
from copy import deepcopy
from datetime import datetime, timezone
import json
from unittest.mock import MagicMock

import pytest
import requests
import sqlalchemy as sa

from src.journal_index import run_journal_index as runner
from src.journal_index import crossref_check as check
from src.journal_index.crossref import CrossrefResult, parse_work


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('Real I/O forbidden')
    monkeypatch.setattr(requests.adapters.HTTPAdapter, 'send', forbidden)
    monkeypatch.setattr(sa.engine.Engine, 'connect', forbidden)
    monkeypatch.setattr(sa.engine.Engine, 'raw_connection', forbidden)


def publication(id=1, **fields):
    row = dict(raw_publication_id=id, title='Reliable metadata matching for scientific publications',
                detail_authors='Ana First, Budi Second', publication_year=2025,
                detail_journal='Journal of Examples')
    row.update(fields)
    return row


def retrieved(status='ambiguous', candidates=None):
    return CrossrefResult(status=status, lookup_method='bibliographic_search',
        fetched_at=datetime(2026, 1, 1, tzinfo=timezone.utc), reason='fixture',
        candidates=candidates or [], http_status=200, attempts=1)


def candidate():
    return parse_work({'DOI': '10.1234/example',
        'title': [publication()['title']], 'author': [{'given': 'Ana', 'family': 'First'},
        {'given': 'Budi', 'family': 'Second'}], 'published': {'date-parts': [[2025]]},
        'container-title': ['Journal of Examples'], 'type': 'journal-article'})


def test_selection_filters_deduplicates_orders_and_limits():
    rows = [publication(5), publication(2), publication(1, detail_doi='10.1234/other'),
            dict(raw_publication_id=3, title='  '), publication(4, detail_doi='10.1234/other'),
            publication(6, title='A different publication with independent metadata')]
    selected = check.select_publications(rows, 2, 'auto')
    assert [r['raw_publication_id'] for r in selected] == [1, 6]
    assert len(check.select_publications(rows, 1, 'auto')) == 1


def test_doi_selection_skips_absent_but_reports_malformed_doi():
    rows = [publication(1), publication(2, detail_doi='malformed')]
    assert [r['raw_publication_id'] for r in check.select_publications(rows, 3, 'doi')] == [2]


@pytest.mark.parametrize('source,doi,method', [
    ('doi', '10.1234/example', 'lookup_doi'), ('search', '10.1234/example', 'search_bibliographic'),
    ('auto', '10.1234/example', 'lookup_doi'), ('auto', None, 'search_bibliographic')])
def test_lookup_strategies(source, doi, method):
    client = MagicMock()
    getattr(client, method).return_value = retrieved()
    reports = list(check.inspect_publications([publication(detail_doi=doi)], client, source, 3))
    getattr(client, method).assert_called_once()
    assert reports[0]['status'] == 'not_found'
    other = 'lookup_doi' if method == 'search_bibliographic' else 'search_bibliographic'
    getattr(client, other).assert_not_called()


@pytest.mark.parametrize('status,works,expected', [
    ('ambiguous', [candidate()], 'matched'), ('ambiguous', [dict(candidate(), authors=[])], 'ambiguous'),
    ('not_found', [], 'not_found'), ('error', [], 'error')])
def test_matcher_output_contract(status, works, expected):
    client = MagicMock()
    client.search_bibliographic.return_value = retrieved(status, works)
    report = list(check.inspect_publications([publication()], client, 'search', 3))[0]
    assert report['status'] == expected and report['reason']
    assert report['candidate_count'] == len(works)
    assert report['raw_publication_id'] == 1
    assert 'author_evidence' in report and 'year_evidence' in report and 'container_evidence' in report
    assert report['http_status'] == 200
    assert json.loads(json.dumps(report)) == report
    assert ('10.1234/example' == report['selected_doi']) == (expected == 'matched')


def test_errors_do_not_stop_next_article_or_leak_exception():
    client = MagicMock()
    client.search_bibliographic.side_effect = [RuntimeError('password=secret'), retrieved()]
    reports = list(check.inspect_publications([publication(1), publication(2, title='Another distinct article title')], client, 'search', 3))
    assert [r['status'] for r in reports] == ['error', 'not_found']
    assert 'secret' not in json.dumps(reports)


def test_no_fallback_after_doi_failure():
    client = MagicMock()
    client.lookup_doi.return_value = retrieved('error')
    reports = list(check.inspect_publications([publication(detail_doi='10.1234/x')], client, 'auto', 3))
    assert reports[0]['status'] == 'error'
    client.search_bibliographic.assert_not_called()


def test_ordered_relation_and_input_unchanged():
    row = publication(publication_authors=[{'author_name': 'Budi Second', 'author_order': 1},
                                          {'author_name': 'Ana First', 'author_order': 0}])
    before = deepcopy(row)
    client = MagicMock()
    client.search_bibliographic.return_value = retrieved(candidates=[candidate()])
    report = list(check.inspect_publications([row], client, 'search', 3))[0]
    assert report['status'] == 'matched'
    assert [a['author_order'] for a in report['scholar_authors']] == [0, 1]
    assert row == before


def test_reader_enforces_read_only_materializes_and_does_not_group():
    engine = MagicMock()
    conn = engine.connect.return_value.__enter__.return_value
    conn.execute.side_effect = [MagicMock(), MagicMock(scalar_one=lambda: 'test_db'),
                                MagicMock(mappings=lambda: [publication()])]
    rows = check.read_publications(engine, 20, 'test_db')
    assert rows[0]['raw_publication_id'] == 1
    sql = [str(call.args[0]) for call in conn.execute.call_args_list]
    assert sql[0] == 'SET TRANSACTION READ ONLY'
    assert all(s.startswith('SELECT') or s == 'SET TRANSACTION READ ONLY' for s in sql)
    assert 'ORDER BY a.author_order' in sql[2] and 'LIMIT :row_limit' in sql[2]
    assert 'detail_title' in sql[2] and 'GROUP BY' not in sql[2]
    engine.begin.assert_not_called()


@pytest.fixture
def cli_setup(monkeypatch):
    import src.loading.postgres as postgres
    monkeypatch.setenv('POSTGRES_DB', 'test_db')
    engine = MagicMock()
    engine.url.database = 'test_db'
    engine.connect.return_value.__enter__.return_value.execute.return_value.scalar_one.return_value = 'test_db'
    monkeypatch.setattr(postgres, 'build_engine', lambda: engine)
    monkeypatch.setattr(runner, 'read_candidates', lambda *a, **k: [])
    client_class = MagicMock()
    client_class.return_value.search_bibliographic.return_value = retrieved()
    monkeypatch.setattr(check, 'CrossrefClient', client_class)
    for name in ('create_table', 'save_result', 'sync_catalog'):
        monkeypatch.setattr(runner, name, MagicMock(side_effect=AssertionError('No writes')))
    return engine, client_class


@pytest.mark.parametrize('args', [[], ['--dry-run']])
def test_normal_and_dry_run_never_construct_crossref(cli_setup, args):
    assert runner.main(args) == 0
    cli_setup[1].assert_not_called()


def test_cli_opt_in_bounded_output_and_close(cli_setup, monkeypatch, capsys):
    engine, factory = cli_setup
    rows = [publication(i, detail_doi=f'10.1234/{i}') for i in range(1, 6)]
    monkeypatch.setattr(check, 'read_publications', lambda *a, **k: rows)
    monkeypatch.setenv('CROSSREF_MAILTO', 'private@example.test')
    assert runner.main(['--check-crossref', '--crossref-limit', '2', '--crossref-source', 'search',
                        '--crossref-delay', '2']) == 0
    output = capsys.readouterr().out
    assert len(output.splitlines()) == 2 and 'private@example' not in output
    assert factory.return_value.search_bibliographic.call_count == 2
    assert factory.call_args.kwargs == {'mailto': 'private@example.test', 'min_interval': 2.0}
    factory.return_value.close.assert_called_once()
    engine.begin.assert_not_called()
    engine.dispose.assert_called_once()


def test_database_failure_stops_before_http(cli_setup, monkeypatch):
    monkeypatch.setattr(check, 'read_publications', MagicMock(side_effect=RuntimeError('db secret')))
    assert runner.main(['--check-crossref']) == 1
    cli_setup[1].assert_not_called()
    cli_setup[0].dispose.assert_called_once()


@pytest.mark.parametrize('extra', [
    ['--save'], ['--dry-run'], ['--sync-catalog'], ['--check-scopus'], ['--save-verification'],
    ['--crossref-limit', '0'], ['--crossref-limit', '11'], ['--crossref-delay', '-1'],
    ['--crossref-delay', 'nan'], ['--crossref-delay', 'inf']])
def test_invalid_modes_rejected_before_io(extra, cli_setup):
    with pytest.raises(SystemExit) as exc:
        runner.main(['--check-crossref', *extra])
    assert exc.value.code == 2
    cli_setup[1].assert_not_called()


def test_missing_title_and_doi_do_not_send_requests():
    client = MagicMock()
    assert list(check.inspect_publications([dict(raw_publication_id=1, title=' ')], client, 'search', 3)) == []
    assert list(check.inspect_publications([publication()], client, 'doi', 3)) == []
    client.lookup_doi.assert_not_called()
    client.search_bibliographic.assert_not_called()


def test_selection_keeps_different_authors_and_conflicting_dois():
    rows = [publication(1, detail_doi='10.1234/a'), publication(2, detail_doi='10.1234/b'),
            publication(3, detail_authors='Another Author'),
            publication(4, detail_doi='https://doi.org/10.1234/A')]
    assert [r['raw_publication_id'] for r in check.select_publications(rows, 10, 'auto')] == [1, 2, 3]


def test_missing_doi_cannot_suppress_later_populated_doi_identities():
    rows = [publication(1), publication(2, detail_doi='10.1234/a'),
            publication(3, detail_doi='10.1234/b')]
    assert [r['raw_publication_id'] for r in check.select_publications(rows, 10, 'auto')] == [1, 2, 3]


@pytest.mark.parametrize('fields', [
    {'detail_date': '2024/12/31'}, {'detail_publication_type': 'conference'},
    {'publication_authors': [{'author_order': 0, 'author_name': 'Ana First', 'daftar_terpotong': True}]},
])
def test_different_matcher_evidence_is_not_an_identical_snapshot(fields):
    rows = [publication(1, publication_authors=[{'author_order': 0, 'author_name': 'Ana First',
                                               'daftar_terpotong': False}])]
    changed = deepcopy(rows[0])
    changed.update(fields, raw_publication_id=2)
    assert len(check.select_publications([*rows, changed], 10, 'auto')) == 2


def test_title_fallback_and_missing_metadata_are_reported():
    row = dict(raw_publication_id=1, detail_title=' ', title=publication()['title'])
    client = MagicMock()
    client.search_bibliographic.return_value = retrieved(candidates=[candidate()])
    report = list(check.inspect_publications([row], client, 'auto', 3))[0]
    assert report['scholar_title'] == row['title'] and report['status'] == 'ambiguous'
    assert report['author_evidence'][0]['status'] == 'missing'


def test_reader_failure_to_enforce_readonly_never_executes_select():
    engine = MagicMock()
    conn = engine.connect.return_value.__enter__.return_value
    conn.execute.side_effect = RuntimeError('read-only setup denied')
    with pytest.raises(RuntimeError):
        check.read_publications(engine, 20, 'test_db')
    assert conn.execute.call_count == 1


def test_reader_database_mismatch_stops_before_raw_read():
    engine = MagicMock()
    conn = engine.connect.return_value.__enter__.return_value
    conn.execute.side_effect = [MagicMock(), MagicMock(scalar_one=lambda: 'other_db')]
    with pytest.raises(ValueError, match='Database connection mismatch'):
        check.read_publications(engine, 20, 'test_db')
    assert conn.execute.call_count == 2


def test_empty_window_does_not_construct_client(cli_setup, monkeypatch, capsys):
    monkeypatch.setattr(check, 'read_publications', lambda *a, **k: [])
    assert runner.main(['--check-crossref']) == 0
    cli_setup[1].assert_not_called()
    assert json.loads(capsys.readouterr().out)['articles_checked'] == 0


def test_crossref_mode_ignores_unrelated_csv_and_closes_before_http(cli_setup, monkeypatch):
    engine, factory = cli_setup
    conn = engine.connect.return_value.__enter__.return_value
    conn.execute.side_effect = [MagicMock(), MagicMock(scalar_one=lambda: 'test_db'),
                                MagicMock(mappings=lambda: [publication()])]
    def response(*args):
        engine.connect.return_value.__exit__.assert_called_once()
        return retrieved()
    factory.return_value.search_bibliographic.side_effect = response
    monkeypatch.setattr(runner.scimagojr, 'load_master', MagicMock(side_effect=AssertionError('No SCImago')))
    assert runner.main(['--check-crossref', '--source', 'scimago', '--scimago-csv', 'missing.csv']) == 0


def test_main_database_failure_has_sanitized_log(cli_setup, monkeypatch, caplog):
    monkeypatch.setattr(check, 'read_publications', MagicMock(side_effect=RuntimeError('password=secret')))
    assert runner.main(['--check-crossref']) == 1
    assert 'secret' not in caplog.text and 'failed' in caplog.text


def test_error_result_sets_exit_code_but_continues(cli_setup, monkeypatch, capsys):
    monkeypatch.setattr(check, 'read_publications', lambda *a, **k: [publication(1),
                        publication(2, title='Independent metadata verification study')])
    cli_setup[1].return_value.search_bibliographic.side_effect = [retrieved('error'), retrieved()]
    assert runner.main(['--check-crossref']) == 1
    assert [json.loads(line)['status'] for line in capsys.readouterr().out.splitlines()] == ['error', 'not_found']


def test_report_selected_conflicts_do_not_include_rejected_candidates():
    wrong = dict(candidate(), doi='10.1234/rejected', publication_dates={
        'published': {'date_parts': [2010], 'precision': 'year'}})
    client = MagicMock()
    client.search_bibliographic.return_value = retrieved(candidates=[wrong, candidate()])
    report = list(check.inspect_publications([publication()], client, 'search', 3))[0]
    assert report['status'] == 'matched' and report['conflicts'] == []
    assert report['selected_conflicts'] == [] and report['candidates_evaluated'] == 2
    assert report['selected_evidence']['authors']['status'] == 'strong'
    assert report['candidate_decisions'][0]['selection'] == 'rejected'
    assert 'year_conflict' in report['rejected_candidates'][0]['conflicts']
    assert report['candidate_decisions'][1]['reason'] == report['reason']


def test_report_jisebi_partial_match_keeps_all_crossref_authors():
    title = ('Aspect-based Sentiment and Correlation-based Emotion Detection on Tweets '
             'for Understanding Public Opinion of Covid-19')
    row = publication(detail_title=title, publication_year=2023, detail_date='2023-04-01',
        detail_journal='Journal of Information Systems Engineering & Business Intelligence',
        detail_authors='Salsabila Mazya Permataning Tyas; Yasinta Romadhona; Diana Purwitasari')
    work = parse_work({'DOI': '10.20473/jisebi.9.1.84-94', 'title': [title],
        'author': [{'given': 'Salsabila', 'family': 'Salsabila'},
                   {'given': 'Salsabila Mazya Permataning', 'family': 'Tyas'},
                   {'given': 'Yasinta', 'family': 'Romadhona'}, {'given': 'Diana', 'family': 'Purwitasari'}],
        'published': {'date-parts': [[2023, 4, 28]]},
        'container-title': ['Journal of Information Systems Engineering and Business Intelligence']})
    client = MagicMock()
    client.search_bibliographic.return_value = retrieved(candidates=[work])
    report = list(check.inspect_publications([row], client, 'search', 3))[0]
    assert report['status'] == 'matched' and report['title_similarity'] == 1.0
    assert report['selected_evidence']['authors']['status'] == 'partial'
    assert report['selected_evidence']['year']['status'] == 'consistent'
    assert report['selected_evidence']['container']['status'] == 'match'
    assert report['conflicts'] == [] and len(report['crossref_authors']) == 4
    assert report['selected_evidence']['authors'] == report['author_evidence'][0]


# --------------------------------------------------------------------------- #
# Filter scholar_id pada pembacaan RAW dan batas artikel unik per profil.      #
# --------------------------------------------------------------------------- #

def _reader_engine(rows):
    engine = MagicMock()
    conn = engine.connect.return_value.__enter__.return_value
    conn.execute.side_effect = [MagicMock(), MagicMock(scalar_one=lambda: 'test_db'),
                                MagicMock(mappings=lambda: rows)]
    return engine, conn


def test_read_publications_scholar_id_filters_query():
    engine, conn = _reader_engine([])
    check.read_publications(engine, 20, 'test_db', scholar_id='-zWuOpYAAAAJ')
    call = conn.execute.call_args_list[2]
    assert 'p.scholar_id = :scholar_id' in str(call.args[0])
    assert call.args[1] == {'row_limit': 20, 'scholar_id': '-zWuOpYAAAAJ'}


def test_read_publications_without_scholar_id_keeps_old_query():
    engine, conn = _reader_engine([])
    check.read_publications(engine, 20, 'test_db')
    call = conn.execute.call_args_list[2]
    assert 'scholar_id' not in str(call.args[0])
    assert call.args[1] == {'row_limit': 20}


def test_select_publications_limit_eight_unique_per_profile():
    rows = [publication(i, detail_doi=f'10.1234/{i}') for i in range(1, 13)]
    rows.append(publication(100, detail_doi='10.1234/1'))  # duplikat DOI id 1
    selected = check.select_publications(rows, 8, 'auto')
    assert [r['raw_publication_id'] for r in selected] == list(range(1, 9))


def test_run_inspection_forwards_scholar_id(monkeypatch, capsys):
    captured = {}
    def fake_read(engine, row_limit, target, scholar_id=None):
        captured.update(row_limit=row_limit, scholar_id=scholar_id)
        return []
    monkeypatch.setattr(check, 'read_publications', fake_read)
    assert check.run_inspection(MagicMock(), target='test_db', row_limit=50, limit=8,
                                source='auto', delay=1, scholar_id='ID_X') == 0
    assert captured == {'row_limit': 50, 'scholar_id': 'ID_X'}
    assert json.loads(capsys.readouterr().out)['articles_checked'] == 0
