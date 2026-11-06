from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import re
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import dialect

from src.journal_index import sinta_catalog as scraper
from src.journal_index import run_journal_index as runner

FIXTURE = Path(__file__).parents[1] / 'fixtures/sinta/universitas_telkom_journals.html'


@pytest.fixture
def parsed():
    return scraper.parse_page(FIXTURE.read_text(encoding='utf-8'), scraper.CATALOG_URL,
                              datetime(2026, 11, 3, tzinfo=timezone.utc))


def test_parse_journal_ids_issns_ranks_and_metrics(parsed):
    rows, pages = parsed
    first, second = rows
    assert (first['sinta_journal_id'], first['journal_name']) == (10709, 'International Journal on Information and Communication Technology (IJoICT)')
    assert (first['p_issn'], first['e_issn'], first['subject_area']) == ('2356-5462', '2356-5462', 'Science, Social')
    assert (first['sinta_rank'], first['garuda_journal_id']) == ('S3', 8973)
    assert (first['impact'], first['h5_index'], first['citations_5yr'], first['citations']) == (0, 0, 0, 0)
    assert second['sinta_journal_id'] == 6706
    assert second['p_issn'] is None and second['e_issn'] == '2581-1088'
    assert (second['sinta_rank'], second['garuda_journal_id']) == ('S4', 16603)
    assert (second['impact'], second['h5_index'], second['citations_5yr'], second['citations']) == (0, 5, 84, 84)
    assert any('page=2' in p for p in pages)


def test_labeled_external_links_and_accreditation(parsed):
    rows, _ = parsed
    row = rows[0]
    assert row['accreditation_status'] == 'Accredited'
    assert row['garuda_status'] == 'Indexed'
    assert row['google_scholar_url'].startswith('https://scholar.google.com/')
    assert row['website_url'] == 'https://ijoiict.org'
    assert row['editor_url'] == 'https://ijoiict.org/editor'
    assert row['affiliation_id'] == 1093


@pytest.mark.parametrize('rank', [f'S{i}' for i in range(1, 7)])
def test_all_sinta_ranks(rank):
    html = FIXTURE.read_text(encoding='utf-8').replace('>S3</span>', f'>{rank}</span>', 1)
    rows, _ = scraper.parse_page(html, scraper.CATALOG_URL)
    assert rows[0]['sinta_rank'] == rank


def test_issn_zero_and_checksum_validation():
    html = FIXTURE.read_text(encoding='utf-8').replace('25811088', '0')
    rows, _ = scraper.parse_page(html, scraper.CATALOG_URL)
    assert rows[1]['e_issn'] is None
    assert rows[0]['p_issn'] == next(iter(runner.issns('23565462')))


def test_optional_fields_and_unknown_markup():
    minimal = '<div class="list-item row mt-3"><div class="affil-name"><a href="/journals/profile/42">Journal</a></div><div class="affil-loc"><a href="/affiliations/profile/1093">Universitas Telkom</a></div></div>'
    rows, pages = scraper.parse_page(minimal, scraper.CATALOG_URL)
    assert rows[0]['p_issn'] is None and rows[0]['impact'] is None and not pages
    with pytest.raises(ValueError, match='no journal cards'):
        scraper.parse_page('<html><body>changed</body></html>', scraper.CATALOG_URL)


def test_fetch_discovers_pages_and_deduplicates():
    html = FIXTURE.read_text(encoding='utf-8')
    session = MagicMock()
    session.get.return_value.text = html
    session.get.return_value.raise_for_status.return_value = None
    rows, pages, cards, duplicates = scraper.fetch_catalog(session=session, sleep=lambda _: None)
    assert (len(rows), pages, cards, duplicates) == (2, 2, 4, 2)
    assert session.get.call_count == 2
    assert session.get.call_args.kwargs['timeout'] > 0


