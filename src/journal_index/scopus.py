"""Scopus source master, entirely local. No network operations.

The only evidence source is the Elsevier ext_list Excel export pinned in
``data/scopus/ext_list_Sep_2026.xlsx``. Serial identities live in
'Scopus Sources Sep. 2026' (current), 'Discontinued Titles Sep. 2026'
(historical) and 'Serial Conf. Proc. with Profile'; accepted-but-not-yet
indexed titles live in 'Accepted Titles Sep. 2026'; proceedings identities
live in 'All Conf. Proceedings Jun. 2026'.

Internal decision statuses are exactly four: 'indexed', 'indicated',
'needs_verification' and 'not_found'. They are carried verbatim as the
result 'status' (the verification table CHECK constraint expects exactly
these four) and mirrored in ``metadata['scopus_status']``. ``init.result``
validates against the shared vocabulary, so the dict is built with the
mapped shared status ('indicated'/'needs_verification' -> 'unverified')
and the internal status is restored afterwards.
"""
import hashlib
import json
import re
import unicodedata
from collections import defaultdict
from difflib import SequenceMatcher
from itertools import chain
from pathlib import Path

from openpyxl import load_workbook

from .crossref import normalize_doi
from .init import issns, normalize_title, result

REFERENCE_YEAR = 2026
DEFAULT_MASTER = Path(__file__).resolve().parents[2] / 'data/scopus/ext_list_Sep_2026.xlsx'
FUZZY_THRESHOLD = 0.93
FUZZY_LIMIT = 5

SHEET_SOURCES = 'Scopus Sources Sep. 2026'
SHEET_ACCEPTED = 'Accepted Titles Sep. 2026'
SHEET_DISCONTINUED = 'Discontinued Titles Sep. 2026'
SHEET_SERIAL_CONF = 'Serial Conf. Proc. with Profile'
SHEET_CONF_PROC = 'All Conf. Proceedings Jun. 2026'
SHEET_ORDER = (SHEET_SOURCES, SHEET_ACCEPTED, SHEET_DISCONTINUED,
               SHEET_SERIAL_CONF, SHEET_CONF_PROC)
SHEET_PRIORITY = {SHEET_SOURCES: 0, SHEET_DISCONTINUED: 1,
                  SHEET_SERIAL_CONF: 2, SHEET_ACCEPTED: 3}

# Internal decision statuses mapped onto the shared result vocabulary.
_RESULT_STATUS = {'indexed': 'indexed', 'indicated': 'unverified',
                  'needs_verification': 'unverified', 'not_found': 'not_found'}


def _finish(status, reason, metadata, values, source='scopus'):
    """Build the result through init.result, then restore the internal status.

    init.result validates the shared status vocabulary; the Scopus decision
    vocabulary ('indicated', 'needs_verification') is narrower and is what the
    verification table persists, so it is written back verbatim.
    """
    checked = result(source, _RESULT_STATUS[status], reason,
                     metadata=dict(metadata), **values)
    checked['status'] = status
    return checked

# Header matching is tolerant on purpose: the real export wraps headers over
# several lines, appends notes to header text ('Source Title (newly added
# titles are highlighted...)') and even misspells one ('Proceedinds Title').
COLUMNS = {
    SHEET_SOURCES: (
        ('source_record_id', r'sourcerecord id', True),
        ('title', r'source title.*', True),
        ('issn', r'issn', True),
        ('eissn', r'eissn', True),
        ('active_status', r'active or inactive', True),
        ('coverage', r'coverage', True),
        ('discontinued_titles', r'titles discontinued by scopus.*', False),
        ('source_type', r'source type', False),
        ('publisher', r'publisher.*', False),
        ('asjc', r'(?:all science journal classification codes|asjc).*', False),
        ('related_title', r'related title.*', False),
    ),
    SHEET_ACCEPTED: (
        # The real export first shows a 'Status/Accepted titles' banner pair,
        # then a second header row ('Source Title, ISSN, EISSN, ...') for the
        # actual data; both layouts must load.
        ('status', r'status.*', False),
        ('title', r'(?:accepted titles|source title).*', True),
        ('issn', r'issn', False),
        ('eissn', r'eissn', False),
        ('publisher', r'publisher.*', False),
    ),
    SHEET_DISCONTINUED: (
        ('source_record_id', r'sourcerecord id', True),
        ('title', r'source title.*', True),
        ('issn', r'issn', True),
        ('eissn', r'eissn', True),
        ('publisher', r'publisher.*', False),
        ('indexation_change', r'indexation change', False),
        ('year', r'year', False),
        ('volume', r'volume', False),
        ('issue', r'issue', False),
        ('page_range', r'page range', False),
    ),
    SHEET_SERIAL_CONF: (
        ('source_record_id', r'sourcerecord id', True),
        ('title', r'source title.*', True),
        ('issn', r'issn', True),
        ('eissn', r'eissn', True),
        ('discontinued_titles', r'titles discontinued by scopus.*', False),
        ('coverage', r'coverage', True),
        ('asjc', r'(?:all science journal classification codes|asjc).*', False),
    ),
    SHEET_CONF_PROC: (
        ('title', r'source title.*', True),
        ('proceedings_title', r'proceed(?:ings|inds) title.*', True),
        ('isbn', r'isbn.*', True),
        ('year', r'year', True),
        ('volume', r'volume', False),
        ('eid', r'eid', True),
        ('publisher', r'publisher.*', False),
        ('asjc', r'(?:all science journal classification codes|asjc).*', False),
    ),
}


