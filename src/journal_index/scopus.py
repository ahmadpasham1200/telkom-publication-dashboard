"""Official Elsevier Serial Title API, STANDARD JSON response.

Specification: https://dev.elsevier.com/documentation/SerialTitleAPI.wadl
Example: https://dev.elsevier.com/payloads/metadata/serialTitleSearchResp.json
SCOPUS_API_KEY is required. SCOPUS_INST_TOKEN is used only with an explicit
use_inst_token=True, for institutions granted this access by Elsevier.
No authentication response body, headers or exception text is retained.
"""
import os
import json
import logging
import re
import time
import requests
from .init import result, matches, issns

API = 'https://api.elsevier.com/content/serial/title'
PAGE_SIZE = 25
LOG = logging.getLogger(__name__)


def diagnostics(**values):
    report = dict(request_attempted=False, http_status=None, api_success=False,
                  results_returned=None, match_count=None, identity_match=None,
                  match_method=None)
    report.update(values)
    return report


def unavailable():
    return result('scopus', 'access_unavailable', 'SCOPUS_API_KEY is not configured',
                  metadata=diagnostics())


def parse_response(candidate, payload):
    if not isinstance(payload, dict) or not isinstance(payload.get('serial-metadata-response'), dict):
        return result('scopus', 'error', 'Unexpected Serial Title JSON envelope', metadata=diagnostics())
    container = payload['serial-metadata-response']
    entries = container.get('entry')
    # Live Serial Title searches can return HTTP 200 with
    # {"serial-metadata-response": {"error": "No results found...", "link": [...]}}
    # instead of entry. HTTP success alone does not establish journal identity.
    # Recognize only the explicit no-results message; never copy its free text
    # or self links, which may contain query parameters or credentials.
    if 'error' in container:
        message = container['error']
        no_results = isinstance(message, str) and re.match(
            r'^no results found(?:\b|[.!])', message.strip(), re.I)
        if not no_results or (entries is not None and entries != []):
            return result('scopus', 'error', 'Unexpected or conflicting Serial Title service error',
                          metadata=diagnostics())
        entries = []
    if entries is None:
        return result('scopus', 'error', 'Missing Serial Title entries', metadata=diagnostics())
    if isinstance(entries, dict):
        entries = [entries]
    if not isinstance(entries, list) or not all(isinstance(e, dict) and isinstance(e.get('dc:title'), str) for e in entries):
        return result('scopus', 'error', 'Malformed Serial Title entries', metadata=diagnostics())
    for entry in entries:
        if (not entry['dc:title'].strip() or
                any(entry.get(key) is not None and not isinstance(entry[key], str)
                    for key in ('prism:issn', 'prism:eIssn', 'prism:aggregationType')) or
                (entry.get('source-id') is not None and
                 (isinstance(entry['source-id'], bool) or not isinstance(entry['source-id'], (str, int))))):
            return result('scopus', 'error', 'Malformed Serial Title identity fields', metadata=diagnostics())
    matched = []
    for entry in entries:
        observed = issns(entry.get('prism:issn', '')) | issns(entry.get('prism:eIssn', ''))
        # An available candidate ISSN must never fall back to title matching.
        same = bool(candidate.identifiers & observed) if candidate.identifiers else matches(candidate, entry['dc:title'])
        if same:
            matched.append(entry)
    report = diagnostics(results_returned=len(entries), match_count=len(matched),
                         identity_match=False,
                         match_method='issn' if candidate.identifiers else 'exact_normalized_title')
    def outcome(status, reason):
        return result('scopus', status, reason, metadata=dict(report))
    if not matched:
        return outcome('unverified' if entries else 'not_found',
                       'Returned serials do not establish identity' if entries else
                       'No serials for this valid bounded query; not evidence of exclusion from Scopus')
    if len(matched) != 1:
        return outcome('unverified', 'Multiple matching serial records')
    if not candidate.identifiers and len(entries) >= PAGE_SIZE:
        return outcome('unverified', 'Title response reaches page limit; additional identities may exist')
    entry = matched[0]
    if str(entry.get('prism:aggregationType', '')).casefold() != 'journal':
        return outcome('unverified', 'Matched serial is not explicitly identified as a journal')
    report['identity_match'] = True
    # Serial metadata alone is not evidence of current Scopus coverage.
    source_id = entry.get('source-id')
    status = 'indexed' if source_id else 'unverified'
    return result('scopus', status,
                  'Scopus source ID is evidence of inclusion, not a claim of current coverage',
                  evidence_url=API,
                  issn=next(iter(issns(entry.get('prism:issn', ''))), None),
                  eissn=next(iter(issns(entry.get('prism:eIssn', ''))), None),
                  metadata=dict(report, source_id=source_id, title=entry.get('dc:title')))


