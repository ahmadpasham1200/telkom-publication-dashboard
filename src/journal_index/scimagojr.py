"""SCImagoJR 2025 master export, entirely local. No network operations.

Legacy manual JSON evidence is retained as an explicit compatibility path.
The normal checker and CLI use the CSV master, never that evidence subset.
"""
import csv
import hashlib
import io
import math
import os
import re
from collections import defaultdict
from difflib import SequenceMatcher
from pathlib import Path
from .init import result, read_evidence, select_evidence, issns
from .init import normalize_title, classify_scholar_publication

DOMAINS = {'www.scimagojr.com', 'scimagojr.com'}
REFERENCE_YEAR = 2025
DEFAULT_MASTER = Path(__file__).resolve().parents[2] / 'data/scimago/scimagojr 2025.csv'


class MasterExportError(ValueError):
    """A missing, unreadable, or incompatible local master export."""


def _number(value, integer=False):
    value = (value or '').strip()
    pattern = r'\d+' if integer else r'\d+(?:[.,]\d+)?'
    if not re.fullmatch(pattern, value):
        return None
    if integer:
        return int(value)
    number = float(value.replace(',', '.'))
    return number if math.isfinite(number) else None


class MasterExport:
    def __init__(self, records, path, encoding, delimiter, digest):
        self.records = records
        self.path, self.encoding, self.delimiter = path, encoding, delimiter
        self.digest = digest
        self.by_title, self.by_issn = defaultdict(list), defaultdict(list)
        for record in records:
            if record['title']:
                self.by_title[normalize_title(record['title'])].append(record)
            for identifier in record['issns']:
                self.by_issn[identifier].append(record)

    def check(self, candidate):
        metadata = {'method': 'scimago_master_csv', 'ranking_source': 'scimago',
                    'reference_year': REFERENCE_YEAR, 'master_file': str(self.path),
                    'master_sha256': self.digest, 'master_encoding': self.encoding,
                    'master_delimiter': self.delimiter, 'identity_match': False}

        def answer(status, reason, **values):
            return result('scimagojr', status, reason, metadata=dict(metadata), **values)

        origin_types = {classify_scholar_publication(row)[0] for row in candidate.publications}
        if origin_types == {'non_journal'}:
            return answer('excluded', 'Only non-journal publication types are present')
        if origin_types != {'journal'}:
            return answer('unverified', 'Unknown or conflicting Scholar publication types; no ranking assigned')
        explicit_names = {(row.get('detail_journal') or '').strip() for row in candidate.publications
                          if (row.get('detail_journal') or '').strip()}
        if len({normalize_title(name) for name in explicit_names}) > 1:
            return answer('unverified', 'Conflicting explicit journal names require identity review')
        query_title = sorted(explicit_names)[0] if explicit_names else candidate.original
        if not query_title.strip():
            return answer('unverified', 'Explicit journal publication metadata is required before master matching')
        identifiers = set().union(*(issns(str(i)) for i in candidate.identifiers)) if candidate.identifiers else set()
        title_matches = self.by_title.get(normalize_title(query_title), [])
        if identifiers:
            found = []
            for identifier in sorted(identifiers):
                for record in self.by_issn.get(identifier, []):
                    if record not in found:
                        found.append(record)
            method = 'issn'
            if not found and title_matches:
                metadata['candidates'] = [self._summary(r) for r in title_matches[:5]]
                return answer('unverified', 'Journal title exists but trusted ISSNs do not match; no title fallback')
        else:
            found, method = title_matches, 'title_exact'
        metadata['match_method'] = method
        if len(found) > 1:
            metadata['candidates'] = [self._summary(r) for r in found[:5]]
            return answer('unverified', 'Multiple master identities match; manual identity review required')
        if found:
            record = found[0]
            for origin in candidate.publications:
                observed = set().union(*(issns(origin.get(key)) for key in
                    ('issn', 'eissn', 'detail_issn', 'detail_eissn')))
                if observed and not observed.intersection(record['issns']):
                    return answer('unverified', 'Same venue title has conflicting publication ISSNs; identities cannot be merged')
            if record['type'].casefold() != 'journal' or not (record['source_id'] or '').isdigit():
                return answer('unverified', 'Matched master source is not an identifiable journal')
            metadata.update(identity_match=True, source_id=record['source_id'],
                            title=record['title'], master_record=record)
            observation = {'year': REFERENCE_YEAR, 'sjr': record['sjr'],
                           'quartile': record['best_quartile'], 'scope': 'best_quartile', 'category': None}
            return answer('indexed', 'Unique journal identity matched in the local SCImagoJR 2025 master by ' + method,
                          evidence_url='https://www.scimagojr.com/journalsearch.php?q=' + record['source_id'] + '&tip=sid',
                          issn=next(iter(record['issns']), None), sjr=[observation])
        if not identifiers:
            suggestions = []
            normalized = normalize_title(query_title)
            for title, records in self.by_title.items():
                matcher = SequenceMatcher(None, normalized, title, autojunk=False)
                if matcher.quick_ratio() < 0.93:
                    continue
                similarity = matcher.ratio()
                if similarity >= 0.93:
                    suggestions.extend(dict(self._summary(r), similarity=round(similarity, 4))
                                       for r in records if r['type'].casefold() == 'journal')
            if suggestions:
                metadata['match_method'] = 'fuzzy_candidates'
                metadata['candidates'] = sorted(suggestions, key=lambda r: (-r['similarity'], r['source_id'] or ''))[:5]
                return answer('unverified', 'Fuzzy similarity supplies candidates only; journal identity is not confirmed')
        return answer('not_found', 'No matching journal identity in the complete local 2025 master by ' + method)

    @staticmethod
    def _summary(record):
        return {key: record[key] for key in ('source_id', 'title', 'issns', 'type')}