class ScopusMasterError(ValueError):
    """A missing, unreadable, or incompatible local Scopus master export."""


# Parsed default masters per process; only successful loads are cached.
_MASTER_CACHE = {}


def _text(value):
    """Normalize a worksheet cell: NFKC, collapse whitespace, de-float numbers."""
    if value is None:
        return None
    if isinstance(value, bool):
        value = str(value)
    elif isinstance(value, float) and value.is_integer():
        value = int(value)
    text = str(value)
    if not text.isascii():  # NFKC is the identity on ASCII; skip it there.
        text = unicodedata.normalize('NFKC', text)
    if text != text.strip() or '\n' in text or '  ' in text or '\t' in text:
        text = ' '.join(text.split())
    return text or None


def _year(value):
    match = re.search(r'(?:19|20)\d{2}', str(value if value is not None else ''))
    return int(match.group()) if match else None


def coverage_years(value):
    """Expand a coverage cell such as '2026; 2023-2024; 1995-2005' into years."""
    years = set()
    for part in re.split(r'[;,]', str(value or '')):
        found = re.findall(r'(?:19|20)\d{2}', part)
        if not found:
            continue
        if re.search(r'(?:19|20)\d{2}\s*[-\u2013\u2014]\s*$', part):
            years.update(range(int(found[0]), REFERENCE_YEAR + 1))
        elif len(found) >= 2:
            start, end = int(found[0]), int(found[1])
            if start <= end and end - start <= 100:
                years.update(range(start, end + 1))
        else:
            years.add(int(found[0]))
    return years


def in_coverage(year, coverage):
    return year is not None and year in coverage_years(coverage)


def _isbn_runs(text):
    for run in re.findall(r'[0-9X\u2013\u2014-]+', text):
        compact = re.sub(r'[^0-9X]', '', run)
        if len(compact) in (10, 13):
            yield compact


def normalize_isbn(value):
    """Compact an ISBN cell: drop hyphens/separators, keep digits and capital X."""
    text = unicodedata.normalize('NFKC', str(value if value is not None else '')).upper()
    return next(_isbn_runs(text), None)


def _isbns(value):
    text = unicodedata.normalize('NFKC', str(value if value is not None else '')).upper()
    return frozenset(_isbn_runs(text))


def _candidate_identity(candidate):
    """Defensively collect ISSNs, titles, ISBNs, year and publisher evidence."""
    rows = [row for row in (getattr(candidate, 'publications', None) or []) if isinstance(row, dict)]
    identifiers = set(getattr(candidate, 'identifiers', None) or ())
    for row in rows:
        for key in ('issn', 'eissn', 'detail_issn', 'detail_eissn'):
            identifiers |= issns(row.get(key))
    titles = []
    original = (getattr(candidate, 'original', '') or '').strip()
    if original:
        titles.append(original)
    for row in rows:
        for key in ('detail_journal', 'detail_venue', 'venue'):
            value = (row.get(key) or '').strip()
            if value:
                titles.append(value)
    normalized_titles = []
    for value in titles:
        normalized = normalize_title(value)
        if normalized and normalized not in normalized_titles:
            normalized_titles.append(normalized)
    year = None
    for row in rows:
        year = year or _year(row.get('publication_year')) or _year(row.get('year'))
    isbns = set()
    values = [getattr(candidate, 'isbn', None)]
    values += [row.get(key) for row in rows for key in ('isbn', 'detail_isbn')]
    for value in values:
        isbns |= _isbns(value)
    publisher = getattr(candidate, 'publisher', None)
    for row in rows:
        publisher = publisher or row.get('publisher')
    return identifiers, normalized_titles, isbns, year, (publisher or None)


def _match_headers(row, spec):
    headers = [normalize_title(_text(value)) for value in row]
    mapping = {}
    for field, pattern, _required in spec:
        for index, header in enumerate(headers):
            if header and re.fullmatch(pattern, header):
                mapping[field] = index
                break
    return mapping