def test_upsert_key_and_refresh_fields():
    rows, _ = scraper.parse_page(FIXTURE.read_text(encoding='utf-8'), scraper.CATALOG_URL)
    statement = sa.dialects.postgresql.insert(scraper.catalog).values(rows[0])
    statement = statement.on_conflict_do_update(index_elements=[scraper.catalog.c.sinta_journal_id],
        set_={'journal_name': statement.excluded.journal_name})
    sql = str(statement.compile(dialect=dialect()))
    assert 'ON CONFLICT (sinta_journal_id)' in sql and 'DO UPDATE' in sql
    conn = MagicMock()
    conn.execute.return_value.rowcount = 1
    assert scraper.upsert_catalog(conn, rows) == 1


def test_dry_run_cli_does_not_connect_database(monkeypatch, capsys):
    monkeypatch.setattr(scraper, 'fetch_catalog', lambda: ([], 2, 0, 0))
    monkeypatch.setattr(runner, 'validate_database_name', lambda *a, **k: pytest.fail('database touched'))
    monkeypatch.setattr('src.loading.postgres.build_engine', lambda: pytest.fail('database connected'))
    assert runner.main(['--sync-sinta-catalog', '--dry-run']) == 0
    assert '"dry_run": true' in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# Tes baru: info pagination, sumber halaman, penjelajah, pemeriksaan           #
# kelengkapan, dan CLI. Semua offline; halaman sintetis diturunkan dari        #
# fixture asli dan folder uji memakai tmp_path.                                #
# --------------------------------------------------------------------------- #

_PAGINATION_RE = re.compile(r'<ul class="pagination">.*?</ul>', re.S)


def _pagination_ul(current, linked, *, mark_active=True, truncated=False):
    items = []
    for index, number in enumerate(linked):
        if truncated and index == len(linked) - 1:
            items.append('<li class="page-item disabled"><span class="page-link">…</span></li>')
        active = ' active' if mark_active and number == current else ''
        items.append(f'<li class="page-item{active}"><a class="page-link" '
                     f'href="/journals/index/1093?page={number}">{number}</a></li>')
    return '<ul class="pagination">' + ''.join(items) + '</ul>'


def _synthetic_page(current, linked, *, ids=(10709, 6706), mark_active=True, truncated=False,
                    total=None, affiliation=None, saved_from=None):
    """Halaman sintetis yang diturunkan dari fixture asli."""
    html = _PAGINATION_RE.sub('', FIXTURE.read_text(encoding='utf-8'))
    html = html.replace('/journals/profile/10709', f'/journals/profile/{ids[0]}')
    html = html.replace('/journals/profile/6706', f'/journals/profile/{ids[1]}')
    if affiliation is not None:
        html = html.replace('/affiliations/profile/1093', f'/affiliations/profile/{affiliation}')
    if total is not None:
        html = html.replace('</body>', f'<p>Showing 1 to 4 of {total} entries</p></body>')
    html = html.replace('</body>', _pagination_ul(current, linked, mark_active=mark_active,
                                                  truncated=truncated) + '</body>')
    if saved_from:
        html = f'<!-- saved from url=(0066){saved_from} -->\n' + html
    return html


def _write_folder(folder, pages):
    folder.mkdir(parents=True, exist_ok=True)
    for name, html in pages:
        (folder / name).write_text(html, encoding='utf-8')
    return folder


def _mock_response(status=200, html=None, headers=None):
    response = MagicMock()
    response.status_code = status
    response.text = html if html is not None else FIXTURE.read_text(encoding='utf-8')
    response.headers = headers or {}
    response.raise_for_status.return_value = None
    return response


def test_read_pagination_reports_links_active_total_and_affiliation():
    info = scraper.read_pagination(_synthetic_page(2, [1, 2, 3], total=6), scraper.CATALOG_URL)
    assert info['active'] == 2 and info['active_in_markup'] is True
    assert [number for number, _ in info['pages']] == [1, 2, 3]
    assert info['total'] == 6 and info['affiliation_id'] == 1093 and info['next'] is None


def test_read_pagination_active_from_url_and_truncated_links():
    info = scraper.read_pagination(_synthetic_page(2, [1, 2, 3], mark_active=False),
                                   f'{scraper.CATALOG_URL}?page=2')
    assert info['active'] == 2 and info['active_in_markup'] is False
    info = scraper.read_pagination(_synthetic_page(3, [1, 2, 3, 10], truncated=True),
                                   scraper.CATALOG_URL)
    # Elipsis bukan tautan halaman; hanya tautan bernomor yang dihitung.
    assert [number for number, _ in info['pages']] == [1, 2, 3, 10]


