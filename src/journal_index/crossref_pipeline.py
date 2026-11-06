"""Bounded cache/enrichment/local-master orchestration. No automatic DDL."""
from datetime import datetime, timedelta, timezone
import json

from .crossref import CrossrefClient, CrossrefResult
from .crossref_matcher import match_article
from .crossref_check import read_publications, select_publications, _first
from . import crossref_store as store
from .crossref_scimago import match_serial, save_master_verification


def parse_ttls(entries):
    overrides = {}
    try:
        for entry in entries:
            name, seconds = entry.split('=', 1)
            overrides[name] = float(seconds)
    except (ValueError, TypeError):
        raise ValueError('Use --crossref-ttl STATUS=SECONDS') from None
    return store.ttl_policy(overrides)


def run(engine, args, target, master=None, *, now=None, clock=None):
    if now is not None and clock is not None:
        raise ValueError('Specify a clock or a fixed compatibility time, not both')
    clock = clock or ((lambda: now) if now is not None else lambda: datetime.now(timezone.utc))
    ttls = parse_ttls(args.crossref_ttl)
    rows = select_publications(read_publications(engine, args.row_limit, target,
                                scholar_id=getattr(args, 'scholar_id', None)),
                               args.crossref_limit, args.crossref_source)
    cached = store.read_cache(engine, [r['raw_publication_id'] for r in rows])
    client, errors = None, 0
    try:
        for row in rows:
            cached_row = cached.get(row['raw_publication_id'])
            state, decision = store.cache_lookup(cached_row, row, args.crossref_source, clock())
            expiry = cached_row['expires_at'] if decision else None
            request_status, saved, verification_saved = None, False, 'not_requested'
            integration_audit_saved = False
            if decision is None and (args.enrich_crossref or args.refresh_crossref):
                if client is None:
                    import os
                    client = CrossrefClient(mailto=os.getenv('CROSSREF_MAILTO'), min_interval=args.crossref_delay)
                doi = _first(row, 'detail_doi', 'doi')
                method = 'doi_lookup' if args.crossref_source == 'doi' or (args.crossref_source == 'auto' and doi) else 'bibliographic_search'
                try:
                    retrieval = client.lookup_doi(doi) if method == 'doi_lookup' else client.search_bibliographic(_first(row, 'detail_title', 'title'))
                    decision = match_article(row, retrieval)
                except Exception:
                    decision = match_article(row, CrossrefResult(status='error', lookup_method=method,
                        fetched_at=clock(), reason='crossref_processing_error'))
                request_status = decision.status
                expiry = decision.fetched_at + timedelta(seconds=ttls[decision.status])
                if args.save_crossref:
                    saved = store.save_enrichment(engine, row, decision, ttls)
                    if not saved:
                        # A concurrent newer/valid matched cache may have won.
                        fresh = store.read_cache(engine, [row['raw_publication_id']])
                        recovered_row = fresh.get(row['raw_publication_id'])
                        recovered_state, recovered = store.cache_lookup(recovered_row, row, args.crossref_source, clock())
                        if recovered_state == 'hit':
                            state, decision = 'protected_existing', recovered
                            expiry = recovered_row['expires_at']
                        else:
                            state, decision = 'protected_unusable', None
                            errors += 1
            errors += bool((decision and decision.status == 'error') or request_status == 'error')
            candidate, checked = None, None
            if decision is not None and expiry <= clock():
                state, decision = 'expired_during_processing', None
            if args.crossref_scimago and decision is not None:
                candidate, checked = match_serial(row, decision, master)
                if expiry <= clock():
                    state, decision, checked = 'expired_during_processing', None, None
                if decision is not None and args.save_scimago and checked['status'] == 'indexed':
                    verification_saved = save_master_verification(engine, candidate, checked, args.cache_days)
                    if verification_saved.startswith('catalog_conflict'):
                        checked = dict(checked, status='unverified', indexed=None, sjr=[],
                            reason=verification_saved,
                            metadata=dict(checked['metadata'], identity_match=False))
                if decision is not None and args.save_crossref:
                    integration_audit_saved = store.save_integration_audit(engine, row, decision, checked)
            # Pengisian RAW opsional: hanya matched tervalidasi, hanya kolom kosong.
            raw_fill = None
            if getattr(args, 'fill_raw_crossref', False) and decision is not None \
                    and decision.status == 'matched':
                raw_fill = store.fill_raw_from_matched(engine, row, decision)
            work = (decision.selected_candidate or {}) if decision else {}
            chosen = next((e for e in decision.evidence if e['candidate_index'] == decision.selected_candidate_index), {}) if decision else {}
            payload = dict(raw_publication_id=row['raw_publication_id'], scholar_title=_first(row, 'detail_title', 'title'),
                cache_status=state, request_status=request_status, crossref_saved=saved,
                crossref_status=decision.status if decision else 'not_checked',
                crossref_reason=decision.reason if decision else 'no_valid_crossref_cache',
                crossref_doi=work.get('doi'), publication_type=work.get('type'),
                issns=work.get('issns', []), issn_types=work.get('issn_types', []),
                selected_evidence=chosen, selected_conflicts=decision.selected_conflicts if decision else [],
                crossref_evidence=decision.evidence if decision else [],
                crossref_provenance=dict(lookup_strategy=decision.lookup_method,
                    retrieved_at=decision.to_dict()['fetched_at'], matcher_version=store.MATCHER_VERSION) if decision else {},
                scimago_status=checked['status'] if checked else 'unverified' if args.crossref_scimago else 'not_requested',
                matching_method=checked['metadata'].get('match_method') if checked else None,
                scimago_reason=checked['reason'] if checked else 'no_valid_crossref_identity' if args.crossref_scimago else None,
                ranking_year=checked['metadata'].get('reference_year') if checked else None,
                quartiles=checked['sjr'] if checked else [], scimago_saved=verification_saved,
                 integration_audit_saved=integration_audit_saved,
                 raw_fill=raw_fill,
                 scimago_metadata=checked['metadata'] if checked else {})
            print(json.dumps(payload, ensure_ascii=True, default=str))
        if not rows:
            print(json.dumps({'crossref_status': 'not_checked', 'reason': 'no_eligible_publications', 'articles_checked': 0}))
    finally:
        if client is not None:
            client.close()
    return 1 if errors else 0