def _build_record(sheet_name, values):
    title = values.get('title')
    if sheet_name == SHEET_ACCEPTED:
        # Notice/banner rows carry text in the status column but no title.
        if not title:
            return None
        return {'sheet': sheet_name, 'status': values.get('status'), 'title': title,
                'issn_raw': values.get('issn'), 'eissn_raw': values.get('eissn'),
                'issns': frozenset(issns(values.get('issn')) | issns(values.get('eissn'))),
                'publisher': values.get('publisher')}
    if sheet_name == SHEET_CONF_PROC:
        record = {'sheet': sheet_name, 'title': title,
                  'proceedings_title': values.get('proceedings_title'),
                  'isbns': _isbns(values.get('isbn')), 'year': _year(values.get('year')),
                  'year_raw': values.get('year'), 'volume': values.get('volume'),
                  'eid': values.get('eid'), 'publisher': values.get('publisher'),
                  'asjc': values.get('asjc')}
        return record if (title or record['proceedings_title'] or record['isbns']) else None
    identifier_set = issns(values.get('issn')) | issns(values.get('eissn'))
    if not (title or identifier_set):
        return None
    record = {'sheet': sheet_name, 'source_record_id': values.get('source_record_id'),
              'title': title, 'issn_raw': values.get('issn'), 'eissn_raw': values.get('eissn'),
              'issns': frozenset(identifier_set)}
    if sheet_name == SHEET_SOURCES:
        status = values.get('active_status')
        record.update(active_status=status,
                      active=(status.casefold() == 'active') if status else None,
                      coverage=values.get('coverage'),
                      coverage_years=frozenset(coverage_years(values.get('coverage'))),
                      source_type=values.get('source_type'), publisher=values.get('publisher'),
                      asjc=values.get('asjc'),
                      discontinued=bool(values.get('discontinued_titles')),
                      related_title=values.get('related_title'))
    elif sheet_name == SHEET_DISCONTINUED:
        record.update(publisher=values.get('publisher'),
                      indexation_change=values.get('indexation_change'),
                      year=values.get('year'), volume=values.get('volume'),
                      issue=values.get('issue'), page_range=values.get('page_range'),
                      discontinued=True)
    else:  # serial conference proceedings profile
        record.update(discontinued=bool(values.get('discontinued_titles')),
                      coverage=values.get('coverage'),
                      coverage_years=frozenset(coverage_years(values.get('coverage'))),
                      asjc=values.get('asjc'))
    return record


def _read_sheet(worksheet, sheet_name, path):
    spec = COLUMNS[sheet_name]
    required = {field for field, _pattern, required in spec if required}
    rows = worksheet.iter_rows(values_only=True)
    buffered, best = [], None
    for index, row in enumerate(rows):
        buffered.append(row)
        mapping = _match_headers(row, spec)
        if required <= set(mapping) and (best is None or len(mapping) > best[0]):
            # Prefer the richest satisfying header row: the accepted sheet
            # shows a banner pair first and the real column headers later.
            best = (len(mapping), mapping, index)
        if index >= 10:
            break
    if best is None:
        raise ScopusMasterError(
            f'Scopus master sheet "{sheet_name}" has no row with the required headers: {path}')
    _count, mapping, header_index = best
    records = []
    for row in chain(buffered[header_index + 1:], rows):
        if not row or not any(value is not None and str(value).strip() for value in row):
            continue
        values = {field: (_text(row[column]) if column < len(row) else None)
                  for field, column in mapping.items()}
        record = _build_record(sheet_name, values)
        if record is not None:
            records.append(record)
    return records


