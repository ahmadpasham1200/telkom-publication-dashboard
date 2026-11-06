"""Offline article identity decisions, using the existing Crossref parser."""
from copy import deepcopy
from datetime import datetime, timezone
import json

import pytest
import requests
import sqlalchemy as sa

from src.journal_index.crossref import CrossrefResult, parse_work
from src.journal_index.crossref_matcher import ArticleMatchResult, match_article, normalize_article_title


@pytest.fixture(autouse=True)
def forbid_io(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('Matcher must never use HTTP or a database')
    monkeypatch.setattr(requests.Session, 'request', forbidden)
    monkeypatch.setattr(requests.adapters.HTTPAdapter, 'send', forbidden)
    monkeypatch.setattr(sa.engine.Engine, 'connect', forbidden)
    monkeypatch.setattr(sa.engine.Engine, 'raw_connection', forbidden)


@pytest.fixture
def scholar():
    return {'raw_publication_id': 7,
            'detail_title': 'Reliable metadata matching for scientific publications',
            'title': 'Fallback should not be used',
            'detail_authors': 'Ana First, Budi Second',
            'publication_year': 2025, 'year': '2025', 'detail_date': '2025',
            'detail_journal': 'Journal of Examples', 'detail_publication_type': 'journal'}


def work(title='Reliable metadata matching for scientific publications', *, doi='10.1234/correct',
         authors=None, year=2025, container='Journal of Examples', score=1, **fields):
    if authors is None:
        authors = [{'given': 'Ana', 'family': 'First'}, {'given': 'Budi', 'family': 'Second'}]
    item = {'DOI': doi, 'title': [title], 'author': authors,
            'published': {'date-parts': [[year]]}, 'container-title': [container] if container else [],
            'type': 'journal-article', 'score': score}
    item.update(fields)
    return parse_work(item, search_result=True)


def retrieval(*works, status=None):
    return CrossrefResult(status=status or ('ambiguous' if works else 'not_found'),
        lookup_method='bibliographic_search', fetched_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        http_status=200, attempts=1, reason='fixture', candidates=list(works))


@pytest.fixture
def jisebi():
    """Metadata supplied by the user; no API retrieval performed by this fixture."""
    title = ('Aspect-based Sentiment and Correlation-based Emotion Detection on Tweets '
             'for Understanding Public Opinion of Covid-19')
    names = ['Salsabila Mazya Permataning Tyas', 'Yasinta Romadhona', 'Diana Purwitasari']
    row = dict(raw_publication_id=101, detail_title=title, publication_year=2023,
               detail_date='2023-04-01', detail_publication_type='journal',
               detail_journal='Journal of Information Systems Engineering & Business Intelligence',
               publication_authors=[dict(author_order=i, author_name=n, daftar_terpotong=False)
                                    for i, n in enumerate(names)])
    candidate = parse_work({'DOI': '10.20473/jisebi.9.1.84-94', 'title': [title],
        'author': [{'given': 'Salsabila', 'family': 'Salsabila'},
                   {'given': 'Salsabila Mazya Permataning', 'family': 'Tyas'},
                   {'given': 'Yasinta', 'family': 'Romadhona'},
                   {'given': 'Diana', 'family': 'Purwitasari'}],
        'published': {'date-parts': [[2023, 4, 28]]},
        'container-title': ['Journal of Information Systems Engineering and Business Intelligence'],
        'type': 'journal-article'})
    return row, candidate


@pytest.fixture
def csnt():
    # Author names were not supplied for CSNT: use seven explicitly synthetic
    # ordered names, retaining the supplied title/DOI/year/container/type.
    names = ['Ana First', 'Budi Second', 'Citra Third', 'Dedi Fourth',
             'Eka Fifth', 'Fajar Sixth', 'Gita Seventh']
    title = 'Network behavior anomaly detection using decision tree'
    container = '2023 IEEE 12th International Conference on Communication Systems and Network Technologies (CSNT)'
    row = dict(raw_publication_id=102, detail_title=title,
               detail_doi='10.1109/csnt57126.2023.10134589', publication_year=2023,
               detail_date='2023', detail_publication_type='conference', detail_venue=container,
               publication_authors=[dict(author_order=i, author_name=n) for i, n in enumerate(names)])
    candidate = parse_work({'DOI': row['detail_doi'], 'title': [title],
        'author': [dict(given=n.split()[0], family=n.split()[1]) for n in names],
        'published': {'date-parts': [[2023]]}, 'container-title': [container], 'type': 'proceedings-article'})
    return row, candidate


def test_real_jisebi_matches_with_partial_ordered_author_evidence(jisebi):
    row, candidate = jisebi
    before = deepcopy((row, candidate))
    result = match_article(row, retrieval(candidate))
    e = result.evidence[0]
    assert result.status == 'matched'
    assert e['title']['similarity'] == 1.0
    assert e['year']['status'] == 'consistent' and e['container']['status'] == 'match'
    assert e['authors']['status'] == 'partial'
    assert e['authors']['relative_order_consistent'] is True
    assert [p['crossref_index'] for p in e['authors']['pairs']] == [1, 2, 3]
    assert e['conflicts'] == result.selected_conflicts == []
    assert len(result.selected_candidate['authors']) == 4
    assert (row, candidate) == before


def test_real_csnt_remains_matched_conference_not_journal(csnt):
    from src.journal_index.init import classify_scholar_publication
    row, candidate = csnt
    result = match_article(row, retrieval(candidate))
    assert result.status == 'matched'
    assert result.evidence[0]['authors']['status'] == 'strong'
    assert result.evidence[0]['year']['status'] == 'consistent'
    assert result.evidence[0]['container']['status'] == 'match'
    assert result.selected_candidate['type'] == 'proceedings-article'
    assert classify_scholar_publication(row)[0] == 'non_journal'


@pytest.mark.parametrize('scholar_date,parts', [
    ('2025-04-01', [2025, 4, 28]), ('2025-01-01', [2025, 12, 28]),
    ('2025-04', [2025, 12]), ('2025', [2025]), (None, [2025]),
])
def test_same_publication_year_ignores_day_month_variations(scholar, scholar_date, parts):
    scholar['detail_date'] = scholar_date
    candidate = work(published={'date-parts': [parts]})
    result = match_article(scholar, retrieval(candidate))
    assert result.status == 'matched' and result.evidence[0]['year']['status'] == 'consistent'
    assert 'year_conflict' not in result.evidence[0]['conflicts']
    assert result.evidence[0]['year']['crossref_dates']['published'] == parts


def test_one_year_difference_is_explicit_conflict_without_support(scholar):
    result = match_article(scholar, retrieval(work(year=2024)))
    assert result.status == 'ambiguous'
    assert 'year_conflict' in result.evidence[0]['conflicts']


def test_alternate_crossref_date_supports_scholar_year(scholar):
    result = match_article(scholar, retrieval(work(year=2024,
        **{'published-online': {'date-parts': [[2025, 1]]}})))
    assert result.status == 'matched'
    assert result.evidence[0]['year']['supporting_date_sources'] == ['published-online']


def test_missing_crossref_dates_are_unknown_not_conflict(scholar):
    candidate = work()
    candidate['publication_dates'] = {}
    result = match_article(scholar, retrieval(candidate))
    assert result.status == 'ambiguous' and result.evidence[0]['year']['status'] == 'missing'
    assert 'year_conflict' not in result.evidence[0]['conflicts']


@pytest.mark.parametrize('container,expected', [
    ('JOURNAL OF TESTING & SCIENCE', 'match'), ('Journal of Testing and Science.', 'match'),
    ('  Journal  of Testing, and Science ', 'match'),
    ('Journal of Testing or Science', 'conflict'), ('Journal of Testing and Social Science', 'conflict'),
])
def test_container_conjunction_variants_remain_conservative(scholar, container, expected):
    scholar['detail_journal'] = 'Journal of Testing & Science'
    result = match_article(scholar, retrieval(work(container=container)))
    evidence = result.evidence[0]['container']
    assert evidence['status'] == expected
    assert evidence['scholar_container'] == scholar['detail_journal']
    assert evidence['crossref_containers'] == [container]


@pytest.mark.parametrize('mutation', ['order', 'missing', 'truncated', 'initials', 'mononym',
                                      'similar_title', 'missing_container', 'wrong_container', 'wrong_year'])
def test_partial_author_matching_does_not_relax_other_requirements(jisebi, mutation):
    row, candidate = jisebi
    if mutation == 'order':
        candidate['authors'][2:] = reversed(candidate['authors'][2:])
    elif mutation == 'missing':
        candidate['authors'].pop()
    elif mutation == 'truncated':
        row['publication_authors'][0]['daftar_terpotong'] = True
    elif mutation == 'initials':
        row['publication_authors'][1]['author_name'] = 'Y. Romadhona'
    elif mutation == 'mononym':
        row['publication_authors'][1]['author_name'] = 'Yasinta'
    elif mutation == 'similar_title':
        candidate['titles'][0] += 's'
    elif mutation == 'missing_container':
        row['detail_journal'] = None
    elif mutation == 'wrong_container':
        candidate['container_titles'] = ['Completely Different Journal']
    else:
        candidate['publication_dates']['published']['date_parts'][0] = 2022
    assert match_article(row, retrieval(candidate)).status == 'ambiguous'


def test_partial_author_match_still_checks_competing_identity(jisebi):
    row, candidate = jisebi
    competitor = deepcopy(candidate)
    competitor['doi'] = '10.1234/competing'
    result = match_article(row, retrieval(candidate, competitor))
    assert result.status == 'ambiguous' and result.reason == 'competing_candidates'


def test_selected_and_rejected_conflicts_are_separate(scholar):
    rejected = work(doi='10.1234/rejected', year=2010)
    result = match_article(scholar, retrieval(rejected, work()))
    assert result.status == 'matched' and result.candidates_evaluated == 2
    assert result.selected_candidate_index == 1 and result.selected_conflicts == []
    assert result.candidate_decisions[0]['reason'] == 'metadata_conflict'
    assert 'year_conflict' in result.candidate_decisions[0]['conflicts']
    assert result.candidate_decisions[1]['selection'] == 'selected'
    assert json.loads(json.dumps(result.to_dict()))['candidate_decisions']


def test_exact_match_has_complete_diagnostics(scholar):
    result = match_article(scholar, retrieval(work()))
    assert isinstance(result, CrossrefResult) and isinstance(result, ArticleMatchResult)
    assert result.status == 'matched' and result.selected_candidate['doi'] == '10.1234/correct'
    assert result.candidates_evaluated == 1 and result.reason
    evidence = result.evidence[0]
    assert evidence['title']['exact'] and evidence['title']['similarity'] == 1
    assert evidence['authors']['status'] == 'strong'
    assert evidence['year']['status'] == 'consistent'
    assert evidence['container']['status'] == 'match'
    assert evidence['conflicts'] == []


@pytest.mark.parametrize('title', [
    'RELIABLE METADATA MATCHING FOR SCIENTIFIC PUBLICATIONS',
    'Reliable metadata matching: for scientific publications!',
    'Ｒｅｌｉａｂｌｅ metadata matching for scientific publications',
])
def test_cosmetic_title_variants(scholar, title):
    assert match_article(scholar, retrieval(work(title))).status == 'matched'


@pytest.mark.parametrize('left,right', [
    ('Étude de la qualité des données scientifiques', 'E\u0301tude de la qualité des données scientifiques'),
    ('中文学术文章元数据匹配研究', '中文学术文章元数据匹配研究'),
    ('تحليل البيانات العلمية وتطابق المقالات', 'تحليل البيانات العلمية وتطابق المقالات'),
    ('Исследование качества научных метаданных', 'Исследование качества научных метаданных'),
])
def test_unicode_multilingual_titles(scholar, left, right):
    scholar['detail_title'] = left
    assert match_article(scholar, retrieval(work(right))).status == 'matched'


@pytest.mark.parametrize('left,right', [
    ('Reliable scientific prediction using 10 samples', 'Reliable scientific prediction using 20 samples'),
    ('Reliable scientific prediction for x+y models', 'Reliable scientific prediction for x-y models'),
    ('Reliable scientific prediction for H2O reactions', 'Reliable scientific prediction for CO2 reactions'),
    ('Reliable scientific prediction for α models', 'Reliable scientific prediction for β models'),
    ('Reliable scientific prediction for x² models', 'Reliable scientific prediction for x2 models'),
    ('Reliable scientific prediction for 1.5 concentrations', 'Reliable scientific prediction for 15 concentrations'),
])
def test_meaningful_numbers_and_scientific_symbols_block_matching(scholar, left, right):
    scholar['detail_title'] = left
    result = match_article(scholar, retrieval(work(right)))
    assert result.status != 'matched'
    assert result.evidence[0]['title']['protected_conflict']
    assert normalize_article_title(left) != normalize_article_title(right)


def test_similar_title_requires_other_evidence_and_calibratable_threshold(scholar):
    candidate = work('Reliable metadata matching for scientific publication')
    assert match_article(scholar, retrieval(candidate)).status == 'matched'
    assert match_article(scholar, retrieval(candidate), title_threshold=1.0).status == 'ambiguous'
    candidate['authors'] = []
    assert match_article(scholar, retrieval(candidate)).status == 'ambiguous'


@pytest.mark.parametrize('title', ['Study', 'Introduction', 'A study', 'Editorial'])
def test_short_or_generic_titles_require_review(scholar, title):
    scholar['detail_title'] = title
    result = match_article(scholar, retrieval(work(title)))
    assert result.status == 'ambiguous' and not result.evidence[0]['title']['informative']


def test_clearly_unrelated_title_is_not_found(scholar):
    result = match_article(scholar, retrieval(work('Marine coral ecology and seasonal ocean currents')))
    assert result.status == 'not_found' and result.selected_candidate is None


def test_author_capitalization_and_relation_order_priority(scholar):
    relations = [{'author_order': 1, 'author_name': 'BUDI SECOND'},
                 {'author_order': 0, 'author_name': ' ana first '}]
    scholar['detail_authors'] = 'Incorrect Person'
    result = match_article(scholar, retrieval(work()), raw_authors=relations)
    assert result.status == 'matched'
    assert result.evidence[0]['authors']['source'] == 'raw_relation'
    assert result.evidence[0]['authors']['scholar_names'] == ['ana first', 'BUDI SECOND']


def test_inverted_names_in_relations_are_supported(scholar):
    relations = [{'author_order': 0, 'author_name': 'First, Ana'},
                 {'author_order': 1, 'author_name': 'Second, Budi'}]
    assert match_article(scholar, retrieval(work()), raw_authors=relations).status == 'matched'


def test_initials_are_not_expanded_or_sufficient_alone(scholar):
    scholar['detail_authors'] = 'A. First'
    result = match_article(scholar, retrieval(work(authors=[{'given': 'Ana', 'family': 'First'}])))
    assert result.status == 'ambiguous'
    assert result.evidence[0]['authors']['pairs'][0]['kind'] == 'initials'
    assert result.evidence[0]['authors']['scholar_names'] == ['A. First']


def test_initials_with_independent_full_coauthor_support(scholar):
    scholar['detail_authors'] = 'A. First, Budi Second'
    assert match_article(scholar, retrieval(work())).status == 'matched'


def test_same_surname_different_people_conflict(scholar):
    candidate = work(authors=[{'given': 'Anne', 'family': 'First'}, {'given': 'Brian', 'family': 'Second'}])
    result = match_article(scholar, retrieval(candidate))
    assert result.status == 'ambiguous'
    assert result.evidence[0]['authors']['status'] == 'conflict'
    assert 'authors_conflict' in result.evidence[0]['conflicts']


@pytest.mark.parametrize('side', ['scholar', 'crossref'])
def test_missing_authors_never_automatically_match(scholar, side):
    candidate = work()
    if side == 'scholar':
        scholar['detail_authors'] = None
    else:
        candidate['authors'] = []
    assert match_article(scholar, retrieval(candidate)).status == 'ambiguous'


@pytest.mark.parametrize('names', ['Ana First, ...', 'Ana First, …', 'Ana First et al.'])
def test_truncated_scholar_authors_are_partial_not_definite_conflict(scholar, names):
    scholar['detail_authors'] = names
    result = match_article(scholar, retrieval(work()))
    evidence = result.evidence[0]['authors']
    assert result.status == 'ambiguous' and evidence['truncated']
    assert evidence['status'] == 'partial'
    assert 'authors_conflict' not in result.evidence[0]['conflicts']


def test_different_author_order_is_diagnosed(scholar):
    candidate = work(authors=[{'given': 'Budi', 'family': 'Second'}, {'given': 'Ana', 'family': 'First'}])
    result = match_article(scholar, retrieval(candidate))
    assert result.status == 'ambiguous'
    assert result.evidence[0]['authors']['order_consistent'] is False


def test_single_mononym_is_weak_evidence(scholar):
    scholar['detail_authors'] = 'Sukarno'
    result = match_article(scholar, retrieval(work(authors=[{'family': 'Sukarno'}])))
    assert result.status == 'ambiguous'


def test_accented_author_names_preserve_identity(scholar):
    scholar['detail_authors'] = 'Jose\u0301 García, Budi Second'
    candidate = work(authors=[{'given': 'José', 'family': 'García'}, {'given': 'Budi', 'family': 'Second'}])
    assert match_article(scholar, retrieval(candidate)).status == 'matched'


def test_year_one_apart_without_online_print_evidence_is_ambiguous(scholar):
    result = match_article(scholar, retrieval(work(year=2024)))
    assert result.status == 'ambiguous'
    assert result.evidence[0]['year']['status'] == 'one_year_difference'


def test_online_print_dates_can_prove_one_year_transition(scholar):
    scholar.update(publication_year=2024, year='2024', detail_date='2024/12/20')
    candidate = work(**{'published-online': {'date-parts': [[2024, 12, 20]]},
                        'published-print': {'date-parts': [[2025, 2, 28]]}})
    result = match_article(scholar, retrieval(candidate))
    assert result.status == 'matched'
    assert result.evidence[0]['year']['status'] == 'online_print_consistent'


def test_far_year_is_strong_conflict(scholar):
    result = match_article(scholar, retrieval(work(year=2020)))
    assert result.status == 'ambiguous' and 'year_conflict' in result.evidence[0]['conflicts']


def test_year_fallback_and_missing_year(scholar):
    scholar['publication_year'] = None
    assert match_article(scholar, retrieval(work())).status == 'matched'
    scholar.update(year=None, detail_date=None)
    assert match_article(scholar, retrieval(work())).status == 'ambiguous'


def test_detail_date_contradiction_is_not_ignored(scholar):
    scholar['detail_date'] = '2020/1/1'
    result = match_article(scholar, retrieval(work()))
    assert result.status == 'ambiguous' and 'year_conflict' in result.evidence[0]['conflicts']


@pytest.mark.parametrize('field,value', [
    ('detail_doi', 123), ('detail_doi', 'not-a-doi'),
    ('detail_date', '2020/99/99'), ('detail_date', 2025),
    ('detail_title', 123), ('detail_authors', 42),
    ('detail_journal', {'not': 'a venue'}),
])
def test_populated_malformed_scholar_metadata_cannot_disappear(scholar, field, value):
    scholar[field] = value
    result = match_article(scholar, retrieval(work()))
    assert result.status == 'ambiguous' and result.input_conflicts


@pytest.mark.parametrize('authors', [[{'given': 'Ana First'}], [{'family': 'Ana First', 'name': 42}]])
def test_incomplete_or_invalid_author_fields_cannot_prove_full_identity(scholar, authors):
    scholar['detail_authors'] = 'Ana First'
    assert match_article(scholar, retrieval(work(authors=authors))).status == 'ambiguous'


def test_clear_container_conflict_blocks_match(scholar):
    result = match_article(scholar, retrieval(work(container='Another Completely Different Journal')))
    assert result.status == 'ambiguous'
    assert 'container_conflict' in result.evidence[0]['conflicts']


def test_missing_scholar_container_is_not_forced(scholar):
    scholar.pop('detail_journal')
    result = match_article(scholar, retrieval(work()))
    assert result.status == 'matched' and result.evidence[0]['container']['status'] == 'missing'


def test_profile_bibliography_is_not_forced_to_be_a_container(scholar):
    scholar.pop('detail_journal')
    scholar['venue'] = 'Journal of Examples 5 (2), 23-32'
    result = match_article(scholar, retrieval(work()))
    assert result.status == 'matched' and result.evidence[0]['container']['status'] == 'insufficient'


@pytest.mark.parametrize('container', ['-', 'N/A', 'not available'])
def test_container_placeholders_are_not_conflicting_journal_names(scholar, container):
    scholar['detail_journal'] = container
    result = match_article(scholar, retrieval(work()))
    assert result.status == 'matched' and result.evidence[0]['container']['status'] == 'insufficient'


def test_empty_crossref_container_string_is_missing_evidence(scholar):
    candidate = work()
    candidate['container_titles'] = ['']
    assert match_article(scholar, retrieval(candidate)).status == 'matched'


def test_generic_scholar_title_is_insufficient_even_with_unrelated_results(scholar):
    scholar['detail_title'] = 'Introduction'
    assert match_article(scholar, retrieval(work())).status == 'ambiguous'


def test_candidate_missing_title_with_consistent_other_metadata_is_uncertain(scholar):
    partial = work(doi='10.1234/partial')
    partial['titles'] = []
    assert match_article(scholar, retrieval(partial)).status == 'ambiguous'
    assert match_article(scholar, retrieval(work(), partial)).status == 'ambiguous'


def test_all_five_candidates_evaluated_and_wrong_first_is_ignored(scholar):
    wrong = [work('Marine coral ecology and seasonal ocean currents', doi=f'10.1234/wrong{i}', score=1000) for i in range(4)]
    result = match_article(scholar, retrieval(*wrong, work(score=1)))
    assert result.status == 'matched' and result.candidates_evaluated == 5
    assert len(result.evidence) == 5 and result.selected_candidate['doi'] == '10.1234/correct'


def test_equal_competing_identities_are_ambiguous(scholar):
    result = match_article(scholar, retrieval(work(doi='10.1234/one'), work(doi='10.1234/two')))
    assert result.status == 'ambiguous' and result.selected_candidate is None
    assert result.reason == 'competing_candidates'


def test_conflicting_candidate_cannot_defeat_consistent_candidate(scholar):
    wrong = work(doi='10.1234/wrong', authors=[{'given': 'Other', 'family': 'Person'}], score=99999)
    assert match_article(scholar, retrieval(wrong, work())).selected_candidate['doi'] == '10.1234/correct'


def test_missing_competitor_evidence_is_not_negative_proof(scholar):
    weak = work(doi='10.1234/weak', authors=[])
    assert match_article(scholar, retrieval(work(), weak)).status == 'ambiguous'


def test_shorter_compatible_crossref_author_list_does_not_exonerate_winner(scholar):
    partial = work(doi='10.1234/partial', authors=[{'given': 'Ana', 'family': 'First'}])
    result = match_article(scholar, retrieval(work(), partial))
    assert result.status == 'ambiguous'
    assert result.evidence[1]['authors']['status'] == 'partial'
    assert 'authors_conflict' not in result.evidence[1]['conflicts']


def test_name_field_with_initials_is_not_full_identity(scholar):
    scholar['detail_authors'] = 'A. Smith'
    result = match_article(scholar, retrieval(work(authors=[{'name': 'A. Smith'}])))
    assert result.status == 'ambiguous'
    assert result.evidence[0]['authors']['pairs'][0]['kind'] == 'initials'


def test_malformed_raw_author_relation_does_not_silently_shorten_list(scholar):
    records = [{'author_order': 0, 'author_name': 'Ana First'}, {'author_order': 1, 'author_name': 42}]
    result = match_article(scholar, retrieval(work(authors=[{'given': 'Ana', 'family': 'First'}])), raw_authors=records)
    assert result.status == 'ambiguous'
    assert result.evidence[0]['authors']['invalid_records']


@pytest.mark.parametrize('left,right', [
    ('Reliable scientific prediction for x′ dynamics', 'Reliable scientific prediction for x dynamics'),
    ('Reliable scientific prediction for x″ dynamics', 'Reliable scientific prediction for x dynamics'),
    ("Reliable scientific prediction for x' dynamics", 'Reliable scientific prediction for x dynamics'),
    ('Reliable scientific prediction for n! combinations', 'Reliable scientific prediction for n combinations'),
    ('Reliable scientific prediction for a·i models', 'Reliable scientific prediction for a i models'),
    ('Reliable catalytic behavior of Co complexes', 'Reliable catalytic behavior of CO complexes'),
    ('Reliable catalytic behavior of NaCl complexes', 'Reliable catalytic behavior of KCl complexes'),
    ('Reliable scientific prediction for xᵃ models', 'Reliable scientific prediction for xa models'),
    ('Reliable scientific prediction for xₐ models', 'Reliable scientific prediction for xa models'),
    ('Reliable scientific prediction for 𝐴 models', 'Reliable scientific prediction for A models'),
])
def test_scientific_notation_is_not_cosmetic_punctuation_or_case(scholar, left, right):
    scholar['detail_title'] = left
    result = match_article(scholar, retrieval(work(right)))
    assert result.status != 'matched'
    assert result.evidence[0]['title']['protected_conflict']


def test_equivalent_unicode_minus_keeps_its_operator_meaning(scholar):
    scholar['detail_title'] = 'Reliable scientific prediction for x−y models'
    assert match_article(scholar, retrieval(work('Reliable scientific prediction for x-y models'))).status == 'matched'


def test_malformed_competitor_is_not_evidence_against_its_identity(scholar):
    malformed = work(doi='10.1234/malformed')
    malformed['authors'] = 42
    assert match_article(scholar, retrieval(work(), malformed)).status == 'ambiguous'


def test_ellipsis_raw_relation_is_partial_even_without_flag(scholar):
    records = [{'author_order': 0, 'author_name': 'Ana First'}, {'author_order': 1, 'author_name': '…'}]
    result = match_article(scholar, retrieval(work(authors=[{'given': 'Ana', 'family': 'First'}])), raw_authors=records)
    assert result.status == 'ambiguous' and result.evidence[0]['authors']['truncated']


@pytest.mark.parametrize('authors', [[{}], [{'given': 'Ana', 'family': 'First'}, {}],
                                  [{'family': 'First'}, {'family': 'Second'}]])
def test_incomplete_crossref_author_records_are_not_definite_conflicts(scholar, authors):
    partial = work(doi='10.1234/partial', authors=authors)
    result = match_article(scholar, retrieval(work(), partial))
    assert result.status == 'ambiguous'
    assert 'authors_conflict' not in result.evidence[1]['conflicts']


def test_relation_with_only_ellipsis_does_not_fall_back_to_complete_authors(scholar):
    result = match_article(scholar, retrieval(work()),
                           raw_authors=[{'author_order': 0, 'author_name': '…'}])
    assert result.status == 'ambiguous'


def test_all_candidate_title_variants_are_considered(scholar):
    candidate = work()
    candidate['titles'].insert(0, 'A completely different translation of the article')
    assert match_article(scholar, retrieval(candidate)).status == 'matched'


def test_conflicting_duplicate_doi_metadata_requires_review(scholar):
    duplicate = work(authors=[{'given': 'Different', 'family': 'Person'}])
    assert match_article(scholar, retrieval(work(), duplicate)).status == 'ambiguous'


def test_candidate_order_does_not_choose_a_different_identity(scholar):
    wrong = work(doi='10.1234/wrong', year=2010)
    for works in [(wrong, work()), (work(), wrong)]:
        result = match_article(scholar, retrieval(*works))
        assert result.status == 'matched' and result.selected_candidate['doi'] == '10.1234/correct'


def test_all_plausible_candidates_with_conflicts_stay_ambiguous(scholar):
    result = match_article(scholar, retrieval(work(year=2010), work(doi='10.1234/other', year=2011)))
    assert result.status == 'ambiguous'
    assert all(item['conflicts'] for item in result.evidence)


def test_empty_candidates_are_not_found(scholar):
    result = match_article(scholar, retrieval())
    assert result.status == 'not_found' and result.candidates_evaluated == 0


def test_same_doi_does_not_override_wrong_title(scholar):
    scholar['detail_doi'] = 'https://doi.org/10.1234/correct'
    result = match_article(scholar, retrieval(work('Marine coral ecology and seasonal ocean currents')))
    assert result.status == 'ambiguous' and not result.evidence[0]['eligible']
    assert 'title_conflict' in result.evidence[0]['conflicts']


def test_duplicate_doi_with_contradictory_title_is_a_metadata_conflict(scholar):
    wrong = work('Marine coral ecology and seasonal ocean currents')
    assert match_article(scholar, retrieval(work(), wrong)).status == 'ambiguous'


def test_different_doi_is_a_conflict_even_with_matching_title(scholar):
    scholar['detail_doi'] = '10.1234/scholar'
    result = match_article(scholar, retrieval(work()))
    assert result.status == 'ambiguous' and 'doi_conflict' in result.evidence[0]['conflicts']


def test_publication_type_conflict_is_diagnosed(scholar):
    result = match_article(scholar, retrieval(work(type='proceedings-article')))
    assert result.status == 'ambiguous' and 'type_conflict' in result.evidence[0]['conflicts']


def test_missing_title_is_ambiguous_and_fallback_is_used(scholar):
    scholar['detail_title'] = ''
    scholar['title'] = work()['titles'][0]
    assert match_article(scholar, retrieval(work())).status == 'matched'
    scholar['title'] = None
    result = match_article(scholar, retrieval(work()))
    assert result.status == 'ambiguous' and result.candidates_evaluated == 1


def test_transport_error_is_preserved_without_matching(scholar):
    result = match_article(scholar, retrieval(status='error'))
    assert result.status == 'error' and result.candidates_evaluated == 0


def test_result_is_deterministic_serializable_and_does_not_mutate_inputs(scholar):
    response = retrieval(work(), work('Marine coral ecology and seasonal ocean currents', doi='10.1234/other'))
    original = deepcopy((scholar, response))
    first = match_article(scholar, response)
    second = match_article(scholar, response)
    assert first.to_dict() == second.to_dict()
    json.dumps(first.to_dict(), allow_nan=False)
    assert (scholar, response) == original
    first.selected_candidate['authors'][0]['given'] = 'Changed'
    assert response == original[1]


def test_every_available_candidate_is_evaluated_without_a_new_cap(scholar):
    result = match_article(scholar, retrieval(*[work(doi=f'10.1234/{i}') for i in range(7)]))
    assert result.candidates_evaluated == 7 and len(result.evidence) == 7
    assert result.status == 'ambiguous'


def test_duplicate_identical_doi_records_do_not_create_false_competitors(scholar):
    assert match_article(scholar, retrieval(work(), work())).status == 'matched'


def test_reason_is_present_for_all_decisions(scholar):
    for response in [retrieval(work()), retrieval(), retrieval(work(authors=[])), retrieval(status='error')]:
        assert match_article(scholar, response).reason


@pytest.mark.parametrize('threshold', [0, 1.1, float('nan'), True])
def test_invalid_threshold_is_rejected(scholar, threshold):
    with pytest.raises(ValueError):
        match_article(scholar, retrieval(work()), title_threshold=threshold)
