"""Pure, conservative article matching over already parsed Crossref metadata.

No transport, storage, catalogue matching, or current-time lookup occurs here.
Retrieval timestamps are retained so the same inputs give the same result.
"""
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import date
from difflib import SequenceMatcher
import math
import re
import unicodedata

from .crossref import CrossrefResult, normalize_doi

# Lexical veto for possible chemical notation, not an inference of work identity.
_ELEMENTS = set(('H He Li Be B C N O F Ne Na Mg Al Si P S Cl Ar K Ca Sc Ti V Cr Mn Fe Co Ni Cu Zn '
                 'Ga Ge As Se Br Kr Rb Sr Y Zr Nb Mo Tc Ru Rh Pd Ag Cd In Sn Sb Te I Xe Cs Ba La Ce '
                 'Pr Nd Pm Sm Eu Gd Tb Dy Ho Er Tm Yb Lu Hf Ta W Re Os Ir Pt Au Hg Tl Pb Bi Po At Rn '
                 'Fr Ra Ac Th Pa U Np Pu Am Cm Bk Cf Es Fm Md No Lr Rf Db Sg Bh Hs Mt Ds Rg Cn Nh Fl Mc Lv Ts Og').split())
_MATH_PUNCTUATION = '-+/=*<>^%|()[]{}′·'


@dataclass(frozen=True)
class ArticleMatchResult(CrossrefResult):
    selected_candidate: dict | None = None
    candidates_evaluated: int = 0
    evidence: list[dict] = field(default_factory=list)
    title_threshold: float = 0.95
    competitor_margin: float = 0.02
    input_conflicts: list[str] = field(default_factory=list)
    selected_candidate_index: int | None = None
    selected_conflicts: list[str] = field(default_factory=list)
    candidate_decisions: list[dict] = field(default_factory=list)


def _text(value):
    return value.strip() if isinstance(value, str) else ''


def _first(row, keys):
    return next(((_text(row.get(key)), key) for key in keys if _text(row.get(key))), ('', None))


def _width_text(value):
    return ''.join(unicodedata.normalize('NFKC', char)
                   if unicodedata.decomposition(char).startswith(('<wide>', '<narrow>')) else char
                   for char in _text(value))


def normalize_article_title(value):
    """NFC/casefold plus width variants, without flattening scientific notation.

    Superscripts/subscripts, decimal punctuation, Greek letters, operators,
    and brackets survive. Only cosmetic punctuation becomes whitespace.
    """
    text = unicodedata.normalize('NFC', _width_text(value)).casefold().replace('−', '-')
    for prime, count in (('″', 2), ('‴', 3), ('⁗', 4)):
        text = text.replace(prime, '′' * count)
    text = re.sub(r"\b([^\W\d_])(['’]{1,4})(?=\W|$)",
                  lambda match: match[1] + '′' * len(match[2]), text)
    result = []
    for index, char in enumerate(text):
        decimal = (char in '.,' and index > 0 and index + 1 < len(text)
                   and text[index - 1].isnumeric() and text[index + 1].isnumeric())
        factorial = char == '!' and bool(re.search(r'(?:\b[^\W\d_]|[0-9])$', text[:index]))
        keep = (char.isalnum() or unicodedata.category(char).startswith(('M', 'S'))
                or char in _MATH_PUNCTUATION or decimal or factorial)
        result.append(char if keep else ' ')
    return ' '.join(''.join(result).split())


def _signature(normalized):
    numbers, current = [], ''
    for index, char in enumerate(normalized):
        if char.isnumeric() or (char in '.,' and current and index + 1 < len(normalized)
                                and normalized[index + 1].isnumeric()):
            current += char
        elif current:
            numbers.append(current)
            current = ''
    if current:
        numbers.append(current)
    tokens = re.findall(r'\w+', normalized)
    return {
        'numbers': numbers,
        'formula_tokens': [token for token in tokens if any(c.isalpha() for c in token)
                           and any(c.isnumeric() for c in token)],
        'symbols': [c for c in normalized if unicodedata.category(c).startswith('S')
                    or c in _MATH_PUNCTUATION + '!'],
        'greek': [c for c in normalized if 'GREEK' in unicodedata.name(c, '')],
        'scripts': [c for c in normalized if unicodedata.decomposition(c).startswith(('<sub>', '<super>'))],
        'mathematical_letters': [c for c in normalized if
            unicodedata.decomposition(c).startswith('<font>') or unicodedata.name(c, '').startswith('MATHEMATICAL')],
        'variables': [token for token in tokens if len(token) == 1 and token.isascii()
                      and token.isalpha() and token not in ('a', 'i')],
    }


