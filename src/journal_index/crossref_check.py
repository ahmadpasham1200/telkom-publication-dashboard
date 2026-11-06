"""Bounded article inspection: read-only RAW input and explicit Crossref calls.

No I/O at import. This module never persists enrichment or journal verification.
"""
from copy import deepcopy
from datetime import datetime, timezone
import json
import unicodedata

import sqlalchemy as sa

from .crossref import CrossrefClient, CrossrefResult, normalize_doi
from .crossref_matcher import match_article


def _first(row, *keys):
    return next((row[k].strip() for k in keys if isinstance(row.get(k), str) and row[k].strip()), '')


def _authors(row):
    records = row.get('publication_authors')
    if records:
        return sorted(deepcopy(records), key=lambda a: a['author_order'])
    return _first(row, 'detail_authors', 'authors')


# Daftar kolom identitas RAW; dipakai ulang untuk mengenali baris duplikat
# pada pengisian RAW (crossref_store) agar kuncinya identik dengan pembacaan.
_RAW_IDENTITY_COLUMNS = '''p.raw_publication_id, p.title, p.authors, p.year, p.publication_year,
            p.detail_title, p.detail_authors, p.detail_date, p.detail_doi,
            p.venue, p.detail_venue,
            to_jsonb(p)->>'detail_journal' AS detail_journal,
            to_jsonb(p)->>'detail_publication_type' AS detail_publication_type,
            to_jsonb(p)->>'issn' AS issn, to_jsonb(p)->>'eissn' AS eissn,
            to_jsonb(p)->>'detail_issn' AS detail_issn, to_jsonb(p)->>'detail_eissn' AS detail_eissn,
            COALESCE((SELECT jsonb_agg(jsonb_build_object(
                'author_name', a.author_name, 'author_order', a.author_order,
                'daftar_terpotong', a.daftar_terpotong) ORDER BY a.author_order)
                FROM raw.scholar_publication_author a
                WHERE a.raw_publication_id = p.raw_publication_id), '[]'::jsonb)
                AS publication_authors'''


def bibliography_key(row):
    """Kunci identitas konservatif: DOI ternormalisasi + sidik jari bibliografi.

    Judul dinormalisasi aman (NFC + collapse spasi; kasus dipertahankan) dan
    tahun/tanggal/journal/tipe ikut, jadi kemiripan judul saja tidak pernah
    dianggap cukup. DOI ditangani terpisah, sama seperti deduplikasi
    select_publications.
    """
    title = _first(row, 'detail_title', 'title')
    doi = _first(row, 'detail_doi', 'doi')
    try:
        doi_key = normalize_doi(doi).casefold() if doi else ''
    except ValueError:
        doi_key = doi  # Let client/matcher report invalid DOI; never search instead.
    bibliography = json.dumps([unicodedata.normalize('NFC', ' '.join(title.split())), _authors(row),
        row.get('publication_year'), row.get('year'), row.get('detail_date'),
        _first(row, 'detail_journal', 'detail_venue', 'venue'),
        row.get('detail_publication_type')], ensure_ascii=True, sort_keys=True)
    return doi_key, bibliography


def read_publications(engine, row_limit, target, scholar_id=None):
    """Materialize one bounded snapshot, enforcing read-only on the server.

    Optional newer columns are extracted via to_jsonb for historical schemas.
    Connection closes (and rolls back its read transaction) before any HTTP.
    scholar_id membatasi snapshot ke satu profil Google Scholar
    (raw.scholar_publication.scholar_id); None mempertahankan perilaku lama.
    """
    with engine.connect() as conn:
        conn.execute(sa.text('SET TRANSACTION READ ONLY'))
        if conn.execute(sa.text('SELECT current_database()')).scalar_one() != target:
            raise ValueError('Database connection mismatch')
        params = {'row_limit': row_limit}
        scholar_filter = ''
        if scholar_id is not None:
            scholar_filter = ' AND p.scholar_id = :scholar_id'
            params['scholar_id'] = scholar_id
        rows = conn.execute(sa.text(f'''SELECT
            {_RAW_IDENTITY_COLUMNS}
            FROM raw.scholar_publication p
            WHERE COALESCE(NULLIF(btrim(p.detail_title), ''), NULLIF(btrim(p.title), '')) IS NOT NULL{scholar_filter}
            ORDER BY p.raw_publication_id LIMIT :row_limit'''), params).mappings()
        return [dict(row) for row in rows]


