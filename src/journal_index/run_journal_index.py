"""Bounded read-only preview by default; --save explicitly enables checks and writes."""
import argparse
import json
import logging
import os
import time
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, insert

from . import scopus, scimagojr, sinta
from .init import candidates, result, issns

LOG = logging.getLogger(__name__)
SOURCES = {'scopus': scopus.check, 'scimagojr': scimagojr.check, 'sinta': sinta.check}
FAILED = ('error', 'access_unavailable', 'unverified')
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


def create_table(engine, catalog_only=False):
    with engine.begin() as conn:
        conn.execute(sa.text('CREATE SCHEMA IF NOT EXISTS raw'))
        # PostgreSQL DDL emitted explicitly with IF NOT EXISTS, never for previews.
        conn.execute(sa.schema.CreateTable(catalog, if_not_exists=True))
        if not catalog_only:
            conn.execute(sa.schema.CreateTable(table, if_not_exists=True))


def upsert_catalog(conn, candidate, identity=None):
    if candidate.classification == 'non_journal':
        raise ValueError('Excluded venue cannot enter catalogue')
    origins = {str(p['raw_publication_id']): p for p in candidate.publications
               if p.get('raw_publication_id') is not None}
    identity = identity or {}
    identifiers = candidate.identifiers | issns(identity.get('issn')) | issns(identity.get('eissn'))
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
    return conn.execute(stmt.on_conflict_do_update(
        constraint='uq_journal_catalog_title', set_={
            'identifiers': catalog.c.identifiers.op('||')(stmt.excluded.identifiers),
            'issn': sa.func.coalesce(catalog.c.issn, stmt.excluded.issn),
            'eissn': sa.func.coalesce(catalog.c.eissn, stmt.excluded.eissn),
            'publication_origins': catalog.c.publication_origins.op('||')(stmt.excluded.publication_origins),
            'updated_at': sa.func.now(),
        }).returning(catalog.c.journal_id)).scalar_one()


def sync_catalog(engine, selected):
    count, errors = 0, 0
    for candidate in selected:
        if candidate.classification == 'non_journal':
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
        return 'verified' if meta.get('identity_match') is True and meta.get('api_success') is True and meta.get('source_id') else 'unverified'
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
    with engine.begin() as conn:
        journal_id = upsert_catalog(conn, candidate, identity if status == 'verified' else None)
        values = dict(journal_id=journal_id, source=checked['source'], status=status,
                      indexed=True if status == 'verified' else None,
                      matched_identity=identity if status == 'verified' else {},
                      evidence_url=checked.get('evidence_url'), checked_at=checked_at,
                      expires_at=checked_at + duration, reason=checked.get('reason'),
                      sjr=checked.get('sjr', []), sinta_rank=checked.get('sinta_rank'),
                      sinta_period=checked.get('sinta_period'),
                      metadata=dict(checked.get('metadata', {}), query_identifiers=sorted(candidate.identifiers)))
        stmt = insert(table).values(**values)
        conn.execute(stmt.on_conflict_do_update(
            constraint='uq_journal_verification_source',
            set_={c.name: stmt.excluded[c.name] for c in table.c
                  if c.name not in ('verification_id', 'journal_id', 'source')}))


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


def read_candidates(engine, limit, row_limit):
    with engine.connect() as conn:
        rows = conn.execute(sa.text('''
            SELECT raw_publication_id, scholar_id, title, venue, detail_venue,
                   publication_year, detail_doi, detail_url,
                   to_jsonb(p)->>'issn' AS issn, to_jsonb(p)->>'eissn' AS eissn,
                   to_jsonb(p)->>'detail_issn' AS detail_issn,
                   to_jsonb(p)->>'detail_eissn' AS detail_eissn
            FROM raw.scholar_publication AS p
            WHERE COALESCE(NULLIF(btrim(detail_venue), ''), NULLIF(btrim(venue), '')) IS NOT NULL
            ORDER BY raw_publication_id LIMIT :row_limit
        '''), {'row_limit': row_limit}).mappings()
        return candidates(rows, limit)


