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
    monkeypatch.setattr(scopus.requests.Session, 'request', forbidden)


def journal(name='Journal of Testing'):
    return candidates([{'raw_publication_id': 1, 'detail_venue': name}])[0]


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


def test_scopus_positive_and_no_inference():
    candidate = journal()
    entry = {'dc:title': candidate.original, 'source-id': '123', 'prism:issn': '0378-5955',
             'prism:aggregationType': 'journal'}
    payload = {'serial-metadata-response': {'entry': [entry]}}
    assert scopus.parse_response(candidate, payload)['status'] == 'indexed'
    entry.pop('source-id')
    assert scopus.parse_response(candidate, payload)['status'] == 'unverified'
    assert scopus.parse_response(candidate, {})['status'] == 'error'
    assert result('scopus', 'not_found')['indexed'] is None
    assert result('scopus', 'not_indexed')['indexed'] is False


def test_missing_credentials(monkeypatch):
    monkeypatch.delenv('SCOPUS_API_KEY', raising=False)
    monkeypatch.delenv('SINTA_USERNAME', raising=False)
    monkeypatch.delenv('SINTA_PASSWORD', raising=False)
    assert scopus.check(journal())['status'] == 'access_unavailable'
    assert sinta.check(journal())['status'] == 'access_unavailable'


def test_scopus_request_security_and_retry(monkeypatch):
    monkeypatch.setenv('SCOPUS_API_KEY', 'secret-sentinel')
    client = MagicMock()
    client.get.side_effect = [scopus.requests.Timeout('secret-sentinel'),
                              SimpleNamespace(status_code=403)]
    sleep = MagicMock()
    checked = scopus.check(journal(), session=client, sleep=sleep)
    assert checked['status'] == 'access_unavailable'
    assert 'secret-sentinel' not in str(checked)
    assert client.get.call_args.kwargs['allow_redirects'] is False
    assert client.get.call_args.kwargs['timeout'] == (5, 20)
    sleep.assert_called_once_with(1)


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
    monkeypatch.setattr(scopus.requests.Session, 'get', MagicMock(side_effect=AssertionError))
    import src.loading.postgres as postgres
    build = MagicMock(side_effect=AssertionError)
    monkeypatch.setattr(postgres, 'build_engine', build)
    with pytest.raises(SystemExit) as exc:
        runner.main(['--help'])
    assert exc.value.code == 0
    build.assert_not_called()


def test_dry_run_never_writes_or_checks(monkeypatch, capsys):
    import src.loading.postgres as postgres
    engine = MagicMock()
    engine.url.database = 'test_db'
    engine.connect.return_value.__enter__.return_value.execute.return_value.scalar_one.return_value = 'test_db'
    monkeypatch.setenv('POSTGRES_DB', 'test_db')
    monkeypatch.setattr(postgres, 'build_engine', lambda: engine)
    monkeypatch.setattr(runner, 'read_candidates', lambda *a: [
        journal(), journal('IEEE Access'), journal('Telkom University')])
    http = MagicMock(side_effect=AssertionError('No HTTP during preview'))
    monkeypatch.setattr(scopus.requests.sessions.Session, 'request', http)
    ddl = MagicMock(side_effect=AssertionError)
    checks = MagicMock(side_effect=AssertionError)
    monkeypatch.setattr(runner, 'create_table', ddl)
    monkeypatch.setattr(runner, 'run_checks', checks)
    assert runner.main(['--dry-run']) == 0
    ddl.assert_not_called()
    checks.assert_not_called()
    engine.begin.assert_not_called()
    http.assert_not_called()
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
def test_no_keyword_identity_matching_uses_source_evidence(monkeypatch, title):
    candidate = journal(title)
    # Synthetic API fixture only; this is not a live index assertion about these titles.
    payload = {'serial-metadata-response': {'entry': [{
        'dc:title': title, 'source-id': 'test-source', 'prism:aggregationType': 'journal'}]}}
    positive = scopus.parse_response(candidate, payload)
    assert positive['status'] == 'indexed' and positive['evidence_url'] == scopus.API
    payload['serial-metadata-response']['entry'][0]['dc:title'] = title + ' Different'
    assert scopus.parse_response(candidate, payload)['status'] == 'unverified'
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
    assert saved['scopus']['evidence_url'] == scopus.API
    assert saved['scimagojr']['indexed'] is None
    assert saved['sinta']['indexed'] is None


def test_all_sources_continue_without_credentials(monkeypatch):
    for key in ('SCOPUS_API_KEY', 'SCIMAGO_EVIDENCE_FILE', 'SINTA_USERNAME', 'SINTA_PASSWORD'):
        monkeypatch.delenv(key, raising=False)
    save = MagicMock()
    monkeypatch.setattr(runner, 'save_result', save)
    counts, errors = runner.run_checks(None, [journal()], list(runner.SOURCES), delay=0)
    assert counts == {'access_unavailable': 3} and errors == 0
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