def check(candidate, session=None, sleep=time.sleep, use_inst_token=False):
    # requests encodes raw parameter values once (including & as %26). Do not
    # interpolate titles into URLs or pre-encode them. The documented title
    # filter searches a partial/complete phrase, not a fuzzy bag of words.
    params = {'issn': ','.join(sorted(candidate.identifiers))} if candidate.identifiers else {'title': candidate.original}
    # No additional publisher/year/subject/content filter: journal type is
    # validated from the returned aggregationType after identity matching.
    params.update({'count': PAGE_SIZE, 'view': 'STANDARD'})
    query_type = 'issn' if candidate.identifiers else 'title'

    def finish(checked):
        checked['metadata'].update(query_type=query_type, query_parameters=dict(params))
        # Explicit allowlist: never log request/response objects, self links,
        # authentication headers, service error text, or exception details.
        safe = {name: checked['metadata'].get(name) for name in (
            'query_type', 'query_parameters', 'request_attempted', 'http_status',
            'results_returned', 'match_count', 'identity_match', 'match_method')}
        safe['status'] = checked['status']
        message = json.dumps(safe, ensure_ascii=True)
        for secret_name in ('SCOPUS_API_KEY', 'SCOPUS_INST_TOKEN'):
            secret = (os.getenv(secret_name) or '').strip()
            if secret:
                message = message.replace(secret, '[redacted]')
        LOG.info('Scopus query diagnostic: %s', message)
        return checked

    key = (os.getenv('SCOPUS_API_KEY') or '').strip()
    if not key:
        return finish(unavailable())
    headers = {'X-ELS-APIKey': key, 'Accept': 'application/json'}
    if use_inst_token:
        token = (os.getenv('SCOPUS_INST_TOKEN') or '').strip()
        if not token:
            return finish(result('scopus', 'access_unavailable', 'Requested institution token is not configured', metadata=diagnostics()))
        headers['X-ELS-Insttoken'] = token
    owned = session is None
    client = session or requests.Session()
    report = diagnostics()
    try:
        for attempt in range(3):
            try:
                report['request_attempted'] = True
                response = client.get(API, params=params, headers=headers, timeout=(5, 20), allow_redirects=False)
                report['http_status'] = response.status_code
                if response.status_code in (401, 403, 429) or 300 <= response.status_code < 400:
                    return finish(result('scopus', 'access_unavailable', 'API authentication, rate limit or access restriction', metadata=report))
                if response.status_code == 404:
                    return finish(result('scopus', 'unverified', 'API resource not found; no valid search result available', metadata=report))
                if response.status_code >= 500 and attempt < 2:
                    sleep(2 ** attempt)
                    continue
                response.raise_for_status()
                checked = parse_response(candidate, response.json())
                checked['metadata'].update(request_attempted=True,
                                           http_status=response.status_code,
                                           api_success=checked['status'] != 'error')
                return finish(checked)
            except (requests.Timeout, requests.ConnectionError):
                if attempt < 2:
                    sleep(2 ** attempt)
                    continue
                return finish(result('scopus', 'access_unavailable', 'API connection failed after bounded retries', metadata=report))
            except (requests.RequestException, ValueError, TypeError, AttributeError):
                return finish(result('scopus', 'error', 'API request or response parsing failed', metadata=report))
    finally:
        if owned:
            client.close()
