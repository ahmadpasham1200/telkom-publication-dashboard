"""Local Scopus master contracts; synthetic Excel fixtures, no network, no real master."""
import hashlib
import socket
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from openpyxl import Workbook

from src.journal_index import scopus
from src.journal_index.init import Candidate, candidates, normalize_title

SHEET_COLUMNS = {
    scopus.SHEET_SOURCES: ['Sourcerecord ID', 'Source Title', 'ISSN', 'EISSN',
                           'Active or Inactive', 'Coverage', 'Titles Discontinued by Scopus',
                           'Source Type', 'Publisher',
                           'All Science Journal Classification Codes (ASJC)',
                           'Title History Indication', 'Related Title'],
    scopus.SHEET_ACCEPTED: ['Status: September 2026',
                            'Accepted titles in the process of being added to Scopus'],
    scopus.SHEET_DISCONTINUED: ['Sourcerecord ID', 'Source Title', 'ISSN', 'EISSN',
                                'Publisher', 'Indexation Change', 'Year', 'Volume', 'Issue',
                                'Page Range'],
    scopus.SHEET_SERIAL_CONF: ['Sourcerecord ID', 'Source Title', 'ISSN', 'EISSN',
                               'Titles Discontinued by Scopus', 'Coverage',
                               'All Science Journal Classification Codes (ASJC)'],
    # The real export misspells this header; fixtures keep the typo.
    scopus.SHEET_CONF_PROC: ['Source Title', 'Proceedinds Title', 'ISBN', 'Year', 'Volume',
                             'EID', 'Publisher', 'ASJC'],
}

DEFAULT_SOURCE = {'Sourcerecord ID': '123', 'Source Title': 'Journal of Testing',
                  'ISSN': '0378-5955', 'EISSN': '0309-0566', 'Active or Inactive': 'Active',
                  'Coverage': '1995-2026', 'Source Type': 'Journal', 'Publisher': 'Elsevier',
                  'All Science Journal Classification Codes (ASJC)': '2612'}

DEFAULT_ROWS = {scopus.SHEET_SOURCES: [DEFAULT_SOURCE]}


def write_master(tmp_path, rows=None, *, name='master.xlsx', messy=False, banners=()):
    """rows: mapping sheet name -> list of row dicts keyed by canonical header."""
    workbook = Workbook()
    workbook.remove(workbook.active)
    for sheet_name, columns in SHEET_COLUMNS.items():
        worksheet = workbook.create_sheet(sheet_name)
        if sheet_name in banners:
            worksheet.append(['Status (recently discontinued titles in this file)']
                             + [None] * (len(columns) - 1))
        headers = [header.replace(' ', '  \n') for header in columns] if messy else columns
        worksheet.append(headers)
        for row in (rows if rows is not None else DEFAULT_ROWS).get(sheet_name, []):
            worksheet.append([row.get(column) for column in columns])
    path = tmp_path / name
    workbook.save(path)
    return path


def candidate(title='Journal of Testing', kind='journal', **fields):
    return candidates([{'raw_publication_id': 1, 'title': 'Article must not be matched',
                        'detail_venue': title, 'detail_publication_type': kind, **fields}],
                      scholar_types=True)[0]


def proceedings_candidate(title='Proceedings of Testing', isbn='978-0-123456-78-9', year=2024):
    row = {'raw_publication_id': 1, 'title': 'Article must not be matched', 'detail_venue': title,
           'detail_publication_type': 'conference', 'publication_year': year, 'isbn': isbn}
    return Candidate(title, normalize_title(title), 'non_journal', 'fixture', publications=[row])


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr(socket.socket, 'connect',
                        MagicMock(side_effect=AssertionError('No network allowed')))


def test_default_master_path_structure():
    assert str(scopus.DEFAULT_MASTER).replace('\\', '/').endswith(
        'data/scopus/ext_list_Sep_2026.xlsx')
    assert scopus.DEFAULT_MASTER == Path(scopus.__file__).resolve().parents[2] / \
        'data/scopus/ext_list_Sep_2026.xlsx'