def test_scopus_retry_is_bounded_and_redacted(monkeypatch):
    monkeypatch.setenv('SCOPUS_API_KEY', 'secret-sentinel')
    client = MagicMock()
    client.get.side_effect = scopus.requests.Timeout('secret-sentinel')
    sleep = MagicMock()
    checked = scopus.check(journal(), session=client, sleep=sleep)
    assert checked['status'] == 'access_unavailable'
    assert client.get.call_count == 3
    assert [call.args[0] for call in sleep.call_args_list] == [1, 2]
    assert 'secret-sentinel' not in str(checked)


def test_scopus_issn_never_falls_back_to_title():
    candidate = journal()
    candidate.identifiers = {'0378-5955'}
    payload = {'serial-metadata-response': {'entry': [{
        'dc:title': candidate.original, 'source-id': 'fixture',
        'prism:aggregationType': 'journal'}]}}
    checked = scopus.parse_response(candidate, payload)
    assert checked['status'] == 'unverified'
    assert checked['metadata']['identity_match'] is False


def test_scopus_partial_title_and_duplicate_identity_are_unverified():
    entry = {'dc:title': 'Journal of Testing Extended', 'source-id': 'fixture',
             'prism:aggregationType': 'journal'}
    payload = {'serial-metadata-response': {'entry': [entry]}}
    checked = scopus.parse_response(journal(), payload)
    assert checked['status'] == 'unverified'
    assert checked['metadata']['results_returned'] == 1
    entry['dc:title'] = 'Journal of Testing'
    payload['serial-metadata-response']['entry'].append(dict(entry, **{'source-id': 'other'}))
    checked = scopus.parse_response(journal(), payload)
    assert checked['status'] == 'unverified'
    assert checked['metadata']['match_count'] == 2


def test_scopus_http_diagnostics_and_optional_token(monkeypatch):
    monkeypatch.setenv('SCOPUS_API_KEY', 'secret-key')
    monkeypatch.setenv('SCOPUS_INST_TOKEN', 'secret-token')
    client = MagicMock()
    client.get.return_value.status_code = 200
    client.get.return_value.json.return_value = {'serial-metadata-response': {'entry': []}}
    checked = scopus.check(journal(), session=client)
    assert checked['status'] == 'not_found'
    assert checked['metadata']['http_status'] == 200
    assert checked['metadata']['api_success'] is True
    assert checked['metadata']['results_returned'] == 0
    assert 'content' not in client.get.call_args.kwargs['params']
    assert 'X-ELS-Insttoken' not in client.get.call_args.kwargs['headers']
    scopus.check(journal(), session=client, use_inst_token=True)
    assert client.get.call_args.kwargs['headers']['X-ELS-Insttoken'] == 'secret-token'
    assert 'secret-' not in str(checked)


def test_check_scopus_without_key_reports_unavailable_without_http(monkeypatch, capsys):
    monkeypatch.setenv('SCOPUS_API_KEY', ' ')
    import src.loading.postgres as postgres
    engine = MagicMock()
    engine.url.database = 'test_db'
    engine.connect.return_value.__enter__.return_value.execute.return_value.scalar_one.return_value = 'test_db'
    monkeypatch.setenv('POSTGRES_DB', 'test_db')
    monkeypatch.setattr(postgres, 'build_engine', lambda: engine)
    monkeypatch.setattr(runner, 'read_candidates', lambda *a: [journal()])
    monkeypatch.setattr(runner.time, 'sleep', lambda _: None)
    http = MagicMock(side_effect=AssertionError('No API without key'))
    monkeypatch.setattr(scopus.requests.Session, 'get', http)
    assert runner.main(['--check-scopus']) == 0
    report = json.loads(capsys.readouterr().out)
    assert report['status'] == 'access_unavailable'
    assert report['metadata']['request_attempted'] is False
    http.assert_not_called()


