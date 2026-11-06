"""PostgreSQL statements and cache policy tested without a database."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock
import hashlib
import json

import pytest
import requests
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from src.journal_index import crossref_store as store
from src.journal_index.crossref import CrossrefResult, parse_work
from src.journal_index.crossref_matcher import match_article

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def forbidden(*a, **k):
        raise AssertionError('No live I/O')
    monkeypatch.setattr(requests.Session, 'request', forbidden)
    monkeypatch.setattr(sa.engine.Engine, 'connect', forbidden)
    monkeypatch.setattr(sa.engine.Engine, 'raw_connection', forbidden)


def article():
    return dict(raw_publication_id=1, detail_title='Reliable metadata matching for scientific publications',
                detail_authors='Ana First, Budi Second', publication_year=2025,
                detail_journal='Journal of Testing', detail_publication_type='journal')


def _legacy_fingerprint(row):
    fields = ('raw_publication_id', 'detail_title', 'title', 'publication_authors', 'detail_authors',
              'authors', 'publication_year', 'year', 'detail_date', 'detail_journal', 'detail_venue',
              'venue', 'detail_doi', 'doi', 'detail_publication_type',
              'issn', 'eissn', 'detail_issn', 'detail_eissn')
    data = {key: row.get(key) for key in fields}
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=True).encode()).hexdigest()


def _legacy_snapshot_id(row, result):
    identity = dict(input=_legacy_fingerprint(row), matcher=store.MATCHER_VERSION,
                    result=result.to_dict())
    return hashlib.sha256(json.dumps(identity, sort_keys=True, ensure_ascii=True,
        separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def _as_legacy_cache(row, result):
    cached = store.snapshot(row, result)
    cached['metadata']['scholar_fingerprint'] = _legacy_fingerprint(row)
    cached['metadata']['snapshot_id'] = _legacy_snapshot_id(row, result)
    return cached


def decision(status='matched', kind='journal-article', identifiers=None):
    item = parse_work({'DOI': '10.1234/test', 'title': [article()['detail_title']],
        'author': [{'given': 'Ana', 'family': 'First'}, {'given': 'Budi', 'family': 'Second'}],
        'published': {'date-parts': [[2025]]}, 'container-title': ['Journal of Testing'],
        'type': kind, 'ISSN': identifiers or []})
    row = article()
    if kind != 'journal-article':
        row['detail_publication_type'] = None
    works = [] if status in ('not_found', 'error') else [item]
    if status == 'ambiguous':
        works[0]['authors'] = []
    return match_article(row, CrossrefResult(status='error' if status == 'error' else 'ambiguous',
        lookup_method='bibliographic_search', fetched_at=NOW, reason='fixture', candidates=works))


@pytest.mark.parametrize('status,ttl', [('matched', 2592000), ('ambiguous', 604800),
                                      ('not_found', 86400), ('error', 3600)])
def test_snapshot_fields_ttl_and_json(status, ttl):
    values = store.snapshot(article(), decision(status))
    assert values['status'] == status
    assert (values['expires_at'] - NOW).total_seconds() == ttl
    assert json.loads(json.dumps(values['metadata'])) == values['metadata']
    if status == 'matched':
        assert values['publication_date'] == [2025] and values['publication_date_precision'] == 'year'
        assert values['doi'] == '10.1234/test' and values['title_similarity'] == 1
    else:
        assert values['doi'] is None and values['issns'] == []


def test_insert_upsert_scoped_protection_and_binding():
    stmt = store.upsert_statement(store.snapshot(article(), decision()))
    sql = str(stmt.compile(dialect=postgresql.dialect()))
    assert 'INSERT INTO raw.crossref_publication_enrichment' in sql
    assert 'ON CONFLICT ON CONSTRAINT uq_crossref_publication' in sql
    assert 'expires_at' in sql and 'excluded.status' in sql and 'excluded.retrieved_at' in sql
    assert 'scholar_publication SET' not in sql and 'journal_index_verification' not in sql
    assert stmt.compile(dialect=postgresql.dialect()).params['raw_publication_id'] == 1


def test_foreign_key_unique_jsonb_and_migration_additive():
    assert next(iter(store.table.c.raw_publication_id.foreign_keys)).target_fullname == 'raw.scholar_publication.raw_publication_id'
    assert isinstance(store.table.c.metadata.type, postgresql.JSONB)
    assert any(c.name == 'uq_crossref_publication' for c in store.table.constraints)
    ddl = Path('sql/ddl/16_create_crossref_publication_enrichment.sql').read_text()
    assert 'REFERENCES raw.scholar_publication(raw_publication_id)' in ddl
    assert 'UNIQUE (raw_publication_id)' in ddl
    assert not any(token in ddl.upper() for token in ('DROP ', 'DELETE ', 'TRUNCATE ', 'ALTER TABLE'))


@pytest.mark.parametrize('change,state', [('none', 'hit'), ('expired', 'expired'),
    ('input', 'input_changed'), ('missing', 'never_checked'), ('version', 'input_changed')])
def test_cache_state_and_roundtrip(change, state):
    row = article()
    cached = store.snapshot(row, decision())
    if change == 'expired':
        cached['expires_at'] = NOW
    elif change == 'input':
        row['detail_title'] += ' changed'
    elif change == 'missing':
        cached = None
    elif change == 'version':
        cached['metadata']['matcher_version'] = 'old'
    actual, result = store.cache_lookup(cached, row, 'auto', NOW)
    assert actual == state
    if state == 'hit':
        assert result.status == 'matched' and result.to_dict() == decision().to_dict()
    else:
        assert result is None


def test_cache_remains_valid_after_crossref_fills_raw_targets():
    row = article()
    result = decision()
    cached = store.snapshot(row, result)
    original_fingerprint = cached['metadata']['scholar_fingerprint']
    original_snapshot_id = cached['metadata']['snapshot_id']
    row.update(detail_doi='10.1234/test', detail_venue='Journal of Testing')

    assert store.fingerprint(row) == original_fingerprint
    assert store.snapshot_id(row, result) == original_snapshot_id
    state, loaded = store.cache_lookup(cached, row, 'auto', NOW)
    assert state == 'hit' and loaded.to_dict() == result.to_dict()


def test_legacy_matched_cache_validates_reconstructed_pre_fill_snapshot():
    before = article()
    result = decision()
    cached = _as_legacy_cache(before, result)
    after = dict(before, detail_doi='10.1234/test', detail_venue='Journal of Testing')

    state, loaded = store.cache_lookup(cached, after, 'auto', NOW)

    assert state == 'hit' and loaded.to_dict() == result.to_dict()


@pytest.mark.parametrize('field,value', [
    ('detail_title', 'Changed source title'),
    ('detail_authors', 'Changed source author'),
    ('publication_year', 2024),
])
def test_legacy_cache_still_invalidates_source_identity_changes(field, value):
    original = article()
    cached = _as_legacy_cache(original, decision())
    changed = dict(original, detail_doi='10.1234/test', detail_venue='Journal of Testing', **{field: value})
    assert store.cache_lookup(cached, changed, 'auto', NOW) == ('input_changed', None)


def test_legacy_matched_cache_rejects_doi_not_matching_selected_work():
    original = article()
    cached = _as_legacy_cache(original, decision())
    changed = dict(original, detail_doi='10.9999/other', detail_venue='Journal of Testing')
    assert store.cache_lookup(cached, changed, 'auto', NOW) == ('input_changed', None)


def test_legacy_cache_rejects_snapshot_id_not_matching_old_fingerprint():
    original = article()
    cached = _as_legacy_cache(original, decision())
    cached['metadata']['snapshot_id'] = '0' * 64
    filled = dict(original, detail_doi='10.1234/test', detail_venue='Journal of Testing')
    assert store.cache_lookup(cached, filled, 'auto', NOW) == ('invalid', None)


@pytest.mark.parametrize('status', ['error', 'not_found'])
def test_legacy_nonmatched_cache_does_not_assume_target_fields_were_filled(status):
    original = article()
    cached = _as_legacy_cache(original, decision(status))
    changed = dict(original, detail_doi='10.1234/test', detail_venue='Journal of Testing')
    assert store.cache_lookup(cached, changed, 'auto', NOW) == ('input_changed', None)


@pytest.mark.parametrize('field,value', [
    ('detail_title', 'A changed source title'),
    ('detail_authors', 'A changed source author'),
    ('publication_year', 2024),
])
def test_source_identity_changes_invalidate_cache(field, value):
    cached = store.snapshot(article(), decision())
    changed = dict(article(), **{field: value})
    assert store.cache_lookup(cached, changed, 'auto', NOW) == ('input_changed', None)


def test_valid_match_protection_and_older_result_policy():
    old = store.snapshot(article(), decision())
    failed = store.snapshot(article(), decision('error'))
    assert not store.can_replace(old, failed, NOW)
    failed['retrieved_at'] = NOW - timedelta(seconds=1)
    assert not store.can_replace(old, failed, NOW + timedelta(days=40))
    failed['retrieved_at'] = NOW + timedelta(days=31)
    assert store.can_replace(old, failed, failed['retrieved_at'])


def test_save_transaction_rollback_on_failure():
    engine = MagicMock()
    conn = engine.begin.return_value.__enter__.return_value
    conn.execute.side_effect = RuntimeError('failure')
    with pytest.raises(RuntimeError):
        store.save_enrichment(engine, article(), decision())
    assert engine.begin.return_value.__exit__.call_args.args[0] is RuntimeError


def test_configurable_ttl_and_invalid_values():
    assert store.snapshot(article(), decision(), {'matched': 120})['expires_at'] == NOW + timedelta(seconds=120)
    for values in ({'matched': 0}, {'other': 10}, {'matched': float('nan')}):
        with pytest.raises(ValueError):
            store.ttl_policy(values)


def test_issn_types_and_all_provenance_preserved():
    result = decision(identifiers=['0378-5955', '0309-0566'])
    values = store.snapshot(article(), result)
    assert values['issns'] == ['0378-5955', '0309-0566']
    assert values['metadata']['result'] == result.to_dict()
    assert values['metadata']['source'] == 'crossref'
    assert 'raw.scholar_publication' not in str(store.upsert_statement(values).compile(dialect=postgresql.dialect()))


def test_cache_error_is_distinct_from_never_checked():
    state, result = store.cache_lookup(store.snapshot(article(), decision('error')), article(), 'auto', NOW)
    assert state == 'hit' and result.status == 'error'


def test_error_reason_sanitized_without_exception_details():
    result = decision('error')
    object.__setattr__(result, 'reason', 'https://host/?mailto=secret@example.test password=secret')
    values = store.snapshot(article(), result)
    assert 'secret' not in json.dumps(values['metadata'])
    assert values['last_error'] == 'crossref_processing_error'


def test_whitespace_doi_uses_same_cache_strategy_as_lookup():
    row = dict(article(), detail_doi=' ')
    assert store.cache_lookup(store.snapshot(row, decision()), row, 'auto', NOW)[0] == 'hit'


@pytest.mark.parametrize('field,value', [('fetched_at', None), ('evidence', [None]),
    ('selected_candidate_index', '0'), ('selected_candidate', []), ('candidates', None)])
def test_malformed_cache_is_invalid_not_batch_failure(field, value):
    cached = store.snapshot(article(), decision())
    cached['metadata']['result'][field] = value
    assert store.cache_lookup(cached, article(), 'auto', NOW) == ('invalid', None)


@pytest.mark.parametrize('value', [None, '2026-01-02', datetime(2026, 1, 2)])
def test_bad_expiry_is_invalid(value):
    cached = store.snapshot(article(), decision())
    cached['expires_at'] = value
    assert store.cache_lookup(cached, article(), 'auto', NOW) == ('invalid', None)


@pytest.mark.parametrize('status', ['ambiguous', 'not_found', 'error'])
def test_malformed_nonmatched_evidence_cannot_crash_reporting(status):
    cached = store.snapshot(article(), decision(status))
    cached['metadata']['result']['evidence'] = [None]
    assert store.cache_lookup(cached, article(), 'auto', NOW) == ('invalid', None)


def test_integration_conflicts_saved_separately_without_changing_ttl():
    engine, conn = MagicMock(), MagicMock()
    engine.begin.return_value.__enter__.return_value = conn
    conn.execute.return_value.rowcount = 1
    assert store.save_integration_audit(engine, article(), decision(),
        {'source': 'scimagojr', 'status': 'unverified', 'reason': 'conflicting_article_serials'})
    stmt = conn.execute.call_args.args[0]
    sql = str(stmt.compile(dialect=postgresql.dialect()))
    assert 'UPDATE raw.crossref_publication_enrichment' in sql
    assert 'expires_at' not in sql and 'retrieved_at =' in sql
    assert 'journal_index_verification' not in sql


@pytest.mark.parametrize('returned,expected', [(1, True), (None, False)])
def test_save_insert_update_bindings_and_protected_return(returned, expected):
    engine, conn = MagicMock(), MagicMock()
    engine.begin.return_value.__enter__.return_value = conn
    conn.execute.return_value.scalar_one_or_none.return_value = returned
    assert store.save_enrichment(engine, article(), decision()) is expected
    statement = next(call.args[0] for call in conn.execute.call_args_list
                     if getattr(call.args[0], 'is_insert', False))
    compiled = statement.compile(dialect=postgresql.dialect())
    assert compiled.params['metadata']['result']['status'] == 'matched'
    assert compiled.params['raw_publication_id'] == 1
    assert 'created_at =' not in str(compiled)  # historical creation time is retained on UPDATE


def test_print_electronic_labels_retained_without_order_inference():
    from src.journal_index.crossref import CrossrefResult
    work = deepcopy(decision(identifiers=['0309-0566', '0378-5955']).candidates[0])
    work['issn_types'] = [{'value': '0378-5955', 'type': 'electronic'},
                          {'value': '0309-0566', 'type': 'print'}]
    result = match_article(article(), CrossrefResult(status='ambiguous', lookup_method='bibliographic_search',
        fetched_at=NOW, reason='fixture', candidates=[work]))
    values = store.snapshot(article(), result)
    assert values['issns'] == ['0309-0566', '0378-5955']
    assert values['metadata']['result']['selected_candidate']['issn_types'] == work['issn_types']


# --------------------------------------------------------------------------- #
# Pengisian kolom RAW kosong dari hasil matched tervalidasi (opsional).        #
# --------------------------------------------------------------------------- #

def test_fill_raw_from_matched_updates_only_empty_columns():
    engine = MagicMock()
    conn = engine.begin.return_value.__enter__.return_value
    current = {'detail_doi': None, 'detail_publisher': 'Already Set', 'detail_venue': '   '}
    conn.execute.side_effect = [MagicMock(mappings=lambda: MagicMock(first=lambda: current)), MagicMock()]
    filled = store.fill_raw_from_matched(engine, article(), decision())
    assert filled == {'detail_doi': '10.1234/test', 'detail_venue': 'Journal of Testing'}
    update_stmt = conn.execute.call_args_list[1].args[0]
    compiled = update_stmt.compile(dialect=postgresql.dialect())
    assert 'UPDATE raw.scholar_publication' in str(compiled)
    assert 'detail_publisher' not in str(compiled)  # kolom yang sudah terisi tidak disentuh
    assert compiled.params['detail_doi'] == '10.1234/test'
    engine.begin.assert_called_once()


def test_fill_raw_populated_row_needs_no_write():
    engine = MagicMock()
    conn = engine.begin.return_value.__enter__.return_value
    current = {'detail_doi': '10.9999/already', 'detail_publisher': 'Set', 'detail_venue': 'Set'}
    conn.execute.side_effect = [MagicMock(mappings=lambda: MagicMock(first=lambda: current))]
    assert store.fill_raw_from_matched(engine, article(), decision()) == {}
    assert conn.execute.call_count == 1  # hanya SELECT; tidak ada UPDATE


@pytest.mark.parametrize('status', ['ambiguous', 'not_found', 'error'])
def test_fill_raw_nonmatched_never_writes(status):
    engine = MagicMock()
    assert store.fill_raw_from_matched(engine, article(), decision(status)) == {}
    engine.begin.assert_not_called()


def test_fill_raw_incomplete_matched_raises_without_write():
    from dataclasses import replace
    result = replace(decision(), selected_conflicts=('conflict',))
    engine = MagicMock()
    with pytest.raises(ValueError):
        store.fill_raw_from_matched(engine, article(), result)
    engine.begin.assert_not_called()


# --------------------------------------------------------------------------- #
# Pengisian baris duplikat pada scholar_id yang sama (identitas konservatif).  #
# --------------------------------------------------------------------------- #

SCHOLAR = 'PiYosHQAAAAJ'


def _fill_engine(lookup_current, siblings=None, sibling_currents=None, updates=0):
    engine = MagicMock()
    conn = engine.begin.return_value.__enter__.return_value
    side = [MagicMock(mappings=lambda: MagicMock(first=lambda: lookup_current))]
    if siblings is not None:
        side.append(MagicMock(mappings=lambda: list(siblings)))
    if sibling_currents is not None:
        side.append(MagicMock(mappings=lambda: list(sibling_currents)))
    side.extend(MagicMock() for _ in range(updates))
    conn.execute.side_effect = side
    return engine, conn


def _updates(conn):
    return [call.args[0] for call in conn.execute.call_args_list
            if 'UPDATE' in str(call.args[0]).upper()[:60] or getattr(call.args[0], 'is_update', False)]


def _update_id(stmt):
    """Ambil id baris target dari statement UPDATE (param raw_publication_id_*)."""
    params = stmt.compile(dialect=postgresql.dialect()).params
    return next(value for key, value in params.items() if key.startswith('raw_publication_id'))


def test_fill_raw_duplicates_same_scholar_id_both_filled():
    lookup = {'detail_doi': None, 'detail_publisher': None, 'detail_venue': None, 'scholar_id': SCHOLAR}
    sibling_row = dict(article(), raw_publication_id=2, scholar_id=SCHOLAR)
    sibling_current = {'raw_publication_id': 2, 'detail_doi': None,
                       'detail_publisher': None, 'detail_venue': None}
    engine, conn = _fill_engine(lookup, siblings=[sibling_row], sibling_currents=[sibling_current],
                                updates=2)
    filled = store.fill_raw_from_matched(engine, article(), decision())
    assert filled == {'detail_doi': '10.1234/test', 'detail_venue': 'Journal of Testing'}
    sibling_call = conn.execute.call_args_list[1]
    assert 'p.scholar_id = :scholar_id' in str(sibling_call.args[0])
    assert sibling_call.args[1] == {'scholar_id': SCHOLAR, 'exclude_id': 1}
    statements = _updates(conn)
    assert len(statements) == 2
    assert {_update_id(stmt) for stmt in statements} == {1, 2}


def test_fill_raw_different_scholar_id_not_filled():
    lookup = {'detail_doi': None, 'detail_publisher': None, 'detail_venue': None, 'scholar_id': SCHOLAR}
    sibling_row = dict(article(), raw_publication_id=2, scholar_id='OTHER_ID')
    engine, conn = _fill_engine(lookup, siblings=[sibling_row], updates=1)
    filled = store.fill_raw_from_matched(engine, article(), decision())
    assert filled == {'detail_doi': '10.1234/test', 'detail_venue': 'Journal of Testing'}
    assert len(_updates(conn)) == 1  # hanya baris lookup
    assert conn.execute.call_count == 3  # lookup, siblings, satu UPDATE


def test_fill_raw_conflicting_year_not_filled():
    lookup = {'detail_doi': None, 'detail_publisher': None, 'detail_venue': None, 'scholar_id': SCHOLAR}
    sibling_row = dict(article(), raw_publication_id=2, scholar_id=SCHOLAR, publication_year=1999)
    engine, conn = _fill_engine(lookup, siblings=[sibling_row], updates=1)
    filled = store.fill_raw_from_matched(engine, article(), decision())
    assert filled == {'detail_doi': '10.1234/test', 'detail_venue': 'Journal of Testing'}
    assert len(_updates(conn)) == 1  # hanya baris lookup; duplikat tahun beda tidak diisi


def test_fill_raw_conflicting_doi_not_filled():
    row = dict(article(), detail_doi='10.1234/test')
    lookup = {'detail_doi': '10.1234/test', 'detail_publisher': None, 'detail_venue': None,
              'scholar_id': SCHOLAR}
    sibling_row = dict(article(), raw_publication_id=2, scholar_id=SCHOLAR, detail_doi='10.9999/other')
    engine, conn = _fill_engine(lookup, siblings=[sibling_row], updates=1)
    filled = store.fill_raw_from_matched(engine, row, decision())
    assert filled == {'detail_venue': 'Journal of Testing'}  # lookup: doi sudah terisi
    assert len(_updates(conn)) == 1  # DOI bertentangan: duplikat tidak diisi


def test_fill_raw_sibling_populated_columns_not_overwritten():
    lookup = {'detail_doi': None, 'detail_publisher': None, 'detail_venue': None, 'scholar_id': SCHOLAR}
    sibling_row = dict(article(), raw_publication_id=2, scholar_id=SCHOLAR)
    sibling_current = {'raw_publication_id': 2, 'detail_doi': None,
                       'detail_publisher': 'Set', 'detail_venue': 'Set'}
    engine, conn = _fill_engine(lookup, siblings=[sibling_row], sibling_currents=[sibling_current],
                                updates=2)
    filled = store.fill_raw_from_matched(engine, article(), decision())
    assert filled == {'detail_doi': '10.1234/test', 'detail_venue': 'Journal of Testing'}
    statements = _updates(conn)
    sibling_stmt = next(stmt for stmt in statements if _update_id(stmt) == 2)
    compiled = str(sibling_stmt.compile(dialect=postgresql.dialect()))
    assert 'detail_doi' in compiled
    assert 'detail_venue' not in compiled and 'detail_publisher' not in compiled


def test_fill_raw_rerun_is_idempotent():
    lookup = {'detail_doi': '10.1234/test', 'detail_publisher': None,
              'detail_venue': 'Journal of Testing', 'scholar_id': SCHOLAR}
    sibling_row = dict(article(), raw_publication_id=2, scholar_id=SCHOLAR)
    sibling_current = {'raw_publication_id': 2, 'detail_doi': '10.1234/test',
                       'detail_publisher': None, 'detail_venue': 'Journal of Testing'}
    engine, conn = _fill_engine(lookup, siblings=[sibling_row], sibling_currents=[sibling_current])
    assert store.fill_raw_from_matched(engine, article(), decision()) == {}
    assert _updates(conn) == []
    assert conn.execute.call_count == 3  # hanya SELECT; tidak ada UPDATE ulang


def test_fill_raw_preserves_different_existing_values():
    lookup = {'detail_doi': '10.9999/preexisting', 'detail_publisher': 'Existing publisher',
              'detail_venue': 'Existing venue', 'scholar_id': None}
    engine, conn = _fill_engine(lookup)

    assert store.fill_raw_from_matched(engine, article(), decision()) == {}
    assert _updates(conn) == []
