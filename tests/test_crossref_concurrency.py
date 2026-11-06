"""Offline transaction model plus PostgreSQL statement assertions; no live DB."""
from copy import deepcopy
from datetime import timedelta
from concurrent.futures import ThreadPoolExecutor
from threading import Lock
from unittest.mock import MagicMock
from contextlib import contextmanager
from dataclasses import replace

import pytest
from sqlalchemy.dialects import postgresql

from src.journal_index import crossref_store as store
from src.journal_index.crossref import CrossrefResult
from src.journal_index.crossref_matcher import match_article
from test_crossref_persistence import article, decision, NOW, offline


def different_match():
    row = article()
    candidate = deepcopy(decision().candidates[0])
    candidate['doi'] = '10.1234/another'
    return match_article(row, CrossrefResult(status='ambiguous', lookup_method='bibliographic_search',
        fetched_at=NOW, reason='fixture', candidates=[candidate]))


@pytest.mark.parametrize('status', ['error', 'not_found'])
def test_valid_match_cannot_be_replaced_by_negative(status):
    old = store.snapshot(article(), decision())
    incoming = store.snapshot(article(), decision(status))
    incoming['retrieved_at'] += timedelta(seconds=1)
    assert not store.can_replace(old, incoming, NOW)


def test_equal_timestamp_matched_snapshots_have_order_independent_winner():
    a, b = [store.snapshot(article(), r) for r in (decision(), different_match())]
    assert a['metadata']['snapshot_id'] != b['metadata']['snapshot_id']
    forward = b if store.can_replace(a, b, NOW) else a
    backward = a if store.can_replace(b, a, NOW) else b
    assert forward['metadata']['snapshot_id'] == backward['metadata']['snapshot_id']


def test_older_commit_later_is_rejected():
    a, b = [store.snapshot(article(), r) for r in (decision(), different_match())]
    b['retrieved_at'] += timedelta(seconds=1)
    assert not store.can_replace(b, a, NOW)


def test_atomic_upsert_has_strict_timestamp_and_hash_tiebreak():
    values = store.snapshot(article(), decision())
    compiled = store.upsert_statement(values).compile(dialect=postgresql.dialect())
    sql = str(compiled)
    assert 'retrieved_at > ' in sql and 'snapshot_id' in compiled.params.values() and 'CASE WHEN' in sql
    assert 'clock_timestamp()' in sql


def test_conflict_history_retains_both_snapshot_identities():
    a, b = [store.snapshot(article(), r) for r in (decision(), different_match())]
    history = store.conflict_history(a, b)
    assert set(history) == {a['metadata']['snapshot_id'], b['metadata']['snapshot_id']}
    assert {entry['doi'] for entry in history.values()} == {'10.1234/test', '10.1234/another'}


def test_audit_update_predicate_contains_exact_snapshot_identity():
    engine = MagicMock()
    store.save_integration_audit(engine, article(), decision(), {'status': 'unverified'})
    stmt = engine.begin.return_value.__enter__.return_value.execute.call_args.args[0]
    sql = str(stmt.compile(dialect=postgresql.dialect()))
    assert 'snapshot_id' in stmt.compile(dialect=postgresql.dialect()).params.values()
    assert 'integration_audits' in str(stmt.compile(dialect=postgresql.dialect()).params)
    assert 'jsonb_build_object(CAST(' in sql  # polymorphic key bind must have a PostgreSQL type
    assert store.snapshot(article(), decision())['metadata']['snapshot_id'] in stmt.compile(dialect=postgresql.dialect()).params.values()


def test_two_thread_transaction_model_converges():
    a, b = [store.snapshot(article(), r) for r in (decision(), different_match())]
    lock, committed = Lock(), []
    def transaction(value):
        with lock:
            if not committed or store.can_replace(committed[0], value, NOW):
                committed[:] = [value]
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(transaction, [a, b]))
    assert committed[0]['metadata']['snapshot_id'] == max(a['metadata']['snapshot_id'], b['metadata']['snapshot_id'])