class ScopusMaster:
    """Indexed view over the five normalized sheets of the local master."""

    def __init__(self, sheets, path, digest):
        self.sheets = sheets
        self.path = path
        self.digest = digest
        self.by_issn = defaultdict(list)
        self.by_title = defaultdict(list)
        self.by_isbn = defaultdict(list)
        self.by_proceedings_title = defaultdict(list)
        self.active_by_id = {}
        self.active_by_issn = {}
        for record in sheets[SHEET_SOURCES]:
            if record['active'] and record['source_record_id']:
                self.active_by_id[record['source_record_id']] = record
            if record['active']:
                for identifier in record['issns']:
                    self.active_by_issn.setdefault(identifier, record)
        for record in chain(sheets[SHEET_SOURCES], sheets[SHEET_DISCONTINUED],
                            sheets[SHEET_SERIAL_CONF], sheets[SHEET_ACCEPTED]):
            for identifier in record['issns']:
                self.by_issn[identifier].append(record)
            title = normalize_title(record['title'] or '')
            if title:
                self.by_title[title].append(record)
        for record in sheets[SHEET_CONF_PROC]:
            for isbn in record['isbns']:
                self.by_isbn[isbn].append(record)
            titles = {normalize_title(record.get(key) or '')
                      for key in ('title', 'proceedings_title')}
            titles.discard('')
            for title in titles:
                self.by_proceedings_title[title].append(record)

    def check(self, candidate):
        metadata = {'master_file': str(self.path), 'master_sha256': self.digest,
                    'identity_match': False, 'match_method': None}

        def answer(status, reason, **values):
            metadata['scopus_status'] = status
            return _finish(status, reason, metadata, values)

        identifiers, titles, isbns, year, publisher = _candidate_identity(candidate)
        if not (identifiers or titles or isbns):
            return answer('not_found',
                          'Candidate carries no title, ISSN or ISBN to match against the local Scopus master')

        # ISBN identity is matched only against the conference-proceedings
        # sheet, never against journal sheets.
        if isbns:
            hits = [(record, 'isbn', isbn) for isbn in sorted(isbns)
                    for record in self.by_isbn.get(isbn, [])]
            if hits:
                return self._proceedings_answer(hits, year, publisher, metadata)

        serial_hits, method = [], None
        if identifiers:
            method = 'issn'
            for identifier in sorted(identifiers):
                serial_hits.extend((record, identifier)
                                   for record in self.by_issn.get(identifier, []))
        if not serial_hits and titles:
            method = 'title_exact'
            for title in titles:
                serial_hits.extend((record, title)
                                   for record in self.by_title.get(title, []))
        if serial_hits:
            metadata['match_method'] = method
            selected, ambiguous = self._select_serial(serial_hits)
            if ambiguous:
                metadata['candidates'] = [self._summary(record)
                                          for record in ambiguous[:FUZZY_LIMIT]]
                return answer('needs_verification',
                              'Multiple distinct Scopus source records match the same identity; '
                              'manual identity review required')
            return self._serial_answer(selected, method, identifiers, titles, year, metadata)

        # A non-serial candidate may still match a proceedings title exactly.
        if titles and (getattr(candidate, 'classification', '') or '') == 'non_journal':
            title_hits = [(record, 'proceedings_title', title) for title in titles
                          for record in self.by_proceedings_title.get(title, [])]
            if title_hits:
                return self._proceedings_answer(title_hits, year, publisher, metadata)

        suggestions = self._fuzzy(titles[0]) if titles else []
        if suggestions:
            metadata['match_method'] = 'fuzzy_candidates'
            metadata['candidates'] = suggestions
            return answer('needs_verification',
                          'Fuzzy similarity supplies candidates only; Scopus identity is not confirmed')
        attempted = 'issn' if identifiers else 'isbn' if isbns else 'title_exact'
        metadata['match_method'] = attempted
        return answer('not_found',
                      'No matching source identity in the local Scopus master by ' + attempted)

    def _select_serial(self, hits):
        """One source may appear in several sheets; keep the current description
        and never auto-pick between distinct source records."""
        by_identity = {}
        for record, _key in hits:
            identity = (record.get('source_record_id')
                        or normalize_title(record.get('title') or ''))
            current = by_identity.get(identity)
            if current is None or SHEET_PRIORITY[record['sheet']] < SHEET_PRIORITY[current['sheet']]:
                by_identity[identity] = record
        records = list(by_identity.values())
        current = [r for r in records if r['sheet'] == SHEET_SOURCES]
        historical = [r for r in records
                      if r['sheet'] in (SHEET_DISCONTINUED, SHEET_SERIAL_CONF)]
        accepted = [r for r in records if r['sheet'] == SHEET_ACCEPTED]
        for tier in (current, historical, accepted):
            if tier:
                return (tier[0], None) if len(tier) == 1 else (None, tier)
        return None, []

    def _serial_answer(self, record, method, identifiers, titles, year, metadata):
        metadata['identity_match'] = True
        sheet = record['sheet']
        record_issns = record.get('issns', frozenset())
        issn_value = next(iter(sorted(issns(record.get('issn_raw')))), None)
        eissn_value = next(iter(sorted(issns(record.get('eissn_raw')))), None)
        if issn_value is None and eissn_value is None and record_issns:
            issn_value = sorted(record_issns)[0]
        if method == 'issn' and identifiers & record_issns:
            matched = sorted(identifiers & record_issns)[0]
        else:
            matched = normalize_title(record.get('title') or '') or (titles[0] if titles else None)
        values = {'matched_identity': matched,
                  'match_confidence': 'high' if method == 'issn' else 'medium',
                  'source_sheet': sheet}
        optional = {'source_record_id': record.get('source_record_id'),
                    'source_title': record.get('title'), 'source_type': record.get('source_type'),
                    'issn': issn_value, 'eissn': eissn_value, 'coverage': record.get('coverage'),
                    'active_status': record.get('active_status'),
                    'publisher': record.get('publisher'), 'asjc': record.get('asjc')}
        values.update({key: value for key, value in optional.items() if value is not None})
        if sheet in (SHEET_SOURCES, SHEET_DISCONTINUED, SHEET_SERIAL_CONF):
            values['discontinued'] = bool(record.get('discontinued'))
        if sheet == SHEET_SOURCES:
            active = record.get('active')
            if active is True:
                if in_coverage(year, record.get('coverage')):
                    status, reason = 'indexed', ('Active Scopus source matched by ' + method +
                                                 '; publication year is within the declared coverage')
                elif year is not None:
                    status, reason = 'indicated', ('Active Scopus source matched by ' + method +
                                                   f', but publication year {year} is outside the '
                                                   'declared coverage')
                else:
                    status, reason = 'indicated', ('Active Scopus source matched by ' + method +
                                                   '; no publication year evidence to confirm coverage')
            elif active is False:
                status, reason = 'indicated', ('Source is Inactive in the Scopus master; identity '
                                               'matched but current indexing is not established')
            else:
                status, reason = 'needs_verification', ('Source active status is missing in the '
                                                        'master; identity match cannot be graded')
        elif sheet == SHEET_DISCONTINUED:
            twin = self._active_twin(record)
            if twin is not None and in_coverage(year, twin.get('coverage')):
                status, reason = 'indexed', ('Discontinued listing matches a source that is Active '
                                             'in the current master and the year is within its coverage')
            else:
                status, reason = 'indicated', ('Source is discontinued in Scopus; a historical '
                                               'identity match cannot prove current indexing')
        elif sheet == SHEET_SERIAL_CONF:
            status, reason = 'indicated', ('Serial conference proceedings profile matched; profile '
                                           'membership alone is not active indexing evidence')
        else:
            status, reason = 'indicated', ('Title is accepted for addition to Scopus, but '
                                           'acceptance is not evidence of active indexing')
        metadata['scopus_status'] = status
        return _finish(status, reason, metadata, values)

    def _proceedings_answer(self, hits, year, publisher, metadata):
        # The export lists one proceedings identity once per piece (distinct
        # EID, identical title/ISBN/year); group hits by identity, never by EID.
        identities, seen = {}, set()
        for record, method, key in hits:
            if id(record) in seen:
                continue
            seen.add(id(record))
            identity = (normalize_title(record.get('proceedings_title')
                                        or record.get('title') or ''), record.get('year'))
            identities.setdefault(identity, []).append((record, method, key))
        if len(identities) > 1 and year is not None:
            # Publication year is supporting evidence: it may disambiguate
            # year-distinct proceedings identities sharing one ISBN.
            matching = {identity: group for identity, group in identities.items()
                        if identity[1] == year}
            if len(matching) == 1:
                identities = matching
        if len(identities) > 1:
            metadata['match_method'] = hits[0][1]
            metadata['identity_match'] = True
            metadata['candidates'] = [self._summary(record)
                                      for record, _method, _key in hits[:FUZZY_LIMIT]]
            metadata['scopus_status'] = 'needs_verification'
            return _finish('needs_verification',
                           'Multiple distinct proceedings records match the same identity; '
                           'manual review required', metadata, {})
        group = next(iter(identities.values()))
        method = group[0][1]
        metadata['match_method'] = method
        metadata['identity_match'] = True
        eids = sorted({record['eid'] for record, _m, _k in group if record.get('eid')})
        if eids:
            metadata['eids'] = eids[:FUZZY_LIMIT]
        record, _method, key = group[0]
        metadata['publisher_match'] = bool(
            publisher and record.get('publisher') and
            normalize_title(publisher) == normalize_title(record['publisher']))
        if record.get('eid'):
            metadata['eid'] = record['eid']
        values = {'matched_identity': key,
                  'match_confidence': 'high' if method == 'isbn' else 'medium',
                  'source_sheet': record['sheet']}
        optional = {'source_title': record.get('title'), 'isbn': key,
                    'publisher': record.get('publisher'), 'asjc': record.get('asjc')}
        values.update({name: value for name, value in optional.items() if value is not None})
        if method == 'isbn':
            if year is None:
                status, reason = 'indicated', ('Proceedings record matched by ISBN; no publication '
                                               'year evidence')
            elif record.get('year') is None:
                status, reason = 'indicated', ('Proceedings record matched by ISBN; the record '
                                               'carries no year to corroborate')
            elif year == record['year']:
                status, reason = 'indexed', ('Proceedings record matched by ISBN and the '
                                             'publication year matches the record year')
            else:
                status, reason = 'indicated', (f'Proceedings record matched by ISBN, but publication '
                                               f"year {year} differs from the record year {record['year']}")
        else:
            status, reason = 'indicated', ('Proceedings title matched exactly; a title match alone '
                                           'is not indexing evidence')
        metadata['scopus_status'] = status
        return _finish(status, reason, metadata, values)

    def _active_twin(self, record):
        twin = self.active_by_id.get(record.get('source_record_id'))
        if twin is None:
            for identifier in sorted(record.get('issns', frozenset())):
                twin = self.active_by_issn.get(identifier)
                if twin is not None:
                    break
        return twin

    def _fuzzy(self, title):
        suggestions = []
        # ratio >= threshold bounds the length ratio tightly; use that as a
        # cheap prefilter before quick_ratio across the whole catalogue.
        low, high = len(title) * 0.87, len(title) * 1.16
        for index, records in chain(self.by_title.items(), self.by_proceedings_title.items()):
            if not low <= len(index) <= high:
                continue
            matcher = SequenceMatcher(None, title, index, autojunk=False)
            if matcher.quick_ratio() < FUZZY_THRESHOLD:
                continue
            similarity = matcher.ratio()
            if similarity >= FUZZY_THRESHOLD:
                suggestions.append(dict(self._summary(records[0]), similarity=round(similarity, 4)))
        return sorted(suggestions,
                      key=lambda item: (-item['similarity'],
                                        str(item.get('source_record_id') or item.get('eid') or ''))
                      )[:FUZZY_LIMIT]

    def _summary(self, record):
        summary = {'source_sheet': record['sheet']}
        for key in ('source_record_id', 'title', 'proceedings_title', 'eid'):
            if record.get(key):
                summary[key] = record[key]
        record_issns = sorted(record.get('issns', frozenset()))
        if record_issns:
            summary['issn'] = record_issns[0]
        record_isbns = sorted(record.get('isbns', frozenset()))
        if record_isbns:
            summary['isbn'] = record_isbns[0]
        return summary