def run_checks(engine, selected, sources, retry_failed=False, delay=1.0,
               persist=True, cache_days=30, checks=None, emit=False):
    counts = {}
    write_errors = 0
    seen = set()
    for candidate in selected:
        # Excluded venues are not journal checks, including in retry mode.
        if candidate.classification == 'non_journal':
            continue
        for source in sources:
            key = (candidate.normalized, source)
            if key in seen:
                continue
            seen.add(key)
            checked = None if retry_failed else cached_result(engine, candidate, source)
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
                                  'status': verification_status(checked), 'reason': checked['reason'],
                                  'evidence_url': checked['evidence_url'], 'metadata': checked['metadata']},
                                 ensure_ascii=True, default=str))
            if not persist:
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
    cli.add_argument('--source', choices=[*SOURCES, 'all'], default='all')
    mode = cli.add_mutually_exclusive_group()
    mode.add_argument('--dry-run', action='store_true', help='Read-only preview; no HTTP, DDL or writes (default)')
    mode.add_argument('--save', action='store_true', help='Explicitly check sources and upsert catalogue/verifications')
    mode.add_argument('--sync-catalog', action='store_true', help='Explicitly create/upsert catalogue only; no source requests')
    mode.add_argument('--check-scopus', action='store_true',
                      help='Check at most 2 journal candidates via Scopus, without DDL or writes')
    cli.add_argument('--scopus-inst-token', action='store_true',
                     help='Use configured institution token only when required by Elsevier entitlement')
    cli.add_argument('--retry-failed', action='store_true', help='With --save, retry existing error/unverified/access_unavailable results only')
    cli.add_argument('--save-verification', action='store_true', help='With --check-scopus, explicitly persist catalogue and verification')
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
    if not 1 <= args.limit <= 200 or not 1 <= args.row_limit <= 10000:
        cli.error('limit must be 1..200 and row-limit must be 1..10000')
    if not 1 <= args.cache_days <= 365:
        cli.error('--cache-days must be 1..365')
    if args.save_verification and not args.check_scopus:
        cli.error('--save-verification requires --check-scopus')
    if args.retry_failed and not args.save:
        cli.error('--retry-failed requires --save')
    if args.check_scopus and args.source not in ('scopus', 'all'):
        cli.error('--check-scopus only supports --source scopus or all')
    if args.scopus_inst_token and not args.check_scopus:
        cli.error('--scopus-inst-token requires --check-scopus')
    engine = None
    try:
        from dotenv import load_dotenv
        from src.loading.postgres import build_engine
        load_dotenv(Path(__file__).resolve().parents[2] / '.env')
        target = (os.getenv('POSTGRES_DB') or '').strip()
        validate_database_name(target, writing=args.sync_catalog or args.save or args.save_verification)
        engine = build_engine()
        if engine.url.database != target:
            raise ValueError('Database configuration mismatch')
        with engine.connect() as conn:
            if conn.execute(sa.text('SELECT current_database()')).scalar_one() != target:
                raise ValueError('Database connection mismatch')
        # Summaries cover the bounded RAW window, independently of the action limit.
        selected = read_candidates(engine, args.row_limit, args.row_limit)
        if args.check_scopus:
            selected = [c for c in selected if c.classification == 'journal'][:min(2, args.limit)]
            if args.save_verification:
                create_table(engine)
            _, errors = run_checks(engine, selected, ['scopus'], persist=args.save_verification,
                                   cache_days=args.cache_days, emit=True,
                                   checks={'scopus': lambda c: scopus.check(c, use_inst_token=args.scopus_inst_token)})
            if not selected:
                print(json.dumps({'source': 'scopus', 'status': 'unverified',
                                  'reason': 'No journal candidates identified within bounded RAW window',
                                  'metadata': scopus.diagnostics()}))
            return 1 if errors else 0
        sources = list(SOURCES) if args.source == 'all' else [args.source]
        if args.sync_catalog:
            create_table(engine, catalog_only=True)
            count, errors = sync_catalog(engine, [c for c in selected if c.classification != 'non_journal'][:args.limit])
            print(json.dumps({'catalogue_synced': count, 'write_errors': errors}))
            return 1 if errors else 0
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
        counts, errors = run_checks(engine, selected, sources, args.retry_failed, cache_days=args.cache_days, emit=True)
        print(json.dumps({'statuses': counts, 'write_errors': errors}))
        return 1 if errors else 0
    except Exception:
        LOG.error('Journal index operation failed; verify POSTGRES_DB, connection and local evidence configuration. Details suppressed.')
        return 1
    finally:
        if engine is not None:
            engine.dispose()


if __name__ == '__main__':
    raise SystemExit(main())