def test_check_scopus_caps_two_journals_without_writes(monkeypatch, capsys):
    import src.loading.postgres as postgres
    engine = MagicMock()
    engine.url.database = 'test_db'
    engine.connect.return_value.__enter__.return_value.execute.return_value.scalar_one.return_value = 'test_db'
    monkeypatch.setenv('POSTGRES_DB', 'test_db')
    monkeypatch.setenv('SCOPUS_API_KEY', 'secret-key')
    monkeypatch.setattr(postgres, 'build_engine', lambda: engine)
    monkeypatch.setattr(runner, 'read_candidates', lambda *a: [
        journal('Telkom University'), journal('Unresolved Venue'),
        journal('Journal A'), journal('Journal B'), journal('Journal C')])
    check = MagicMock(return_value=result('scopus', 'access_unavailable', metadata={
        'http_status': 403, 'api_success': False, 'request_attempted': True,
        'results_returned': None, 'identity_match': None}))
    monkeypatch.setattr(scopus, 'check', check)
    monkeypatch.setattr(runner.time, 'sleep', lambda _: None)
    ddl, save = MagicMock(), MagicMock()
    monkeypatch.setattr(runner, 'create_table', ddl)
    monkeypatch.setattr(runner, 'save_result', save)
    assert runner.main(['--check-scopus', '--limit', '100']) == 0
    assert [call.args[0].original for call in check.call_args_list] == ['Journal A', 'Journal B']
    ddl.assert_not_called()
    save.assert_not_called()
    engine.begin.assert_not_called()
    assert 'secret-key' not in capsys.readouterr().out


def test_scopus_issn_match_takes_precedence_over_changed_title(monkeypatch):
    candidate = journal()
    candidate.identifiers = {'0378-5955'}
    payload = {'serial-metadata-response': {'entry': [{
        'dc:title': 'Changed title', 'prism:eIssn': '0378-5955',
        'source-id': '123', 'prism:aggregationType': 'journal'}]}}
    checked = scopus.parse_response(candidate, payload)
    assert checked['status'] == 'indexed'
    assert checked['metadata']['match_method'] == 'issn'
    assert checked['metadata']['identity_match'] is True
    monkeypatch.setenv('SCOPUS_API_KEY', 'secret-key')
    client = MagicMock()
    client.get.return_value.status_code = 403
    checked = scopus.check(candidate, session=client)
    assert client.get.call_args.kwargs['params']['issn'] == '0378-5955'
    assert 'title' not in client.get.call_args.kwargs['params']
    assert checked['metadata']['http_status'] == 403
    assert checked['metadata']['api_success'] is False
    assert checked['metadata']['results_returned'] is None
    assert checked['indexed'] is None


def test_scopus_title_page_limit_and_malformed_response_are_not_positive():
    entry = {'dc:title': 'Journal of Testing', 'source-id': '123',
             'prism:aggregationType': 'journal'}
    entries = [entry] + [dict(entry, **{'dc:title': 'Other ' + str(i)}) for i in range(24)]
    checked = scopus.parse_response(journal(), {'serial-metadata-response': {'entry': entries}})
    assert checked['status'] == 'unverified'
    assert checked['metadata']['results_returned'] == 25
    assert scopus.parse_response(journal(), {'serial-metadata-response': {'entry': 'bad'}})['status'] == 'error'


def test_check_scopus_cannot_enable_save_or_dry_run():
    for mode in ('--save', '--dry-run'):
        with pytest.raises(SystemExit) as exc:
            runner.main(['--check-scopus', mode])
        assert exc.value.code == 2


@pytest.mark.parametrize('container', [
    {'error': 'No results found', 'link': []},
    {'error': 'No results found for the search query', 'link': []},
    {'entry': []},
    {'entry': None, 'error': 'No results found'},
])
def test_scopus_valid_empty_responses(container, monkeypatch):
    monkeypatch.setenv('SCOPUS_API_KEY', 'secret-key')
    client = MagicMock()
    client.get.return_value.status_code = 200
    client.get.return_value.json.return_value = {'serial-metadata-response': container}
    checked = scopus.check(journal(), session=client)
    assert checked['status'] == 'not_found'
    assert checked['indexed'] is None
    assert checked['metadata']['results_returned'] == 0
    assert checked['metadata']['identity_match'] is False
    assert checked['metadata']['http_status'] == 200
    assert checked['metadata']['api_success'] is True


@pytest.mark.parametrize('payload', [None, [], {},
    {'serial-metadata-response': []},
    {'serial-metadata-response': {}},
    {'serial-metadata-response': {'entry': None}},
    {'serial-metadata-response': {'entry': [None]}},
    {'serial-metadata-response': {'entry': {'dc:title': 42}}},
    {'serial-metadata-response': {'entry': {'dc:title': ''}}},
    {'serial-metadata-response': {'entry': {'dc:title': 'Journal of Testing', 'prism:issn': []}}},
    {'serial-metadata-response': {'entry': {'dc:title': 'Journal of Testing', 'source-id': {'token': 'secret-sentinel'}}}},
    {'serial-metadata-response': {'error': 'Unknown failure secret-sentinel'}},
    {'serial-metadata-response': {'error': {'statusText': 'secret-sentinel'}}},
    {'serial-metadata-response': {'error': 'No results found', 'entry': 'invalid'}},
    {'serial-metadata-response': {'error': 'No results found', 'entry': [{'dc:title': 'Journal of Testing'}]}},
])
def test_scopus_invalid_payloads_fail_closed(payload, caplog):
    checked = scopus.parse_response(journal(), payload)
    assert checked['status'] == 'error'
    assert checked['indexed'] is None
    assert checked['metadata']['results_returned'] is None
    assert 'secret-sentinel' not in str(checked) + caplog.text