def load_master(path=None):
    """Read the local ext_list Excel export; raise ScopusMasterError when unusable."""
    path = Path(path or DEFAULT_MASTER)
    try:
        content = path.read_bytes()
    except FileNotFoundError as exc:
        raise ScopusMasterError(f'Scopus master Excel not found: {path}') from exc
    except OSError as exc:
        raise ScopusMasterError(f'Cannot read Scopus master Excel: {path}') from exc
    try:
        workbook = load_workbook(path, read_only=True, data_only=True)
    except Exception as exc:
        raise ScopusMasterError(f'Cannot open Scopus master Excel: {path}') from exc
    try:
        sheets = {}
        for sheet_name in SHEET_ORDER:
            if sheet_name not in workbook.sheetnames:
                raise ScopusMasterError(
                    f'Scopus master Excel is missing sheet "{sheet_name}": {path}')
            sheets[sheet_name] = _read_sheet(workbook[sheet_name], sheet_name, path)
    finally:
        workbook.close()
    return ScopusMaster(sheets, path.resolve(), hashlib.sha256(content).hexdigest())


def check(candidate, *, master=None, master_path=None):
    """Load the local master lazily when not supplied; master errors become 'error' results.

    The pinned default export takes on the order of a minute to parse, so the
    default master is cached per process; batch callers that omit master=
    never reload it per candidate. Explicit master_path values are always
    loaded fresh (they are typically small test fixtures).
    """
    path = master_path or DEFAULT_MASTER
    try:
        if master is not None:
            return master.check(candidate)
        if master_path is None:
            key = str(DEFAULT_MASTER)
            loaded = _MASTER_CACHE.get(key)
            if loaded is None:
                loaded = load_master(DEFAULT_MASTER)
                _MASTER_CACHE[key] = loaded
            return loaded.check(candidate)
        return load_master(path).check(candidate)
    except ScopusMasterError as exc:
        return result('scopus', 'error', str(exc),
                      metadata={'match_method': None, 'master_file': str(path)})


