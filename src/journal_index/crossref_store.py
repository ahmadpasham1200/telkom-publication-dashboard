"""Per-RAW-article Crossref snapshots; no schema creation or import-time I/O."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import re

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, insert

from .crossref_check import _RAW_IDENTITY_COLUMNS, bibliography_key
from .crossref_matcher import ArticleMatchResult

MATCHER_VERSION = 'article-v2-ordered-subset'
DEFAULT_TTLS = dict(matched=30 * 86400, ambiguous=7 * 86400, not_found=86400, error=3600)


class CrossrefSchemaError(ValueError):
    """Reviewed schema is unavailable; never auto-apply DDL."""


meta = sa.MetaData(schema='raw')
# Reference only; this module never calls create_all/CreateTable.
# Kolom tambahan hanya referensi untuk UPDATE pengisian RAW (fill_raw_from_matched).
sa.Table('scholar_publication', meta, sa.Column('raw_publication_id', sa.Integer, primary_key=True),
    sa.Column('scholar_id', sa.String(50)), sa.Column('detail_doi', sa.Text),
    sa.Column('detail_publisher', sa.Text), sa.Column('detail_venue', sa.Text))
table = sa.Table('crossref_publication_enrichment', meta,
    sa.Column('enrichment_id', sa.BigInteger, primary_key=True),
    sa.Column('raw_publication_id', sa.Integer, sa.ForeignKey('raw.scholar_publication.raw_publication_id'), nullable=False),
    sa.Column('status', sa.Text, nullable=False), sa.Column('lookup_strategy', sa.Text, nullable=False),
    sa.Column('doi', sa.Text), sa.Column('matched_title', JSONB, nullable=False),
    sa.Column('matched_authors', JSONB, nullable=False), sa.Column('publication_year', sa.Integer),
    sa.Column('publication_date', JSONB), sa.Column('publication_date_precision', sa.Text),
    sa.Column('container_title', JSONB, nullable=False), sa.Column('issns', JSONB, nullable=False),
    sa.Column('publisher', sa.Text), sa.Column('publication_type', sa.Text),
    sa.Column('author_evidence', JSONB, nullable=False), sa.Column('year_evidence', JSONB, nullable=False),
    sa.Column('container_evidence', JSONB, nullable=False), sa.Column('title_similarity', sa.Float),
    sa.Column('candidate_count', sa.Integer, nullable=False), sa.Column('selected_evidence', JSONB, nullable=False),
    sa.Column('selected_conflicts', JSONB, nullable=False), sa.Column('metadata', JSONB, nullable=False),
    sa.Column('retrieved_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False), sa.Column('last_error', sa.Text),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.UniqueConstraint('raw_publication_id', name='uq_crossref_publication'),
    sa.CheckConstraint("status IN ('matched','ambiguous','not_found','error')", name='ck_crossref_status'),
    sa.CheckConstraint("lookup_strategy IN ('doi_lookup','bibliographic_search')", name='ck_crossref_lookup'),
    sa.CheckConstraint('expires_at > retrieved_at', name='ck_crossref_expiry'),
    sa.CheckConstraint("status <> 'matched' OR (doi IS NOT NULL AND candidate_count > 0)", name='ck_crossref_matched'),
)


def ttl_policy(overrides=None):
    values = dict(DEFAULT_TTLS)
    for key, value in (overrides or {}).items():
        if key not in values or type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
            raise ValueError('Crossref TTL requires a known status and positive finite seconds')
        # Prevent datetime overflow and accidental indefinite cache validity.
        if value > 365 * 86400:
            raise ValueError('Crossref TTL cannot exceed 365 days')
        values[key] = value
    return values


def fingerprint(row):
    # detail_doi/detail_venue are enrichment targets and may be populated
    # after this cache is written. Keep the fingerprint on source identity.
    fields = ('raw_publication_id', 'detail_title', 'title', 'publication_authors', 'detail_authors',
              'authors', 'publication_year', 'year', 'detail_date', 'detail_journal',
              'venue', 'doi', 'detail_publication_type',
              'issn', 'eissn', 'detail_issn', 'detail_eissn')
    data = {k: row.get(k) for k in fields}
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=True).encode()).hexdigest()


def _legacy_fingerprint(row):
    """Fingerprint format written before detail DOI/venue became fill targets."""
    fields = ('raw_publication_id', 'detail_title', 'title', 'publication_authors', 'detail_authors',
              'authors', 'publication_year', 'year', 'detail_date', 'detail_journal', 'detail_venue',
              'venue', 'detail_doi', 'doi', 'detail_publication_type',
              'issn', 'eissn', 'detail_issn', 'detail_eissn')
    data = {key: row.get(key) for key in fields}
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=True).encode()).hexdigest()


def _snapshot_id_for_fingerprint(result, input_fingerprint):
    payload = result.to_dict()
    if not re.fullmatch(r'[a-z][a-z0-9_]{0,100}', payload['reason']):
        payload['reason'] = 'crossref_processing_error'
    identity = dict(input=input_fingerprint, matcher=MATCHER_VERSION, result=payload)
    return hashlib.sha256(json.dumps(identity, sort_keys=True, ensure_ascii=True,
                                     separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def snapshot_id(row, result):
    return _snapshot_id_for_fingerprint(result, fingerprint(row))


def _legacy_matched_row(row, result, expected_fingerprint):
    """Reconstruct only NULL/blank target values that this cached match could fill."""
    targets = raw_fill_values(result)
    doi, venue = targets.get('detail_doi'), targets.get('detail_venue')
    current_doi = row.get('detail_doi')
    if current_doi and (not doi or current_doi.strip().casefold() != doi.casefold()):
        return None
    choices = {}
    for field, value in (('detail_doi', doi), ('detail_venue', venue)):
        current = row.get(field)
        if current and value and current.strip() == value:
            choices[field] = (None, '')
        else:
            choices[field] = (current,)
    # RAW fill only writes into NULL/blank columns; test those exact prior states.
    from itertools import product
    for prior_values in product(*choices.values()):
        reconstructed = dict(row, **dict(zip(choices, prior_values)))
        if _legacy_fingerprint(reconstructed) == expected_fingerprint:
            return reconstructed
    return None


def snapshot(row, result, ttls=None):
    if not isinstance(result, ArticleMatchResult):
        raise TypeError('Persistence requires an article matcher result')
    work = (result.selected_candidate or {}) if result.status == 'matched' else {}
    chosen = next((e for e in result.evidence if e['candidate_index'] == result.selected_candidate_index), {})
    if result.status == 'matched':
        index = result.selected_candidate_index
        if (type(index) is not int or not 0 <= index < len(result.candidates)
                or not work or work != result.candidates[index] or not chosen.get('eligible')
                or chosen.get('doi') != work.get('doi') or chosen.get('conflicts')
                or result.selected_conflicts or result.input_conflicts):
            raise ValueError('Incomplete matched result cannot be persisted')
    dates = work.get('publication_dates', {})
    preferred = next((dates[k] for k in ('published', 'issued', 'published-print', 'published-online') if k in dates), {})
    parts = preferred.get('date_parts', [])
    payload = result.to_dict()
    # All production client/matcher reasons are codes. Never persist exception
    # text supplied by an adapter, nor arbitrary transport strings as errors.
    reason = payload['reason']
    if not re.fullmatch(r'[a-z][a-z0-9_]{0,100}', reason):
        reason = 'crossref_processing_error'
    payload['reason'] = reason
    return dict(raw_publication_id=row['raw_publication_id'], status=result.status,
        lookup_strategy=result.lookup_method, doi=work.get('doi'), matched_title=work.get('titles', []),
        matched_authors=work.get('authors', []), publication_year=parts[0] if parts else None,
        publication_date=parts or None, publication_date_precision=preferred.get('precision'),
        container_title=work.get('container_titles', []), issns=work.get('issns', []),
        publisher=work.get('publisher'), publication_type=work.get('type'),
        author_evidence=chosen.get('authors', {}), year_evidence=chosen.get('year', {}),
        container_evidence=chosen.get('container', {}), title_similarity=chosen.get('title', {}).get('similarity'),
        candidate_count=len(result.candidates), selected_evidence=chosen,
        selected_conflicts=result.selected_conflicts,
        metadata=dict(source='crossref', matcher_version=MATCHER_VERSION, scholar_fingerprint=fingerprint(row),
                      snapshot_id=snapshot_id(row, result),
                      result=payload, article_provenance={'raw_publication_id': row['raw_publication_id']}),
        retrieved_at=result.fetched_at, expires_at=result.fetched_at + timedelta(seconds=ttl_policy(ttls)[result.status]),
        last_error=reason if result.status == 'error' else None)


def can_replace(prior, incoming, now):
    priority = dict(error=0, not_found=1, ambiguous=2, matched=3)
    order = lambda r: (r['retrieved_at'], priority[r['status']], r['metadata'].get('snapshot_id', ''))
    return (order(incoming) > order(prior) and not (prior['status'] == 'matched'
        and prior['expires_at'] > now and incoming['status'] != 'matched'))


def conflict_history(prior, incoming):
    history = deepcopy((prior.get('metadata') or {}).get('snapshot_conflicts') or {})
    for values in (prior, incoming):
        metadata = values.get('metadata') or {}
        key = metadata.get('snapshot_id')
        if key:
            history[key] = dict(snapshot_id=key, retrieved_at=values['retrieved_at'].isoformat(),
                status=values['status'], doi=values.get('doi'), result=metadata.get('result'),
                integration_audits=metadata.get('integration_audits', {}))
    # Bounded, deterministic diagnostic history; not an unbounded event ledger.
    keys = sorted(history, key=lambda k: (history[k]['retrieved_at'], k))[-20:]
    return {key: history[key] for key in keys}


def upsert_statement(values):
    stmt = insert(table).values(**values)
    def priority(status):
        return sa.case((status == 'matched', 3), (status == 'ambiguous', 2), (status == 'not_found', 1), else_=0)
    prior_priority, incoming_priority = priority(table.c.status), priority(stmt.excluded.status)
    tie = sa.and_(stmt.excluded.retrieved_at == table.c.retrieved_at, sa.or_(
        incoming_priority > prior_priority, sa.and_(incoming_priority == prior_priority,
            stmt.excluded.metadata['snapshot_id'].astext.collate('C') >
            sa.func.coalesce(table.c.metadata['snapshot_id'].astext, '').collate('C'))))
    protect = sa.and_(sa.or_(stmt.excluded.retrieved_at > table.c.retrieved_at, tie),
        sa.or_(table.c.status != 'matched', table.c.expires_at <= sa.func.clock_timestamp(), stmt.excluded.status == 'matched'))
    return stmt.on_conflict_do_update(constraint='uq_crossref_publication',
        set_={**{k: stmt.excluded[k] for k in values if k != 'raw_publication_id'}, 'updated_at': sa.func.now()},
        where=protect).returning(table.c.enrichment_id)


def save_enrichment(engine, row, result, ttls=None):
    values = snapshot(row, result, ttls)
    with engine.begin() as conn:
        conn.execute(sa.text('SELECT pg_advisory_xact_lock(166844, :publication_id)'),
                     {'publication_id': row['raw_publication_id']})
        prior = conn.execute(sa.select(table).where(table.c.raw_publication_id == row['raw_publication_id'])
                             .with_for_update()).mappings().first()
        if isinstance(prior, dict) or isinstance(prior, sa.engine.RowMapping):
            if prior['metadata'].get('snapshot_id') != values['metadata']['snapshot_id']:
                values['metadata']['snapshot_conflicts'] = conflict_history(prior, values)
        saved = conn.execute(upsert_statement(values)).scalar_one_or_none() is not None
        if not saved:
            current = conn.execute(sa.select(table).where(table.c.raw_publication_id == row['raw_publication_id'])
                                   .with_for_update()).mappings().first()
            if isinstance(current, dict) or isinstance(current, sa.engine.RowMapping):
                if current['metadata'].get('snapshot_id') != values['metadata']['snapshot_id']:
                    conn.execute(table.update().where(table.c.raw_publication_id == row['raw_publication_id']).values(
                        metadata=table.c.metadata.op('||')({'snapshot_conflicts': conflict_history(current, values)}),
                        updated_at=sa.func.now()))
        return saved


def save_integration_audit(engine, row, result, checked):
    """Store snapshot-bound local-master observations without changing cache TTL.

    The snapshot predicate prevents attaching an old result to a newer enrichment.
    This is called only with the separate --save-crossref authorization.
    """
    observation = json.loads(json.dumps(checked, default=str))
    identity = snapshot_id(row, result)
    audit_body = {k: v for k, v in observation.items() if k != 'checked_at'}
    audit_id = hashlib.sha256(json.dumps(audit_body, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    entry = dict(snapshot_id=identity, observation=observation)
    audits = sa.func.coalesce(table.c.metadata['integration_audits'], sa.cast({}, JSONB)).op('||')(
        sa.cast({audit_id: entry}, JSONB))
    metadata = table.c.metadata.op('||')(sa.func.jsonb_build_object(sa.cast('integration_audits', sa.Text), audits))
    # Keep the legacy latest-observation field deterministic as well.
    latest = sa.case((sa.cast(audit_id, sa.Text).collate('C') > sa.func.coalesce(
        table.c.metadata['scimago_audit_id'].astext, '').collate('C'),
        metadata.op('||')(sa.cast({'scimago_2025': observation, 'scimago_audit_id': audit_id}, JSONB))), else_=metadata)
    with engine.begin() as conn:
        stmt = table.update().where(table.c.raw_publication_id == row['raw_publication_id'],
            table.c.retrieved_at == result.fetched_at, table.c.status == result.status,
            table.c.metadata['scholar_fingerprint'].astext == fingerprint(row),
            table.c.metadata['snapshot_id'].astext == identity).values(metadata=latest, updated_at=sa.func.now())
        return conn.execute(stmt).rowcount == 1


def read_cache(engine, publication_ids):
    with engine.connect() as conn:
        conn.execute(sa.text('SET TRANSACTION READ ONLY'))
        exists = conn.execute(sa.text("SELECT to_regclass('raw.crossref_publication_enrichment')")).scalar_one_or_none()
        if not exists:
            raise CrossrefSchemaError('Crossref table missing: review/apply migration 16 separately; no automatic migration')
        return {r['raw_publication_id']: dict(r) for r in conn.execute(
            sa.select(table).where(table.c.raw_publication_id.in_(publication_ids))).mappings()}


def cache_lookup(cached, row, source='auto', now=None):
    now = now or datetime.now(timezone.utc)
    if cached is None:
        return 'never_checked', None
    metadata = cached.get('metadata') or {}
    expiry = cached.get('expires_at')
    if not isinstance(metadata, dict) or not isinstance(expiry, datetime) or expiry.tzinfo is None or expiry.utcoffset() is None:
        return 'invalid', None
    if metadata.get('matcher_version') != MATCHER_VERSION:
        return 'input_changed', None
    has_doi = any(isinstance(row.get(k), str) and row[k].strip() for k in ('detail_doi', 'doi'))
    method = 'doi_lookup' if source == 'doi' or (source == 'auto' and has_doi) else 'bibliographic_search'
    filled_doi = row.get('detail_doi')
    if expiry <= now:
        return 'expired', None
    try:
        payload = deepcopy(metadata['result'])
        if not isinstance(payload, dict) or not isinstance(payload.get('fetched_at'), str):
            return 'invalid', None
        payload['fetched_at'] = datetime.fromisoformat(payload['fetched_at'].replace('Z', '+00:00'))
        result = ArticleMatchResult(**payload)
        if not all(isinstance(getattr(result, key), list) for key in
                   ('candidates', 'evidence', 'selected_conflicts', 'input_conflicts', 'candidate_decisions')):
            return 'invalid', None
        if (not all(isinstance(w, dict) for w in result.candidates)
                or not all(isinstance(e, dict) and type(e.get('candidate_index')) is int
                    and 0 <= e['candidate_index'] < len(result.candidates)
                    and isinstance(e.get('conflicts'), list) for e in result.evidence)):
            return 'invalid', None
        if result.status != cached['status']:
            raise ValueError('Cache status mismatch')
        if (result.status == 'matched' and isinstance(filled_doi, str) and filled_doi.strip()
                and (not isinstance(result.selected_candidate, dict)
                     or not isinstance(result.selected_candidate.get('doi'), str)
                     or filled_doi.strip().casefold() != result.selected_candidate['doi'].strip().casefold())):
            return 'input_changed', None
        stored_fingerprint = metadata.get('scholar_fingerprint')
        current_fingerprint = fingerprint(row)
        identity_row, identity_fingerprint = row, current_fingerprint
        legacy = stored_fingerprint != current_fingerprint
        if legacy:
            if result.status != 'matched':
                return 'input_changed', None
            identity_row = _legacy_matched_row(row, result, stored_fingerprint)
            if identity_row is None:
                return 'input_changed', None
            identity_fingerprint = stored_fingerprint
        strategy_matches = cached['lookup_strategy'] == method
        source_doi = row.get('doi')
        selected = result.selected_candidate if isinstance(result.selected_candidate, dict) else {}
        cached_doi = selected.get('doi')
        filled_from_this_cache = (source == 'auto' and cached['lookup_strategy'] == 'bibliographic_search'
            and not (isinstance(source_doi, str) and source_doi.strip())
            and isinstance(filled_doi, str) and isinstance(cached_doi, str)
            and filled_doi.strip().casefold() == cached_doi.strip().casefold())
        if not strategy_matches and not filled_from_this_cache:
            return 'strategy_changed', None
        if result.status == 'matched':
            # Validate completeness without reclassifying old ambiguous results.
            snapshot(row, result)
        if metadata.get('snapshot_id') != _snapshot_id_for_fingerprint(result, identity_fingerprint):
            return 'invalid', None
    except (KeyError, ValueError, TypeError, AttributeError, IndexError, OverflowError):
        return 'invalid', None
    return 'hit', result


# Kolom RAW yang boleh diisi dari hasil matched; urutan tetap untuk laporan.
RAW_FILL_COLUMNS = ('detail_doi', 'detail_publisher', 'detail_venue')


def raw_fill_values(result):
    """Nilai kandidat isi RAW dari kandidat matched; nilai kosong dibuang."""
    work = result.selected_candidate or {}
    venue = next((t.strip() for t in work.get('container_titles', [])
                  if isinstance(t, str) and t.strip()), None)
    values = {'detail_doi': (work.get('doi') or '').strip() or None,
              'detail_publisher': (work.get('publisher') or '').strip() or None,
              'detail_venue': venue}
    return {k: v for k, v in values.items() if v}


def fill_raw_from_matched(engine, row, result):
    """Isi kolom KOSONG raw.scholar_publication dari hasil matched tervalidasi.

    Hanya status matched yang lolos snapshot() (validasi kelengkapan existing)
    boleh mengisi; ambiguous/not_found/error tidak pernah menulis. Seluruh baris
    RAW dengan scholar_id sama yang identitasnya sama dengan baris lookup
    (kunci DOI/bibliografi yang sama seperti deduplikasi select_publications)
    ikut diisi; identitas yang bertentangan (tahun/DOI berbeda) tidak pernah
    diisi. Kolom yang sudah bernilai tidak pernah ditimpa - hanya kolom
    NULL/blank yang masuk SET, dibaca ulang di bawah lock baris dalam satu
    transaksi. Tidak ada DDL dan tidak ada data Google Scholar yang
    diubah/dihapus. Bila scholar_id tidak tersedia (pemanggil lama), hanya
    baris lookup yang diproses. Return: gabungan kolom yang terisi.
    """
    if result.status != 'matched':
        return {}
    snapshot(row, result)  # gerbang validasi; ValueError bila matched tidak lengkap
    values = raw_fill_values(result)
    if not values:
        return {}
    raw = meta.tables['raw.scholar_publication']
    with engine.begin() as conn:
        lookup_current = conn.execute(sa.select(*[getattr(raw.c, name) for name in RAW_FILL_COLUMNS],
            raw.c.scholar_id).where(raw.c.raw_publication_id == row['raw_publication_id'])
            .with_for_update()).mappings().first()
        if lookup_current is None:
            return {}
        targets = [(row['raw_publication_id'], lookup_current)]
        scholar_id = lookup_current.get('scholar_id')
        if isinstance(scholar_id, str) and scholar_id.strip():
            # Cari baris duplikat HANYA pada scholar_id yang sama; identitas
            # dinilai dengan kunci yang sama seperti deduplikasi pembacaan.
            siblings = conn.execute(sa.text(f'''SELECT {_RAW_IDENTITY_COLUMNS}, p.scholar_id
                FROM raw.scholar_publication p
                WHERE p.scholar_id = :scholar_id
                  AND p.raw_publication_id <> :exclude_id
                ORDER BY p.raw_publication_id'''),
                {'scholar_id': scholar_id, 'exclude_id': row['raw_publication_id']}).mappings()
            lookup_doi, lookup_key = bibliography_key(row)
            matched_ids = []
            for sibling in siblings:
                if sibling.get('scholar_id') != scholar_id:
                    continue
                sibling_doi, sibling_key = bibliography_key(dict(sibling))
                doi_conflict = bool(lookup_doi and sibling_doi and lookup_doi != sibling_doi)
                same_doi = bool(lookup_doi and sibling_doi and lookup_doi == sibling_doi)
                if doi_conflict or not (same_doi or lookup_key == sibling_key):
                    continue  # identitas ambigu atau berbeda: jangan diisi
                matched_ids.append(sibling['raw_publication_id'])
            if matched_ids:
                locked = conn.execute(sa.select(*[getattr(raw.c, name) for name in RAW_FILL_COLUMNS],
                    raw.c.raw_publication_id).where(raw.c.raw_publication_id.in_(matched_ids))
                    .with_for_update()).mappings()
                targets.extend((current['raw_publication_id'], current) for current in locked)
        filled = {}
        for publication_id, current in targets:
            fillable = {name: value for name, value in values.items()
                        if current[name] is None or not str(current[name]).strip()}
            if not fillable:
                continue
            conn.execute(raw.update().where(raw.c.raw_publication_id == publication_id)
                         .values(**fillable))
            filled.update(fillable)
        return filled