def test_local_folder_three_files_complete(tmp_path):
    folder = _write_folder(tmp_path, [
        ('z9.html', _synthetic_page(1, [1, 2, 3], ids=(10709, 6706))),
        ('m4.html', _synthetic_page(2, [1, 2, 3], ids=(20709, 16706))),
        ('a0.html', _synthetic_page(3, [1, 2, 3], ids=(30709, 26706))),
    ])
    result = scraper.run_catalog(source='local', html_dir=folder)
    assert result.status == 'LENGKAP'
    assert (result.pages_processed, result.items_found, result.items_unique,
            result.duplicates_dropped) == (3, 6, 6, 0)
    assert result.source_type == 'local'
    assert all(row['source_type'] == 'local' for row in result.rows)


def test_local_folder_missing_linked_page_partial(tmp_path):
    folder = _write_folder(tmp_path, [
        ('z9.html', _synthetic_page(1, [1, 2, 3])),
        ('m4.html', _synthetic_page(2, [1, 2, 3], ids=(20709, 16706))),
    ])
    result = scraper.run_catalog(source='local', html_dir=folder)
    assert result.status == 'PARTIAL'
    assert any('halaman 3 belum ada' in warning for warning in result.warnings)


def test_local_folder_gap_between_pages_partial(tmp_path):
    folder = _write_folder(tmp_path, [
        ('z9.html', _synthetic_page(1, [1, 2, 3])),
        ('a0.html', _synthetic_page(3, [1, 2, 3], ids=(30709, 26706))),
    ])
    result = scraper.run_catalog(source='local', html_dir=folder)
    assert result.status == 'PARTIAL'
    assert 'halaman 2' in ' | '.join(result.warnings)


def test_truncated_pagination_never_assumes_hidden_pages(tmp_path):
    folder = _write_folder(tmp_path, [
        ('z9.html', _synthetic_page(1, [1, 2, 3, 10], truncated=True)),
        ('m4.html', _synthetic_page(2, [1, 2, 3, 10], truncated=True, ids=(20709, 16706))),
        ('a0.html', _synthetic_page(3, [1, 2, 3, 10], truncated=True, ids=(30709, 26706))),
    ])
    result = scraper.run_catalog(source='local', html_dir=folder)
    # Halaman 4-9 tidak pernah dianggap ditemukan; hanya halaman 10 yang
    # terbukti ditaut dan hilang dari folder.
    assert result.status == 'PARTIAL'
    assert 'halaman 10 belum ada' in ' | '.join(result.warnings)
    assert result.pages_processed == 3


def test_duplicate_journal_across_pages_deduplicated(tmp_path):
    folder = _write_folder(tmp_path, [
        ('z9.html', _synthetic_page(1, [1, 2])),
        ('m4.html', _synthetic_page(2, [1, 2])),  # jurnal yang sama persis
    ])
    result = scraper.run_catalog(source='local', html_dir=folder)
    assert result.status == 'LENGKAP'
    assert (result.items_found, result.items_unique, result.duplicates_dropped) == (4, 2, 2)


def test_saved_error_page_fails(tmp_path):
    folder = _write_folder(tmp_path, [
        ('z9.html', _synthetic_page(1, [1, 2])),
        ('m4.html', '<html><body>' + _pagination_ul(2, [1, 2]) + '</body></html>'),
    ])
    result = scraper.run_catalog(source='local', html_dir=folder)
    assert result.status == 'GAGAL'
    assert 'no journal cards' in ' | '.join(result.warnings)


def test_wrong_affiliation_rejected(tmp_path):
    folder = _write_folder(tmp_path, [('z9.html', _synthetic_page(1, [1, 2], affiliation=999))])
    result = scraper.run_catalog(source='local', html_dir=folder)
    assert result.status == 'GAGAL'
    assert 'affiliation' in ' | '.join(result.warnings)