# --- Manual article-level verification (Scopus Document Search evidence) ---
#
# Manual evidence is evaluated independently of the automatic Excel-master
# result above: it never feeds, overrides, or suppresses that result. DOI and
# EID are optional and never required; only a proven article identity may
# grade a verified record as 'indexed'. ISBN-only or proceedings-title-only
# corroboration is never sufficient, because it identifies the venue, not the
# article.

EVIDENCE_MAX_BYTES = 2_000_000
EVIDENCE_REQUIRED = {
    'raw_publication_id': int,
    'verification_source': str,
    'title': str,
    'first_author': str,
    'source_title': str,
    'publication_year': int,
    'verified': bool,
}


class ScopusEvidenceError(ValueError):
    """A missing, unreadable, or malformed manual Scopus evidence JSONL file."""


def load_manual_evidence(path):
    """Parse a JSONL file of manual Scopus document-check evidence records.

    Returns ``(evidence_by_id, duplicates)``: one validated record per
    raw_publication_id (the first occurrence wins) and the number of skipped
    duplicate lines. Blank lines are skipped; any malformed line raises
    ScopusEvidenceError naming the line number. DOI and EID are optional and
    may be null, empty, or absent, as is the optional alternate_titles list
    (absent or null means an empty list).
    """
    path = Path(path)
    try:
        if path.stat().st_size > EVIDENCE_MAX_BYTES:
            raise ScopusEvidenceError(
                f'Manual evidence file is oversized (> {EVIDENCE_MAX_BYTES} bytes): {path}')
        text = path.read_text(encoding='utf-8-sig')
    except OSError as exc:
        raise ScopusEvidenceError(f'Cannot read manual evidence file: {path}') from exc
    except UnicodeDecodeError as exc:
        raise ScopusEvidenceError(f'Manual evidence file is not valid UTF-8: {path}') from exc
    by_id, duplicates = {}, 0
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except ValueError as exc:
            raise ScopusEvidenceError(
                f'Manual evidence line {line_number}: invalid JSON ({exc})') from exc
        if not isinstance(obj, dict):
            raise ScopusEvidenceError(
                f'Manual evidence line {line_number}: expected a JSON object')
        for field_name in EVIDENCE_REQUIRED:
            if field_name not in obj or obj[field_name] is None:
                raise ScopusEvidenceError(
                    f'Manual evidence line {line_number}: missing required field "{field_name}"')
        _validate_evidence_types(obj, line_number)
        for optional in ('doi', 'eid'):
            value = obj.get(optional)
            if value is not None and not isinstance(value, str):
                raise ScopusEvidenceError(
                    f'Manual evidence line {line_number}: '
                    f'field "{optional}" must be a string or null')
        alternates = _validate_alternate_titles(obj, line_number)
        record = {
            'raw_publication_id': obj['raw_publication_id'],
            'verification_source': obj['verification_source'].strip(),
            'title': obj['title'].strip(),
            'first_author': obj['first_author'].strip(),
            'source_title': obj['source_title'].strip(),
            'publication_year': obj['publication_year'],
            'verified': obj['verified'],
            'doi': obj['doi'].strip() or None if isinstance(obj.get('doi'), str) else None,
            'eid': obj['eid'].strip() or None if isinstance(obj.get('eid'), str) else None,
            # Optional localized-title variants of the SAME article; matching
            # candidates only, never written anywhere.
            'alternate_titles': alternates,
        }
        raw_id = record['raw_publication_id']
        if raw_id in by_id:
            duplicates += 1
            continue
        by_id[raw_id] = record
    return by_id, duplicates