def _chemical_tokens(value):
    found = []
    for token in re.findall(r'\b[A-Za-z]{2,}\b', _width_text(value)):
        if token.casefold() in ('in', 'as', 'at', 'be', 'he', 'no', 'am', 'on'):
            continue
        parts = re.findall(r'[A-Z][a-z]?', token)
        if ''.join(parts) == token and all(part in _ELEMENTS for part in parts):
            found.append(token)
    return found


def _informative(title):
    generic = {'introduction', 'editorial', 'review', 'abstract', 'preface', 'untitled',
               'conclusion', 'study', 'a study', 'pendahuluan', 'pengantar'}
    letters = sum(c.isalpha() for c in title)
    compact_script = sum(any(name in unicodedata.name(c, '') for name in
                             ('CJK', 'HIRAGANA', 'KATAKANA')) for c in title) >= 6
    return title not in generic and ((letters >= 12 and len(title.split()) >= 2) or compact_script)


def _title_evidence(scholar_title, titles, threshold):
    normalized = normalize_article_title(scholar_title)
    chemicals = _chemical_tokens(scholar_title)
    comparisons = []
    for title in titles:
        other = normalize_article_title(title)
        other_chemicals = _chemical_tokens(title)
        chemical_conflict = bool(chemicals and other_chemicals and chemicals != other_chemicals)
        comparisons.append({'candidate_title': title, 'normalized': other,
            'similarity': SequenceMatcher(None, normalized, other, autojunk=False).ratio()
                          if normalized and other else 0.0,
            'exact': bool(normalized) and normalized == other,
            'protected_signature': _signature(other), 'chemical_tokens': other_chemicals,
            'scientific_case_conflict': chemical_conflict,
            'protected_conflict': bool(normalized and other) and
                (_signature(normalized) != _signature(other) or chemical_conflict)})
    best = max(comparisons, key=lambda item: item['similarity'], default={
        'candidate_title': None, 'normalized': '', 'similarity': 0.0, 'exact': False, 'protected_conflict': False})
    return dict(best, scholar_normalized=normalized, informative=_informative(normalized),
                available=any(item['normalized'] for item in comparisons),
                scholar_protected_signature=_signature(normalized), scholar_chemical_tokens=chemicals,
                threshold=threshold, variants=comparisons)


def _name(value):
    value = _text(value)
    if value.count(',') == 1:
        family, given = value.split(',')
        value = given + ' ' + family
    value = unicodedata.normalize('NFC', unicodedata.normalize('NFKC', value).casefold())
    value = value.replace("'", '').replace('’', '')
    return ' '.join(''.join(c if c.isalnum() or unicodedata.category(c).startswith('M') else ' '
                           for c in value).split())


def _without_accents(value):
    return ''.join(c for c in unicodedata.normalize('NFD', value) if not unicodedata.combining(c))


def _name_pair(scholar_name, author):
    given, family = _name(author.get('given')), _name(author.get('family'))
    full = ' '.join(part for part in (given, family) if part) or _name(author.get('name'))
    normalized = _name(scholar_name)
    if not normalized or not full:
        return None
    if normalized == full:
        if len(normalized.split()) == 1:
            return 'mononym'
        complete_name = _text(author.get('name')) and any(len(t) > 1 for t in full.split()[:-1])
        if (given and family and any(len(t) > 1 for t in given.split())) or complete_name:
            return 'full'
        return 'given_only' if given and not family else 'initials'
    if _without_accents(normalized) == _without_accents(full):
        return 'accent_variant'
    raw, family_parts, given_parts = normalized.split(), family.split(), given.split()
    if family_parts and raw == family_parts:
        return 'surname_only'
    if family_parts and not given_parts and raw[-len(family_parts):] == family_parts:
        return 'surname_only'
    if given_parts and not family_parts and raw[:len(given_parts)] == given_parts:
        return 'given_only'
    if given_parts and family_parts and raw[-len(family_parts):] == family_parts:
        raw_given = raw[:-len(family_parts)]
        if len(raw_given) == len(given_parts) and all(
                left == right or (len(left) == 1 and right.startswith(left))
                or (len(right) == 1 and left.startswith(right)) for left, right in zip(raw_given, given_parts)):
            return 'initials'
    return None


