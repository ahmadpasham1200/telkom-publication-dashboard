"""Matched article -> serial identity -> local master evidence, never HTTP."""
from copy import deepcopy
import re
import unicodedata

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY, insert

from .init import Candidate, classify_scholar_publication, issns, normalize_title, result


def serial_title(value):
    text = unicodedata.normalize('NFC', value).casefold().replace('&', ' and ')
    return ' '.join(''.join(' ' if unicodedata.category(c).startswith('P') else c for c in text).split())


def match_serial(row, enrichment, master):
    """Never use unselected search candidates, nor infer journal type from ISSN."""
    def unresolved(reason, status='unverified'):
        return None, result('scimagojr', status, reason,
            metadata={'method': 'crossref_identity_gate', 'identity_match': False,
                      'crossref_status': enrichment.status})

    if enrichment.status != 'matched' or not enrichment.selected_candidate:
        return unresolved('Crossref article identity is not matched; no serial evidence used')
    work = enrichment.selected_candidate
    kind = work.get('type')
    if kind in ('proceedings-article', 'book', 'book-chapter', 'dissertation', 'posted-content',
                'reference-entry', 'report', 'dataset', 'peer-review', 'standard'):
        return unresolved('Crossref publication type is not a journal article', 'excluded')
    if kind != 'journal-article':
        return unresolved('Unknown Crossref publication type; no journal ranking assigned')
    if classify_scholar_publication(row)[0] == 'non_journal':
        return unresolved('Scholar and Crossref publication types conflict')
    titles = [t.strip() for t in work.get('container_titles', []) if t.strip()]
    if not titles or len({serial_title(t) for t in titles}) != 1:
        return unresolved('Missing or conflicting Crossref container identities')
    identifiers = set()
    for value in work.get('issns', []):
        if not re.fullmatch(r'\s*\d{4}-?\d{3}[\dXx]\s*', value) or not issns(value):
            return unresolved('Malformed or checksum-invalid Crossref ISSN; no speculative title fallback')
        identifiers.update(issns(value))
    title = titles[0]
    # Pick a master spelling only for the conjunction/cosmetic equivalence,
    # without relaxing MasterExport's exact-title or fuzzy thresholds.
    aliases = [r for r in master.records if serial_title(r['title'] or '') == serial_title(title)]
    if not identifiers and len(aliases) > 1:
        return unresolved('Multiple master serial identities share equivalent container titles')
    query_title = aliases[0]['title'] if len(aliases) == 1 else title
    original = deepcopy(row)
    origin = dict(deepcopy(row), scholar_original=original,
                  crossref_provenance={'doi': work['doi'], 'retrieved_at': enrichment.to_dict()['fetched_at'],
                                       'status': enrichment.status, 'issns': work.get('issns', []),
                                       'issn_types': work.get('issn_types', []), 'containers': titles})
    # Preserve explicitly observed Scholar serial identifiers as independent evidence.
    for key in ('issn', 'eissn', 'detail_issn', 'detail_eissn'):
        origin[key] = row.get(key)
    candidate = Candidate(query_title, normalize_title(query_title), 'journal',
                          'Matched Crossref journal article; serial identity still requires local master',
                          publications=[dict(origin, detail_publication_type='journal', detail_journal=query_title)],
                          identifiers=identifiers)
    checked = master.check(candidate)
    # The master adapter needs a classified query view. The persisted provenance
    # must retain Scholar values even at their original top-level field names.
    candidate.publications = [origin]
    if checked['status'] == 'indexed':
        record = checked['metadata']['master_record']
        if any(serial_title(t) != serial_title(record['title']) for t in titles):
            return unresolved('Crossref ISSN matches a master record but container title conflicts')
        # Canonical master identity avoids grouping unrelated Scholar venues.
        candidate.original, candidate.normalized = record['title'], normalize_title(record['title'])
    checked['metadata']['crossref_provenance'] = deepcopy(origin['crossref_provenance'])
    checked['metadata']['raw_publication_id'] = row['raw_publication_id']
    checked['metadata']['scholar_original'] = original
    return candidate, checked


def save_master_verification(engine, candidate, checked, cache_days=30):
    """Add article origins; preserve every existing source verification snapshot.

    No DDL. Only confirmed local master identities may enter this write path.
    Catalogue matching reuses a single existing serial identity even under aliases.
    """
    from .run_journal_index import catalog, table, upsert_catalog, verification_status
    from datetime import timedelta

    if candidate is None or verification_status(checked) != 'verified':
        return 'not_saved_unverified'
    identifiers = set(checked['metadata']['master_record']['issns'])
    identity = dict(title=checked['metadata']['title'], identifiers=sorted(identifiers),
                    issn=checked.get('issn'), eissn=checked.get('eissn'),
                    source_id=checked['metadata']['source_id'])
    with engine.begin() as conn:
        # Serialize this path, including previously absent titles/serials. Existing
        # catalogue APIs remain untouched; conflicting rows are never overwritten.
        conn.execute(sa.text("SELECT pg_advisory_xact_lock(hashtextextended('crossref-scimago-catalog', 0))"))
        known = list(conn.execute(sa.select(catalog).where(sa.or_(
            catalog.c.journal_normalized == candidate.normalized,
            catalog.c.identifiers.has_any(sa.cast(sorted(identifiers), ARRAY(sa.Text))))).with_for_update()).mappings())
        if len(known) > 1:
            return 'catalog_conflict_multiple_identities'
        candidate = deepcopy(candidate)
        if known:
            prior = known[0]
            prior_ids = set(prior['identifiers'] or {})
            if prior_ids and identifiers and not prior_ids.intersection(identifiers):
                return 'catalog_conflict_serial_identifiers'
            if prior['classification'] == 'non_journal':
                return 'catalog_conflict_publication_type'
            verified = conn.execute(sa.select(table).where(table.c.journal_id == prior['journal_id'],
                table.c.source == 'scimagojr')).mappings().first()
            old_id = ((verified or {}).get('matched_identity') or {}).get('source_id')
            if old_id and old_id != identity['source_id']:
                return 'catalog_conflict_verified_source'
            candidate.normalized, candidate.original = prior['journal_normalized'], prior['journal_original']
        journal_id = upsert_catalog(conn, candidate, identity, strict_identifiers=True)
        if journal_id is None:
            return 'catalog_conflict_concurrent_serial_identity'
        values = dict(journal_id=journal_id, source='scimagojr', status='verified', indexed=True,
            matched_identity=identity, evidence_url=checked['evidence_url'],
            checked_at=checked['checked_at'], expires_at=checked['checked_at'] + timedelta(days=cache_days),
            reason=checked['reason'], sjr=checked['sjr'], sinta_rank=None, sinta_period=None,
            metadata=deepcopy(checked['metadata']))
        stmt = insert(table).values(**values).on_conflict_do_nothing(
            constraint='uq_journal_verification_source').returning(table.c.verification_id)
        saved = conn.execute(stmt).scalar_one_or_none()
        return 'saved' if saved is not None else 'existing_verification_preserved'