def _validate_evidence_types(obj, line_number):
    for field_name, expected in EVIDENCE_REQUIRED.items():
        value = obj[field_name]
        if expected is bool:
            if not isinstance(value, bool):
                raise ScopusEvidenceError(
                    f'Manual evidence line {line_number}: field "verified" must be a boolean')
        elif expected is int:
            if isinstance(value, bool) or not isinstance(value, int):
                raise ScopusEvidenceError(
                    f'Manual evidence line {line_number}: field "{field_name}" must be an integer')
        elif not isinstance(value, str) or not value.strip():
            raise ScopusEvidenceError(
                f'Manual evidence line {line_number}: '
                f'field "{field_name}" must be a non-empty string')


def _validate_alternate_titles(obj, line_number):
    """Validate the optional alternate_titles list; absent/null means empty."""
    alternates = obj.get('alternate_titles')
    if alternates is None:
        return []
    if not isinstance(alternates, list):
        raise ScopusEvidenceError(
            f'Manual evidence line {line_number}: '
            f'field "alternate_titles" must be a list of strings or null')
    for item in alternates:
        if not isinstance(item, str) or not item.strip():
            raise ScopusEvidenceError(
                f'Manual evidence line {line_number}: field "alternate_titles" '
                f'must contain only non-empty strings')
    return [item.strip() for item in alternates]


def normalize_person(value):
    """NFKC, casefold, strip punctuation, collapse spaces for one person name."""
    text = unicodedata.normalize('NFKC', str(value or '')).casefold()
    return ' '.join(''.join(c if c.isalnum() or c.isspace() else ' ' for c in text).split())


def _person_parts(value):
    """Split a person name into (surname, given), tolerating 'Doe, J.' and 'John Doe'."""
    raw = str(value or '')
    if ',' in raw:
        surname_raw, _comma, given_raw = raw.partition(',')
        return normalize_person(surname_raw), normalize_person(given_raw)
    tokens = normalize_person(raw).split()
    if not tokens:
        return '', ''
    if len(tokens) == 1:
        return tokens[0], ''
    return tokens[-1], ' '.join(tokens[:-1])


def person_matches(evidence_name, row_name):
    """Conservative person comparison: exact surname plus first given initial.

    A missing given name on either side cannot contradict the surname, so it
    stays compatible; a present initial must match the other initial.
    """
    surname_a, given_a = _person_parts(evidence_name)
    surname_b, given_b = _person_parts(row_name)
    if not surname_a or not surname_b or surname_a != surname_b:
        return False
    initial_a, initial_b = given_a[:1], given_b[:1]
    if not initial_a or not initial_b:
        return True
    return initial_a == initial_b


def _doi_equal(a, b):
    """Compare DOIs through the shared Crossref normalizer; invalid never matches."""
    try:
        return normalize_doi(a) == normalize_doi(b)
    except ValueError:
        return False


def _year_int(value):
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        text = value.strip()
        return int(text) if text.isdigit() else None
    return None


def _titles_match(a, b):
    left, right = normalize_title(a), normalize_title(b)
    return bool(left) and bool(right) and left == right


def _evidence_title_candidates(evidence):
    """Evidence title candidates: the main title plus declared alternate titles."""
    titles = [evidence.get('title')]
    alternates = evidence.get('alternate_titles')
    if isinstance(alternates, (list, tuple)):
        titles.extend(alternates)
    return titles


def _row_title_candidates(row):
    """RAW title candidates: the main title plus any alternate titles it carries.

    RAW rows do not expose alternate titles today; the key is read defensively
    so a future RAW column needs no matcher change.
    """
    titles = [row.get('title')]
    alternates = row.get('alternate_titles')
    if isinstance(alternates, (list, tuple)):
        titles.extend(alternates)
    return titles


def _titles_match_any(evidence, row):
    """At least one evidence title candidate must equal one RAW title candidate."""
    for evidence_title in _evidence_title_candidates(evidence):
        for row_title in _row_title_candidates(row):
            if _titles_match(evidence_title, row_title):
                return True
    return False


def _row_first_author(row):
    """First author of the RAW record, from the detail page or the profile list."""
    for key in ('detail_authors', 'authors'):
        value = (row.get(key) or '').strip()
        if not value:
            continue
        separator = ';' if ';' in value else ','
        return value.split(separator)[0].strip()
    return ''


def _source_titles_match(value, row):
    target = normalize_title(value)
    if not target:
        return False
    for key in ('detail_journal', 'detail_venue', 'venue'):
        observed = normalize_title(row.get(key))
        if observed and observed == target:
            return True
    return False


