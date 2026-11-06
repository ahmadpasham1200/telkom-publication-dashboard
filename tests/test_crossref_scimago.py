"""Article identity and local serial/index identity stay separate."""
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import requests
import sqlalchemy as sa

from src.journal_index import crossref_scimago as bridge
from src.journal_index import scimagojr
from src.journal_index.crossref import CrossrefResult, parse_work
from src.journal_index.crossref_matcher import match_article
from src.journal_index import crossref_store as store


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def forbidden(*a, **k):
        raise AssertionError('No live I/O')
    monkeypatch.setattr(requests.Session, 'request', forbidden)
    monkeypatch.setattr(sa.engine.Engine, 'connect', forbidden)
    monkeypatch.setattr(sa.engine.Engine, 'raw_connection', forbidden)


def record(title='Journal of Testing', source='123', identifiers=None):
    return dict(title=title, source_id=source, issns=identifiers or ['0378-5955'], type='journal',
                sjr=1.2, best_quartile='Q2', h_index=42, categories='Testing', areas='Science')


def master(*records):
    return scimagojr.MasterExport(list(records) or [record()], Path('synthetic-master.csv'), 'utf-8', ';', 'fixture')


def matched(kind='journal-article', identifiers=None, title='Journal of Testing', rawid=1):
    row = dict(raw_publication_id=rawid, detail_title='Reliable metadata matching for scientific publications',
               detail_authors='Ana First, Budi Second', publication_year=2025, detail_journal=title)
    work = parse_work({'DOI': f'10.1234/example{rawid}', 'title': [row['detail_title']],
        'author': [{'given': 'Ana', 'family': 'First'}, {'given': 'Budi', 'family': 'Second'}],
        'published': {'date-parts': [[2025]]}, 'container-title': [title],
        'ISSN': identifiers or [], 'type': kind})
    result = match_article(row, CrossrefResult(status='ambiguous', lookup_method='bibliographic_search',
        reason='fixture', fetched_at=datetime(2026, 1, 1, tzinfo=timezone.utc), candidates=[work]))
    assert result.status == 'matched'
    return row, result


@pytest.mark.parametrize('identifier', ['0378-5955', '03785955', '0309-0566'])
def test_valid_serial_matching_preserves_provenance(identifier):
    row, result = matched(identifiers=[identifier])
    candidate, checked = bridge.match_serial(row, result, master(record(identifiers=['0378-5955', '0309-0566'])))
    assert checked['status'] == 'indexed' and checked['metadata']['match_method'] == 'issn'
    assert checked['metadata']['reference_year'] == 2025
    assert checked['sjr'][0]['quartile'] == 'Q2'
    assert checked['metadata']['crossref_provenance']['doi'] == result.selected_candidate['doi']
    assert candidate.publications[0]['scholar_original'] == row


@pytest.mark.parametrize('kind,expected', [('proceedings-article', 'excluded'), ('book-chapter', 'excluded'),
    ('book', 'excluded'), ('dissertation', 'excluded'), ('posted-content', 'excluded'),
    (None, 'unverified'), ('unknown-new-type', 'unverified')])
def test_only_journal_articles_receive_quartiles(kind, expected):
    row, result = matched(kind=kind, identifiers=['0378-5955'])
    candidate, checked = bridge.match_serial(row, result, master())
    assert candidate is None and checked['status'] == expected and checked['sjr'] == []


@pytest.mark.parametrize('status', ['ambiguous', 'not_found', 'error'])
def test_nonmatched_crossref_does_not_supply_journal_evidence(status):
    row, result = matched(identifiers=['0378-5955'])
    object.__setattr__(result, 'status', status)
    candidate, checked = bridge.match_serial(row, result, master())
    assert candidate is None and checked['status'] == 'unverified'
    assert checked['sjr'] == []


@pytest.mark.parametrize('identifiers,records,expected', [
    (['1234-5678'], [record()], 'unverified'),
    (['0378-5955'], [record(), record(source='456')], 'unverified'),
    (['0309-0566'], [record()], 'unverified'),
    (['0309-0566'], [record(title='A Completely Different Journal', identifiers=['0309-0566'])], 'unverified'),
    ([], [record()], 'indexed'),
    ([], [record(), record(source='456', identifiers=['0309-0566'])], 'unverified'),
])
def test_serial_conflicts_and_title_fallback(identifiers, records, expected):
    row, result = matched(identifiers=identifiers)
    _, checked = bridge.match_serial(row, result, master(*records))
    assert checked['status'] == expected
    if expected != 'indexed':
        assert checked['sjr'] == []


def test_fuzzy_is_only_unverified():
    row, result = matched(title='Journal of Testings')
    _, checked = bridge.match_serial(row, result, master())
    assert checked['status'] == 'unverified' and checked['metadata']['match_method'] == 'fuzzy_candidates'


def test_issn_absent_from_master_can_be_not_found_without_claim_about_crossref():
    row, result = matched(identifiers=['0309-0566'], title='Unknown Journal')
    _, checked = bridge.match_serial(row, result, master())
    assert checked['status'] == 'not_found' and result.status == 'matched'