def _scholar_authors(row, records):
    records = records if records is not None else row.get('publication_authors')
    if records:
        if not isinstance(records, (list, tuple)) or not all(isinstance(r, Mapping) for r in records):
            return [], 'raw_relation', True, True, True
        orders = [r.get('author_order', index) for index, r in enumerate(records)]
        invalid = any(type(o) is not int or o < 0 for o in orders) or len(set(orders)) != len(orders)
        ordered = records if invalid else [r for _, r in sorted(zip(orders, records), key=lambda pair: pair[0])]
        names = [_text(r.get('author_name')) for r in ordered if _text(r.get('author_name'))]
        invalid_names = len(names) != len(records)
        truncated = (any(r.get('daftar_terpotong') is True for r in records)
                     or any(re.search(r'\.\.\.|…|\bet\s+al\b', name, re.I) for name in names))
        names = [name for name in names if name not in ('...', '…')]
        if names:
            return names, 'raw_relation', truncated, invalid, invalid_names
        if invalid_names or truncated:
            return [], 'raw_relation', truncated, invalid, invalid_names
    text, source = _first(row, ('detail_authors', 'authors'))
    truncated = bool(re.search(r'\.\.\.|…|\bet\s+al\b', text, re.I))
    text = re.sub(r'\.\.\.|…|\bet\s+al\.?', '', text, flags=re.I)
    return [name.strip() for name in re.split(';' if ';' in text else ',', text) if name.strip()], source, truncated, False, False


def _author_evidence(row, authors, records):
    names, source, truncated, invalid_order, invalid_records = _scholar_authors(row, records)
    crossref_names = [' '.join(_text(a.get(k)) for k in ('given', 'family')).strip()
                      or _text(a.get('name')) for a in authors]
    result = dict(source=source, scholar_names=names, crossref_names=crossref_names,
                  truncated=truncated, invalid_order=invalid_order, invalid_records=invalid_records,
                  pairs=[], order_consistent=None, relative_order_consistent=None,
                  ordered_full_subset=False, status='missing')
    if not names or not authors or not any(crossref_names):
        return result
    matrix = [[_name_pair(name, author) for author in authors] for name in names]
    used, paired = set(), set()
    for kind in ('full', 'accent_variant', 'initials', 'mononym', 'surname_only', 'given_only'):
        for index, possibilities in enumerate(matrix):
            if index in paired:
                continue
            available = [j for j, relation in enumerate(possibilities) if relation == kind and j not in used]
            if len(available) == 1:
                target = available[0]
                if sum(other not in paired and row_options[target] == kind
                       for other, row_options in enumerate(matrix)) != 1:
                    continue
                result['pairs'].append({'scholar_index': index, 'crossref_index': target, 'kind': kind})
                used.add(target)
                paired.add(index)
    result['pairs'].sort(key=lambda pair: pair['scholar_index'])
    result['order_consistent'] = all(p['scholar_index'] == p['crossref_index'] for p in result['pairs'])
    positions = [p['crossref_index'] for p in result['pairs']]
    result['relative_order_consistent'] = bool(positions) and all(
        left < right for left, right in zip(positions, positions[1:]))
    # A supported subset is distinct from complete/strong author evidence.
    # Require multiple unambiguous full names; initials, truncation, missing
    # names and reorderings cannot use this route to automatic identification.
    result['ordered_full_subset'] = (len(names) >= 2 and len(authors) > len(names)
        and len(paired) == len(names) and result['relative_order_consistent']
        and not (truncated or invalid_order or invalid_records)
        and all(crossref_names) and all(p['kind'] == 'full' for p in result['pairs']))
    if truncated or invalid_order or invalid_records or not all(crossref_names):
        result['status'] = 'partial'
    elif paired and len(authors) < len(names):
        result['status'] = 'partial'
    elif len(paired) < len(names):
        result['status'] = 'conflict' if any(not any(options) for options in matrix) else 'weak'
    elif len(names) != len(authors) or not result['order_consistent']:
        result['status'] = 'partial'
    else:
        result['status'] = ('strong' if any(p['kind'] == 'full' for p in result['pairs'])
                            and all(p['kind'] in ('full', 'initials', 'accent_variant') for p in result['pairs']) else 'weak')
    return result