def select_publications(rows, limit, source):
    """Oldest RAW id wins; deduplicate DOI or an exact bibliography snapshot.

    Bibliographic equality retains numbers, symbols and case. Conflicting populated
    DOIs remain separate, even if their bibliographic fingerprints are identical.
    Missing DOI may reuse an already observed fingerprint without another lookup.
    """
    selected, seen_dois, seen_bibliography = [], set(), {}
    for row in sorted(rows, key=lambda r: r['raw_publication_id']):
        title = _first(row, 'detail_title', 'title')
        doi = _first(row, 'detail_doi', 'doi')
        if not title or (source == 'doi' and not doi):
            continue
        doi_key, bibliography = bibliography_key(row)
        observed = seen_bibliography.get(bibliography)
        if (doi_key and doi_key in seen_dois) or (
                observed is not None and (not doi_key or doi_key in observed)):
            continue
        if doi_key:
            seen_dois.add(doi_key)
        seen_bibliography.setdefault(bibliography, set()).add(doi_key)
        selected.append(row)
        if len(selected) >= limit:
            break
    return selected


def inspect_publications(rows, client, source, limit):
    """One lookup per selected article, with no automatic DOI-to-search fallback."""
    for row in select_publications(rows, limit, source):
        title = _first(row, 'detail_title', 'title')
        doi = _first(row, 'detail_doi', 'doi')
        method = 'doi_lookup' if source == 'doi' or (source == 'auto' and doi) else 'bibliographic_search'
        try:
            retrieval = client.lookup_doi(doi) if method == 'doi_lookup' else client.search_bibliographic(title)
            decision = match_article(row, retrieval)
        except Exception:
            # Unexpected per-article failures must not expose URLs/contact/config.
            retrieval = CrossrefResult(status='error', lookup_method=method,
                fetched_at=datetime.now(timezone.utc), reason='article_inspection_failed')
            decision = match_article(row, retrieval)
        selected = decision.selected_candidate or {}
        evidence = decision.evidence
        chosen = next((e for e in evidence if e['candidate_index'] == decision.selected_candidate_index), {})
        conflicts = (decision.selected_conflicts if decision.status == 'matched' else
                     sorted(set(decision.input_conflicts + [c for e in evidence for c in e['conflicts']])))
        yield dict(raw_publication_id=row['raw_publication_id'], scholar_title=title,
            scholar_authors=_authors(row), scholar_year=row.get('publication_year') or row.get('year'),
            scholar_date=row.get('detail_date'), lookup_strategy=method,
            candidate_count=len(retrieval.candidates), status=decision.status,
            selected_doi=selected.get('doi'), crossref_titles=selected.get('titles', []),
            crossref_authors=selected.get('authors', []), container_titles=selected.get('container_titles', []),
            issns=selected.get('issns', []), type=selected.get('type'),
            title_similarity=chosen.get('title', {}).get('similarity'),
            author_evidence=[e.get('authors', {}) for e in evidence],
            year_evidence=[e.get('year', {}) for e in evidence],
            container_evidence=[e.get('container', {}) for e in evidence],
            evidence=evidence, conflicts=conflicts, reason=decision.reason,
            selected_candidate_index=decision.selected_candidate_index,
            selected_evidence=chosen or None, selected_conflicts=decision.selected_conflicts,
            candidates_evaluated=decision.candidates_evaluated,
            candidate_decisions=decision.candidate_decisions,
            rejected_candidates=[item for item in decision.candidate_decisions if item['selection'] == 'rejected'],
            unresolved_candidates=[item for item in decision.candidate_decisions if item['selection'] == 'unresolved'],
            checked_at=decision.to_dict()['fetched_at'], http_status=decision.http_status,
            request_attempts=retrieval.attempts)


def run_inspection(engine, *, target, row_limit, limit, source, delay, mailto=None, scholar_id=None):
    rows = read_publications(engine, row_limit, target, scholar_id=scholar_id)
    # No client is created until all database reads have succeeded.
    selected = select_publications(rows, limit, source)
    if not selected:
        print(json.dumps({'mode': 'crossref', 'status': 'not_found',
                          'reason': 'no_eligible_publications_in_bounded_raw_window',
                          'row_limit': row_limit, 'articles_checked': 0}))
        return 0
    client = CrossrefClient(mailto=mailto, min_interval=delay)
    errors = 0
    try:
        for report in inspect_publications(selected, client, source, limit):
            print(json.dumps(report, ensure_ascii=True))
            errors += report['status'] == 'error'
    finally:
        client.close()
    return 1 if errors else 0