@pytest.mark.parametrize('as_list', [False, True])
def test_scopus_documented_entry_forms(as_list):
    candidate = journal()
    candidate.identifiers = {'0378-5955'}
    entry = {'dc:title': candidate.original, 'prism:issn': '0378-5955',
             'prism:aggregationType': 'journal', 'source-id': '123'}
    payload = {'serial-metadata-response': {'entry': [entry] if as_list else entry}}
    checked = scopus.parse_response(candidate, payload)
    assert checked['status'] == 'indexed'
    assert checked['metadata']['identity_match'] is True
    assert checked['metadata']['match_method'] == 'issn'


def test_scopus_title_encoding_and_safe_diagnostics(monkeypatch, caplog):
    from urllib.parse import parse_qs, urlsplit
    import logging
    monkeypatch.setenv('SCOPUS_API_KEY', 'secret-key-sentinel')
    monkeypatch.setenv('SCOPUS_INST_TOKEN', 'secret-token-sentinel')
    candidate = journal('Journal of Information Systems Engineering & Business Intelligence')
    client = MagicMock()
    client.get.return_value.status_code = 200
    client.get.return_value.json.return_value = {'serial-metadata-response': {'error': 'No results found'}}
    with caplog.at_level(logging.INFO, logger=scopus.__name__):
        checked = scopus.check(candidate, session=client, use_inst_token=True)
    params = client.get.call_args.kwargs['params']
    prepared = scopus.requests.Request('GET', scopus.API, params=params).prepare()
    assert '%26' in prepared.url
    assert '%2526' not in prepared.url
    assert parse_qs(urlsplit(prepared.url).query)['title'] == [candidate.original]
    assert set(params) == {'title', 'count', 'view'}
    assert checked['metadata']['query_type'] == 'title'
    assert checked['metadata']['query_parameters'] == params
    assert checked['status'] == 'not_found' and checked['indexed'] is None
    assert 'not evidence' in checked['reason']
    assert 'query_type' in caplog.text and 'results_returned' in caplog.text
    assert 'identity_match' in caplog.text and 'http_status' in caplog.text
    for secret in ('secret-key-sentinel', 'secret-token-sentinel', 'X-ELS-APIKey', 'X-ELS-Insttoken'):
        assert secret not in caplog.text + str(checked['metadata'])


def test_scopus_issn_diagnostics_and_404_is_not_empty_search(monkeypatch, caplog):
    import logging
    monkeypatch.setenv('SCOPUS_API_KEY', 'secret-key-sentinel')
    candidate = journal()
    candidate.identifiers = {'0378-5955'}
    client = MagicMock()
    client.get.return_value.status_code = 404
    with caplog.at_level(logging.INFO, logger=scopus.__name__):
        checked = scopus.check(candidate, session=client)
    assert checked['status'] == 'unverified'
    assert checked['metadata']['query_type'] == 'issn'
    assert checked['metadata']['query_parameters']['issn'] == '0378-5955'
    assert 'title' not in checked['metadata']['query_parameters']
    assert checked['metadata']['results_returned'] is None
    assert checked['metadata']['http_status'] == 404
    assert 'secret-key-sentinel' not in caplog.text


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
    checked = adapter.check(candidate)
    assert checked['status'] == expected
    assert checked['indexed'] is (True if expected == 'indexed' else None)
    if expected == 'indexed':
        if adapter is scimagojr:
            assert checked['sjr'] == [{'quartile': 'Q2', 'year': 2024, 'category': 'Testing'}]
        else:
            assert checked['sinta_rank'] == 'S3'
            assert checked['sinta_period'] == '2023-2027'


def test_manual_title_rule_does_not_change_general_or_scopus_issn_matching():
    candidate = journal()
    candidate.identifiers = {'0378-5955'}
    assert matches(candidate, 'Different Journal', ['0378-5955']) is True
    checked = scopus.parse_response(candidate, {'serial-metadata-response': {'entry': [{
        'dc:title': 'Different Journal', 'prism:eIssn': '0378-5955',
        'prism:aggregationType': 'journal', 'source-id': '123'}]}})
    assert checked['status'] == 'indexed'
    assert checked['metadata']['identity_match'] is True