def _year(value):
    if type(value) is int and 1 <= value <= 9999:
        return value
    if isinstance(value, str) and re.fullmatch(r'[0-9]{4}', value.strip()):
        year = int(value.strip())
        return year if year else None
    return None


def _scholar_date(value):
    text = _text(value)
    if not re.fullmatch(r'[0-9]{4}(?:[-/][0-9]{1,2}(?:[-/][0-9]{1,2})?)?', text):
        return []
    parts = [int(part) for part in re.split('[-/]', text)]
    try:
        date(*(parts + [1] * (3 - len(parts))))
    except (ValueError, OverflowError):
        return []
    return parts


def _year_evidence(row, dates):
    detail_parts = _scholar_date(row.get('detail_date'))
    primary = _year(row.get('publication_year')) or _year(row.get('year'))
    primary = primary or (detail_parts[0] if detail_parts else None)
    observed = {name: value['date_parts'] for name, value in dates.items()
                if name in ('published', 'issued', 'published-online', 'published-print') and value.get('date_parts')}
    result = dict(status='missing', scholar_year=primary, scholar_detail_date=detail_parts,
                  crossref_dates=deepcopy(observed),
                  crossref_date_precision={name: value.get('precision') for name, value in dates.items()
                                          if name in observed},
                  supporting_date_sources=[], conflict=False)
    if primary is None or not observed:
        return result
    years = {parts[0] for parts in observed.values()}
    preferred = next(observed[name][0] for name in ('published', 'issued', 'published-print', 'published-online') if name in observed)
    pair = {observed[name][0] for name in ('published-online', 'published-print') if name in observed}
    bridge = (len(pair) == 2 and max(pair) - min(pair) == 1 and primary in pair and preferred in pair)
    result['supporting_date_sources'] = [name for name, parts in observed.items() if parts[0] == primary]
    conflict = max(years) - min(years) > 1 or primary not in years
    if detail_parts and detail_parts[0] != primary:
        conflict |= not (bridge and detail_parts[0] in pair)
    # Article identity compares publication years, never equality of day/month.
    # Keep the historical one_year_difference label, now explicitly a conflict.
    result['conflict'] = bool(conflict)
    result['status'] = ('one_year_difference' if conflict and abs(primary - preferred) == 1
                        and (not detail_parts or detail_parts[0] == primary) and max(years) - min(years) <= 1
                        else 'conflict' if conflict else 'online_print_consistent' if bridge else 'consistent')
    return result


def _normalize_container(value):
    # Separate namespace from article-title normalization: only cosmetic
    # punctuation and the explicit conjunction alias are interchangeable.
    text = unicodedata.normalize('NFC', _width_text(value)).casefold().replace('&', ' and ')
    return ' '.join(''.join(' ' if unicodedata.category(c).startswith('P') else c for c in text).split())


def _container_evidence(row, containers):
    value, source = _first(row, ('detail_journal', 'detail_venue', 'venue'))
    normalized = _normalize_container(value)
    result = dict(source=source, scholar_container=value or None, crossref_containers=list(containers), status='missing')
    usable = [other for other in containers if _normalize_container(other)]
    if value.casefold() in ('-', '—', 'n/a', 'na', 'unknown', 'none', 'not available', 'tidak diketahui'):
        result['status'] = 'insufficient'
    elif not normalized or not usable:
        return result
    elif normalized in {_normalize_container(other) for other in usable}:
        result['status'] = 'match'
    elif normalized in ('unknown', 'n a', 'journal', 'conference', 'book') or (
            source == 'venue' and re.search(r'\b(vol|volume|issue|pp|pages)\b|\d+\s*\(\d+\)|\d+\s*[-–]\s*\d+', value, re.I)):
        result['status'] = 'insufficient'
    else:
        result['status'] = 'conflict'
    return result


