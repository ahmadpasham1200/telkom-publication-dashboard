"""Bounded read-only preview by default; --save explicitly enables checks and writes."""
import argparse
import json
import logging
import math
import os
import sys
import time
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, insert

from . import scopus, scimagojr, sinta, scimago_manual
from .init import candidates, result, issns, normalize_title
from .crossref_store import CrossrefSchemaError
from . import sinta_catalog

LOG = logging.getLogger(__name__)
SOURCES = {'scopus': scopus.check, 'scimagojr': scimagojr.check, 'sinta': sinta.check}
FAILED = ('error', 'access_unavailable', 'unverified')
# Kode keluar mode --sync-sinta-catalog: 0 LENGKAP, 1 PARTIAL, 2 GAGAL, 3 BLOCKED.
SINTA_EXIT_CODES = {'LENGKAP': 0, 'PARTIAL': 1, 'GAGAL': 2, 'BLOCKED': 3}
metadata = sa.MetaData(schema='raw')
catalog = sa.Table(
    'journal_catalog', metadata,
    sa.Column('journal_id', sa.BigInteger, primary_key=True),
    sa.Column('journal_original', sa.Text, nullable=False),
    sa.Column('journal_normalized', sa.Text, nullable=False),
    sa.Column('issn', sa.Text), sa.Column('eissn', sa.Text),
    sa.Column('identifiers', JSONB, nullable=False),
    sa.Column('classification', sa.Text, nullable=False),
    sa.Column('classification_reason', sa.Text, nullable=False),
    sa.Column('publication_origins', JSONB, nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.UniqueConstraint('journal_normalized', name='uq_journal_catalog_title'),
    sa.CheckConstraint("classification IN ('journal','ambiguous')"),
)
table = sa.Table(
    'journal_index_verification', metadata,
    sa.Column('verification_id', sa.BigInteger, primary_key=True),
    sa.Column('journal_id', sa.BigInteger, sa.ForeignKey('raw.journal_catalog.journal_id'), nullable=False),
    sa.Column('source', sa.Text, nullable=False),
    sa.Column('status', sa.Text, nullable=False),
    sa.Column('indexed', sa.Boolean),
    sa.Column('matched_identity', JSONB, nullable=False),
    sa.Column('sjr', JSONB, nullable=False),
    sa.Column('sinta_rank', sa.Text), sa.Column('sinta_period', sa.Text),
    sa.Column('evidence_url', sa.Text),
    sa.Column('checked_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('reason', sa.Text),
    sa.Column('metadata', JSONB, nullable=False),
    sa.UniqueConstraint('journal_id', 'source', name='uq_journal_verification_source'),
    sa.CheckConstraint("source IN ('scopus','scimagojr','sinta')"),
    sa.CheckConstraint("status IN ('verified','unverified','not_found','access_unavailable','error')"),
    sa.CheckConstraint("status <> 'verified' OR (indexed IS TRUE AND evidence_url IS NOT NULL)"),
)
# Article-level Scopus master verification; reference only. The reviewed DDL in
# sql/ddl/19_create_scopus_publication_verification.sql is never applied from Python.
scopus_verification = sa.Table(
    'scopus_publication_verification', metadata,
    sa.Column('verification_id', sa.BigInteger, primary_key=True),
    sa.Column('raw_publication_id', sa.Integer,
              sa.ForeignKey('raw.scholar_publication.raw_publication_id'), nullable=False),
    sa.Column('scopus_status', sa.String(32), nullable=False),
    sa.Column('source_record_id', sa.BigInteger),
    sa.Column('source_title', sa.Text), sa.Column('source_type', sa.Text),
    sa.Column('issn', sa.Text), sa.Column('eissn', sa.Text), sa.Column('isbn', sa.Text),
    sa.Column('coverage', sa.Text), sa.Column('active_status', sa.Text),
    sa.Column('discontinued', sa.Boolean), sa.Column('publisher', sa.Text), sa.Column('asjc', sa.Text),
    sa.Column('matched_identity', JSONB), sa.Column('match_confidence', sa.String(16)),
    sa.Column('reason', sa.Text), sa.Column('source_sheet', sa.Text), sa.Column('metadata', JSONB),
    sa.Column('checked_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.UniqueConstraint('raw_publication_id', name='uq_scopus_pub_verification_raw'),
    sa.CheckConstraint("scopus_status IN ('indexed','indicated','needs_verification','not_found')",
                       name='ck_scopus_pub_verification_status'),
)
# The reviewed schema forbids 'error'; transient failures are never persisted.
SCOPUS_SAVED_STATUSES = ('indexed', 'indicated', 'needs_verification', 'not_found')


class ScopusVerificationSchemaError(ValueError):
    """Reviewed Scopus verification schema is unavailable; never auto-apply DDL."""


def create_table(engine, catalog_only=False):
    with engine.begin() as conn:
        conn.execute(sa.text('CREATE SCHEMA IF NOT EXISTS raw'))
        # PostgreSQL DDL emitted explicitly with IF NOT EXISTS, never for previews.
        conn.execute(sa.schema.CreateTable(catalog, if_not_exists=True))
        if not catalog_only:
            conn.execute(sa.schema.CreateTable(table, if_not_exists=True))


def upsert_catalog(conn, candidate, identity=None, *, strict_identifiers=False):
    if candidate.classification == 'non_journal':
        raise ValueError('Excluded venue cannot enter catalogue')
    origins = {str(p['raw_publication_id']): p for p in candidate.publications
               if p.get('raw_publication_id') is not None}
    identity = identity or {}
    identifiers = candidate.identifiers | issns(identity.get('issn')) | issns(identity.get('eissn'))
    for identifier in identity.get('identifiers', []):
        identifiers.update(issns(identifier))
    prior = conn.execute(sa.select(catalog.c.identifiers).where(
        catalog.c.journal_normalized == candidate.normalized).with_for_update()).mappings().first()
    if isinstance(prior, Mapping):
        known = set(prior['identifiers'] or {})
        if known and identifiers and not known & identifiers:
            raise ValueError('Conflicting ISSNs require identity review')
    stmt = insert(catalog).values(
        journal_original=candidate.original, journal_normalized=candidate.normalized,
        identifiers={i: True for i in sorted(identifiers)},
        issn=next(iter(issns(identity.get('issn'))), None),
        eissn=next(iter(issns(identity.get('eissn'))), None),
        classification=candidate.classification, classification_reason=candidate.reason,
        publication_origins=json.loads(json.dumps(origins, default=str)))
    guard = (catalog.c.identifiers.op('<@')(stmt.excluded.identifiers) if strict_identifiers else None)
    response = conn.execute(stmt.on_conflict_do_update(
        constraint='uq_journal_catalog_title', set_={
            'identifiers': catalog.c.identifiers.op('||')(stmt.excluded.identifiers),
            'issn': sa.func.coalesce(catalog.c.issn, stmt.excluded.issn),
            'eissn': sa.func.coalesce(catalog.c.eissn, stmt.excluded.eissn),
            'publication_origins': catalog.c.publication_origins.op('||')(stmt.excluded.publication_origins),
            'updated_at': sa.func.now(),
        }, where=guard).returning(catalog.c.journal_id))
    return response.scalar_one_or_none() if strict_identifiers else response.scalar_one()


def sync_catalog(engine, selected):
    count, errors = 0, 0
    for candidate in selected:
        if candidate.classification == 'non_journal' or not candidate.normalized:
            continue
        try:
            with engine.begin() as conn:
                upsert_catalog(conn, candidate)
            count += 1
        except Exception:
            errors += 1
            LOG.error('Catalogue upsert failed; sensitive details suppressed')
    return count, errors


def verification_status(checked):
    if checked['status'] != 'indexed':
        return checked['status'] if checked['status'] in FAILED + ('not_found',) else 'unverified'
    meta = checked.get('metadata', {})
    if not checked.get('evidence_url'):
        return 'unverified'
    if checked['source'] == 'scopus':
        if meta.get('master_file') and meta.get('master_sha256'):
            # Local Excel master evidence: indexed means a unique master identity
            # matched offline; no HTTP api_success is involved or required.
            return 'verified' if checked['status'] == 'indexed' else 'unverified'
        return 'verified' if meta.get('identity_match') is True and meta.get('api_success') is True and meta.get('source_id') else 'unverified'
    if checked['source'] == 'scimagojr' and meta.get('method') == 'scimago_master_csv':
        return 'verified' if (meta.get('identity_match') is True and meta.get('source_id')
                              and meta.get('reference_year') == 2025
                              and meta.get('match_method') in ('issn', 'title_exact')) else 'unverified'
    # Existing local-evidence adapters perform exact identity and official URL checks.
    return 'verified' if meta.get('method') == 'manual_evidence' else 'unverified'


def save_result(engine, candidate, checked, cache_days=30):
    status = verification_status(checked)
    checked_at = checked['checked_at']
    duration = timedelta(days=cache_days) if status == 'verified' else (
        timedelta(days=1) if status == 'not_found' else timedelta(hours=1))
    identity = {'title': checked.get('metadata', {}).get('title') or candidate.original,
                'issn': checked.get('issn'), 'eissn': checked.get('eissn'),
                'source_id': checked.get('metadata', {}).get('source_id')}
    csv_match = checked['source'] == 'scimagojr' and checked.get('metadata', {}).get('method') == 'scimago_master_csv'
    if csv_match:
        identity['identifiers'] = checked.get('metadata', {}).get('master_record', {}).get('issns', [])
    with engine.begin() as conn:
        journal_id = upsert_catalog(conn, candidate, identity if status == 'verified' else None)
        if checked['source'] == 'scimagojr':
            conn.execute(sa.text('SELECT pg_advisory_xact_lock(166847, :journal_id)'),
                         {'journal_id': journal_id})
            manual_table_exists = conn.execute(sa.text(
                "SELECT to_regclass('raw.scimago_manual_verification')")).scalar_one_or_none()
            if isinstance(manual_table_exists, str):
                manual_exists = conn.execute(sa.select(
                    scimago_manual.manual_verification.c.manual_verification_id
                ).where(scimago_manual.manual_verification.c.journal_id == journal_id)
                 .limit(1)).scalar_one_or_none()
                if manual_exists is not None:
                    LOG.info('Manual SCImago decision retained for journal_id %s', journal_id)
                    return
        values = dict(journal_id=journal_id, source=checked['source'], status=status,
                      indexed=True if status == 'verified' else None,
                      matched_identity=identity if status == 'verified' else {},
                      evidence_url=checked.get('evidence_url'), checked_at=checked_at,
                      expires_at=checked_at + duration, reason=checked.get('reason'),
                      sjr=checked.get('sjr', []), sinta_rank=checked.get('sinta_rank'),
                      sinta_period=checked.get('sinta_period'),
                      metadata=dict(checked.get('metadata', {}), query_identifiers=sorted(candidate.identifiers)))
        stmt = insert(table).values(**values)
        # A failed local recheck must not erase an established positive snapshot.
        protect = (sa.or_(table.c.status != 'verified', stmt.excluded.status == 'verified')
                   if checked['source'] == 'scimagojr' else None)
        conn.execute(stmt.on_conflict_do_update(
            constraint='uq_journal_verification_source',
            set_={c.name: stmt.excluded[c.name] for c in table.c
                  if c.name not in ('verification_id', 'journal_id', 'source')}, where=protect))


def scopus_status_guard(stmt):
    """An established 'indexed' snapshot is never downgraded by a weaker status."""
    return sa.or_(scopus_verification.c.scopus_status != 'indexed',
                  stmt.excluded.scopus_status == 'indexed')


def scopus_verification_values(candidate, checked):
    """One verification row per RAW publication origin; duplicate RAW rows are preserved."""
    metadata = dict(checked.get('metadata', {}), query_identifiers=sorted(candidate.identifiers))
    values = []
    for origin in candidate.publications:
        raw_id = origin.get('raw_publication_id')
        if raw_id is None:
            continue
        values.append(dict(
            raw_publication_id=raw_id, scopus_status=checked['status'],
            source_record_id=checked.get('source_record_id'), source_title=checked.get('source_title'),
            source_type=checked.get('source_type'), issn=checked.get('issn'),
            eissn=checked.get('eissn'), isbn=checked.get('isbn'), coverage=checked.get('coverage'),
            active_status=checked.get('active_status'), discontinued=checked.get('discontinued'),
            publisher=checked.get('publisher'), asjc=checked.get('asjc'),
            matched_identity=checked.get('matched_identity'),
            match_confidence=checked.get('match_confidence'), reason=checked.get('reason'),
            source_sheet=checked.get('source_sheet'), metadata=metadata,
            checked_at=checked['checked_at']))
    return values


def save_scopus_result(engine, candidate, checked):
    """Upsert Scopus master verification per RAW article; no DDL, no RAW writes.

    Idempotent on uq_scopus_pub_verification_raw. An existing 'indexed' row is
    never downgraded by a weaker new status; otherwise latest evidence wins and
    checked_at/updated_at are refreshed. 'error' results are not persistable.
    """
    if checked.get('status') not in SCOPUS_SAVED_STATUSES:
        LOG.warning('Scopus status %s is not persistable; nothing saved', checked.get('status'))
        return 0
    values = scopus_verification_values(candidate, checked)
    if not values:
        return 0
    saved = 0
    with engine.begin() as conn:
        for row_values in values:
            conn.execute(sa.text('SELECT pg_advisory_xact_lock(166845, :publication_id)'),
                         {'publication_id': row_values['raw_publication_id']})
            stmt = insert(scopus_verification).values(**row_values)
            conn.execute(stmt.on_conflict_do_update(
                constraint='uq_scopus_pub_verification_raw',
                set_={**{name: stmt.excluded[name] for name in row_values if name != 'raw_publication_id'},
                      'updated_at': sa.func.now()},
                where=scopus_status_guard(stmt)))
            saved += 1
    return saved


def save_scopus_results(engine, rows_results):
    """Persist (candidate, checked) pairs after verifying the reviewed schema exists."""
    with engine.connect() as conn:
        exists = conn.execute(sa.text(
            "SELECT to_regclass('raw.scopus_publication_verification')")).scalar_one_or_none()
    if not isinstance(exists, str):
        raise ScopusVerificationSchemaError(
            'raw.scopus_publication_verification is missing; review and apply '
            'sql/ddl/19_create_scopus_publication_verification.sql manually; '
            'no automatic migration is performed')
    return sum(save_scopus_result(engine, candidate, checked) for candidate, checked in rows_results)


# Article-level manual Scopus verification (Scopus Document Search evidence);
# reference only. The reviewed DDL in sql/ddl/20_create_scopus_manual_verification.sql
# is never applied from Python. Deliberately a separate table from the automatic
# master results above: neither save path ever writes the other's table.
scopus_manual_verification = sa.Table(
    'scopus_manual_verification', metadata,
    sa.Column('verification_id', sa.BigInteger, primary_key=True),
    sa.Column('raw_publication_id', sa.Integer,
              sa.ForeignKey('raw.scholar_publication.raw_publication_id'), nullable=False),
    sa.Column('verification_source', sa.String(64), nullable=False),
    sa.Column('title', sa.Text), sa.Column('first_author', sa.Text),
    sa.Column('source_title', sa.Text), sa.Column('publication_year', sa.Integer),
    sa.Column('doi', sa.Text), sa.Column('eid', sa.Text),
    sa.Column('verified', sa.Boolean, nullable=False),
    sa.Column('scopus_status', sa.String(32), nullable=False),
    sa.Column('matched_identity', JSONB),
    sa.Column('reason', sa.Text), sa.Column('metadata', JSONB),
    sa.Column('checked_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.UniqueConstraint('raw_publication_id', name='uq_scopus_manual_verification_raw'),
    sa.CheckConstraint("scopus_status IN ('indexed','indicated','needs_verification','not_found')",
                       name='ck_scopus_manual_verification_status'),
)

# Distinct advisory-lock namespace from the automatic table (166845).
SCOPUS_MANUAL_ADVISORY_KEY = 166846


class ScopusManualVerificationSchemaError(ValueError):
    """Reviewed Scopus manual verification schema is unavailable; never auto-apply DDL."""


def save_scopus_manual_result(engine, evidence, checked):
    """Upsert one manual Scopus document-check verification; no DDL, no RAW writes.

    Idempotent on uq_scopus_manual_verification_raw; latest manual evidence
    wins and checked_at/updated_at are refreshed. Never touches the automatic
    raw.scopus_publication_verification table or any other table.
    """
    if checked.get('status') not in SCOPUS_SAVED_STATUSES:
        LOG.warning('Scopus manual status %s is not persistable; nothing saved',
                    checked.get('status'))
        return 0
    raw_id = evidence.get('raw_publication_id')
    if raw_id is None:
        return 0
    values = dict(
        raw_publication_id=raw_id,
        verification_source=evidence.get('verification_source'),
        title=evidence.get('title'), first_author=evidence.get('first_author'),
        source_title=evidence.get('source_title'),
        publication_year=evidence.get('publication_year'),
        doi=evidence.get('doi'), eid=evidence.get('eid'),
        verified=bool(evidence.get('verified')),
        scopus_status=checked['status'],
        matched_identity=checked.get('matched_identity'),
        reason=checked.get('reason'),
        metadata=dict(checked.get('metadata', {})),
        checked_at=checked['checked_at'])
    with engine.begin() as conn:
        conn.execute(sa.text('SELECT pg_advisory_xact_lock(:namespace, :publication_id)'),
                     {'namespace': SCOPUS_MANUAL_ADVISORY_KEY, 'publication_id': raw_id})
        stmt = insert(scopus_manual_verification).values(**values)
        conn.execute(stmt.on_conflict_do_update(
            constraint='uq_scopus_manual_verification_raw',
            set_={**{name: stmt.excluded[name] for name in values if name != 'raw_publication_id'},
                  'updated_at': sa.func.now()}))
    return 1


def save_scopus_manual_results(engine, evidence_results):
    """Persist (evidence, checked) pairs after verifying the reviewed manual schema exists."""
    with engine.connect() as conn:
        exists = conn.execute(sa.text(
            "SELECT to_regclass('raw.scopus_manual_verification')")).scalar_one_or_none()
    if not isinstance(exists, str):
        raise ScopusManualVerificationSchemaError(
            'raw.scopus_manual_verification is missing; review and apply '
            'sql/ddl/20_create_scopus_manual_verification.sql manually; '
            'no automatic migration is performed')
    return sum(save_scopus_manual_result(engine, evidence, checked)
               for evidence, checked in evidence_results)


def cached_result(engine, candidate, source, now=None):
    if engine is None:
        return None
    now = now or datetime.now(timezone.utc)
    try:
        with engine.connect() as conn:
            exists = conn.execute(sa.text("SELECT to_regclass('raw.journal_index_verification')")).scalar_one_or_none()
            if not isinstance(exists, str):
                return None
            row = conn.execute(sa.select(table, catalog.c.identifiers.label('catalog_identifiers')).join(catalog).where(
                catalog.c.journal_normalized == candidate.normalized,
                table.c.source == source)).mappings().first()
        if not isinstance(row, Mapping) or row['source'] != source:
            return None
        # Reuse established catalogue identifiers even when a verification expired.
        known = set(row.get('catalog_identifiers') or {})
        if not candidate.identifiers:
            candidate.identifiers.update(known)
        identity = row['matched_identity'] or {}
        observed = issns(identity.get('issn')) | issns(identity.get('eissn'))
        checked_identifiers = observed or set((row['metadata'] or {}).get('query_identifiers', []))
        if candidate.identifiers and (not checked_identifiers or not candidate.identifiers & checked_identifiers):
            return None
        if row['expires_at'] <= now:
            return None
        # Reconstruct adapter format without inferring another source's status.
        status = 'indexed' if row['status'] == 'verified' else row['status']
        checked = result(source, status, row['reason'], evidence_url=row['evidence_url'],
                         checked_at=row['checked_at'], issn=identity.get('issn'), eissn=identity.get('eissn'),
                         sjr=row['sjr'], sinta_rank=row['sinta_rank'], sinta_period=row['sinta_period'],
                         metadata=dict(row['metadata'] or {}, reused=True))
        if status == 'indexed' and verification_status(checked) != 'verified':
            return None
        return checked
    except Exception:
        LOG.warning('Verification cache unavailable; sensitive details suppressed')
        return None


def summarize_candidates(selected):
    eligible = [c for c in selected if c.classification != 'non_journal']
    return dict(publications_with_venue=sum(len(c.publications) for c in selected),
                candidate_publications=sum(len(c.publications) for c in eligible),
                unique_journals=len(eligible), excluded_journals=len(selected) - len(eligible),
                ambiguous_journals=sum(c.classification == 'ambiguous' for c in eligible))


def publication_rows(conn, row_limit, *, scholar_types=False):
    """Bounded RAW publication window; the identical query for every candidate builder."""
    return [dict(row) for row in conn.execute(sa.text('''
            SELECT raw_publication_id, scholar_id, title, venue, detail_venue,
                   publication_year, detail_doi, detail_url, authors, detail_authors,
                   to_jsonb(p)->>'issn' AS issn, to_jsonb(p)->>'eissn' AS eissn,
                   to_jsonb(p)->>'detail_issn' AS detail_issn,
                   to_jsonb(p)->>'detail_eissn' AS detail_eissn,
                   to_jsonb(p)->>'detail_journal' AS detail_journal,
                   to_jsonb(p)->>'detail_publication_type' AS detail_publication_type,
                   COALESCE(to_jsonb(p)->>'detail_venue_label',
                       (SELECT (r.raw_envelope->'data'->'rows'->p.urutan)->>'detail_venue_label'
                        FROM raw.scholar_scrape_result r WHERE r.result_id=p.result_id)
                   ) AS detail_venue_label
            FROM raw.scholar_publication AS p
            WHERE (:scholar_types OR COALESCE(NULLIF(btrim(detail_venue), ''), NULLIF(btrim(venue), '')) IS NOT NULL)
            ORDER BY raw_publication_id LIMIT :row_limit
        '''), {'row_limit': row_limit, 'scholar_types': scholar_types}).mappings()]


def read_candidates(engine, limit, row_limit, *, scholar_types=False):
    with engine.connect() as conn:
        rows = publication_rows(conn, row_limit, scholar_types=scholar_types)
        return candidates(rows, len(rows) if limit is None else limit,
                          scholar_types=scholar_types)


def matched_crossref_serials(conn, raw_ids):
    """Matched Crossref enrichments keyed by RAW id; empty when the table is absent."""
    ids = sorted({raw_id for raw_id in raw_ids if raw_id is not None})
    if not ids:
        return {}
    try:
        exists = conn.execute(sa.text(
            "SELECT to_regclass('raw.crossref_publication_enrichment')")).scalar_one_or_none()
        if not isinstance(exists, str):
            return {}
        matched = {}
        rows = conn.execute(sa.text(
            'SELECT raw_publication_id, container_title, metadata '
            'FROM raw.crossref_publication_enrichment '
            "WHERE status = 'matched' AND raw_publication_id = ANY(:ids)"), {'ids': ids}).mappings()
        for row in rows:
            work = ((row.get('metadata') or {}).get('result') or {}).get('selected_candidate') or {}
            matched[row['raw_publication_id']] = dict(
                container_title=row.get('container_title') or [], work=work)
        return matched
    except Exception:
        LOG.warning('Crossref enrichment unavailable; sensitive details suppressed')
        return {}


def scopus_rows_with_enrichment(engine, row_limit):
    """Bounded RAW rows with matched Crossref serial enrichment applied.

    Matched Crossref serial identity is preferred when Scholar metadata is
    missing or ambiguous; existing Scholar values are never overwritten.
    """
    with engine.connect() as conn:
        rows = publication_rows(conn, row_limit, scholar_types=True)
        matched = matched_crossref_serials(conn, [row['raw_publication_id'] for row in rows])
    for row in rows:
        enrichment = matched.get(row.get('raw_publication_id'))
        if not enrichment:
            continue
        titles = [t.strip() for t in enrichment['container_title'] if isinstance(t, str) and t.strip()]
        work = enrichment['work']
        if work.get('type') != 'journal-article' or len({normalize_title(t) for t in titles}) != 1:
            continue
        if not (row.get('detail_journal') or '').strip():
            row['detail_journal'] = titles[0]
        crossref_issns = set().union(*(issns(value or '') for value in work.get('issns', [])))
        if crossref_issns:
            observed = set().union(*(issns(row.get(key) or '') for key in
                                      ('issn', 'eissn', 'detail_issn', 'detail_eissn')))
            extra = sorted(crossref_issns - observed)
            if extra:
                row['detail_issn'] = ' '.join([row.get('detail_issn') or '', *extra]).strip()
    return rows


def read_scopus_candidates(engine, limit, row_limit):
    """Journal candidates for the local Scopus Excel master; entirely offline."""
    rows = scopus_rows_with_enrichment(engine, row_limit)
    return candidates(rows, len(rows) if limit is None else limit, scholar_types=True)


def run_checks(engine, selected, sources, retry_failed=False, delay=1.0,
               persist=True, cache_days=30, checks=None, emit=False, local_scimago=False):
    counts = {}
    write_errors = 0
    seen = set()
    for candidate in selected:
        # Excluded venues are not journal checks, including in retry mode.
        if candidate.classification == 'non_journal' and not local_scimago:
            continue
        for source in sources:
            key = (candidate.normalized, source, candidate.classification)
            if key in seen:
                continue
            seen.add(key)
            # CSV is cheap/local: never reuse manual evidence or another edition.
            checked = None if retry_failed or local_scimago or source == 'scimagojr' else cached_result(engine, candidate, source)
            if retry_failed:
                with engine.connect() as conn:
                    prior = conn.execute(sa.select(table.c.status).join(catalog).where(
                        catalog.c.journal_normalized == candidate.normalized,
                        table.c.source == source)).scalar_one_or_none()
                if prior not in FAILED:
                    continue
                checked = None
            # A missing title keyword is not negative evidence. Source adapters
            # must match identity and obtain their own evidence, also for ambiguous names.
            reused = checked is not None
            if not reused:
                try:
                    checked = (checks or SOURCES)[source](candidate)
                except Exception:
                    # Never log exception text: transport/config errors may contain secrets.
                    checked = result(source, 'error', 'Source check failed; sensitive exception details suppressed')
                time.sleep(delay)
            counts[checked['status']] = counts.get(checked['status'], 0) + 1
            if emit:
                print(json.dumps({'journal': candidate.original, 'source': source,
                                  'status': checked['status'] if local_scimago else verification_status(checked), 'reason': checked['reason'],
                                  'evidence_url': checked['evidence_url'], 'metadata': checked['metadata']},
                                 ensure_ascii=True, default=str))
            if not persist or checked['status'] == 'excluded' or not candidate.normalized:
                continue
            try:
                if reused:
                    if engine is not None:
                        with engine.begin() as conn:
                            upsert_catalog(conn, candidate)
                    continue
                if cache_days == 30:
                    save_result(engine, candidate, checked)
                else:
                    save_result(engine, candidate, checked, cache_days=cache_days)
            except Exception:
                write_errors += 1
                LOG.error('Result persistence failed for source %s; details suppressed', source)
    return counts, write_errors


def parser():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument('--limit', type=int, default=20, help='Maximum candidates (1..200; default 20)')
    cli.add_argument('--row-limit', type=int, default=1000, help='Bounded publication window (1..10000)')
    cli.add_argument('--source', choices=[*SOURCES, 'scimago', 'all', 'http', 'local'], default='all',
                     help="Check sources; 'http'/'local' memilih sumber halaman katalog SINTA pada --sync-sinta-catalog")
    cli.add_argument('--html-dir', type=Path, default=sinta_catalog.DEFAULT_HTML_DIR,
                     help='Folder HTML lokal untuk --sync-sinta-catalog --source local (default data/sinta/1093)')
    cli.add_argument('--captured-at', metavar='ISO8601',
                     help='Waktu tangkapan HTML lokal (ISO-8601); tanpa ini dipakai waktu modifikasi berkas')
    cli.add_argument('--scimago-csv', type=Path, default=scimagojr.DEFAULT_MASTER,
                     help='Local SCImagoJR 2025 master export; never downloaded')
    cli.add_argument('--scopus-file', type=Path, default=scopus.DEFAULT_MASTER,
                     help='Local Scopus source-list Excel master (ext_list_Sep_2026.xlsx); never downloaded')
    cli.add_argument('--scopus-evidence-file', type=Path, default=None,
                     help='JSONL of manual Scopus document-check evidence per RAW article; '
                          'requires --check-scopus, read-only without --save-scopus')
    mode = cli.add_mutually_exclusive_group()
    mode.add_argument('--dry-run', action='store_true', help='Read-only preview; no HTTP, DDL or writes (default)')
    mode.add_argument('--save', action='store_true', help='Explicitly check sources and upsert catalogue/verifications')
    mode.add_argument('--sync-catalog', action='store_true', help='Explicitly create/upsert catalogue only; no source requests')
    cli.add_argument('--sync-sinta-catalog', action='store_true', help='Fetch the SINTA affiliation journal catalogue; dry-run unless --save')
    mode.add_argument('--check-scopus', action='store_true',
                      help='Match journal candidates against the local Scopus Excel master; '
                           'read-only without --save-scopus, no HTTP and no DDL')
    mode.add_argument('--scimago', action='store_true',
                      help='Verify every RAW journal against the local SCImago master and save results')
    mode.add_argument('--scopus', action='store_true',
                      help='Verify every RAW journal against the local Scopus master and save per publication')
    mode.add_argument('--manual-scimago', action='store_true',
                      help='Interactively review and save manual SCImago decisions')
    mode.add_argument('--check-crossref', action='store_true',
                      help='Explicit real Crossref inspection of individual RAW articles; no writes')
    mode.add_argument('--enrich-crossref', action='store_true',
                      help='Use article cache, request Crossref for misses/expired entries; writes require --save-crossref')
    mode.add_argument('--crossref-scimago', action='store_true',
                      help='Match valid cached Crossref articles to local master; offline by default')
    cli.add_argument('--refresh-crossref', action='store_true', help='With --crossref-scimago, explicitly allow HTTP for cache misses')
    cli.add_argument('--save-crossref', action='store_true', help='Explicitly persist article enrichment only; never DDL')
    cli.add_argument('--save-scimago', action='store_true', help='With --crossref-scimago, explicitly persist master-confirmed journal identities')
    cli.add_argument('--crossref-ttl', action='append', default=[], metavar='STATUS=SECONDS',
                     help='Override Crossref TTL per status (max 365 days)')
    cli.add_argument('--test-db-allowlist', type=Path, help='Explicit trusted JSON allowlist for isolated Crossref test targets')
    cli.add_argument('--confirm-test-db', help='Confirm the exact allowlisted crossref_test_* database name')
    cli.add_argument('--crossref-limit', type=int, default=3, help='Maximum Crossref articles (1..10; default 3)')
    cli.add_argument('--crossref-source', choices=('doi', 'search', 'auto'), default='auto')
    cli.add_argument('--crossref-delay', type=float, default=1.0, help='Minimum seconds between Crossref requests (default 1)')
    cli.add_argument('--scholar-id', metavar='SCHOLAR_ID',
                     help='Batasi Crossref ke satu profil Google Scholar '
                          '(raw.scholar_publication.scholar_id); tanpa flag ini '
                          'perilaku lama dipertahankan. Gunakan bentuk '
                          '--scholar-id=ID untuk ID berawalan tanda minus')
    cli.add_argument('--fill-raw-crossref', action='store_true',
                     help='With --enrich-crossref, fill empty RAW columns '
                          '(detail_doi, detail_publisher, detail_venue) from '
                          'validated matched Crossref results; populated values '
                          'are never overwritten')
    cli.add_argument('--retry-failed', action='store_true', help='With --save, retry existing error/unverified/access_unavailable results only')
    cli.add_argument('--save-scopus', action='store_true',
                     help='With --check-scopus, explicitly persist master verification per RAW article; never DDL')
    cli.add_argument('--cache-days', type=int, default=30, help='Verified result validity (1..365 days; default 30)')
    return cli


def validate_database_name(target, writing=False):
    """Validate configuration before connecting; CLI also checks engine/server names."""
    if not target:
        raise ValueError('Missing POSTGRES_DB')
    if writing and target != 'publication_dashboard':
        raise ValueError('Journal catalogue writes require publication_dashboard')


def main(argv=None):
    cli = parser()
    args = cli.parse_args(argv)
    automatic_mode = 'scimago' if args.scimago else 'scopus' if args.scopus else None
    if args.source == 'scimago':
        args.source = 'scimagojr'
    if not 1 <= args.limit <= 200 or not 1 <= args.row_limit <= 10000:
        cli.error('limit must be 1..200 and row-limit must be 1..10000')
    if not 1 <= args.cache_days <= 365:
        cli.error('--cache-days must be 1..365')
    if not 1 <= args.crossref_limit <= 10:
        cli.error('--crossref-limit must be 1..10')
    if not math.isfinite(args.crossref_delay) or args.crossref_delay < 0:
        cli.error('--crossref-delay must be finite and nonnegative')
    if args.save_scopus and not args.check_scopus:
        cli.error('--save-scopus requires --check-scopus')
    if args.scopus_evidence_file is not None and not args.check_scopus:
        cli.error('--scopus-evidence-file requires --check-scopus')
    if args.retry_failed and not args.save:
        cli.error('--retry-failed requires --save')
    if args.check_scopus and args.source not in ('scopus', 'all'):
        cli.error('--check-scopus only supports --source scopus or all')
    crossref_mode = args.enrich_crossref or args.crossref_scimago
    if args.refresh_crossref and not args.crossref_scimago:
        cli.error('--refresh-crossref requires --crossref-scimago')
    if args.save_crossref and not crossref_mode:
        cli.error('--save-crossref requires --enrich-crossref or --crossref-scimago')
    if args.save_scimago and not args.crossref_scimago:
        cli.error('--save-scimago requires --crossref-scimago')
    if args.save_scimago and args.refresh_crossref and not args.save_crossref:
        cli.error('Refreshing and saving SCImago also requires --save-crossref for durable article provenance')
    if args.fill_raw_crossref and not args.enrich_crossref:
        cli.error('--fill-raw-crossref requires --enrich-crossref')
    if args.scholar_id is not None:
        args.scholar_id = args.scholar_id.strip()
        if not args.scholar_id:
            cli.error('--scholar-id must be a non-empty profile ID')
        if not (args.check_crossref or crossref_mode or args.check_scopus):
            cli.error('--scholar-id requires --check-crossref, --enrich-crossref, '
                      '--crossref-scimago or --check-scopus')
    if automatic_mode and (args.dry_run or args.save or args.sync_catalog or args.check_scopus or
                           args.check_crossref or args.enrich_crossref or args.crossref_scimago):
        cli.error('--scimago/--scopus cannot be combined with another operation mode')
    if args.crossref_ttl:
        from .crossref_pipeline import parse_ttls
        try:
            parse_ttls(args.crossref_ttl)
        except ValueError as exc:
            cli.error(str(exc))
        if not crossref_mode:
            cli.error('--crossref-ttl requires a Crossref enrichment/cache mode')
    if args.sync_sinta_catalog and (args.save_scopus or args.save_crossref or args.save_scimago):
        cli.error('--sync-sinta-catalog cannot be combined with other persistence modes')
    if args.sync_sinta_catalog and (args.sync_catalog or args.check_scopus or args.manual_scimago or args.check_crossref or
                                    args.enrich_crossref or args.crossref_scimago):
        cli.error('--sync-sinta-catalog cannot be combined with another journal-index mode')
    # Sumber halaman katalog SINTA: satu sumber per run; default http menjaga perilaku lama.
    sinta_source = args.source if args.source in ('http', 'local') else 'http'
    if args.source in ('http', 'local') and not args.sync_sinta_catalog:
        cli.error('--source http/local only applies to --sync-sinta-catalog')
    if args.sync_sinta_catalog and args.source not in ('http', 'local', 'all'):
        cli.error('--sync-sinta-catalog only supports --source http or local')
    captured_at = None
    if args.captured_at:
        if not (args.sync_sinta_catalog and sinta_source == 'local'):
            cli.error('--captured-at only applies to --sync-sinta-catalog with --source local')
        try:
            captured_at = datetime.fromisoformat(args.captured_at)
        except ValueError:
            cli.error('--captured-at must be an ISO-8601 timestamp')
        if captured_at.tzinfo is None:
            captured_at = captured_at.replace(tzinfo=timezone.utc)
    test_target = None
    if args.test_db_allowlist or args.confirm_test_db:
        if not (args.test_db_allowlist and args.confirm_test_db and crossref_mode):
            cli.error('Test target opt-in requires both --test-db-allowlist and --confirm-test-db in a Crossref cache/enrichment mode')
        from .crossref_target import load_target
        try:
            test_target = load_target(args.test_db_allowlist, args.confirm_test_db)
        except ValueError as exc:
            cli.error(str(exc))
    engine = None
    try:
        if args.sync_sinta_catalog:
            result = sinta_catalog.run_catalog(source=sinta_source, html_dir=args.html_dir,
                                               captured_at=captured_at)
            valid = result.items_unique
            saved = 0
            if args.save:
                from dotenv import load_dotenv
                from src.loading.postgres import build_engine
                load_dotenv(Path(__file__).resolve().parents[2] / '.env')
                target = (os.getenv('POSTGRES_DB') or '').strip()
                validate_database_name(target, writing=True)
                engine = build_engine()
                if engine.url.database != target:
                    raise ValueError('Database configuration mismatch')
                with engine.connect() as conn:
                    if conn.execute(sa.text('SELECT current_database()')).scalar_one() != target:
                        raise ValueError('Database connection mismatch')
                with engine.begin() as conn:
                    sinta_catalog.upsert_catalog(conn, result.rows)
                # rowcount executemany (psycopg3/insertmanyvalues) tidak tersedia
                # (-1); upsert tanpa WHERE pasti menyentuh tepat len(result.rows)
                # baris, jadi jumlah tersimpan dilaporkan dari panjang baris.
                saved = len(result.rows)
            for warning in result.warnings:
                print(f'Peringatan: {warning}', file=sys.stderr)
            print(json.dumps({
                'pages_found': result.pages_found, 'pages_processed': result.pages_processed,
                'items_found': result.items_found, 'items_unique': valid,
                'duplicates_dropped': result.duplicates_dropped, 'source_type': result.source_type,
                'status': result.status, 'warnings': result.warnings,
                'captured_at_source': result.captured_at_source,
                'pages': result.pages_processed, 'cards_found': result.items_found,
                'valid': valid, 'skipped': result.items_found - valid - result.duplicates_dropped,
                'duplicates': result.duplicates_dropped,
                'saved': saved, 'dry_run': not args.save,
            }, ensure_ascii=True))
            # Dry-run tetap pratinjau (selalu 0); --save memakai kode keluar status.
            return 0 if args.dry_run else SINTA_EXIT_CODES[result.status]
        # Validate masters before connecting or enabling any writes.
        local_scimago = args.source == 'scimagojr' or automatic_mode == 'scimago'
        needs_master = args.crossref_scimago or automatic_mode == 'scimago' or (not crossref_mode and not args.check_crossref and not args.sync_catalog and (local_scimago or (args.save and args.source == 'all')))
        master = scimagojr.load_master(args.scimago_csv) if needs_master else None
        # The Scopus Excel master is loaded once, before any database connection.
        scopus_master = scopus.load_master(args.scopus_file) if args.check_scopus or automatic_mode == 'scopus' else None
        # Manual evidence is parsed before any database connection as well; a
        # missing or malformed file fails fast with the path in the message.
        evidence_by_id, evidence_duplicates = {}, 0
        if args.check_scopus and args.scopus_evidence_file is not None:
            try:
                evidence_by_id, evidence_duplicates = scopus.load_manual_evidence(
                    args.scopus_evidence_file)
            except scopus.ScopusEvidenceError as exc:
                LOG.error('%s', exc)
                return 1
            if evidence_duplicates:
                LOG.warning('%d duplicate manual evidence rows skipped; first occurrence kept',
                            evidence_duplicates)
        from dotenv import load_dotenv
        from src.loading.postgres import build_engine
        load_dotenv(Path(__file__).resolve().parents[2] / '.env')
        target = (os.getenv('POSTGRES_DB') or '').strip()
        validate_database_name(target, writing=not test_target and (
            args.sync_catalog or args.save or args.save_scopus or args.save_crossref or args.save_scimago or
            automatic_mode or args.manual_scimago))
        engine = build_engine()
        if engine.url.database != target:
            raise ValueError('Database configuration mismatch')
        if test_target:
            from .crossref_target import verify_target
            verify_target(engine, test_target)
        if crossref_mode:
            from .crossref_pipeline import run
            return run(engine, args, target, master)
        if args.check_crossref:
            from .crossref_check import run_inspection
            return run_inspection(engine, target=target, row_limit=args.row_limit,
                limit=args.crossref_limit, source=args.crossref_source, delay=args.crossref_delay,
                mailto=os.getenv('CROSSREF_MAILTO'), scholar_id=args.scholar_id)
        with engine.connect() as conn:
            if conn.execute(sa.text('SELECT current_database()')).scalar_one() != target:
                raise ValueError('Database connection mismatch')
        if args.manual_scimago:
            return scimago_manual.run(engine)
        # Summaries cover the bounded RAW window, independently of the action limit.
        if automatic_mode == 'scimago':
            selected = read_candidates(engine, None, None, scholar_types=True)
        elif automatic_mode == 'scopus':
            selected = read_scopus_candidates(engine, None, None)
        elif args.check_scopus:
            selected = read_scopus_candidates(engine, args.row_limit, args.row_limit)
        elif local_scimago:
            selected = read_candidates(engine, args.row_limit, args.row_limit, scholar_types=True)
        else:
            selected = read_candidates(engine, args.row_limit, args.row_limit)
        if args.check_scopus:
            # Strictly read-only matching against the local Excel master; the only
            # write path is the explicit --save-scopus persistence below.
            selected = [c for c in selected if c.classification == 'journal']
            if args.scholar_id is not None:
                selected = [c for c in selected if any(
                    p.get('scholar_id') == args.scholar_id for p in c.publications)]
            selected = selected[:args.limit]
            counts = {}
            checked_rows = []
            for candidate in selected:
                try:
                    checked = scopus_master.check(candidate)
                except Exception:
                    # Never log exception text: file/config errors may contain secrets.
                    checked = result('scopus', 'error',
                                     'Scopus master check failed; sensitive exception details suppressed')
                counts[checked['status']] = counts.get(checked['status'], 0) + 1
                print(json.dumps({'journal': candidate.original, 'source': 'scopus',
                                  'status': checked['status'], 'reason': checked['reason'],
                                  'metadata': checked.get('metadata', {})},
                                 ensure_ascii=True, default=str))
                checked_rows.append((candidate, checked))
            if not selected:
                print(json.dumps({'source': 'scopus', 'status': 'not_found',
                                  'reason': 'No journal candidates identified within bounded RAW window',
                                  'metadata': {'master_file': str(scopus_master.path),
                                               'master_sha256': scopus_master.digest}}))
            # Manual evidence is evaluated per RAW row, independently of the
            # automatic master results above; it never feeds or overrides them.
            manual_pairs, manual_counts, manual_saved = [], {}, 0
            unmatched = 0
            if evidence_by_id:
                manual_rows = {}
                for row in scopus_rows_with_enrichment(engine, args.row_limit):
                    if row.get('raw_publication_id') is not None:
                        manual_rows[row['raw_publication_id']] = row
                for raw_id, item in evidence_by_id.items():
                    row = manual_rows.get(raw_id)
                    if row is None:
                        # Outside the bounded window: reported, never silently dropped.
                        manual_checked = scopus.unmatched_manual_evidence(item)
                        unmatched += 1
                    else:
                        manual_checked = scopus.check_manual_evidence(item, row)
                        manual_pairs.append((item, manual_checked))
                    manual_counts[manual_checked['status']] = \
                        manual_counts.get(manual_checked['status'], 0) + 1
                    print(json.dumps({'source': 'scopus_manual', 'evidence': 'manual',
                                      'raw_publication_id': raw_id,
                                      'verification_source': item.get('verification_source'),
                                      'status': manual_checked['status'],
                                      'reason': manual_checked['reason'],
                                      'metadata': manual_checked.get('metadata', {})},
                                     ensure_ascii=True, default=str))
            if args.save_scopus and checked_rows:
                try:
                    saved = save_scopus_results(engine, checked_rows)
                except ScopusVerificationSchemaError as exc:
                    LOG.error('%s', exc)
                    return 1
                print(json.dumps({'source': 'scopus', 'saved_rows': saved, 'statuses': counts},
                                 ensure_ascii=True))
            if args.save_scopus and manual_pairs:
                try:
                    manual_saved = save_scopus_manual_results(engine, manual_pairs)
                except ScopusManualVerificationSchemaError as exc:
                    LOG.error('%s', exc)
                    return 1
            if evidence_by_id:
                print(json.dumps({'source': 'scopus_manual', 'evidence': 'manual',
                                  'statuses': manual_counts, 'unmatched': unmatched,
                                  'duplicates': evidence_duplicates,
                                  'saved_rows': manual_saved if args.save_scopus else 0,
                                  'dry_run': not args.save_scopus}, ensure_ascii=True))
            return 0
        if automatic_mode:
            source = 'scimagojr' if automatic_mode == 'scimago' else 'scopus'
            source_path = master.path if source == 'scimagojr' else scopus_master.path
            print(f'{source.upper()} source file: {source_path}')
            create_table(engine) if source == 'scimagojr' else None
            eligible = [c for c in selected if c.classification != 'non_journal']
            excluded = len(selected) - len(eligible)
            checked_count = indexed = not_found = unverified = errors = saved = 0
            for candidate in eligible:
                try:
                    checked = (master.check(candidate) if source == 'scimagojr'
                               else scopus_master.check(candidate))
                    checked_count += 1
                    status = checked['status'] if source == 'scopus' else verification_status(checked)
                    if status == 'indexed' or status == 'verified':
                        indexed += 1
                    elif status == 'not_found':
                        not_found += 1
                    else:
                        unverified += 1
                    if source == 'scimagojr':
                        save_result(engine, candidate, checked)
                        saved += 1
                    elif checked.get('status') in SCOPUS_SAVED_STATUSES:
                        saved += save_scopus_results(engine, [(candidate, checked)])
                    print(json.dumps({'journal': candidate.original, 'source': source,
                                      'status': status, 'reason': checked.get('reason')},
                                     ensure_ascii=True, default=str))
                except (sa.exc.DBAPIError, ScopusVerificationSchemaError):
                    errors += 1
                    LOG.error('Database write failed for %s; subsequent checks stopped', candidate.original)
                    break
                except Exception:
                    errors += 1
                    unverified += 1
                    LOG.error('Journal check or save failed for %s; continuing', candidate.original)
            total_publications = sum(len(c.publications) for c in selected)
            print('=' * 50)
            print(f'{source.upper()} JOURNAL VERIFICATION')
            print('=' * 50)
            print(f'Total publikasi RAW diperiksa : {total_publications}')
            print(f'Jurnal unik diperiksa         : {checked_count}')
            print(f'Indexed                       : {indexed}')
            print(f'Not found                     : {not_found}')
            print(f'Unverified                    : {unverified}')
            print(f'Excluded                      : {excluded}')
            print('\nDATABASE')
            print(f'Inserted/updated              : {saved}')
            print('Unchanged                     : 0')
            print(f'Errors                        : {errors}')
            if errors:
                print(f'Alasan error                  : {errors} jurnal gagal; lihat log')
            completed = checked_count == len(eligible) and errors == 0
            print(f'Status: {"COMPLETED" if completed else "INCOMPLETE"}')
            return 0 if completed else 1
        sources = list(SOURCES) if args.source == 'all' else [args.source]
        if args.sync_catalog:
            create_table(engine, catalog_only=True)
            count, errors = sync_catalog(engine, [c for c in selected if c.classification != 'non_journal'][:args.limit])
            print(json.dumps({'catalogue_synced': count, 'write_errors': errors}))
            return 1 if errors else 0
        if local_scimago:
            if args.save:
                create_table(engine)
            counts, errors = run_checks(engine, selected[:args.limit], ['scimagojr'],
                retry_failed=args.retry_failed, persist=args.save, delay=0,
                cache_days=args.cache_days, checks={'scimagojr': master.check},
                emit=True, local_scimago=True)
            print(json.dumps({'statuses': counts, 'write_errors': errors,
                              'preview_only': not args.save, 'reference_year': 2025,
                              'master_rows': len(master.records), 'row_limit': args.row_limit}))
            return 1 if errors or counts.get('error') else 0
        if not args.save:
            for candidate in selected[:args.limit]:
                excluded = candidate.classification == 'non_journal'
                needs_verification = candidate.classification == 'ambiguous'
                print(json.dumps({'journal': candidate.original,
                                  'classification': candidate.classification,
                                  'reason': candidate.reason,
                                  'action': 'excluded_non_journal' if excluded else
                                            'verify_identity' if needs_verification else 'check_sources',
                                  'verification_required': needs_verification,
                                  'sources': [] if excluded else sources,
                                  'publication_count': len(candidate.publications)}, ensure_ascii=True))
            print(json.dumps({'summary': summarize_candidates(selected), 'row_limit': args.row_limit,
                              'preview_only': True}))
            return 0
        create_table(engine)
        selected = [c for c in selected if c.classification != 'non_journal'][:args.limit]
        # Other sources retain their existing candidates; SCImago uses explicit
        # Scholar type evidence even when the CLI checks all sources.
        counts, errors = run_checks(engine, selected, [s for s in sources if s != 'scimagojr'],
            args.retry_failed, cache_days=args.cache_days, emit=True)
        if 'scimagojr' in sources:
            strict = read_candidates(engine, args.row_limit, args.row_limit, scholar_types=True)
            local_counts, local_errors = run_checks(engine, strict[:args.limit], ['scimagojr'],
                retry_failed=args.retry_failed, cache_days=args.cache_days, emit=True,
                checks={'scimagojr': master.check}, local_scimago=True, delay=0)
            for status, count in local_counts.items():
                counts[status] = counts.get(status, 0) + count
            errors += local_errors
        print(json.dumps({'statuses': counts, 'write_errors': errors}))
        return 1 if errors else 0
    except CrossrefSchemaError:
        LOG.error(
            'Crossref table unavailable. Review '
            'sql/ddl/16_create_crossref_publication_enrichment.sql '
            'and apply separately with approval; no migration was run.'
        )
        return 1

    except scimagojr.MasterExportError as exc:
        LOG.error('%s', exc)
        return 1

    except scopus.ScopusMasterError as exc:
        LOG.error('%s', exc)
        return 1

    except ScopusVerificationSchemaError as exc:
        LOG.error('%s', exc)
        return 1

    except ScopusManualVerificationSchemaError as exc:
        LOG.error('%s', exc)
        return 1

    except scimago_manual.ManualScimagoSchemaError as exc:
        LOG.error('%s', exc)
        return 1

    except Exception:
        LOG.error('Journal index operation failed; exception details suppressed')
        return 1

    finally:
        if engine is not None:
            engine.dispose()


if __name__ == '__main__':
    raise SystemExit(main())