def test_shared_scholar_venue_does_not_merge_different_serials():
    row1, result1 = matched(identifiers=['0378-5955'])
    row2, result2 = matched(identifiers=['0309-0566'], title='Another Journal', rawid=2)
    row1['venue'] = row2['venue'] = 'Same original Scholar venue'
    source = master(record(), record(title='Another Journal', source='456', identifiers=['0309-0566']))
    pairs = [bridge.match_serial(row1, result1, source), bridge.match_serial(row2, result2, source)]
    assert pairs[0][0].normalized != pairs[1][0].normalized
    assert [p[1]['metadata']['source_id'] for p in pairs] == ['123', '456']


def test_jisebi_partial_snapshot_and_title_matching_without_invented_issn():
    row, result = matched(title='Journal of Information Systems Engineering & Business Intelligence')
    row.update(detail_title='Aspect-based Sentiment and Correlation-based Emotion Detection on Tweets for Understanding Public Opinion of Covid-19',
               detail_authors='Salsabila Mazya Permataning Tyas; Yasinta Romadhona; Diana Purwitasari',
               publication_year=2023, detail_date='2023-04-01')
    work = deepcopy(result.selected_candidate)
    work.update(doi='10.20473/jisebi.9.1.84-94', titles=[row['detail_title']],
        authors=[{'given': 'Salsabila', 'family': 'Salsabila'},
                 {'given': 'Salsabila Mazya Permataning', 'family': 'Tyas'},
                 {'given': 'Yasinta', 'family': 'Romadhona'}, {'given': 'Diana', 'family': 'Purwitasari'}],
        publication_dates={'published': {'date_parts': [2023, 4, 28], 'precision': 'day'}},
        container_titles=['Journal of Information Systems Engineering and Business Intelligence'])
    result = match_article(row, CrossrefResult(status='ambiguous', lookup_method=result.lookup_method,
        reason='fixture', fetched_at=result.fetched_at, candidates=[work]))
    assert result.status == 'matched'
    values = store.snapshot(row, result)
    assert values['author_evidence']['status'] == 'partial' and values['issns'] == []
    # Synthetic master record for regression only, not a real ranking assertion.
    _, checked = bridge.match_serial(row, result, master(record(title=work['container_titles'][0])))
    assert checked['status'] == 'indexed' and checked['metadata']['match_method'] == 'title_exact'


def test_csnt_persists_as_article_but_never_gets_journal_rank():
    row, prior = matched(kind='proceedings-article')
    names = ['Ana First', 'Budi Second', 'Citra Third', 'Dedi Fourth',
             'Eka Fifth', 'Fajar Sixth', 'Gita Seventh']  # Synthetic: actual names not supplied.
    container = '2023 IEEE 12th International Conference on Communication Systems and Network Technologies (CSNT)'
    row.update(detail_title='Network behavior anomaly detection using decision tree',
        detail_doi='10.1109/csnt57126.2023.10134589', detail_journal=None, detail_venue=container,
        detail_publication_type='conference', publication_year=2023, detail_date='2023',
        detail_authors='; '.join(names))
    work = parse_work({'DOI': row['detail_doi'], 'title': [row['detail_title']],
        'author': [dict(given=n.split()[0], family=n.split()[1]) for n in names],
        'published': {'date-parts': [[2023]]}, 'container-title': [container], 'type': 'proceedings-article'})
    result = match_article(row, CrossrefResult(status='ambiguous', lookup_method=prior.lookup_method,
        reason='fixture', fetched_at=prior.fetched_at, candidates=[work]))
    assert result.status == 'matched' and result.evidence[0]['authors']['status'] == 'strong'
    assert store.snapshot(row, result)['publication_type'] == 'proceedings-article'
    candidate, checked = bridge.match_serial(row, result, master())
    assert candidate is None and checked['status'] == 'excluded' and checked['sjr'] == []


def test_scholar_nonjournal_type_cannot_be_overridden_by_crossref():
    row, result = matched()
    row['detail_publication_type'] = 'conference'
    _, checked = bridge.match_serial(row, result, master())
    assert checked['status'] == 'unverified'


def test_multiple_container_identities_require_review():
    row, result = matched(identifiers=['0378-5955'])
    result.selected_candidate['container_titles'].append('Another Journal')
    _, checked = bridge.match_serial(row, result, master())
    assert checked['status'] == 'unverified' and checked['sjr'] == []


def test_save_preserves_existing_verification_and_scopes_source(monkeypatch):
    from src.journal_index import run_journal_index as runner
    from sqlalchemy.dialects import postgresql
    row, result = matched(identifiers=['0378-5955'])
    candidate, checked = bridge.match_serial(row, result, master())
    engine, conn = MagicMock(), MagicMock()
    engine.begin.return_value.__enter__.return_value = conn
    conn.execute.side_effect = [MagicMock(), MagicMock(mappings=lambda: []),
                               MagicMock(scalar_one_or_none=lambda: None)]
    monkeypatch.setattr(runner, 'upsert_catalog', MagicMock(return_value=9))
    assert bridge.save_master_verification(engine, candidate, checked) == 'existing_verification_preserved'
    stmt = conn.execute.call_args.args[0]
    sql = str(stmt.compile(dialect=postgresql.dialect()))
    assert 'DO NOTHING' in sql and 'DO UPDATE' not in sql
    assert stmt.compile(dialect=postgresql.dialect()).params['source'] == 'scimagojr'


