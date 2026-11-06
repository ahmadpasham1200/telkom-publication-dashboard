"""Crossref transport and metadata parsing only; no article matcher or storage.

Successful retrieval is ``ambiguous`` until a future matcher evaluates the
article. ``matched`` is reserved in the result contract, never emitted here.
Importing this module reads no configuration and performs no I/O.
"""
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime
import math
import re
import time
from typing import Literal
from urllib.parse import quote, unquote

import requests

API_URL = 'https://api.crossref.org/works'
Status = Literal['matched', 'ambiguous', 'not_found', 'error']
LookupMethod = Literal['doi_lookup', 'bibliographic_search']


@dataclass(frozen=True)
class CrossrefResult:
    status: Status
    lookup_method: LookupMethod
    fetched_at: datetime
    reason: str
    candidates: list[dict] = field(default_factory=list)
    http_status: int | None = None
    attempts: int = 0

    def __post_init__(self):
        if self.status not in ('matched', 'ambiguous', 'not_found', 'error'):
            raise ValueError('Invalid Crossref result status')
        if self.lookup_method not in ('doi_lookup', 'bibliographic_search'):
            raise ValueError('Invalid Crossref lookup method')
        if self.fetched_at.tzinfo is None or self.fetched_at.utcoffset() is None:
            raise ValueError('Crossref timestamps must be timezone-aware')

    def to_dict(self):
        values = asdict(self)
        values['fetched_at'] = self.fetched_at.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')
        return values


def normalize_doi(value):
    """Remove recognized resolver/doi prefixes without guessing suffix boundaries.

    Decode resolver URL paths once. Plain DOI percent signs, case, and suffix
    punctuation are preserved; URL queries/fragments are not silently stripped.
    Syntax validation here does not prove global DOI registration or validity.
    """
    if not isinstance(value, str):
        raise ValueError('invalid_doi')
    doi = value.strip()
    resolver = re.match(r'^https?://(?:dx\.)?doi\.org/', doi, re.I)
    if resolver:
        doi = unquote(doi[resolver.end():], errors='strict')
    else:
        doi = re.sub(r'^doi:\s*', '', doi, count=1, flags=re.I)
    if not re.fullmatch(r'10\.[0-9]{4,9}/[^\s\x00-\x1f\x7f]+', doi):
        raise ValueError('invalid_doi')
    try:
        doi.encode('utf-8')
    except UnicodeError:
        raise ValueError('invalid_doi') from None
    return doi


def _text(value):
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError('invalid_metadata')
    return value.strip() or None


def _strings(value):
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError('invalid_metadata')
    return list(value)


def _objects(value):
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise ValueError('invalid_metadata')
    return deepcopy(value)


def _partial_date(value):
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError('invalid_metadata')
    parts = value.get('date-parts')
    if parts is None or parts == [] or parts == [[]]:
        return None
    if (not isinstance(parts, list) or len(parts) != 1 or not isinstance(parts[0], list)
            or not 1 <= len(parts[0]) <= 3 or any(type(item) is not int for item in parts[0])):
        raise ValueError('invalid_metadata')
    parts = parts[0]
    # Complete only for calendar validation, never invent missing output parts.
    try:
        date(*(parts + [1] * (3 - len(parts))))
    except (ValueError, OverflowError):
        raise ValueError('invalid_metadata') from None
    return {'date_parts': list(parts), 'precision': ('year', 'month', 'day')[len(parts) - 1]}


def parse_work(item, *, search_result=False):
    """Parse one work into independent metadata; no identity/matching decision.

    ISSNs and issn-type observations remain source-ordered and unassigned.
    Optional absent fields are empty lists/dicts or None; malformed shapes fail
    closed rather than being interpreted as a work with no metadata.
    """
    if not isinstance(item, dict):
        raise ValueError('invalid_metadata')
    dates = {}
    for name in ('published', 'published-online', 'published-print', 'issued'):
        parsed = _partial_date(item.get(name))
        if parsed is not None:
            dates[name] = parsed
    score = item.get('score') if search_result else None
    if type(score) not in (int, float):
        score = None
    else:
        try:
            if not math.isfinite(score):
                score = None
        except OverflowError:
            score = None
    return {
        'doi': normalize_doi(item.get('DOI')),
        'titles': _strings(item.get('title')),
        'authors': _objects(item.get('author')),
        'publication_dates': dates,
        'container_titles': _strings(item.get('container-title')),
        'issns': _strings(item.get('ISSN')),
        'issn_types': _objects(item.get('issn-type')),
        'type': _text(item.get('type')),
        'publisher': _text(item.get('publisher')),
        'url': _text(item.get('URL')),
        'score': score,
    }


def parse_response(payload, *, lookup_method, fetched_at, http_status=200, attempts=0):
    """Parse singleton/list envelopes; at most five unvalidated search candidates."""
    if lookup_method not in ('doi_lookup', 'bibliographic_search'):
        raise ValueError('Invalid Crossref lookup method')
    values = dict(lookup_method=lookup_method, fetched_at=fetched_at,
                  http_status=http_status, attempts=attempts)
    search = lookup_method == 'bibliographic_search'
    expected_type = 'work-list' if search else 'work'
    if (not isinstance(payload, dict) or payload.get('status') != 'ok'
            or payload.get('message-type') != expected_type or not isinstance(payload.get('message'), dict)):
        return CrossrefResult(status='error', reason='invalid_response_structure', **values)
    items = payload['message'].get('items') if search else [payload['message']]
    if not isinstance(items, list):
        return CrossrefResult(status='error', reason='invalid_response_structure', **values)
    if not items:
        return CrossrefResult(status='not_found', reason='empty_crossref_search', **values)
    try:
        works = [parse_work(item, search_result=search) for item in items[:5]]
    except (ValueError, TypeError):
        return CrossrefResult(status='error', reason='invalid_metadata', **values)
    return CrossrefResult(status='ambiguous', reason='metadata_retrieved_not_matched',
                          candidates=works, **values)


