"""Crossref foundation fixtures and offline contracts: no HTTP or database I/O."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from urllib.parse import unquote

import pytest
import requests

from src.journal_index.crossref import (
    CrossrefClient, CrossrefResult, normalize_doi, parse_response, parse_work,
)


@pytest.fixture(autouse=True)
def forbid_live_io(monkeypatch):
    import sqlalchemy as sa
    def forbidden(*args, **kwargs):
        raise AssertionError('Live HTTP/database access is forbidden')
    monkeypatch.setattr(requests.Session, 'request', forbidden)
    monkeypatch.setattr(requests.adapters.HTTPAdapter, 'send', forbidden)
    monkeypatch.setattr(sa.engine.Engine, 'connect', forbidden)
    monkeypatch.setattr(sa.engine.Engine, 'raw_connection', forbidden)


@pytest.fixture
def work():
    return {
        'DOI': '10.1234/Example.(A)',
        'title': ['An Example Article', 'Judul Terjemahan'],
        'author': [{'given': 'A.', 'family': 'First', 'sequence': 'first',
                    'ORCID': 'https://orcid.org/0000-0000-0000-0000'},
                   {'given': 'Budi', 'family': 'Second', 'sequence': 'additional'}],
        'published': {'date-parts': [[2025]]},
        'published-online': {'date-parts': [[2024, 12]]},
        'published-print': {'date-parts': [[2025, 2, 28]]},
        'issued': {'date-parts': [[2024, 12]]},
        'container-title': ['Journal of Examples', 'Alternative Journal Title'],
        'ISSN': ['0309-0566', '0378-5955'],
        'issn-type': [{'value': '0378-5955', 'type': 'print'},
                      {'value': '0309-0566', 'type': 'electronic'}],
        'type': 'journal-article', 'publisher': 'Example Publisher',
        'URL': 'https://doi.org/10.1234/Example.(A)',
    }


def lookup_payload(work):
    return {'status': 'ok', 'message-type': 'work', 'message': deepcopy(work)}


def search_payload(works):
    return {'status': 'ok', 'message-type': 'work-list', 'message': {'items': deepcopy(works)}}


class FakeResponse:
    def __init__(self, payload=None, status=200, headers=None, broken_json=False):
        self.payload, self.status_code = payload, status
        self.headers = headers or {}
        self.broken_json = broken_json

    def json(self):
        if self.broken_json:
            raise ValueError('secret-sentinel malformed response body')
        return deepcopy(self.payload)


class FakeSession:
    def __init__(self, *events):
        self.events, self.calls = list(events), []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        event = self.events.pop(0)
        if isinstance(event, Exception):
            raise event
        return event


class FakeClock:
    def __init__(self):
        self.seconds, self.sleeps = 0.0, []

    def monotonic(self):
        return self.seconds

    def now(self):
        return datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=self.seconds)

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.seconds += seconds


def client_for(*events, **kwargs):
    session, clock = FakeSession(*events), FakeClock()
    return CrossrefClient(session=session, sleep=clock.sleep, monotonic=clock.monotonic,
                          now=clock.now, **kwargs), session, clock


def test_full_lookup_contract_and_no_automatic_match(work):
    client, session, clock = client_for(FakeResponse(lookup_payload(work)))
    result = client.lookup_doi('https://doi.org/10.1234/Example.(A)')
    assert isinstance(result, CrossrefResult)
    assert result.status == 'ambiguous' and result.reason == 'metadata_retrieved_not_matched'
    assert result.lookup_method == 'doi_lookup' and result.http_status == 200
    assert result.fetched_at == clock.now() and result.attempts == 1
    parsed = result.candidates[0]
    assert parsed['doi'] == work['DOI']
    assert parsed['titles'] == work['title']
    assert parsed['authors'] == work['author']
    assert parsed['container_titles'] == work['container-title']
    assert parsed['type'] == 'journal-article'
    assert parsed['publisher'] == 'Example Publisher'
    assert parsed['url'] == work['URL']
    assert parsed['score'] is None
    assert result.to_dict()['fetched_at'].endswith('Z')
    assert len(session.calls) == 1
    assert not any(key.startswith('detail_') for key in parsed)


@pytest.mark.parametrize('status', ['matched', 'ambiguous', 'not_found', 'error'])
def test_result_status_contract(status):
    result = CrossrefResult(status=status, lookup_method='doi_lookup',
                           fetched_at=datetime(2026, 1, 1, tzinfo=timezone.utc), reason='contract_only')
    assert result.status == status
    with pytest.raises(ValueError):
        CrossrefResult(status='indexed', lookup_method='doi_lookup',
                       fetched_at=datetime.now(timezone.utc), reason='invalid')


def test_optional_fields_and_independent_copies():
    item = {'DOI': '10.1234/minimal', 'title': None, 'author': [], 'ISSN': None,
            'container-title': [], 'publisher': None, 'URL': '', 'issued': None}
    parsed = parse_work(item)
    for key in ('titles', 'authors', 'issns', 'issn_types', 'container_titles'):
        assert parsed[key] == []
    assert parsed['publication_dates'] == {}
    assert parsed['type'] is None and parsed['publisher'] is None and parsed['url'] is None
    parsed['titles'].append('Independent')
    assert parse_work({'DOI': '10.1234/other'})['titles'] == []


def test_arrays_authors_and_issn_order_are_preserved(work):
    parsed = parse_work(work)
    assert parsed['authors'][0]['family'] == 'First'
    assert parsed['authors'][1]['given'] == 'Budi'
    assert parsed['issns'] == ['0309-0566', '0378-5955']
    assert parsed['issn_types'] == work['issn-type']
    without_types = deepcopy(work)
    without_types.pop('issn-type')
    assert parse_work(without_types)['issn_types'] == []
    parsed['authors'][0]['given'] = 'Changed'
    assert work['author'][0]['given'] == 'A.'


@pytest.mark.parametrize('parts,precision', [([2025], 'year'), ([2025, 2], 'month'), ([2024, 2, 29], 'day')])
def test_partial_publication_dates(parts, precision):
    parsed = parse_work({'DOI': '10.1234/date', 'published': {'date-parts': [parts]}})
    assert parsed['publication_dates']['published'] == {'date_parts': parts, 'precision': precision}


@pytest.mark.parametrize('value,expected', [
    (' 10.1234/ABC.(x); ', '10.1234/ABC.(x);'),
    ('doi:10.1234/ABC', '10.1234/ABC'),
    ('https://doi.org/10.1234/a%2Fb%3Fx%3D1%26y%23part', '10.1234/a/b?x=1&y#part'),
    ('HTTP://DX.DOI.ORG/10.1234/Case.', '10.1234/Case.'),
    ('10.1234/literal%2Fvalue', '10.1234/literal%2Fvalue'),
    ('https://doi.org/10.1234/literal%252Fvalue', '10.1234/literal%2Fvalue'),
])
def test_doi_normalization_without_suffix_damage(value, expected):
    assert normalize_doi(value) == expected


def test_doi_special_characters_are_encoded_once(work):
    doi = '10.1234/a/b?x=1&y#z%(A);'
    client, session, _ = client_for(FakeResponse(lookup_payload(dict(work, DOI=doi))))
    assert client.lookup_doi(doi).status == 'ambiguous'
    url, kwargs = session.calls[0]
    suffix = url.removeprefix('https://api.crossref.org/works/')
    assert unquote(suffix) == doi
    assert not any(char in suffix for char in '/?#&')
    assert kwargs['allow_redirects'] is False


@pytest.mark.parametrize('value', [None, '', 'not a doi', 'https://evil.example/10.1234/a', '10.1234/a\nb', '10.١٢٣٤/a'])
def test_invalid_doi_is_local_error_without_request(value):
    client, session, _ = client_for()
    assert client.lookup_doi(value).status == 'error'
    assert not session.calls


def test_404_is_crossref_scoped_not_global_doi_invalidity():
    client, session, _ = client_for(FakeResponse(status=404))
    result = client.lookup_doi('10.1234/missing')
    assert result.status == 'not_found'
    assert result.reason == 'doi_not_found_in_crossref'
    assert result.http_status == 404 and len(session.calls) == 1


def test_search_five_candidates_preserves_scores_and_contact(work):
    works = [dict(work, DOI=f'10.1234/{i}', score=100-i) for i in range(6)]
    client, session, _ = client_for(FakeResponse(search_payload(works)), mailto='operator@example.org', timeout=(2, 7))
    result = client.search_bibliographic('Example & title')
    assert result.status == 'ambiguous' and len(result.candidates) == 5
    assert [item['score'] for item in result.candidates] == [100, 99, 98, 97, 96]
    _, kwargs = session.calls[0]
    assert kwargs['params'] == {'query.bibliographic': 'Example & title', 'rows': 5, 'mailto': 'operator@example.org'}
    assert kwargs['timeout'] == (2, 7)
    assert 'operator@example.org' in kwargs['headers']['User-Agent']


def test_contact_is_optional_and_no_personal_default(work):
    client, session, _ = client_for(FakeResponse(lookup_payload(work)))
    client.lookup_doi(work['DOI'])
    kwargs = session.calls[0][1]
    assert kwargs['params'] == {}
    assert 'mailto' not in kwargs['headers']['User-Agent']


def test_valid_empty_search():
    client, _, _ = client_for(FakeResponse(search_payload([])))
    result = client.search_bibliographic('No result')
    assert result.status == 'not_found' and result.candidates == []


def test_doi_is_not_truncated():
    doi = '10.1234/' + 'A' * 512 + '.;'
    assert normalize_doi(doi) == doi
    assert parse_work({'DOI': doi})['doi'] == doi


def test_one_search_candidate_is_still_unmatched(work):
    client, _, _ = client_for(FakeResponse(search_payload([work])))
    assert client.search_bibliographic('Title').status == 'ambiguous'


@pytest.mark.parametrize('items', [None, 'wrong', [None], [{'DOI': 'invalid secret-sentinel'}]])
def test_bad_search_structure_does_not_become_not_found(items):
    client, _, _ = client_for(FakeResponse({'status': 'ok', 'message-type': 'work-list', 'message': {'items': items}}))
    result = client.search_bibliographic('Title')
    assert result.status == 'error'
    assert 'secret-sentinel' not in str(result.to_dict())


@pytest.mark.parametrize('field,value', [
    ('title', 'not an array'), ('container-title', {}), ('author', [None]),
    ('ISSN', '0378-5955'), ('issn-type', [False]),
    ('published', {'date-parts': [[2025, 2, 30]]}),
    ('published', {'date-parts': [[2025, True]]}),
])
def test_invalid_optional_shapes_are_sanitized_errors(field, value, work):
    work[field] = value
    result = parse_response(lookup_payload(work), lookup_method='doi_lookup',
                            fetched_at=datetime.now(timezone.utc))
    assert result.status == 'error' and result.reason == 'invalid_metadata'


def test_all_date_sources_preserve_their_own_precision(work):
    dates = parse_work(work)['publication_dates']
    assert dates['published']['date_parts'] == [2025]
    assert dates['published-online'] == {'date_parts': [2024, 12], 'precision': 'month'}
    assert dates['published-print'] == {'date_parts': [2025, 2, 28], 'precision': 'day'}
    assert dates['issued']['precision'] == 'month'


def test_retry_after_http_date(work):
    from email.utils import format_datetime
    headers = {'Retry-After': format_datetime(datetime(2026, 1, 1, 0, 0, 5, tzinfo=timezone.utc), usegmt=True)}
    client, _, clock = client_for(FakeResponse(status=429, headers=headers), FakeResponse(lookup_payload(work)))
    assert client.lookup_doi(work['DOI']).status == 'ambiguous'
    assert clock.sleeps == [5]


def test_retry_after_above_wait_budget_stops_without_early_retry():
    client, session, clock = client_for(FakeResponse(status=429, headers={'Retry-After': '3600'}))
    result = client.lookup_doi('10.1234/test')
    assert result.status == 'error' and result.reason == 'retry_after_exceeds_wait_limit'
    assert len(session.calls) == 1 and clock.sleeps == []


def test_search_http_404_is_endpoint_error():
    client, _, _ = client_for(FakeResponse(status=404))
    assert client.search_bibliographic('Title').status == 'error'


def test_retry_can_be_disabled():
    client, session, clock = client_for(FakeResponse(status=503), max_attempts=1)
    assert client.lookup_doi('10.1234/test').attempts == 1
    assert len(session.calls) == 1 and clock.sleeps == []


@pytest.mark.parametrize('value', [None, '', '   ', 123, 'title' + chr(0xd800)])
def test_invalid_search_input_never_requests(value):
    client, session, _ = client_for()
    assert client.search_bibliographic(value).status == 'error'
    assert not session.calls


def test_unencodable_doi_is_sanitized_before_transport():
    client, session, _ = client_for()
    result = client.lookup_doi('10.1234/' + chr(0xd800))
    assert result.status == 'error' and result.reason == 'invalid_doi'
    assert not session.calls


def test_overflowing_partial_date_is_sanitized(work):
    work['published'] = {'date-parts': [[10**100]]}
    client, _, _ = client_for(FakeResponse(lookup_payload(work)))
    result = client.lookup_doi(work['DOI'])
    assert result.status == 'error' and result.reason == 'invalid_metadata'


def test_unicode_contact_uses_ascii_safe_header_and_original_query(work):
    client, session, _ = client_for(FakeResponse(lookup_payload(work)), mailto='用户@example.org')
    assert client.lookup_doi(work['DOI']).status == 'ambiguous'
    kwargs = session.calls[0][1]
    assert kwargs['params']['mailto'] == '用户@example.org'
    kwargs['headers']['User-Agent'].encode('ascii')


def test_contact_header_newline_rejected():
    with pytest.raises(ValueError, match='contact'):
        CrossrefClient(mailto='operator@example.org\r\nX-Test: injected')


def test_retry_wait_budget_also_bounds_exponential_backoff():
    client, session, clock = client_for(FakeResponse(status=503), max_retry_wait=0)
    result = client.lookup_doi('10.1234/test')
    assert result.status == 'error' and result.reason == 'retry_wait_exceeds_limit'
    assert len(session.calls) == 1 and clock.sleeps == []


def test_429_retry_after_and_success(work):
    client, session, clock = client_for(FakeResponse(status=429, headers={'Retry-After': '4'}),
                                       FakeResponse(lookup_payload(work)))
    result = client.lookup_doi(work['DOI'])
    assert result.status == 'ambiguous' and result.attempts == 2
    assert clock.sleeps == [4]
    assert len(session.calls) == 2


@pytest.mark.parametrize('event,reason', [
    (FakeResponse(status=503), 'http_503'),
    (FakeResponse(status=429), 'http_429'),
    (requests.Timeout('secret-sentinel'), 'timeout'),
    (requests.ConnectionError('secret-sentinel'), 'connection_error'),
])
def test_retry_exhaustion_is_bounded_and_sanitized(event, reason, caplog):
    client, session, clock = client_for(event, event, event)
    result = client.lookup_doi('10.1234/test')
    assert result.status == 'error' and result.reason == reason
    assert result.attempts == 3 and len(session.calls) == 3
    assert clock.sleeps == [2, 4]
    assert 'secret-sentinel' not in str(result.to_dict()) + caplog.text


@pytest.mark.parametrize('status', [400, 401, 403, 301, 302])
def test_other_http_errors_do_not_retry(status):
    client, session, clock = client_for(FakeResponse(status=status))
    result = client.lookup_doi('10.1234/test')
    assert result.status == 'error' and result.http_status == status
    assert len(session.calls) == 1 and clock.sleeps == []


def test_broken_json_does_not_leak_body(caplog):
    client, session, _ = client_for(FakeResponse(broken_json=True))
    result = client.lookup_doi('10.1234/test')
    assert result.status == 'error' and result.reason == 'invalid_json'
    assert 'secret-sentinel' not in str(result.to_dict()) + caplog.text
    assert len(session.calls) == 1


@pytest.mark.parametrize('payload', [None, [], {}, {'status': 'failed', 'message': {}},
    {'status': 'ok', 'message-type': 'work', 'message': []},
    {'status': 'ok', 'message-type': 'work', 'message': {'DOI': None}},
    {'status': 'ok', 'message-type': 'work-list', 'message': {'items': 'wrong'}}])
def test_invalid_envelope_is_error(payload):
    client, _, _ = client_for(FakeResponse(payload))
    assert client.lookup_doi('10.1234/test').status == 'error'


def test_throttling_between_calls(work):
    client, session, clock = client_for(FakeResponse(lookup_payload(work)), FakeResponse(lookup_payload(work)))
    client.lookup_doi(work['DOI'])
    client.lookup_doi(work['DOI'])
    assert clock.sleeps == [1]
    assert len(session.calls) == 2  # No cache at this stage.


def test_live_transport_guard_is_active():
    with pytest.raises(AssertionError, match='Live HTTP'):
        requests.Session().get('https://api.crossref.org/works')