def test_stale_audit_is_rejected_by_snapshot_predicate():
    a, b = [store.snapshot(article(), r) for r in (decision(), different_match())]
    assert a['metadata']['snapshot_id'] != b['metadata']['snapshot_id']
    engine = MagicMock()
    engine.begin.return_value.__enter__.return_value.execute.return_value.rowcount = 0
    assert store.save_integration_audit(engine, article(), decision(), {'status': 'indexed'}) is False


class TransactionModel:
    """Serial row-lock model, NOT an emulator/proof of PostgreSQL execution."""
    def __init__(self):
        self.lock, self.row = Lock(), None

    @contextmanager
    def begin(self):
        with self.lock:
            before = deepcopy(self.row)
            try:
                yield self
            except Exception:
                self.row = before
                raise

    def execute(self, stmt, params=None):
        result = MagicMock()
        result.scalar_one_or_none.return_value = None
        if getattr(stmt, 'is_select', False):
            result.mappings.return_value.first.return_value = deepcopy(self.row)
        elif getattr(stmt, 'is_insert', False):
            values = deepcopy(stmt.compile(dialect=postgresql.dialect()).params)
            # INSERT parameters precede expression-only conflict parameters.
            values = {k: values[k] for k in store.table.c.keys() if k in values}
            if self.row is None or store.can_replace(self.row, values, NOW):
                self.row = values
                result.scalar_one_or_none.return_value = 1
        elif getattr(stmt, 'is_update', False):
            bound = stmt.compile(dialect=postgresql.dialect()).params
            objects = [v for v in bound.values() if isinstance(v, dict)]
            conflict = next((v for v in objects if 'snapshot_conflicts' in v), None)
            if conflict:
                self.row['metadata'].update(conflict)
                result.rowcount = 1
            else:
                identity = self.row['metadata']['snapshot_id']
                matches = identity in [v for v in bound.values() if isinstance(v, str)]
                result.rowcount = int(matches)
                if matches:
                    entries = next(v for v in objects if any(isinstance(e, dict) and 'snapshot_id' in e for e in v.values()))
                    self.row['metadata'].setdefault('integration_audits', {}).update(entries)
        return result


@pytest.mark.parametrize('status', ['error', 'not_found'])
def test_two_transactions_keep_valid_match_against_later_negative(status):
    engine = TransactionModel()
    assert store.save_enrichment(engine, article(), decision())
    later = replace(decision(status), fetched_at=NOW + timedelta(seconds=1))
    assert not store.save_enrichment(engine, article(), later)
    assert engine.row['status'] == 'matched'
    assert len(engine.row['metadata']['snapshot_conflicts']) == 2


def test_two_transactions_reject_older_result_committing_later():
    engine = TransactionModel()
    newer = replace(different_match(), fetched_at=NOW + timedelta(seconds=1))
    store.save_enrichment(engine, article(), newer)
    assert not store.save_enrichment(engine, article(), decision())
    assert engine.row['doi'] == newer.selected_candidate['doi']


def test_two_threads_use_actual_save_function_with_deterministic_tie():
    engine = TransactionModel()
    results = [decision(), different_match()]
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda r: store.save_enrichment(engine, article(), r), results))
    expected = max(store.snapshot(article(), r)['metadata']['snapshot_id'] for r in results)
    assert engine.row['metadata']['snapshot_id'] == expected
    assert len(engine.row['metadata']['snapshot_conflicts']) == 2


def test_two_audits_for_different_snapshots_cannot_cross_attach():
    engine = TransactionModel()
    original, newer = decision(), replace(different_match(), fetched_at=NOW + timedelta(seconds=1))
    store.save_enrichment(engine, article(), original)
    store.save_enrichment(engine, article(), newer)
    assert not store.save_integration_audit(engine, article(), original, {'status': 'indexed', 'identity': 'old'})
    assert store.save_integration_audit(engine, article(), newer, {'status': 'unverified', 'identity': 'new'})
    audits = engine.row['metadata']['integration_audits']
    assert {v['snapshot_id'] for v in audits.values()} == {engine.row['metadata']['snapshot_id']}
    assert {v['observation']['identity'] for v in audits.values()} == {'new'}
