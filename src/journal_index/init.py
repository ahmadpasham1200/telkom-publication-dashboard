"""Shared identities, candidate classification and evidence handling.

This is a regular module; package initialization lives in __init__.py.
"""
import json
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, parse_qs

STATUSES = {'indexed', 'not_indexed', 'not_found', 'unverified',
            'access_unavailable', 'error'}


def normalize_title(value):
    value = unicodedata.normalize('NFKC', value or '').casefold()
    return ' '.join(''.join(c if c.isalnum() else ' ' for c in value).split())


def issns(value):
    """Extract only checksum-valid ISSNs, never infer them from DOI/URL."""
    found = set()
    for match in re.finditer(r'(?<!\w)(\d{4})-?(\d{3}[\dXx])(?!\w)', value or ''):
        compact = ''.join(match.groups()).upper()
        digits = [int(c) if c != 'X' else 10 for c in compact]
        if sum(n * w for n, w in zip(digits, range(8, 0, -1))) % 11 == 0:
            found.add(compact[:4] + '-' + compact[4:])
    return found


def classify(title):
    normalized = normalize_title(title)
    if re.search(r'\b(proceedings|conference|symposium|workshop|seminar|prosiding|konferensi)\b', normalized):
        return 'non_journal', 'Conference/proceedings venue'
    if re.search(r'\b(journal|jurnal|bulletin|buletin|transactions|periodical)\b', normalized):
        return 'journal', 'Explicit journal title marker; identity still requires source matching'
    if re.search(r'\b(university|universitas|institute|institut|press|publisher|publishing)\b', normalized):
        return 'non_journal', 'Institution/publisher venue'
    return 'ambiguous', 'No explicit journal marker; source identity verification required before any index claim'


@dataclass
class Candidate:
    original: str
    normalized: str
    classification: str
    reason: str
    publications: list = field(default_factory=list)
    identifiers: set = field(default_factory=set)


def candidates(rows, limit=20):
    grouped = {}
    for row in rows:
        title = (row.get('detail_venue') or '').strip() or (row.get('venue') or '').strip()
        if not title:
            continue
        key = normalize_title(title)
        if not key:
            continue
        if key not in grouped:
            if len(grouped) >= limit:
                continue
            kind, reason = classify(title)
            grouped[key] = Candidate(title, key, kind, reason)
        candidate = grouped[key]
        # Only explicitly identified ISSN fields, never numbers in a DOI/URL.
        for field_name in ('issn', 'eissn', 'detail_issn', 'detail_eissn'):
            candidate.identifiers.update(issns(row.get(field_name)))
        # Preserve every observed spelling and origin within the bounded source window.
        candidate.publications.append({k: row.get(k) for k in (
            'raw_publication_id', 'scholar_id', 'title', 'venue', 'detail_venue',
            'publication_year', 'detail_doi', 'detail_url')})
    return list(grouped.values())


def matches(candidate, title, identifiers=()):
    observed = set().union(*(issns(str(i)) for i in identifiers)) if identifiers else set()
    if candidate.identifiers:
        return bool(candidate.identifiers & observed)
    return bool(candidate.normalized) and candidate.normalized == normalize_title(title)


def result(source, status, reason=None, **values):
    if status not in STATUSES:
        raise ValueError('Invalid check status')
    base = dict(source=source, status=status, indexed=True if status == 'indexed' else
                False if status == 'not_indexed' else None,
                checked_at=datetime.now(timezone.utc), reason=reason,
                evidence_url=None, issn=None, eissn=None, sjr=[], sinta_rank=None,
                sinta_period=None, metadata={})
    base.update(values)
    return base


def valid_url(url, domains):
    parsed = urlsplit(url or '')
    if not (parsed.scheme == 'https' and parsed.hostname in domains and
            not parsed.username and not parsed.password and not parsed.fragment and
            parsed.port in (None, 443)):
        return False
    if parsed.hostname in {'www.scimagojr.com', 'scimagojr.com'}:
        query = parse_qs(parsed.query)
        return (parsed.path == '/journalsearch.php' and set(query) <= {'q', 'tip', 'clean'}
                and len(query.get('q', [])) == 1 and query['q'][0].isdigit()
                and query.get('tip') == ['sid']
                and query.get('clean', ['0']) in (['0'], ['1']))
    return bool(re.fullmatch(r'/journals/profile/\d+/?', parsed.path)) and not parsed.query


def read_evidence(path, source):
    """Read a bounded local, manually transcribed evidence list, never sessions."""
    if not path:
        return None
    try:
        file = Path(path)
        if file.stat().st_size > 2_000_000:
            raise ValueError('Oversized evidence')
        records = json.loads(file.read_text(encoding='utf-8-sig'))
        if not isinstance(records, list) or not all(isinstance(r, dict) for r in records):
            raise ValueError('Invalid evidence')
        return records
    except (OSError, ValueError):
        return result(source, 'error', 'Cannot read local evidence list')


def select_evidence(candidate, records, source, domains):
    found = [r for r in records if matches(candidate, r.get('title'),
             [r.get('issn', ''), r.get('eissn', '')])]
    if not found:
        return result(source, 'not_found', 'No match in supplied evidence subset; not a catalogue-wide conclusion')
    if any(not valid_url(r.get('evidence_url'), domains) for r in found):
        return result(source, 'unverified', 'Missing safe official evidence URL')
    identities = {normalize_title(r.get('title')) for r in found}
    if len(identities) != 1:
        return result(source, 'unverified', 'Conflicting journal identities in evidence')
    # Manual evidence needs both identifiers and title; Scopus and the general
    # matches() helper retain their existing ISSN-first identity rules.
    if candidate.identifiers and identities != {candidate.normalized}:
        return result(source, 'unverified', 'ISSN matches but normalized journal title differs in manual evidence')
    return found