class CrossrefClient:
    """Explicit REST calls with bounded retry and serial throttling.

    Contact is optional and passed explicitly; no .env/config file is read.
    Inject session/sleep/monotonic/now for fully offline deterministic testing.
    The client has no cache and is intended for serial use.
    """
    def __init__(self, *, session=None, timeout=(5, 20), mailto=None,
                 max_attempts=3, min_interval=1.0, max_retry_wait=60.0,
                 sleep=time.sleep, monotonic=time.monotonic, now=None):
        if type(max_attempts) is not int or not 1 <= max_attempts <= 3:
            raise ValueError('max_attempts must be 1..3')
        if not math.isfinite(min_interval) or min_interval < 0:
            raise ValueError('min_interval must be finite and nonnegative')
        if not math.isfinite(max_retry_wait) or max_retry_wait < 0:
            raise ValueError('max_retry_wait must be finite and nonnegative')
        timeouts = timeout if isinstance(timeout, tuple) else (timeout,)
        if len(timeouts) not in (1, 2) or any(type(t) not in (int, float) or not math.isfinite(t) or t <= 0 for t in timeouts):
            raise ValueError('timeout must contain positive finite seconds')
        mailto = _text(mailto)
        if mailto and any(char in mailto for char in '\r\n'):
            raise ValueError('Invalid contact configuration')
        try:
            self._contact_header = quote(mailto, safe='@._+-') if mailto else None
        except UnicodeError:
            raise ValueError('Invalid contact configuration') from None
        self.session = session if session is not None else requests.Session()
        self._owns_session = session is None
        self.timeout, self.mailto = timeout, mailto
        self.max_attempts, self.min_interval, self.max_retry_wait = max_attempts, min_interval, max_retry_wait
        self.sleep, self.monotonic = sleep, monotonic
        self.now = now or (lambda: datetime.now(timezone.utc))
        self._next_request = 0.0

    def close(self):
        if self._owns_session:
            self.session.close()

    def lookup_doi(self, doi):
        try:
            normalized = normalize_doi(doi)
            encoded = quote(normalized, safe='')
        except (ValueError, UnicodeError):
            return self._result('error', 'invalid_doi', 'doi_lookup')
        return self._request(API_URL + '/' + encoded, {}, 'doi_lookup')

    def search_bibliographic(self, query):
        if not isinstance(query, str) or not query.strip():
            return self._result('error', 'invalid_query', 'bibliographic_search')
        try:
            query.encode('utf-8')
        except UnicodeError:
            return self._result('error', 'invalid_query', 'bibliographic_search')
        return self._request(API_URL, {'query.bibliographic': query.strip(), 'rows': 5}, 'bibliographic_search')

    def _result(self, status, reason, method, http_status=None, attempts=0):
        return CrossrefResult(status=status, reason=reason, lookup_method=method,
                              fetched_at=self.now(), http_status=http_status, attempts=attempts)

    def _retry_after(self, value):
        try:
            seconds = float(value)
            return max(0, seconds) if math.isfinite(seconds) else None
        except (TypeError, ValueError):
            try:
                timestamp = parsedate_to_datetime(value)
                if timestamp.tzinfo is None:
                    return None
                return max(0, (timestamp - self.now()).total_seconds())
            except (TypeError, ValueError, OverflowError):
                return None

    def _request(self, url, params, method):
        params = dict(params)
        agent = 'telkom-publication-dashboard-crossref/1.0'
        if self.mailto:
            params['mailto'] = self.mailto
            agent += f' (mailto:{self._contact_header})'
        headers = {'Accept': 'application/json', 'User-Agent': agent}
        retry_delay = 0.0
        for attempt in range(1, self.max_attempts + 1):
            delay = max(0, self._next_request - self.monotonic(), retry_delay)
            if delay:
                self.sleep(delay)
            self._next_request = self.monotonic() + self.min_interval
            status, retry_after = None, None
            try:
                response = self.session.get(url, params=params, headers=headers,
                                            timeout=self.timeout, allow_redirects=False)
            except requests.Timeout:
                reason = 'timeout'
            except requests.RequestException:
                reason = 'connection_error'
            else:
                status = response.status_code
                if status == 404 and method == 'doi_lookup':
                    return self._result('not_found', 'doi_not_found_in_crossref', method, status, attempt)
                if status == 200:
                    try:
                        payload = response.json()
                    except ValueError:
                        return self._result('error', 'invalid_json', method, status, attempt)
                    return parse_response(payload, lookup_method=method, fetched_at=self.now(),
                                          http_status=status, attempts=attempt)
                reason = f'http_{status}'
                if status != 429 and not 500 <= status <= 599:
                    return self._result('error', reason, method, status, attempt)
                retry_after = self._retry_after(response.headers.get('Retry-After'))
            if attempt == self.max_attempts:
                return self._result('error', reason, method, status, attempt)
            if retry_after is not None and retry_after > self.max_retry_wait:
                # Stop instead of retrying earlier than the server permits.
                return self._result('error', 'retry_after_exceeds_wait_limit', method, status, attempt)
            retry_delay = max(2 ** attempt, retry_after or 0)
            if retry_delay > self.max_retry_wait:
                return self._result('error', 'retry_wait_exceeds_limit', method, status, attempt)