def test_duplicate_page_number_rejected(tmp_path):
    folder = _write_folder(tmp_path, [
        ('z9.html', _synthetic_page(1, [1, 2])),
        ('m4.html', _synthetic_page(1, [1, 2])),  # nomor sama, nama file beda
    ])
    result = scraper.run_catalog(source='local', html_dir=folder)
    assert result.status == 'GAGAL'
    joined = ' | '.join(result.warnings)
    assert 'z9.html' in joined and 'm4.html' in joined


def test_file_without_page_marker_or_url_rejected(tmp_path):
    folder = _write_folder(tmp_path, [('z9.html', _synthetic_page(1, [1, 2], mark_active=False))])
    result = scraper.run_catalog(source='local', html_dir=folder)
    assert result.status == 'GAGAL'
    assert 'nomor halaman tidak dapat ditentukan' in ' | '.join(result.warnings)


def test_page_number_from_saved_from_url(tmp_path):
    folder = _write_folder(tmp_path, [
        ('z9.html', _synthetic_page(1, [1, 2])),
        ('m4.html', _synthetic_page(2, [1, 2], ids=(20709, 16706), mark_active=False,
                                    saved_from='https://sinta.kemdiktisaintek.go.id/journals/index/1093?page=2')),
    ])
    result = scraper.run_catalog(source='local', html_dir=folder)
    assert result.status == 'LENGKAP'
    page2 = next(row for row in result.rows if row['sinta_journal_id'] == 20709)
    assert page2['source_url'] == 'https://sinta.kemdiktisaintek.go.id/journals/index/1093?page=2'
    page1 = next(row for row in result.rows if row['sinta_journal_id'] == 10709)
    assert page1['source_url'] is None  # tanpa URL sumber: kosong, tidak mengarang


def test_filename_order_does_not_matter(tmp_path):
    ordered = _write_folder(tmp_path / 'a', [
        ('1.html', _synthetic_page(1, [1, 2, 3], ids=(10709, 6706))),
        ('2.html', _synthetic_page(2, [1, 2, 3], ids=(20709, 16706))),
        ('3.html', _synthetic_page(3, [1, 2, 3], ids=(30709, 26706))),
    ])
    scrambled = _write_folder(tmp_path / 'b', [
        ('z9.html', _synthetic_page(1, [1, 2, 3], ids=(10709, 6706))),
        ('m4.html', _synthetic_page(2, [1, 2, 3], ids=(20709, 16706))),
        ('a0.html', _synthetic_page(3, [1, 2, 3], ids=(30709, 26706))),
    ])
    first = scraper.run_catalog(source='local', html_dir=ordered)
    second = scraper.run_catalog(source='local', html_dir=scrambled)
    assert first.status == second.status == 'LENGKAP'
    assert sorted(row['sinta_journal_id'] for row in first.rows) == \
        sorted(row['sinta_journal_id'] for row in second.rows)


def test_http_403_blocks_immediately_with_single_request():
    session = MagicMock()
    session.get.return_value = _mock_response(status=403, html='Forbidden',
                                              headers={'Server': 'cloudflare'})
    result = scraper.run_catalog(source='http', session=session, sleep=lambda _: None)
    assert result.status == 'BLOCKED'
    assert session.get.call_count == 1  # tepat satu request, tanpa retry
    message = result.warnings[0]
    assert 'HTTP 403' in message and 'Server: cloudflare' in message and 'Forbidden' in message


def test_http_429_reports_retry_after_without_retry():
    session = MagicMock()
    session.get.return_value = _mock_response(status=429, html='slow down',
                                              headers={'Server': 'nginx', 'Retry-After': '30'})
    result = scraper.run_catalog(source='http', session=session, sleep=lambda _: None)
    assert result.status == 'BLOCKED'
    assert 'Retry-After: 30' in result.warnings[0]
    assert session.get.call_count == 1