def test_reads_all_five_sheets_with_digest(tmp_path):
    path = write_master(tmp_path, rows={
        scopus.SHEET_SOURCES: [dict(DEFAULT_SOURCE)],
        scopus.SHEET_ACCEPTED: [{'Status: September 2026': 'Accepted',
                                 'Accepted titles in the process of being added to Scopus':
                                     'New Journal'}],
        scopus.SHEET_DISCONTINUED: [{'Sourcerecord ID': '777', 'Source Title': 'Old Journal',
                                     'ISSN': '0000-0051', 'EISSN': '', 'Publisher': 'Old Press',
                                     'Indexation Change': 'Discontinued', 'Year': '2020'}],
        scopus.SHEET_SERIAL_CONF: [{'Sourcerecord ID': '93910',
                                    'Source Title': 'Test Symposium Proceedings',
                                    'ISSN': '0378-5955', 'EISSN': '', 'Coverage': '2007-2015'}],
        scopus.SHEET_CONF_PROC: [{'Source Title': 'Test Conference',
                                  'Proceedinds Title': 'Test Conference Proceedings',
                                  'ISBN': '978-0-123456-78-9', 'Year': 2024, 'Volume': '7',
                                  'EID': '84935468465', 'Publisher': 'Conf Press'}]})
    master = scopus.load_master(path)
    assert set(master.sheets) == set(scopus.SHEET_ORDER)
    assert master.path == path.resolve()
    assert master.digest == hashlib.sha256(path.read_bytes()).hexdigest()
    source = master.sheets[scopus.SHEET_SOURCES][0]
    assert source['source_record_id'] == '123' and source['active'] is True
    assert source['coverage_years'] == frozenset(range(1995, 2027))
    assert master.sheets[scopus.SHEET_DISCONTINUED][0]['discontinued'] is True
    assert master.sheets[scopus.SHEET_SERIAL_CONF][0]['coverage_years'] == frozenset(range(2007, 2016))
    proceedings = master.sheets[scopus.SHEET_CONF_PROC][0]
    assert proceedings['year'] == 2024
    assert proceedings['isbns'] == frozenset({'9780123456789'})
    assert master.sheets[scopus.SHEET_ACCEPTED][0]['title'] == 'New Journal'


def test_messy_headers_and_banner_rows_still_load(tmp_path):
    path = write_master(tmp_path, messy=True, banners={scopus.SHEET_DISCONTINUED})
    master = scopus.load_master(path)
    assert master.sheets[scopus.SHEET_SOURCES][0]['title'] == 'Journal of Testing'
    assert master.sheets[scopus.SHEET_DISCONTINUED] == []
    checked = master.check(candidate(issn='0378-5955', publication_year=2020))
    assert checked['status'] == 'indexed'


def test_issn_normalization_is_checksum_validated(tmp_path):
    path = write_master(tmp_path, rows={scopus.SHEET_SOURCES: [
        dict(DEFAULT_SOURCE, ISSN='03785955', EISSN='1234-5678')]})
    master = scopus.load_master(path)
    assert master.sheets[scopus.SHEET_SOURCES][0]['issns'] == frozenset({'0378-5955'})
    checked = master.check(candidate(issn='0378-5955', publication_year=2020))
    assert checked['status'] == 'indexed' and checked['metadata']['match_method'] == 'issn'


def test_normalize_isbn():
    assert scopus.normalize_isbn('978-0-123456-78-9') == '9780123456789'
    assert scopus.normalize_isbn(' 0-19-852663-6 ') == '0198526636'
    assert scopus.normalize_isbn('0-19-852663-x') == '019852663X'
    assert scopus.normalize_isbn('9781604238464') == '9781604238464'
    assert scopus.normalize_isbn('not an isbn') is None
    assert scopus.normalize_isbn(None) is None
    assert scopus.normalize_isbn('12345678901234567890') is None


def test_coverage_years_and_in_coverage():
    assert scopus.coverage_years('1995-2026') == set(range(1995, 2027))
    assert scopus.coverage_years('2026; 2023-2024; 2007-2015') == \
        {2026, 2023, 2024} | set(range(2007, 2016))
    assert scopus.coverage_years('2020') == {2020}
    assert scopus.coverage_years('1995-') == set(range(1995, scopus.REFERENCE_YEAR + 1))
    assert scopus.coverage_years(None) == set()
    assert scopus.in_coverage(2020, '1995-2026')
    assert not scopus.in_coverage(2027, '1995-2026')
    assert not scopus.in_coverage(None, '1995-2026')