def load_master(path=DEFAULT_MASTER):
    """Read CSV positionally so duplicate Publisher columns are never lost."""
    path = Path(path)
    try:
        content = path.read_bytes()
    except FileNotFoundError as exc:
        raise MasterExportError(f'SCImagoJR 2025 master CSV not found: {path}') from exc
    except OSError as exc:
        raise MasterExportError('Cannot read SCImagoJR 2025 master CSV') from exc
    encodings = ('utf-16',) if content.startswith((b'\xff\xfe', b'\xfe\xff')) else ('utf-8-sig', 'cp1252')
    for encoding in encodings:
        try:
            text = content.decode(encoding)
            break
        except UnicodeError:
            continue
    else:
        raise MasterExportError('Unsupported SCImagoJR CSV encoding')
    try:
        dialect = csv.Sniffer().sniff(text[:65536], delimiters=',;\t|')
        reader = csv.reader(io.StringIO(text, newline=''), dialect)
        headers = [value.strip() for value in next(reader)]
        required = {'Sourceid', 'Title', 'Type', 'Issn', 'SJR', 'SJR Best Quartile',
                    'H index', 'Total Docs. (2025)'}
        if not required <= set(headers):
            raise MasterExportError('Invalid SCImagoJR 2025 CSV headers; expected 2025 edition and journal identity/ranking columns')
        records = []
        for line_number, values in enumerate(reader, 2):
            if not values or not any(value.strip() for value in values):
                continue
            if len(values) != len(headers):
                raise MasterExportError(f'Invalid SCImagoJR CSV column count near record {line_number}')
            columns = defaultdict(list)
            for header, value in zip(headers, values):
                columns[header].append(value.strip() or None)

            def first(header):
                return columns.get(header, [None])[0]

            quartile = (first('SJR Best Quartile') or '').upper()
            records.append({'source_id': first('Sourceid'), 'title': first('Title'),
                            'type': first('Type') or '', 'issn_raw': first('Issn'),
                            'issns': sorted(issns(first('Issn'))),
                            'publishers': columns.get('Publisher', []),
                            'sjr': _number(first('SJR')), 'sjr_raw': first('SJR'),
                            'best_quartile': quartile if re.fullmatch('Q[1-4]', quartile) else None,
                            'h_index': _number(first('H index'), integer=True),
                            'coverage': first('Coverage'), 'categories': first('Categories'),
                            'areas': first('Areas'), 'country': first('Country'), 'region': first('Region')})
        if not records:
            raise MasterExportError('SCImagoJR 2025 master CSV contains no data rows')
    except (csv.Error, StopIteration) as exc:
        raise MasterExportError('Cannot detect delimiter or parse SCImagoJR 2025 CSV') from exc
    return MasterExport(records, path.resolve(), encoding, dialect.delimiter, hashlib.sha256(content).hexdigest())


def parse_quartiles(record):
    """Keep each year/category observation separately, including best-quartile scope."""
    observations = []
    for item in record.get('sjr', []):
        quartile = str(item.get('quartile', '')).upper()
        year = str(item.get('year', ''))
        category = item.get('category')
        if re.fullmatch(r'Q[1-4]', quartile) and re.fullmatch(r'(19|20)\d{2}', year):
            observations.append({'quartile': quartile, 'year': int(year),
                                 'category': str(category) if category else None})
    return observations


def check_manual_evidence(candidate, evidence_path=None):
    records = read_evidence(evidence_path or os.getenv('SCIMAGO_EVIDENCE_FILE'), 'scimagojr')
    if records is None:
        return result('scimagojr', 'access_unavailable',
                      'Supply SCIMAGO_EVIDENCE_FILE with manually verified journal evidence')
    if isinstance(records, dict):
        return records
    selected = select_evidence(candidate, records, 'scimagojr', DOMAINS)
    if isinstance(selected, dict):
        return selected
    observations = [q for r in selected for q in parse_quartiles(r)]
    first = selected[0]
    if not observations:
        return result('scimagojr', 'unverified', 'Matched evidence has no valid year/quartile observations')
    return result('scimagojr', 'indexed', 'Manually supplied SCImago evidence; no inference about Scopus',
                  evidence_url=first['evidence_url'], sjr=observations,
                  issn=next(iter(issns(first.get('issn', ''))), None),
                  eissn=next(iter(issns(first.get('eissn', ''))), None),
                  metadata={'method': 'manual_evidence',
                            'evidence_urls': sorted({r['evidence_url'] for r in selected})})


def check(candidate, evidence_path=None):
    """Default to the full local master; explicit JSON paths retain old callers."""
    if evidence_path is not None and Path(evidence_path).suffix.lower() == '.json':
        return check_manual_evidence(candidate, evidence_path)
    try:
        return load_master(evidence_path or DEFAULT_MASTER).check(candidate)
    except MasterExportError as exc:
        return result('scimagojr', 'error', str(exc),
                      metadata={'method': 'scimago_master_csv', 'ranking_source': 'scimago',
                                'reference_year': REFERENCE_YEAR})