def test_http_total_change_triggers_single_reexploration_then_partial():
    session = MagicMock()
    session.get.side_effect = [
        _mock_response(html=_synthetic_page(1, [1, 2], total=4)),
        _mock_response(html=_synthetic_page(2, [1, 2], ids=(20709, 16706), total=4)),
        # recheck pertama: total berubah -> penjelajahan diulang sekali
        _mock_response(html=_synthetic_page(1, [1, 2], total=6)),
        _mock_response(html=_synthetic_page(1, [1, 2], total=8)),
        _mock_response(html=_synthetic_page(2, [1, 2], ids=(20709, 16706), total=8)),
        # recheck kedua: masih berubah -> PARTIAL
        _mock_response(html=_synthetic_page(1, [1, 2], total=9)),
    ]
    result = scraper.run_catalog(source='http', session=session, sleep=lambda _: None, recheck=True)
    assert result.status == 'PARTIAL'
    assert any('berubah' in warning for warning in result.warnings)
    assert session.get.call_count == 6


def test_http_recheck_stable_completes():
    session = MagicMock()
    session.get.side_effect = [
        _mock_response(html=_synthetic_page(1, [1, 2], total=4)),
        _mock_response(html=_synthetic_page(2, [1, 2], ids=(20709, 16706), total=4)),
        _mock_response(html=_synthetic_page(1, [1, 2], total=4)),
    ]
    result = scraper.run_catalog(source='http', session=session, sleep=lambda _: None, recheck=True)
    assert result.status == 'LENGKAP'
    assert session.get.call_count == 3


def test_captured_at_flag_sets_fetched_at(tmp_path):
    folder = _write_folder(tmp_path, [('z9.html', _synthetic_page(1, [1]))])
    captured = datetime(2026, 11, 3, 8, 0, tzinfo=timezone.utc)
    result = scraper.run_catalog(source='local', html_dir=folder, captured_at=captured)
    assert result.status == 'LENGKAP'
    assert all(row['fetched_at'] == captured for row in result.rows)
    assert result.captured_at_source == 'flag --captured-at'
    without_flag = scraper.run_catalog(source='local', html_dir=folder)
    assert without_flag.captured_at_source == 'waktu modifikasi berkas'


def test_upsert_accepts_local_rows_with_null_source_url():
    rows, _ = scraper.parse_page(FIXTURE.read_text(encoding='utf-8'), scraper.CATALOG_URL)
    rows[0]['source_type'] = 'local'
    rows[0]['source_url'] = None
    statement = sa.dialects.postgresql.insert(scraper.catalog).values(rows[0])
    sql = str(statement.compile(dialect=dialect()))
    assert 'source_type' in sql and 'source_url' in sql


def test_sinta_exit_codes_mapping():
    assert runner.SINTA_EXIT_CODES == {'LENGKAP': 0, 'PARTIAL': 1, 'GAGAL': 2, 'BLOCKED': 3}


def test_cli_local_dry_run_reports_status(capsys, tmp_path):
    folder = _write_folder(tmp_path, [
        ('z9.html', _synthetic_page(1, [1, 2])),
        ('m4.html', _synthetic_page(2, [1, 2], ids=(20709, 16706))),
    ])
    code = runner.main(['--sync-sinta-catalog', '--source', 'local',
                        '--html-dir', str(folder), '--dry-run'])
    out = capsys.readouterr().out
    assert code == 0  # dry-run tetap pratinjau
    assert '"status": "LENGKAP"' in out and '"source_type": "local"' in out
    assert '"dry_run": true' in out


def test_cli_blocked_prints_diagnosis_and_dry_run_exits_zero(monkeypatch, capsys):
    def blocked():
        raise scraper.SourceBlocked(
            'Akses ditolak server: HTTP 403 untuk url | Server: test | Awal body: denied')
    monkeypatch.setattr(scraper, 'fetch_catalog', blocked)
    code = runner.main(['--sync-sinta-catalog', '--dry-run'])
    captured = capsys.readouterr()
    assert code == 0  # dry-run tetap pratinjau
    assert '"status": "BLOCKED"' in captured.out
    assert 'HTTP 403' in captured.err  # diagnosis dicetak ke stderr


def test_cli_rejects_invalid_source_and_captured_at_combinations():
    with pytest.raises(SystemExit):
        runner.main(['--sync-sinta-catalog', '--source', 'scopus'])
    with pytest.raises(SystemExit):
        runner.main(['--source', 'local'])
    with pytest.raises(SystemExit):
        runner.main(['--sync-sinta-catalog', '--source', 'local', '--captured-at', 'bukan-tanggal'])