def test_exact_issn_match_is_indexed_within_coverage(tmp_path):
    path = write_master(tmp_path)
    master = scopus.load_master(path)
    checked = master.check(candidate(issn='0378-5955', publication_year=2020))
    assert checked['status'] == 'indexed'
    assert checked['metadata']['scopus_status'] == 'indexed'
    assert checked['metadata']['match_method'] == 'issn'
    assert checked['metadata']['identity_match'] is True
    assert checked['metadata']['master_file'] == str(path.resolve())
    assert checked['metadata']['master_sha256'] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert checked['source_record_id'] == '123'
    assert checked['source_title'] == 'Journal of Testing'
    assert checked['source_type'] == 'Journal'
    assert checked['issn'] == '0378-5955' and checked['eissn'] == '0309-0566'
    assert checked['coverage'] == '1995-2026' and checked['active_status'] == 'Active'
    assert checked['discontinued'] is False
    assert checked['publisher'] == 'Elsevier' and checked['asjc'] == '2612'
    assert checked['matched_identity'] == '0378-5955'
    assert checked['match_confidence'] == 'high'
    assert checked['source_sheet'] == scopus.SHEET_SOURCES


def test_eissn_only_source_matches_via_eissn(tmp_path):
    path = write_master(tmp_path, rows={scopus.SHEET_SOURCES: [
        dict(DEFAULT_SOURCE, ISSN='', EISSN='03090566')]})
    master = scopus.load_master(path)
    checked = master.check(candidate(eissn='0309-0566', publication_year=2024))
    assert checked['status'] == 'indexed'
    assert checked['metadata']['match_method'] == 'issn'
    assert checked['eissn'] == '0309-0566'


def test_exact_isbn_match_for_proceedings(tmp_path):
    path = write_master(tmp_path, rows={scopus.SHEET_CONF_PROC: [
        {'Source Title': 'Test Conference', 'Proceedinds Title': 'Test Conference Proceedings',
         'ISBN': '978-0-123456-78-9', 'Year': 2024, 'EID': '84935468465',
         'Publisher': 'Conf Press', 'ASJC': '2310'}]})
    master = scopus.load_master(path)
    checked = master.check(proceedings_candidate())
    assert checked['status'] == 'indexed'
    assert checked['metadata']['scopus_status'] == 'indexed'
    assert checked['metadata']['match_method'] == 'isbn'
    assert checked['metadata']['eid'] == '84935468465'
    assert checked['isbn'] == '9780123456789'
    assert checked['matched_identity'] == '9780123456789'
    assert checked['match_confidence'] == 'high'
    assert checked['source_sheet'] == scopus.SHEET_CONF_PROC
    # Year is supporting evidence only: a mismatch never breaks identity.
    mismatch = master.check(proceedings_candidate(year=2019))
    assert mismatch['status'] == 'indicated'
    assert mismatch['metadata']['scopus_status'] == 'indicated'


def test_exact_title_match(tmp_path):
    master = scopus.load_master(write_master(tmp_path))
    checked = master.check(candidate(' JOURNAL — of  Testing! ', publication_year=2024))
    assert checked['status'] == 'indexed'
    assert checked['metadata']['match_method'] == 'title_exact'
    assert checked['matched_identity'] == 'journal of testing'
    assert checked['match_confidence'] == 'medium'


def test_fuzzy_match_never_indexes(tmp_path):
    master = scopus.load_master(write_master(tmp_path))
    checked = master.check(candidate('Journal of Testings'))
    assert checked['status'] == 'needs_verification'
    assert checked['metadata']['scopus_status'] == 'needs_verification'
    assert checked['metadata']['match_method'] == 'fuzzy_candidates'
    assert checked['metadata']['candidates'][0]['source_record_id'] == '123'
    assert checked['indexed'] is None


def test_year_within_and_outside_coverage(tmp_path):
    path = write_master(tmp_path, rows={scopus.SHEET_SOURCES: [
        dict(DEFAULT_SOURCE, Coverage='2026; 2023-2024')]})
    master = scopus.load_master(path)
    outside = master.check(candidate(publication_year=2020))
    assert outside['status'] == 'indicated'
    assert outside['metadata']['scopus_status'] == 'indicated'
    assert outside['indexed'] is None
    within = master.check(candidate(publication_year=2024))
    assert within['status'] == 'indexed'
    assert within['metadata']['scopus_status'] == 'indexed'