def _doi(value):
    try:
        return normalize_doi(value).casefold()
    except (ValueError, UnicodeError):
        return None


def _input_issues(row, records):
    """Absence permits fallback; a populated malformed preferred field does not."""
    groups = [('detail_title', 'title'), ('detail_journal', 'detail_venue', 'venue'),
              ('detail_doi', 'doi'), ('publication_year', 'year'), ('detail_date',),
              ('detail_publication_type',)]
    if not (records if records is not None else row.get('publication_authors')):
        groups.append(('detail_authors', 'authors'))
    issues = []
    for group in groups:
        for key in group:
            value = row.get(key)
            if value is None or (isinstance(value, str) and not value.strip()):
                continue
            if key in ('publication_year', 'year'):
                valid = _year(value) is not None
            elif key in ('detail_doi', 'doi'):
                valid = _doi(value) is not None
            elif key == 'detail_date':
                valid = bool(_scholar_date(value))
            else:
                valid = isinstance(value, str)
            if not valid:
                issues.append('invalid_' + key)
            break
    return issues


def _evaluate(row, work, index, title, threshold, records):
    title_info = _title_evidence(title, work.get('titles', []), threshold)
    author_info = _author_evidence(row, work.get('authors', []), records)
    year_info = _year_evidence(row, work.get('publication_dates', {}))
    container_info = _container_evidence(row, work.get('container_titles', []))
    raw_doi, _ = _first(row, ('detail_doi', 'doi'))
    known_doi, candidate_doi = _doi(raw_doi), _doi(work.get('doi'))
    doi_info = dict(scholar_doi=raw_doi or None, candidate_doi=work.get('doi'),
                    equal=bool(known_doi) and known_doi == candidate_doi)
    conflicts = []
    if title_info['available'] and title_info['scholar_normalized'] and title_info['similarity'] < 0.80:
        conflicts.append('title_conflict')
    if title_info['protected_conflict']:
        conflicts.append('title_protected_content_conflict')
    for key, info in (('authors', author_info), ('year', year_info), ('container', container_info)):
        if info['status'] == 'conflict' or info.get('conflict'):
            conflicts.append(key + '_conflict')
    if raw_doi and (not known_doi or known_doi != candidate_doi):
        conflicts.append('doi_conflict')
    if not candidate_doi:
        conflicts.append('invalid_candidate_doi')
    expected = {'journal': 'journal-article', 'conference': 'proceedings-article',
                'proceedings': 'proceedings-article', 'book': 'book', 'book_chapter': 'book-chapter',
                'thesis': 'dissertation', 'dissertation': 'dissertation'}.get(_text(row.get('detail_publication_type')).casefold())
    if expected and work.get('type') and work['type'] != expected:
        conflicts.append('type_conflict')
    supported_partial = (author_info['status'] == 'partial' and author_info['ordered_full_subset']
                         and title_info['exact'] and container_info['status'] == 'match')
    eligible = (not conflicts and title_info['informative'] and title_info['similarity'] >= threshold
                and (author_info['status'] == 'strong' or supported_partial)
                and year_info['status'] in ('consistent', 'online_print_consistent'))
    return dict(candidate_index=index, doi=work.get('doi'), title=title_info, authors=author_info,
                year=year_info, container=container_info, doi_evidence=doi_info, conflicts=conflicts,
                eligible=eligible, plausible=title_info['similarity'] >= min(0.80, threshold) or doi_info['equal']
                    or (not title_info['available'] and author_info['status'] == 'strong'
                        and year_info['status'] in ('consistent', 'online_print_consistent')))