def _identity_conflicts(evidence, row, alternate_titles=False):
    """Name the first conflicting or insufficient article-identity field.

    With alternate_titles enabled (bibliographic DOI fallback only), the
    evidence title may match any RAW title candidate - the main title or the
    row's alternate titles - and the evidence's own alternate titles count as
    title candidates too; first_author, year and source checks are unchanged.
    """
    if alternate_titles:
        if not _titles_match_any(evidence, row):
            return 'no title candidate (main or alternate) matches the RAW title'
    elif not _titles_match(evidence.get('title'), row.get('title')):
        return 'title differs or is missing'
    if not person_matches(evidence.get('first_author'), _row_first_author(row)):
        return 'first_author differs or is missing'
    evidence_year = _year_int(evidence.get('publication_year'))
    row_year = _year_int(row.get('publication_year'))
    if evidence_year is None or row_year is None or evidence_year != row_year:
        return 'publication_year differs or is missing'
    if not _source_titles_match(evidence.get('source_title'), row):
        return 'source_title differs or is missing'
    return ''


def check_manual_evidence(evidence, row):
    """Grade one manual Scopus document-check record against one RAW row.

    Status rules (verbatim internal vocabulary, mirrored in
    metadata['scopus_status']): 'indexed' only when the evidence is verified
    AND article identity is proven by (a) a DOI matching the RAW detail DOI,
    (b) an EID corroborated by a conservative RAW title match, (c) - with no
    DOI/EID at all - conservative matches on title, first author, publication
    year and source title all at once, or (d) - with an evidence DOI but an
    empty RAW DOI - the same full conservative bibliographic identity, where
    the title may match any RAW title candidate (main or the row's alternate
    titles) and the evidence's alternate titles count as title candidates too.
    A DOI
    present on both sides but different is a hard conflict and never falls
    back to bibliographic matching. Everything else, including verified=false,
    an EID without a matching title, any conflicting identity field, or
    ISBN-only/proceedings-title-only corroboration, is 'needs_verification'
    with the reason naming the field.
    """
    metadata = {'verification_source': evidence.get('verification_source'),
                'evidence': 'manual'}

    def answer(status, reason, **values):
        metadata['scopus_status'] = status
        return _finish(status, reason, metadata, values, source='scopus_manual')

    if evidence.get('verified') is not True:
        return answer('needs_verification',
                      'Manual evidence marks the article as not verified in Scopus')
    doi, eid = evidence.get('doi'), evidence.get('eid')
    row_doi = row.get('detail_doi')
    if doi:
        if row_doi:
            if _doi_equal(doi, row_doi):
                metadata['identity_method'] = 'doi'
                return answer('indexed', 'Manual Scopus document check verified the article; '
                                         'the evidence DOI matches the RAW detail DOI',
                              matched_identity=normalize_doi(doi))
            # A DOI present on both sides but different is a hard conflict;
            # bibliographic similarity must never override it.
            return answer('needs_verification',
                          'Manual evidence carries a DOI but the RAW record has a different one')
        # RAW DOI is empty: conservative bibliographic identity may still prove
        # the same article; the evidence DOI is never written back into RAW.
        # Alternate titles (evidence-side and row-side) are title candidates here.
        conflicts = _identity_conflicts(evidence, row, alternate_titles=True)
        if conflicts:
            return answer('needs_verification',
                          'Manual evidence carries a DOI but the RAW record has no DOI and the '
                          'bibliographic identity does not prove the same article: ' + conflicts)
        metadata['identity_method'] = 'bibliographic'
        return answer('indexed', 'Manual Scopus document check verified the article; the RAW '
                                 'record has no DOI, but title, first author, publication year '
                                 'and source title all match the RAW record',
                      matched_identity=normalize_title(evidence.get('title')))
    if eid:
        if _titles_match(evidence.get('title'), row.get('title')):
            metadata['identity_method'] = 'eid_title'
            return answer('indexed', 'Manual Scopus document check verified the article; '
                                     'the evidence EID is corroborated by the RAW title',
                          matched_identity=eid, eid=eid)
        return answer('needs_verification',
                      'Manual evidence carries an EID but its title does not match the RAW title')
    conflicts = _identity_conflicts(evidence, row)
    if conflicts:
        return answer('needs_verification',
                      'Manual evidence without DOI or EID lacks a proven article identity: '
                      + conflicts)
    metadata['identity_method'] = 'title_author_year_source'
    return answer('indexed', 'Manual Scopus document check verified the article; title, first '
                             'author, publication year and source title all match the RAW record',
                  matched_identity=normalize_title(evidence.get('title')))


def unmatched_manual_evidence(evidence):
    """Result for evidence whose raw_publication_id is outside the bounded window.

    Unmatched evidence is reported, never silently dropped and never an error.
    """
    metadata = {'verification_source': evidence.get('verification_source'),
                'evidence': 'manual', 'scopus_status': 'needs_verification'}
    return _finish('needs_verification', 'raw_publication_not_in_window', metadata, {},
                   source='scopus_manual')