def test_accepted_titles_are_never_indexed(tmp_path):
    path = write_master(tmp_path, rows={
        scopus.SHEET_SOURCES: [],
        scopus.SHEET_ACCEPTED: [
            {'Status: September 2026': 'Notice: Scopus is owned by Elsevier B.V.'},
            {'Status: September 2026': 'Accepted',
             'Accepted titles in the process of being added to Scopus': 'Journal of Testing'}]})
    master = scopus.load_master(path)
    assert all(record['title'] for record in master.sheets[scopus.SHEET_ACCEPTED])
    checked = master.check(candidate())
    assert checked['status'] == 'indicated'
    assert checked['metadata']['scopus_status'] == 'indicated'
    assert checked['source_sheet'] == scopus.SHEET_ACCEPTED
    assert checked['indexed'] is not True


def test_accepted_sheet_real_two_header_layout(tmp_path):
    """The real export shows a banner pair, then the actual column headers."""
    workbook = Workbook()
    workbook.remove(workbook.active)
    for sheet_name, columns in SHEET_COLUMNS.items():
        worksheet = workbook.create_sheet(sheet_name)
        if sheet_name == scopus.SHEET_ACCEPTED:
            worksheet.append(['Status: September 2026',
                              'Accepted titles in the process of being added to Scopus'])
            worksheet.append(['Notice: Scopus is owned by Elsevier B.V. and Elsevier is sole owner'])
            worksheet.append(['Source Title', 'ISSN', 'EISSN', 'Date of Acceptance', 'Publisher'])
            worksheet.append(['Jurnal Perlindungan Tanaman Indonesia', '14101637', '25484788',
                              '2026-09-04 13:13:00', 'Universitas Gadjah Mada'])
        else:
            worksheet.append(columns)
    path = tmp_path / 'real_layout.xlsx'
    workbook.save(path)
    master = scopus.load_master(path)
    records = master.sheets[scopus.SHEET_ACCEPTED]
    assert len(records) == 1  # banner and notice rows are not data
    assert records[0]['title'] == 'Jurnal Perlindungan Tanaman Indonesia'
    assert records[0]['issns'] == frozenset({'1410-1637', '2548-4788'})
    assert records[0]['publisher'] == 'Universitas Gadjah Mada'
    # Accepted titles are matchable by ISSN too, but never indexed.
    checked = master.check(candidate('Jurnal Perlindungan Tanaman Indonesia',
                                     issn='1410-1637', publication_year=2026))
    assert checked['status'] == 'indicated'
    assert checked['source_sheet'] == scopus.SHEET_ACCEPTED
    assert checked['metadata']['match_method'] == 'issn'


def test_discontinued_source_is_matchable_but_not_indexed(tmp_path):
    path = write_master(tmp_path, rows={
        scopus.SHEET_SOURCES: [],
        scopus.SHEET_DISCONTINUED: [{'Sourcerecord ID': '777', 'Source Title': 'Journal of Testing',
                                     'ISSN': '0378-5955', 'EISSN': '', 'Publisher': 'Old Press',
                                     'Indexation Change': 'Discontinued', 'Year': '2020'}]})
    master = scopus.load_master(path)
    checked = master.check(candidate(issn='0378-5955', publication_year=2019))
    assert checked['status'] == 'indicated'
    assert checked['metadata']['scopus_status'] == 'indicated'
    assert checked['discontinued'] is True
    assert checked['source_sheet'] == scopus.SHEET_DISCONTINUED


def test_discontinued_listing_with_active_twin_and_year_is_indexed(tmp_path):
    path = write_master(tmp_path, rows={
        scopus.SHEET_SOURCES: [dict(DEFAULT_SOURCE)],
        scopus.SHEET_DISCONTINUED: [{'Sourcerecord ID': '123',
                                     'Source Title': 'Journal of Testing (old listing)',
                                     'ISSN': '0378-5955', 'EISSN': ''}]})
    master = scopus.load_master(path)
    checked = master.check(candidate(issn='0378-5955', publication_year=2020))
    assert checked['metadata']['scopus_status'] == 'indexed'
    assert checked['source_sheet'] == scopus.SHEET_SOURCES


def test_serial_conf_profile_match_is_indicated(tmp_path):
    path = write_master(tmp_path, rows={
        scopus.SHEET_SOURCES: [],
        scopus.SHEET_SERIAL_CONF: [{'Sourcerecord ID': '93910',
                                    'Source Title': '1990 IEEE Nuclear Science Symposium',
                                    'ISSN': '0378-5955', 'EISSN': '',
                                    'Coverage': '2007-2015; 1995-2005', 'ASJC': '3108'}]})
    master = scopus.load_master(path)
    checked = master.check(candidate('1990 IEEE Nuclear Science Symposium', kind='conference',
                                     issn='0378-5955', publication_year=2010))
    assert checked['status'] == 'indicated'
    assert checked['metadata']['scopus_status'] == 'indicated'
    assert checked['source_sheet'] == scopus.SHEET_SERIAL_CONF
    assert checked['metadata']['match_method'] == 'issn'