def match_article(scholar, crossref_result, *, raw_authors=None, title_threshold=0.95, competitor_margin=0.02):
    """Evaluate every supplied parsed candidate without I/O or input mutation.

    A single identity needs title + strong authors + year, or an exact title,
    matching container, year and a supported ordered full-name author subset.
    A close competitor lacking evidence still requires review; explicit
    conflicts disqualify competitors. Relevance scores never select a winner.
    """
    if not isinstance(crossref_result, CrossrefResult):
        raise TypeError('Expected a parsed CrossrefResult')
    if (type(title_threshold) not in (int, float) or not math.isfinite(title_threshold)
            or not 0 < title_threshold <= 1 or type(competitor_margin) not in (int, float)
            or not math.isfinite(competitor_margin) or not 0 <= competitor_margin <= 1):
        raise ValueError('Invalid matcher threshold or competitor margin')
    evidence = []
    input_conflicts = []

    def finish(status, reason, selected=None, selected_index=None):
        decisions = []
        for item in evidence:
            chosen = item['candidate_index'] == selected_index
            selection = 'selected' if chosen else 'rejected' if status == 'matched' else 'unresolved'
            explanation = (reason if chosen else 'metadata_conflict' if item['conflicts']
                else 'duplicate_selected_identity' if status == 'matched' and item['eligible']
                else 'no_plausible_title' if not item['plausible']
                else 'competing_identity' if item['eligible'] else 'insufficient_evidence')
            decisions.append(dict(candidate_index=item['candidate_index'], doi=item['doi'],
                                  selection=selection, reason=explanation, conflicts=list(item['conflicts'])))
        return ArticleMatchResult(status=status, lookup_method=crossref_result.lookup_method,
            fetched_at=crossref_result.fetched_at, reason=reason, candidates=deepcopy(crossref_result.candidates),
            http_status=crossref_result.http_status, attempts=crossref_result.attempts,
            selected_candidate=deepcopy(selected), candidates_evaluated=len(evidence),
            evidence=evidence, title_threshold=title_threshold, competitor_margin=competitor_margin,
            input_conflicts=list(input_conflicts), selected_candidate_index=selected_index,
            selected_conflicts=list(evidence[selected_index]['conflicts']) if selected_index is not None else [],
            candidate_decisions=decisions)

    if crossref_result.status == 'error':
        return finish('error', crossref_result.reason)
    if not isinstance(scholar, Mapping):
        return finish('error', 'invalid_scholar_publication')
    input_conflicts = _input_issues(scholar, raw_authors)
    title, _ = _first(scholar, ('detail_title', 'title'))
    for index, work in enumerate(crossref_result.candidates):
        try:
            evidence.append(_evaluate(scholar, work, index, title, title_threshold, raw_authors))
        except (ValueError, TypeError, AttributeError, KeyError, IndexError):
            evidence.append(dict(candidate_index=index, doi=None, eligible=False, plausible=True,
                                 conflicts=['invalid_candidate_metadata'], title={'similarity': 0.0}))
        evidence[-1]['conflicts'].extend(input_conflicts)
        if input_conflicts:
            evidence[-1]['eligible'] = False
    if input_conflicts:
        return finish('ambiguous', 'invalid_scholar_metadata')
    if not normalize_article_title(title):
        return finish('ambiguous', 'missing_scholar_title')
    if not _informative(normalize_article_title(title)):
        return finish('ambiguous', 'insufficient_scholar_title')
    qualified = [item for item in evidence if item['eligible']]
    identities = {_doi(item['doi']) for item in qualified}
    if len(identities) > 1:
        return finish('ambiguous', 'competing_candidates')
    if qualified:
        winner = qualified[0]
        for other in evidence:
            if other is winner or other['eligible']:
                continue
            if 'invalid_candidate_metadata' in other['conflicts']:
                return finish('ambiguous', 'unresolved_invalid_candidate')
            if _doi(other['doi']) == _doi(winner['doi']) and other['conflicts']:
                return finish('ambiguous', 'conflicting_records_for_same_doi')
            if (other['plausible'] and not other['conflicts']
                    and (not other['title']['available']
                         or other['title']['similarity'] >= winner['title']['similarity'] - competitor_margin)):
                return finish('ambiguous', 'unresolved_competing_candidate')
        return finish('matched', 'unique_consistent_candidate', crossref_result.candidates[winner['candidate_index']],
                      winner['candidate_index'])
    if any(item['plausible'] for item in evidence):
        return finish('ambiguous', 'insufficient_or_conflicting_evidence')
    return finish('not_found', 'no_plausible_candidate')