def test_catalog_alias_reuses_identity_without_renaming_original(monkeypatch):
    from src.journal_index import run_journal_index as runner
    row, result = matched(identifiers=['0378-5955'])
    candidate, checked = bridge.match_serial(row, result, master())
    prior = dict(journal_id=9, journal_normalized='old journal alias', journal_original='Old Journal Alias',
                 classification='journal', identifiers={'0378-5955': True})
    engine, conn = MagicMock(), MagicMock()
    engine.begin.return_value.__enter__.return_value = conn
    conn.execute.side_effect = [MagicMock(), MagicMock(mappings=lambda: [prior]),
        MagicMock(mappings=lambda: MagicMock(first=lambda: None)), MagicMock(scalar_one_or_none=lambda: 8)]
    upsert = MagicMock(return_value=9)
    monkeypatch.setattr(runner, 'upsert_catalog', upsert)
    assert bridge.save_master_verification(engine, candidate, checked) == 'saved'
    assert upsert.call_args.args[1].normalized == 'old journal alias'
    assert candidate.normalized == 'journal of testing'


@pytest.mark.parametrize('prior,expected', [
    ([dict(journal_id=9, identifiers={'0309-0566': True}, classification='journal')], 'catalog_conflict_serial_identifiers'),
    ([dict(journal_id=9), dict(journal_id=10)], 'catalog_conflict_multiple_identities')])
def test_conflicting_catalogue_rows_never_upsert(prior, expected, monkeypatch):
    from src.journal_index import run_journal_index as runner
    row, result = matched(identifiers=['0378-5955'])
    candidate, checked = bridge.match_serial(row, result, master())
    engine, conn = MagicMock(), MagicMock()
    engine.begin.return_value.__enter__.return_value = conn
    conn.execute.side_effect = [MagicMock(), MagicMock(mappings=lambda: prior)]
    upsert = MagicMock()
    monkeypatch.setattr(runner, 'upsert_catalog', upsert)
    assert bridge.save_master_verification(engine, candidate, checked) == expected
    upsert.assert_not_called()


def test_unverified_never_writes():
    row, result = matched()
    candidate, checked = bridge.match_serial(row, result, master(record(title='Unrelated Journal')))
    engine = MagicMock()
    assert bridge.save_master_verification(engine, candidate, checked) == 'not_saved_unverified'
    engine.begin.assert_not_called()


def test_save_master_failure_rolls_back(monkeypatch):
    from src.journal_index import run_journal_index as runner
    row, result = matched()
    candidate, checked = bridge.match_serial(row, result, master())
    engine, conn = MagicMock(), MagicMock()
    engine.begin.return_value.__enter__.return_value = conn
    conn.execute.side_effect = [MagicMock(), MagicMock(mappings=lambda: []), RuntimeError('write failed')]
    monkeypatch.setattr(runner, 'upsert_catalog', MagicMock(return_value=9))
    with pytest.raises(RuntimeError):
        bridge.save_master_verification(engine, candidate, checked)
    assert engine.begin.return_value.__exit__.call_args.args[0] is RuntimeError


def test_atomic_catalogue_update_rejects_concurrent_conflicting_writer():
    from src.journal_index import run_journal_index as runner
    from sqlalchemy.dialects import postgresql
    row, result = matched(identifiers=['0378-5955'])
    candidate, checked = bridge.match_serial(row, result, master())
    conn = MagicMock()
    conn.execute.side_effect = [MagicMock(mappings=lambda: MagicMock(first=lambda: None)),
                               MagicMock(scalar_one_or_none=lambda: None)]
    assert runner.upsert_catalog(conn, candidate, {'identifiers': ['0378-5955']}, strict_identifiers=True) is None
    sql = str(conn.execute.call_args.args[0].compile(dialect=postgresql.dialect()))
    assert ' WHERE ' in sql and '<@' in sql


def test_conjunction_aliases_with_distinct_serials_are_not_title_proof():
    row, result = matched(title='Journal of Testing & Science')
    records = [record(title='Journal of Testing & Science'),
               record(title='Journal of Testing and Science', source='456', identifiers=['0309-0566'])]
    _, checked = bridge.match_serial(row, result, master(*records))
    assert checked['status'] == 'unverified' and checked['sjr'] == []


def test_catalogue_origin_keeps_scholar_fields_at_original_keys():
    row, result = matched(title='Journal of Testing & Science')
    row['detail_publication_type'] = 'unknown'
    candidate, checked = bridge.match_serial(row, result,
        master(record(title='Journal of Testing and Science')))
    assert checked['status'] == 'indexed'
    assert candidate.publications[0]['detail_journal'] == row['detail_journal']
    assert candidate.publications[0]['detail_publication_type'] == 'unknown'
    assert candidate.original == 'Journal of Testing and Science'