def test_inactive_source_is_indicated(tmp_path):
    path = write_master(tmp_path, rows={scopus.SHEET_SOURCES: [
        dict(DEFAULT_SOURCE, **{'Active or Inactive': 'Inactive'})]})
    master = scopus.load_master(path)
    checked = master.check(candidate(issn='0378-5955', publication_year=2020))
    assert checked['metadata']['scopus_status'] == 'indicated'
    assert checked['active_status'] == 'Inactive'


def test_ambiguous_candidates_are_never_auto_picked(tmp_path):
    path = write_master(tmp_path, rows={scopus.SHEET_SOURCES: [
        dict(DEFAULT_SOURCE),
        dict(DEFAULT_SOURCE, **{'Sourcerecord ID': '456', 'ISSN': '0309-0566', 'EISSN': ''})]})
    master = scopus.load_master(path)
    checked = master.check(candidate())
    assert checked['status'] == 'needs_verification'
    assert checked['metadata']['scopus_status'] == 'needs_verification'
    assert len(checked['metadata']['candidates']) == 2
    assert checked['indexed'] is None


def test_isbn_and_issn_identities_do_not_cross_sheets(tmp_path):
    path = write_master(tmp_path, rows={
        scopus.SHEET_SOURCES: [dict(DEFAULT_SOURCE)],
        scopus.SHEET_SERIAL_CONF: [{'Sourcerecord ID': '93910',
                                    'Source Title': 'Journal of Testing Conf',
                                    'ISSN': '0378-5955', 'EISSN': '', 'Coverage': '2000-2010'}],
        scopus.SHEET_CONF_PROC: [{'Source Title': 'Journal of Testing',
                                  'Proceedinds Title': 'Journal of Testing Proceedings',
                                  'ISBN': '978-0-123456-78-9', 'Year': 2024, 'EID': '1'}]})
    master = scopus.load_master(path)
    # ISBN-only identity matches only the proceedings sheet, never journal sheets.
    by_isbn = master.check(proceedings_candidate(title='Unrelated Conference'))
    assert by_isbn['source_sheet'] == scopus.SHEET_CONF_PROC
    assert by_isbn['metadata']['match_method'] == 'isbn'
    # ISSN identity matches serial sheets only, never the proceedings sheet.
    by_issn = master.check(candidate(kind='conference', issn='0378-5955', publication_year=2005))
    assert by_issn['source_sheet'] == scopus.SHEET_SOURCES
    assert by_issn['metadata']['match_method'] == 'issn'


def test_proceedings_title_match_for_non_serial_candidate(tmp_path):
    path = write_master(tmp_path, rows={
        scopus.SHEET_SOURCES: [],
        scopus.SHEET_CONF_PROC: [{'Source Title': 'Test Conference',
                                  'Proceedinds Title': 'Proceedings of Testing',
                                  'ISBN': '', 'Year': 2024, 'EID': '5'}]})
    master = scopus.load_master(path)
    checked = master.check(proceedings_candidate(isbn=None))
    assert checked['status'] == 'indicated'
    assert checked['metadata']['scopus_status'] == 'indicated'
    assert checked['metadata']['match_method'] == 'proceedings_title'
    assert checked['source_sheet'] == scopus.SHEET_CONF_PROC


def test_proceedings_pieces_sharing_one_isbn_are_one_identity(tmp_path):
    """The export lists one proceedings identity once per piece (distinct EID)."""
    path = write_master(tmp_path, rows={scopus.SHEET_CONF_PROC: [
        {'Source Title': 'Test Conference', 'Proceedinds Title': 'Test Conference Proceedings',
         'ISBN': '978-0-123456-78-9', 'Year': 2024, 'EID': '111'},
        {'Source Title': 'Test Conference', 'Proceedinds Title': 'Test Conference Proceedings',
         'ISBN': '9780123456789', 'Year': 2024, 'EID': '222'}]})
    master = scopus.load_master(path)
    checked = master.check(proceedings_candidate())
    assert checked['status'] == 'indexed'
    assert checked['metadata']['eids'] == ['111', '222']
    assert 'candidates' not in checked['metadata']


def test_proceedings_year_disambiguates_or_flags_ambiguity(tmp_path):
    rows = [
        {'Source Title': 'Test Conference', 'Proceedinds Title': 'Test Conference 2024',
         'ISBN': '978-0-123456-78-9', 'Year': 2024, 'EID': '111'},
        {'Source Title': 'Test Conference', 'Proceedinds Title': 'Test Conference 2019',
         'ISBN': '978-0-123456-78-9', 'Year': 2019, 'EID': '222'}]
    master = scopus.load_master(write_master(tmp_path, rows={scopus.SHEET_CONF_PROC: rows}))
    # The candidate year disambiguates between the two proceedings identities
    # and matches the record year, so the match is indexed.
    checked = master.check(proceedings_candidate(title='Test Conference', year=2019))
    assert checked['status'] == 'indexed'
    assert checked['metadata']['eids'] == ['222']
    # Without year evidence the ambiguity is never auto-picked.
    ambiguous = master.check(proceedings_candidate(title='Test Conference', year=None))
    assert ambiguous['status'] == 'needs_verification'
    assert len(ambiguous['metadata']['candidates']) == 2


def test_not_found_when_nothing_matches(tmp_path):
    master = scopus.load_master(write_master(tmp_path))
    checked = master.check(candidate('A Completely Different Journal'))
    assert checked['status'] == 'not_found'
    assert checked['metadata']['scopus_status'] == 'not_found'
    assert checked['indexed'] is None


def test_missing_broken_or_incompatible_master_raises_with_path(tmp_path):
    missing = tmp_path / 'missing.xlsx'
    with pytest.raises(scopus.ScopusMasterError, match='not found'):
        scopus.load_master(missing)
    broken = tmp_path / 'broken.xlsx'
    broken.write_text('not an excel file')
    with pytest.raises(scopus.ScopusMasterError, match='Cannot open'):
        scopus.load_master(broken)
    workbook = Workbook()
    workbook.remove(workbook.active)
    workbook.create_sheet(scopus.SHEET_SOURCES).append(
        ['Sourcerecord ID', 'Source Title', 'ISSN', 'EISSN', 'Active or Inactive', 'Coverage'])
    partial = tmp_path / 'partial.xlsx'
    workbook.save(partial)
    with pytest.raises(scopus.ScopusMasterError, match='missing sheet'):
        scopus.load_master(partial)
    workbook2 = Workbook()
    workbook2.remove(workbook2.active)
    for sheet_name in scopus.SHEET_ORDER:
        worksheet = workbook2.create_sheet(sheet_name)
        worksheet.append(['Sourcerecord ID', 'Source Title'] if
                         sheet_name == scopus.SHEET_SOURCES else ['unrelated header'])
    bad_headers = tmp_path / 'bad_headers.xlsx'
    workbook2.save(bad_headers)
    with pytest.raises(scopus.ScopusMasterError, match='required headers'):
        scopus.load_master(bad_headers)


def test_top_level_check_returns_error_result_for_missing_master(tmp_path):
    checked = scopus.check(candidate(), master_path=tmp_path / 'missing.xlsx')
    assert checked['status'] == 'error'
    assert str(tmp_path / 'missing.xlsx') in checked['reason']
    assert checked['metadata']['master_file'] == str(tmp_path / 'missing.xlsx')


def test_top_level_check_lazy_loads_and_accepts_master_instance(tmp_path):
    path = write_master(tmp_path)
    master = scopus.load_master(path)
    selected = candidate(issn='0378-5955', publication_year=2020)
    assert scopus.check(selected, master=master)['status'] == 'indexed'
    assert scopus.check(selected, master_path=path)['status'] == 'indexed'


def test_numeric_cells_are_normalized(tmp_path):
    path = write_master(tmp_path, rows={
        scopus.SHEET_SOURCES: [dict(DEFAULT_SOURCE, **{'Sourcerecord ID': 123456})],
        scopus.SHEET_CONF_PROC: [{'Source Title': 'Conf', 'Proceedinds Title': 'Conf Proc',
                                  'ISBN': 9781604238464, 'Year': 2007.0, 'EID': 84935468465}]})
    master = scopus.load_master(path)
    assert master.sheets[scopus.SHEET_SOURCES][0]['source_record_id'] == '123456'
    record = master.sheets[scopus.SHEET_CONF_PROC][0]
    assert record['year'] == 2007
    assert record['isbns'] == frozenset({'9781604238464'})
    assert record['eid'] == '84935468465'
