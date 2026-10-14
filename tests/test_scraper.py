"""Test berbasis fixture untuk parser scraper Google Scholar.

Cakupan file ini hanya fungsi parser murni di
src/ingestion/scrape_scholar_playwright.py yang tidak menyentuh
jaringan/browser:

    parse_profile_html, parse_detail_html, _parse_h_index,
    _parse_tahun_daftar, _detail_kosong, detect_block,
    _ekstrak_bibliografi_profil, _ekstrak_doi

Nilai ekspektasi diturunkan langsung dari isi fixture di
fixtures/scholar/ - fixture adalah sumber kebenaran. Fungsi yang
membutuhkan Playwright (scrape_scholar) TIDAK diuji di sini.

Cara menjalankan:
    python -m pytest tests/test_scraper.py -q
"""

import asyncio
import sys
from pathlib import Path

# --- Bootstrap import -------------------------------------------------
# Repo ini tidak punya __init__.py di mana pun (implicit namespace
# package), jadi `python -m pytest` dari root sudah cukup. Sisanya
# tetap ditambahkan supaya `pytest tests/...` (tanpa -m) juga
# bekerja dari direktori mana pun.
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import pytest  # noqa: E402
from bs4 import BeautifulSoup  # noqa: E402

from src.ingestion.scrape_scholar_playwright import (  # noqa: E402
    DETAIL_KEYS,
    _detail_kosong,
    _ekstrak_bibliografi_profil,
    _ekstrak_doi,
    _parse_h_index,
    _parse_tahun_daftar,
    detect_block,
    parse_detail_html,
    parse_profile_html,
)

# Alias modul: fase detail async dipanggil lewat atribut modul
# (_scrape_mod._ambil_detail_publikasi) dan monkeypatch pada
# parse_detail_html juga menarget modul yang sama - satu jalur.
import src.ingestion.scrape_scholar_playwright as _scrape_mod  # noqa: E402


# ============================================================
# HELPER FIXTURE
# ============================================================

_FIXTURES = _ROOT / "fixtures" / "scholar"

# Sebelas kunci yang WAJIB ada di setiap baris hasil parse_profil:
# 8 kunci lama + volume/issue/pages (bibliografi hasil ekstraksi venue).
KUNCI_BARIS = {
    "title", "authors", "venue", "extra", "citations", "year",
    "publication_year", "detail_url", "volume", "issue", "pages",
}


def _baca(nama):
    """Baca satu fixture Scholar sebagai teks UTF-8."""
    return (_FIXTURES / nama).read_text(encoding="utf-8")


# ============================================================
# 1. parse_profile_html
# ============================================================

def test_parse_profile_html_kepala():
    """Nama dan h-index profil terbaca persis dari fixture."""
    nama, rows, h_index = parse_profile_html(_baca("profil_hlid.html"))
    assert nama == "Tri Agus Djoko Kuntjoro"
    assert h_index == 2
    assert rows, "profil fixture harus menghasilkan baris publikasi"


def test_parse_profile_html_sebelas_kunci_per_baris():
    """Tiap baris membawa persis 11 kunci, tidak kurang tidak lebih."""
    _, rows, _ = parse_profile_html(_baca("profil_hlid.html"))
    for baris in rows:
        assert set(baris.keys()) == KUNCI_BARIS


def test_parse_profile_html_tipe_field_turunan():
    """publication_year int/None, detail_url str/None, bibliografi str/None."""
    _, rows, _ = parse_profile_html(_baca("profil_hlid.html"))

    # Fixture ini punya tahun numerik; pastikan jalur int benar-benar
    # terpakai supaya asersi tipe di bawah tidak hampa.
    assert any(
        isinstance(b["publication_year"], int) for b in rows
    ), "fixture seharusnya punya minimal satu publication_year int"

    for baris in rows:
        assert isinstance(baris["publication_year"], (int, type(None)))
        assert isinstance(baris["detail_url"], (str, type(None)))
        assert isinstance(baris["citations"], int)
        # Tiga kunci bibliografi baru: string terparse atau None.
        assert isinstance(baris["volume"], (str, type(None)))
        assert isinstance(baris["issue"], (str, type(None)))
        assert isinstance(baris["pages"], (str, type(None)))


# ============================================================
# 2. _parse_h_index
# ============================================================

def test_parse_h_index_dari_fixture():
    """Label 'indeks-h' (hl=id) di fixture terbaca sebagai 2."""
    soup = BeautifulSoup(_baca("profil_hlid.html"), "html.parser")
    assert _parse_h_index(soup) == 2


def test_parse_h_index_label_en_sintetis():
    """Label 'h-index' (hl=en) pada soup sintetis juga dikenali."""
    html = (
        '<table id="gsc_rsb_st">'
        "<tr><td></td><td>All</td><td>Since 2021</td></tr>"
        "<tr><td>Citations</td><td>80</td><td>80</td></tr>"
        "<tr><td>h-index</td><td>7</td><td>7</td></tr>"
        "<tr><td>i10-index</td><td>3</td><td>3</td></tr>"
        "</table>"
    )
    soup = BeautifulSoup(html, "html.parser")
    assert _parse_h_index(soup) == 7


def test_parse_h_index_tanpa_tabel():
    """Tanpa tabel #gsc_rsb_st, nilai baliknya None."""
    soup = BeautifulSoup("<html><body>tidak ada tabel</body></html>",
                         "html.parser")
    assert _parse_h_index(soup) is None


# ============================================================
# 3. _parse_tahun_daftar
# ============================================================

@pytest.mark.parametrize("teks, tahun", [
    ("2024", 2024),
    ("2024-2025", 2024),
    ("in press", None),
    ("", None),
    (None, None),
])
def test_parse_tahun_daftar(teks, tahun):
    """Ambil tahun 4 digit pertama; teks tanpa tahun -> None."""
    assert _parse_tahun_daftar(teks) == tahun


# ============================================================
# 4. parse_detail_html - artikel JURNAL (detail_1.html)
# ============================================================

# Enam nama pengarang lengkap persis seperti di fixture detail_1.html.
PENGARANG_JURNAL = [
    "Galuh Widya Cahyaningtyas",
    "Fannush Shofi Akbar",
    "Risdilah Mimma Untsa",
    "Puguh Chondro Jadmiko",
    "Efrilia M Khusna",
    "Tri Agus Djoko Kuntjoro",
]


def test_parse_detail_jurnal_judul_dan_penulis():
    """Judul terisi dan daftar pengarang memuat 6 nama lengkap."""
    detail = parse_detail_html(_baca("detail_1.html"))
    assert detail["detail_title"]
    assert detail["detail_authors"]

    daftar = [n.strip() for n in detail["detail_authors"].split(",")]
    assert len(daftar) == 6
    for nama in PENGARANG_JURNAL:
        assert nama in detail["detail_authors"]


def test_parse_detail_jurnal_metadata():
    """Tanggal, label venue, jilid, dan terbitan sesuai fixture."""
    detail = parse_detail_html(_baca("detail_1.html"))
    assert detail["detail_date"] == "2024/12/31"
    assert detail["detail_venue_label"] == "Jurnal"
    assert detail["detail_volume"] == "5"
    assert detail["detail_issue"] == "2"


def test_parse_detail_jurnal_tautan_dan_deskripsi():
    """Tautan artikel/PDF absolut dan deskripsi terisi."""
    detail = parse_detail_html(_baca("detail_1.html"))
    assert detail["detail_link"].startswith("http")
    assert detail["detail_pdf_link"].startswith("http")
    assert detail["detail_description"]


# ============================================================
# 5. parse_detail_html - artikel KONFERENSI (detail_2.html)
# ============================================================

def test_parse_detail_konferensi():
    """Konferensi tanpa jilid: volume None, field lain terisi."""
    detail = parse_detail_html(_baca("detail_2.html"))
    assert detail["detail_venue_label"] == "Konferensi"
    assert detail["detail_pages"] == "53-58"
    assert detail["detail_publisher"] == "IEEE"
    # Konferensi tidak punya 'Jilid' -> wajib None, bukan tebakan.
    assert detail["detail_volume"] is None
    # Fixture ini memang tidak menyediakan tautan PDF.
    assert detail["detail_pdf_link"] is None


# ============================================================
# 6. _detail_kosong
# ============================================================

def test_detail_kosong_semua_none():
    """Dict kosong punya semua kunci DETAIL_KEYS bernilai None."""
    hasil = _detail_kosong()
    assert set(hasil.keys()) == set(DETAIL_KEYS)
    assert all(nilai is None for nilai in hasil.values())


# ============================================================
# 7. detect_block
# ============================================================

def test_detect_block_fixture_normal():
    """Profil fixture normal tidak boleh dianggap halaman blokir."""
    assert detect_block(_baca("profil_hlid.html")) is False


@pytest.mark.parametrize("html", [
    "<html>We have detected unusual traffic from your network.</html>",
    "<html>Please verify you are not a robot.</html>",
    '<html><div id="captcha"></div></html>',
    "<html>https://www.google.com/sorry/index</html>",
])
def test_detect_block_penanda_terkenal(html):
    """Setiap penanda blokir/captcha dikenali."""
    assert detect_block(html) is True


# ============================================================
# 8. _ekstrak_doi  (ekstraksi DOI dari URL halaman detail)
# ============================================================

def test_ekstrak_doi_dari_detail_link():
    doi, konflik = _ekstrak_doi(
        "https://dl.acm.org/doi/abs/10.1145/3055635.3056657", None)
    assert doi == "10.1145/3055635.3056657"
    assert konflik is None


def test_ekstrak_doi_dari_detail_pdf_link():
    doi, konflik = _ekstrak_doi(
        None, "https://dl.acm.org/doi/pdf/10.1145/3055635.3056658")
    assert doi == "10.1145/3055635.3056658"
    assert konflik is None


def test_ekstrak_doi_dua_sumber_doi_sama():
    doi, konflik = _ekstrak_doi(
        "https://dl.acm.org/doi/abs/10.1145/3055635.3056657",
        "https://dl.acm.org/doi/pdf/10.1145/3055635.3056657")
    assert doi == "10.1145/3055635.3056657"
    assert konflik is None


def test_ekstrak_doi_hanya_satu_url_punya_doi():
    doi, konflik = _ekstrak_doi(
        "https://link.springer.com/chapter/10.1007/978-981-19-1804-9_34", None)
    assert doi == "10.1007/978-981-19-1804-9_34"
    assert konflik is None


def test_ekstrak_doi_konflik_dua_doi_beda():
    doi, konflik = _ekstrak_doi(
        "https://dl.acm.org/doi/abs/10.1145/3055635.3056657",
        "https://link.springer.com/chapter/10.1007/978-981-19-1804-9_34")
    assert doi is None
    assert konflik == {
        "detail_link_doi": "10.1145/3055635.3056657",
        "detail_pdf_doi": "10.1007/978-981-19-1804-9_34",
    }


def test_ekstrak_doi_tidak_ada():
    assert _ekstrak_doi(None, None) == (None, None)
    assert _ekstrak_doi(
        "https://example.org/artikel/123",
        "https://example.org/artikel/456") == (None, None)


def test_ekstrak_doi_tolak_pii_dan_angka_mirip_doi():
    # PII Elsevier bukan DOI (tidak diawali '10.')
    assert _ekstrak_doi(
        "https://www.sciencedirect.com/science/article/pii/S2352340925007656",
        None) == (None, None)
    # '10.1234' tanpa '/suffix' bukan DOI
    assert _ekstrak_doi("https://example.org/10.1234", None) == (None, None)
    # cluster id Scholar bukan DOI
    assert _ekstrak_doi(
        "https://scholar.google.com/scholar?cluster=11610803497464792099",
        None) == (None, None)


def test_ekstrak_doi_buang_query_dan_fragment():
    doi, _ = _ekstrak_doi(
        "https://dl.acm.org/doi/abs/10.1145/3055635.3056657?download=1#sec2",
        None)
    assert doi == "10.1145/3055635.3056657"


def test_ekstrak_doi_buang_penutup_yang_tidak_sah():
    # ',' ']' '}' BUKAN karakter suffix DOI -> aman dibuang.
    assert _ekstrak_doi("https://example.org/10.1000/abc,", None)[0] == "10.1000/abc"
    assert _ekstrak_doi("https://example.org/10.1000/abc]", None)[0] == "10.1000/abc"
    assert _ekstrak_doi("https://example.org/10.1000/abc}", None)[0] == "10.1000/abc"


def test_ekstrak_doi_pertahankan_penutup_yang_sah():
    # '.', ';', ')' SAH sebagai karakter suffix DOI -> TIDAK dipotong,
    # supaya DOI valid tidak terpangkas (keputusan sadar, bukan tebakan).
    assert _ekstrak_doi("https://example.org/10.1000/abc.", None)[0] == "10.1000/abc."
    assert _ekstrak_doi("https://example.org/10.1000/abc;", None)[0] == "10.1000/abc;"
    assert _ekstrak_doi("https://example.org/10.1000/a(b)", None)[0] == "10.1000/a(b)"


def test_ekstrak_doi_suffix_ekstensi_berkas_tidak_dipotong():
    # BATASAN DIKETAHUI: ".html" tersusun dari karakter suffix DOI yang
    # sah sehingga tidak dapat dibedakan dari DOI asli. Tidak ditebak /
    # dipotong; nilai diambil apa adanya (mungkin over-capture).
    assert _ekstrak_doi(
        "https://example.org/10.1000/abc.html", None)[0] == "10.1000/abc.html"


def test_ekstrak_doi_input_kosong_dan_null():
    assert _ekstrak_doi(None, None) == (None, None)
    assert _ekstrak_doi("", "") == (None, None)
    assert _ekstrak_doi("", None) == (None, None)
    assert _ekstrak_doi(None, "https://example.org/tanpa-doi") == (None, None)


def test_ekstrak_doi_slash_terencode():
    doi, _ = _ekstrak_doi("https://example.org/doi/10.1000%2Fabc123", None)
    assert doi == "10.1000/abc123"


_HTML_DETAIL_DOI = (
    "<html><body>"
    '<div id="gsc_oci_title">Judul Uji</div>'
    '<a class="gsc_oci_title_link" '
    'href="https://dl.acm.org/doi/abs/10.1145/3055635.3056657">sumber</a>'
    '<div class="gsc_oci_title_ggi">'
    '<a href="https://dl.acm.org/doi/pdf/10.1145/3055635.3056657">pdf</a>'
    "</div>"
    '<div class="gs_scl"><div class="gsc_oci_field">Jurnal</div>'
    '<div class="gsc_oci_value">J</div></div>'
    "</body></html>"
)


def test_parse_detail_html_isi_detail_doi_tanpa_ubah_link():
    detail = parse_detail_html(_HTML_DETAIL_DOI)
    assert detail["detail_link"] == "https://dl.acm.org/doi/abs/10.1145/3055635.3056657"
    assert detail["detail_pdf_link"] == "https://dl.acm.org/doi/pdf/10.1145/3055635.3056657"
    assert detail["detail_doi"] == "10.1145/3055635.3056657"
    assert set(DETAIL_KEYS) <= set(detail.keys())


_HTML_DETAIL_KONFLIK = (
    "<html><body>"
    '<a class="gsc_oci_title_link" '
    'href="https://dl.acm.org/doi/abs/10.1145/3055635.3056657">a</a>'
    '<div class="gsc_oci_title_ggi">'
    '<a href="https://link.springer.com/chapter/10.1007/978-981-19-1804-9_34">b</a>'
    "</div></body></html>"
)


def test_parse_detail_html_konflik_doi_null_dan_tercatat(capsys):
    detail = parse_detail_html(_HTML_DETAIL_KONFLIK)
    assert detail["detail_doi"] is None
    keluaran = capsys.readouterr().out
    assert "10.1145/3055635.3056657" in keluaran
    assert "10.1007/978-981-19-1804-9_34" in keluaran


def test_parse_detail_html_selalu_punya_kunci_detail_doi():
    assert "detail_doi" in parse_detail_html(_baca("detail_2.html"))
    assert "detail_doi" in parse_detail_html(_baca("detail_1.html"))


# ============================================================
# 9. Log konflik DOI tidak boleh membocorkan field lain
# ============================================================

_HTML_KONFLIK_DENGAN_FIELD_LAIN = (
    "<html><body>"
    '<div id="gsc_oci_title">JUDUL RAHASIA</div>'
    '<div id="gsc_oci_descr">DESKRIPSI SENSITIF PANJANG</div>'
    '<a class="gsc_oci_title_link" '
    'href="https://dl.acm.org/doi/abs/10.1145/3055635.3056657">x</a>'
    '<div class="gsc_oci_title_ggi">'
    '<a href="https://link.springer.com/chapter/10.1007/978-981-19-1804-9_34">y</a>'
    "</div>"
    '<div class="gs_scl"><div class="gsc_oci_field">Pengarang</div>'
    '<div class="gsc_oci_value">PENULIS RAHASIA</div></div>'
    "</body></html>"
)


def test_log_konflik_tidak_mencetak_deskripsi_judul_penulis(capsys):
    detail = parse_detail_html(_HTML_KONFLIK_DENGAN_FIELD_LAIN)
    assert detail["detail_doi"] is None
    keluaran = capsys.readouterr().out
    # Kedua DOI + kedua URL dicatat (cukup untuk pemeriksaan).
    assert "10.1145/3055635.3056657" in keluaran
    assert "10.1007/978-981-19-1804-9_34" in keluaran
    # Field lain TIDAK bocor ke log.
    assert "JUDUL RAHASIA" not in keluaran
    assert "DESKRIPSI SENSITIF PANJANG" not in keluaran
    assert "PENULIS RAHASIA" not in keluaran


# ============================================================
# 10. DOI: ekstensi berkas, kunci banding, kebijakan konservatif
# ============================================================

def test_ekstrak_doi_ekstensi_html_satu_sisi_link_bersih():
    doi, konflik = _ekstrak_doi(
        "https://example.org/10.1000/abc",
        "https://example.org/10.1000/abc.html")
    assert doi == "10.1000/abc"
    assert konflik is None


def test_ekstrak_doi_ekstensi_pdf_satu_sisi_pdf_bersih():
    doi, konflik = _ekstrak_doi(
        "https://repository.example/10.1000/abc.pdf",
        "https://doi.org/10.1000/abc")
    assert doi == "10.1000/abc"
    assert konflik is None


def test_ekstrak_doi_ekstensi_kedua_sisi_beda_menjadi_konflik():
    doi, konflik = _ekstrak_doi(
        "https://example.org/10.1000/abc.html",
        "https://example.org/10.1000/abc.pdf")
    assert doi is None
    assert konflik == {
        "detail_link_doi": "10.1000/abc.html",
        "detail_pdf_doi": "10.1000/abc.pdf",
    }


def test_ekstrak_doi_ekstensi_sama_di_kedua_sisi_diambil_apa_adanya():
    doi, konflik = _ekstrak_doi(
        "https://a.example/10.1000/abc.html",
        "https://b.example/10.1000/abc.html")
    assert doi == "10.1000/abc.html"
    assert konflik is None


def test_ekstrak_doi_uppercase_dianggap_sama():
    doi, konflik = _ekstrak_doi(
        "https://example.org/10.1000/ABC",
        "https://example.org/10.1000/abc")
    assert doi == "10.1000/ABC"
    assert konflik is None


def test_ekstrak_doi_satu_url_uppercase_verbatim():
    assert _ekstrak_doi("https://example.org/10.1000/ABC", None)[0] == "10.1000/ABC"


def test_ekstrak_doi_pilih_kandidat_tanpa_tanda_baca_penutup():
    doi, konflik = _ekstrak_doi(
        "https://example.org/10.1000/abc.",
        "https://example.org/10.1000/abc")
    assert doi == "10.1000/abc"
    assert konflik is None


def test_ekstrak_doi_query_string_didukung():
    # PERUBAHAN ATURAN SADAR DI P11: sebelumnya tes ini mendokumentasikan
    # query-string TIDAK didukung (query dipotong -> (None, None)). P11
    # menambahkan dukungan AMAN untuk DOI di query (aturan di _doi_dari_url:
    # potong di '&', first-match, fragment tetap ditolak, prioritas path) -
    # perilaku BARU yang disepakati, bukan kebetulan.
    assert _ekstrak_doi(
        "https://example.org/redirect?doi=10.1000/abc", None) == (
        "10.1000/abc", None)


def test_ekstrak_doi_tidak_menyatukan_doi_berbeda_karena_ekstensi():
    doi, konflik = _ekstrak_doi(
        "https://example.org/10.1000/abc.html",
        "https://example.org/10.1000/xyz.pdf")
    assert doi is None
    assert konflik is not None


def test_ekstrak_doi_tidak_menyamakan_prefix_berbeda():
    doi, konflik = _ekstrak_doi(
        "https://example.org/10.1000/abc",
        "https://example.org/10.1000/abcd")
    assert doi is None
    assert konflik is not None


def test_ekstrak_doi_percent_encoding_dengan_ekstensi():
    doi, konflik = _ekstrak_doi(
        "https://example.org/10.1000%2Fabc123.pdf",
        "https://doi.org/10.1000/abc123")
    assert doi == "10.1000/abc123"
    assert konflik is None


_HTML_DETAIL_EKSTENSI = (
    "<html><body>"
    '<a class="gsc_oci_title_link" '
    'href="https://example.org/10.1000/abc.html">x</a>'
    '<div class="gsc_oci_title_ggi">'
    '<a href="https://doi.org/10.1000/abc">y</a>'
    "</div></body></html>"
)


def test_parse_detail_html_url_sumber_tetap_dan_ekstensi_diselesaikan():
    detail = parse_detail_html(_HTML_DETAIL_EKSTENSI)
    assert detail["detail_link"] == "https://example.org/10.1000/abc.html"
    assert detail["detail_pdf_link"] == "https://doi.org/10.1000/abc"
    assert detail["detail_doi"] == "10.1000/abc"


# ============================================================
# 11. Bibliografi profil: volume/issue/pages dari venue/extra
# ============================================================

def test_bibliografi_volume_issue_tanpa_halaman():
    """Pola '... <volume> (<issue>), <tahun>' -> vol+issue, pages None."""
    hasil = _ekstrak_bibliografi_profil(
        "Journal of Computer Electronic and Telecommunication 5 (2), 2024",
        None,
    )
    assert hasil == {"volume": "5", "issue": "2", "pages": None}


def test_bibliografi_volume_issue_dan_halaman():
    """Pola '... <volume> (<issue>), <rentang-halaman>, <tahun>'."""
    hasil = _ekstrak_bibliografi_profil(
        "Journal Of Computer Science Contributions (JUCOSCO) 5 (1), "
        "23-32, 2025",
        None,
    )
    assert hasil == {"volume": "5", "issue": "1", "pages": "23-32"}


@pytest.mark.parametrize("venue, halaman", [
    ("Journal X 3 (4), 10-20, 2022", "10-20"),   # hyphen biasa
    ("Journal X 3 (4), 10–20, 2022", "10-20"),   # en dash
    ("Journal X 3 (4), 10—20, 2022", "10-20"),   # em dash
])
def test_bibliografi_tiga_jenis_tanda_hubung_halaman(venue, halaman):
    """Hyphen, en dash, dan em dash semuanya dikenali sebagai halaman."""
    hasil = _ekstrak_bibliografi_profil(venue, None)
    assert hasil == {"volume": "3", "issue": "4", "pages": halaman}


def test_bibliografi_tahun_tidak_jadi_volume():
    """Volume yang kebetulan mirip tahun (2024) DITOLAK, issue tetap ada."""
    hasil = _ekstrak_bibliografi_profil("Journal 2024 (2), 2024", None)
    assert hasil == {"volume": None, "issue": "2", "pages": None}


def test_bibliografi_rentang_tahun_bukan_halaman():
    """'2020-2021' di akhir adalah tahun, bukan rentang halaman."""
    hasil = _ekstrak_bibliografi_profil("Journal 99 (1), 2020-2021", None)
    assert hasil == {"volume": "99", "issue": "1", "pages": None}


@pytest.mark.parametrize("venue", [
    "2023 IEEE International Conference on Communication, "
    "Networks and Satellite (COMNETSAT)",
    "Some Plain Journal Name",
])
def test_bibliografi_tanpa_metadata_semua_none(venue):
    """Teks tanpa pola yang didukung -> semua None (tanpa tebakan)."""
    assert _ekstrak_bibliografi_profil(venue, None) == {
        "volume": None, "issue": None, "pages": None,
    }


def test_bibliografi_input_kosong_dan_null():
    """venue/extra None atau kosong -> semua None."""
    kosong = {"volume": None, "issue": None, "pages": None}
    assert _ekstrak_bibliografi_profil(None, None) == kosong
    assert _ekstrak_bibliografi_profil("", "") == kosong
    assert _ekstrak_bibliografi_profil(None, "") == kosong
    assert _ekstrak_bibliografi_profil("", None) == kosong


def test_bibliografi_nbsp_tidak_menghasilkan_tebakan():
    """NBSP tidak membuat nilai karangan; pola dengan NBSP tetap terbaca."""
    # NBSP + elipsis sebelum tahun: tahun terkonsumsi, sisanya bukan
    # pola yang didukung -> semua None.
    venue_konferensi = (
        "2023 IEEE International Conference on Communication, "
        "Networks and Satellite\xa0…, 2023"
    )
    assert _ekstrak_bibliografi_profil(venue_konferensi, None) == {
        "volume": None, "issue": None, "pages": None,
    }
    # NBSP di dalam pola yang didukung tetap terparse (normalisasi
    # hanya untuk pencocokan, bukan untuk nilai yang disimpan).
    assert _ekstrak_bibliografi_profil(
        "Journal\xa0X 5 (2)\xa0, 2023", None
    ) == {"volume": "5", "issue": "2", "pages": None}


def test_bibliografi_extra_sebagai_sumber_tambahan():
    """Field yang None di venue bisa diisi dari extra."""
    hasil = _ekstrak_bibliografi_profil(
        "Plain Journal Name", "5 (1), 23-32")
    assert hasil == {"volume": "5", "issue": "1", "pages": "23-32"}


def test_bibliografi_venue_menang_di_atas_extra():
    """Venue jadi sumber utama; extra hanya mengisi field yang None.

    Venue "Journal A 7 (8), 2020" tidak punya halaman -> pages boleh
    datang dari extra ("1-2"), tapi volume/issue tetap milik venue.
    """
    hasil = _ekstrak_bibliografi_profil(
        "Journal A 7 (8), 2020", "9 (0), 1-2, 2020")
    assert hasil == {"volume": "7", "issue": "8", "pages": "1-2"}


def _html_profil_sintetis(venue, extra=None):
    """Profil Scholar minimal berisi tepat satu baris publikasi.

    `extra` opsional; kalau None, div gs_gray ketiga tidak dibuat
    (meniru fixture profil di mana extra selalu None).
    """
    grays = (
        '<div class="gs_gray">Penulis Uji</div>'
        f'<div class="gs_gray">{venue}</div>'
    )
    if extra is not None:
        grays += f'<div class="gs_gray">{extra}</div>'
    return (
        "<html><body>"
        '<div id="gsc_prf_in">Nama Uji</div>'
        "<table><tr class=\"gsc_a_tr\">"
        '<td><a class="gsc_a_at" '
        'href="/citations?view_op=view_citation&amp;citation_for_view=UJI%3A1"'
        f">Judul Uji</a>{grays}</td>"
        '<td><a class="gsc_a_ac">7</a><span class="gsc_a_h">2021</span></td>'
        "</tr></table>"
        "</body></html>"
    )


# Halaman detail sintetis dengan Jilid + Halaman (label hl=id).
_HTML_DETAIL_NOMOR = (
    "<html><body>"
    '<div id="gsc_oci_title">Judul Uji</div>'
    '<div class="gs_scl"><div class="gsc_oci_field">Jilid</div>'
    '<div class="gsc_oci_value">9</div></div>'
    '<div class="gs_scl"><div class="gsc_oci_field">Halaman</div>'
    '<div class="gsc_oci_value">53-58</div></div>'
    "</body></html>"
)


def test_bibliografi_integrasi_fixture():
    """volume/issue/pages terisi dari venue fixture; extra tetap None."""
    html = _baca("profil_hlid.html")
    _, rows, _ = parse_profile_html(html)

    # Baris 0: jurnal dengan volume+issue tanpa halaman.
    jurnal = rows[0]
    assert jurnal["venue"] == (
        "Journal of Computer Electronic and Telecommunication 5 (2), 2024"
    )
    assert jurnal["volume"] == "5"
    assert jurnal["issue"] == "2"
    assert jurnal["pages"] is None

    # Baris 2: jurnal dengan volume+issue+rentang halaman.
    jurnal_halaman = rows[2]
    assert jurnal_halaman["volume"] == "5"
    assert jurnal_halaman["issue"] == "1"
    assert jurnal_halaman["pages"] == "23-32"

    # Baris 1: konferensi (NBSP + elipsis) -> tanpa tebakan.
    konferensi = rows[1]
    assert konferensi["volume"] is None
    assert konferensi["issue"] is None
    assert konferensi["pages"] is None

    # extra pada fixture selalu None dan TIDAK dipalsukan oleh ekstraksi.
    assert all(baris["extra"] is None for baris in rows)

    # venue tiap baris identik dengan teks asli hasil parse independen
    # (BeautifulSoup di tes ini) -> string venue tidak dimodifikasi.
    soup = BeautifulSoup(html, "html.parser")
    daftar_grays = [
        tr.select("div.gs_gray") for tr in soup.select("tr.gsc_a_tr")
    ]
    assert len(daftar_grays) == len(rows)
    for baris, grays in zip(rows, daftar_grays):
        assert baris["venue"] == grays[1].get_text(strip=True)
        assert baris["extra"] == (
            grays[2].get_text(strip=True) if len(grays) > 2 else None
        )


def test_profil_pertahankan_teks_venue_dan_extra():
    """parse_profile_html tidak mengubah teks venue/extra (termasuk NBSP)."""
    venue = "Journal\xa0A 4 (2), 10-11, 2021"
    extra = "5 (1),\xa023-32"
    _, rows, _ = parse_profile_html(_html_profil_sintetis(venue, extra))
    assert len(rows) == 1
    assert rows[0]["venue"] == venue
    assert rows[0]["extra"] == extra


def test_gabung_detail_tidak_menimpa_bibliografi_profil():
    """row.update(detail) menambah key detail_* tanpa menyentuh
    volume/issue/pages profil; ekstraksi profil tidak pernah menulis
    key yang diawali 'detail_'."""
    _, rows, _ = parse_profile_html(
        _html_profil_sintetis("Journal A 4 (2), 10-11, 2021")
    )
    baris = rows[0]
    assert baris["volume"] == "4"
    assert baris["issue"] == "2"
    assert baris["pages"] == "10-11"

    # Satu-satunya key 'detail_*' dari parse profil adalah detail_url;
    # helper bibliografi tidak boleh menulis key detail apa pun.
    assert {k for k in baris if k.startswith("detail_")} == {"detail_url"}

    baris.update(parse_detail_html(_HTML_DETAIL_NOMOR))

    # Kedua set utuh: profil (tanpa prefix) dan detail (prefix detail_).
    assert baris["volume"] == "4"
    assert baris["issue"] == "2"
    assert baris["pages"] == "10-11"
    assert baris["detail_volume"] == "9"
    assert baris["detail_pages"] == "53-58"


# ============================================================
# 12. parse_detail_html - detail_citations (angka sitasi bersih)
# ============================================================

def _html_sitasi_sintetis(teks_anchor):
    """HTML sintetis satu blok 'Total kutipan'.

    `teks_anchor` = isi anchor "Dirujuk N kali" di luar graph wrapper
    (None -> tidak ada anchor sama sekali, hanya histogram). Histogram
    selalu punya tahun 2020/2021/2022 + bar '3'/'4' DAN href ber-`cites=`
    di dalam #gsc_oci_graph_wrapper (harus dikecualikan parser).
    """
    anchor_dirujuk = ""
    if teks_anchor is not None:
        anchor_dirujuk = (
            '<div style="margin-bottom:1em">'
            '<a href="https://scholar.google.co.id/scholar?oi=bibs'
            '&amp;hl=id&amp;cites=111&amp;as_sdt=5">'
            + teks_anchor
            + "</a></div>"
        )
    wrapper = (
        '<div id="gsc_oci_graph_wrapper">'
        '<span class="gsc_oci_g_t">2020</span>'
        '<a class="gsc_oci_g_a" '
        'href="https://scholar.google.co.id/scholar?cites=111">'
        '<span class="gsc_oci_g_al">3</span></a>'
        '<span class="gsc_oci_g_t">2021</span>'
        '<a class="gsc_oci_g_a" '
        'href="https://scholar.google.co.id/scholar?cites=111">'
        '<span class="gsc_oci_g_al">4</span></a>'
        '<span class="gsc_oci_g_t">2022</span>'
        "</div>"
    )
    return (
        "<html><body>"
        '<div class="gs_scl">'
        '<div class="gsc_oci_field">Total kutipan</div>'
        f'<div class="gsc_oci_value">{anchor_dirujuk}{wrapper}</div>'
        "</div>"
        "</body></html>"
    )


def test_sitasi_fixture_detail_1_dan_detail_2():
    """Angka sitasi bersih dari fixture; digit histogram tidak bocor."""
    nilai1 = parse_detail_html(_baca("detail_1.html"))["detail_citations"]
    assert nilai1 == "3"
    # Teks gabungan lama ("Dirujuk 3 kali2025202612") dilarang:
    # tahun & bar histogram tidak boleh ikut ke nilai.
    assert "2025" not in nilai1
    assert "2026" not in nilai1
    assert "kali" not in nilai1

    nilai2 = parse_detail_html(_baca("detail_2.html"))["detail_citations"]
    assert nilai2 == "2"
    assert "2024" not in nilai2
    assert "2025" not in nilai2
    assert "kali" not in nilai2


def test_sitasi_sintetis_dengan_histogram_multi_tahun():
    """Anchor 'Dirujuk 7 kali' menang; tahun 2020-2022 & bar 3/4 tidak."""
    detail = parse_detail_html(_html_sitasi_sintetis("Dirujuk 7 kali"))
    nilai = detail["detail_citations"]
    assert nilai == "7"
    assert "2020" not in nilai
    assert "2021" not in nilai
    assert "2022" not in nilai


def test_sitasi_nol_tetap_string_0():
    """'Dirujuk 0 kali' -> "0" (string), bukan None dan bukan kosong."""
    detail = parse_detail_html(_html_sitasi_sintetis("Dirujuk 0 kali"))
    assert detail["detail_citations"] == "0"


def test_sitasi_struktur_tidak_sesuai_hasil_none():
    """Value hanya berisi graph wrapper (tanpa anchor 'Dirujuk') -> None.

    Tahun histogram TIDAK dijadikan sitasi (konservatif).
    """
    detail = parse_detail_html(_html_sitasi_sintetis(None))
    assert detail["detail_citations"] is None


def test_sitasi_label_total_kutipan_tidak_ada():
    """Tanpa blok 'Total kutipan', detail_citations tetap None."""
    html = (
        "<html><body>"
        '<div class="gs_scl"><div class="gsc_oci_field">Jurnal</div>'
        '<div class="gsc_oci_value">Tech Journal</div></div>'
        "</body></html>"
    )
    detail = parse_detail_html(html)
    assert detail["detail_citations"] is None
    # blok lain tetap terbaca normal
    assert detail["detail_venue"] == "Tech Journal"


def test_sitasi_fixture_tidak_merusak_field_lain():
    """Perbaikan sitasi tidak mengubah metadata lain di detail_1."""
    detail = parse_detail_html(_baca("detail_1.html"))
    assert detail["detail_citations"] == "3"
    assert detail["detail_title"]
    assert detail["detail_venue"]
    assert detail["detail_volume"] == "5"
    assert detail["detail_venue_label"] == "Jurnal"


# ============================================================
# 13. parse_detail_html - detail_description (utama + fallback label)
# ============================================================

def _blok_deskripsi(teks_value, *, pakai_id=False):
    """Satu blok div.gs_scl berlabel 'Deskripsi'.

    `pakai_id=False` -> value div TANPA id (inilah skenario fallback:
    selector utama #gsc_oci_descr tidak menemukan apa pun).
    """
    atribut_id = ' id="gsc_oci_descr"' if pakai_id else ""
    return (
        '<div class="gs_scl">'
        '<div class="gsc_oci_field">Deskripsi</div>'
        f'<div class="gsc_oci_value"{atribut_id}>{teks_value}</div>'
        "</div>"
    )


_ABSTRAK_UJI = "Abstrak uji. Kalimat kedua, dengan tanda koma; dan titik."


def test_deskripsi_jalur_utama_fixture():
    """Jalur utama #gsc_oci_descr tetap dipakai pada fixture."""
    nilai = parse_detail_html(_baca("detail_1.html"))["detail_description"]
    assert nilai.startswith("Radio Detection and Ranging (RADAR)")
    # teks label tidak pernah ikut ke nilai
    assert "Deskripsi" not in nilai


def test_deskripsi_fallback_label_tanpa_id():
    """Blok 'Deskripsi' tanpa id mengisi description bila utama None."""
    html = "<html><body>" + _blok_deskripsi(_ABSTRAK_UJI) + "</body></html>"
    hasil = parse_detail_html(html)["detail_description"]
    # multi-kalimat + tanda baca dipertahankan verbatim
    assert hasil == _ABSTRAK_UJI


def test_deskripsi_label_tidak_ikut_nilai():
    """Hasil persis teks value; label 'Deskripsi' tidak bocor."""
    html = "<html><body>" + _blok_deskripsi(_ABSTRAK_UJI) + "</body></html>"
    hasil = parse_detail_html(html)["detail_description"]
    assert hasil == _ABSTRAK_UJI
    assert "Deskripsi" not in hasil


def test_deskripsi_whitespace_dibersihkan():
    """Spasi/newline di sekeliling value dibuang (get_text strip)."""
    html = (
        "<html><body>"
        + _blok_deskripsi("\n    Abstrak uji bertanda spasi.   \n")
        + "</body></html>"
    )
    hasil = parse_detail_html(html)["detail_description"]
    assert hasil == "Abstrak uji bertanda spasi."
    assert hasil == hasil.strip()


def test_deskripsi_kedua_jalur_kosong_none():
    """Tanpa id dan tanpa blok Deskripsi -> None; value kosong -> None."""
    assert parse_detail_html(
        "<html><body><p>tidak ada apa pun</p></body></html>"
    )["detail_description"] is None

    html_kosong = (
        "<html><body>" + _blok_deskripsi("   ") + "</body></html>"
    )
    assert parse_detail_html(html_kosong)["detail_description"] is None


def test_deskripsi_id_utama_tidak_ditimpa_fallback():
    """#gsc_oci_descr ada -> fallback tidak menimpa (prioritas utama)."""
    html = (
        "<html><body>"
        '<div id="gsc_oci_descr">TeksUtama</div>'
        + _blok_deskripsi("TeksFallback")
        + "</body></html>"
    )
    assert parse_detail_html(html)["detail_description"] == "TeksUtama"


def test_deskripsi_fallback_tidak_mengganggu_field_lain():
    """Fallback description tidak mengubah venue/sitasi/field lain."""
    html = (
        "<html><body>"
        '<div class="gs_scl"><div class="gsc_oci_field">Jurnal</div>'
        '<div class="gsc_oci_value">Tech Journal</div></div>'
        + _blok_deskripsi("Abstrak uji dari fallback.")
        + '<div class="gs_scl"><div class="gsc_oci_field">Total kutipan</div>'
        '<div class="gsc_oci_value"><div style="margin-bottom:1em">'
        '<a href="https://scholar.google.co.id/scholar?oi=bibs&hl=id'
        '&cites=111&as_sdt=5">Dirujuk 7 kali</a>'
        "</div></div></div>"
        "</body></html>"
    )
    detail = parse_detail_html(html)
    assert detail["detail_venue"] == "Tech Journal"
    assert detail["detail_citations"] == "7"
    assert detail["detail_description"] == "Abstrak uji dari fallback."


# ============================================================
# 14. Fase detail (_ambil_detail_publikasi) - FakePage, tanpa browser
# ============================================================

class _Resp:
    """Tiruan objek respons Playwright (hanya memakai atribut .status)."""

    def __init__(self, status=200):
        self.status = status


class FakePage:
    """Tiruan page Playwright untuk fase detail TANPA browser sungguhan.

    `paket` = daftar tuple per panggilan goto, berurutan:
        (hasil_goto, hasil_selector, html[, url_akhir])
      - hasil_goto: Exception -> dilempar saat goto; int -> resp dengan
        status HTTP itu; None -> resp 200.
      - hasil_selector: Exception -> dilempar saat wait_for_selector;
        None -> normal.
      - html: nilai kembalian content().
      - url_akhir (opsional): nilai page.url setelah goto (menggambarkan
        redirect; default = url tujuan). Dipakai tes jalur sorry-page.

    Mencatat url goto & ms jitter supaya tes bisa memastikan alur
    (urutan langkah & pemanggilan jitter) benar-benar terjadi.
    """

    def __init__(self, paket=()):
        self._paket = [tuple(p) for p in paket]
        self._aktif: tuple = (None, None, "")  # diisi goto()
        self.goto_urls = []
        self.timeouts = []
        self.url = ""

    async def goto(self, url, wait_until=None, timeout=None):
        self.goto_urls.append(url)
        self._aktif = self._paket.pop(0)  # kosong -> IndexError = pelanggaran
        hasil = self._aktif[0]
        if isinstance(hasil, Exception):
            raise hasil
        # URL final setelah redirect (elemen ke-4 paket, opsional).
        self.url = self._aktif[3] if len(self._aktif) > 3 else url
        return _Resp(hasil if isinstance(hasil, int) else 200)

    async def wait_for_selector(self, selector, timeout=None):
        hasil = self._aktif[1]
        if isinstance(hasil, Exception):
            raise hasil

    async def content(self):
        return self._aktif[2]

    async def wait_for_timeout(self, ms):
        self.timeouts.append(ms)


# HTML detail normal: judul + blok Jurnal (cukup untuk parse_detail_html).
_HTML_DETAIL_OK = (
    "<html><body>"
    '<div id="gsc_oci_title">Judul Detail</div>'
    '<div class="gs_scl"><div class="gsc_oci_field">Jurnal</div>'
    '<div class="gsc_oci_value">Tech Journal</div></div>'
    "</body></html>"
)

# Marker yang dikenali detect_block().
_HTML_BLOKIR = "<html>We have detected unusual traffic from your network.</html>"


def _baris_profil(url, judul):
    """Satu baris hasil parse profil (11 kunci) dengan url detail."""
    return {
        "title": judul,
        "authors": "Penulis Uji",
        "venue": "Venue Profil",
        "extra": None,
        "citations": 5,
        "year": "2024",
        "publication_year": 2024,
        "detail_url": url,
        "volume": "1",
        "issue": "2",
        "pages": "3-4",
    }


def _jalankan_fase_detail(page, rows):
    """Jalankan fase detail sinkron lewat asyncio.run (tanpa pytest-asyncio)."""
    return asyncio.run(_scrape_mod._ambil_detail_publikasi(page, rows))


def test_fase_detail_invarian_key_setelah_block():
    """Semua row keluar dengan lengkapnya key DETAIL_KEYS (None bila gagal)."""
    rows = [
        _baris_profil("https://scholar.example/1", "Sukses"),
        _baris_profil("https://scholar.example/2", "Terblokir"),
        _baris_profil("https://scholar.example/3", "BelumDicoba"),
    ]
    page = FakePage([
        (None, None, _HTML_DETAIL_OK),
        (None, None, _HTML_BLOKIR),
        # baris ke-3 sengaja tanpa paket: kalau dicoba, goto melempar
        # IndexError dan url-nya tercatat -> ketahuan.
    ])
    partial, pesan, blocked, gagal = _jalankan_fase_detail(page, rows)

    # row sukses: nilai detail nyata
    assert rows[0]["detail_title"] == "Judul Detail"
    assert rows[0]["detail_venue"] == "Tech Journal"

    # row terblokir & belum dicoba: SEMUA key detail ada dan None
    for baris in (rows[1], rows[2]):
        assert set(DETAIL_KEYS) <= set(baris.keys())
        assert all(baris[k] is None for k in DETAIL_KEYS)

    # row ke-3 tidak pernah dicoba (block menghentikan fase)
    assert page.goto_urls == [
        "https://scholar.example/1", "https://scholar.example/2",
    ]


def test_fase_detail_url_kosong_tanpa_partial():
    """Row tanpa detail_url: entri baru + struktur None + partial False."""
    rows = [_baris_profil(None, "Tanpa URL")]
    page = FakePage([])
    partial, pesan, blocked, gagal = _jalankan_fase_detail(page, rows)

    assert partial is False          # kondisi data profil, bukan gagal proses
    assert blocked is False
    assert pesan is None
    assert page.goto_urls == []      # tidak ada navigasi
    assert all(rows[0][k] is None for k in DETAIL_KEYS)

    assert len(gagal) == 1
    entri = gagal[0]
    assert entri["reason"] == "detail_url_tidak_tersedia"
    assert entri["index"] == 0
    assert entri["raw_title"] == "Tanpa URL"
    assert entri["detail_url"] is None
    # format aditif lama tetap ada
    assert {"index", "raw_title", "reason", "detail_url"} <= set(entri)


def test_fase_detail_goto_gagal():
    """goto melempar -> detail_fetch_error + partial + pesan first-wins."""
    rows = [_baris_profil("https://scholar.example/1", "Gagal Buka")]
    page = FakePage([(RuntimeError("conn refused"), None, "")])
    partial, pesan, blocked, gagal = _jalankan_fase_detail(page, rows)

    assert partial is True
    assert blocked is False
    assert "detail gagal dibuka" in pesan
    assert "conn refused" in pesan
    assert len(gagal) == 1
    assert gagal[0]["reason"] == "detail_fetch_error"
    assert gagal[0]["index"] == 0
    assert gagal[0]["detail_url"] == "https://scholar.example/1"
    assert all(rows[0][k] is None for k in DETAIL_KEYS)


def test_fase_detail_http_500():
    """HTTP >=400 -> detail_fetch_error dengan url tercatat."""
    rows = [_baris_profil("https://scholar.example/1", "HTTP 500")]
    page = FakePage([(500, None, "")])
    partial, pesan, blocked, gagal = _jalankan_fase_detail(page, rows)

    assert partial is True
    assert "detail HTTP 500" in pesan
    assert len(gagal) == 1
    assert gagal[0]["reason"] == "detail_fetch_error"
    assert gagal[0]["detail_url"] == "https://scholar.example/1"


def test_fase_detail_http_429_hentikan_loop():
    """HTTP 429 -> gagal dicatat, loop detail BERHENTI, profil utuh.

    Baris sesudah 429 tidak boleh diminta (rate limit dihormati):
    FakePage tanpa paket tambahan akan melempar IndexError pada goto
    dan mencatat url-nya, jadi ketahuan kalau loop tetap lanjut.
    """
    rows = [
        _baris_profil("https://scholar.example/1", "Kena 429"),
        _baris_profil("https://scholar.example/2", "BelumDicoba"),
        _baris_profil("https://scholar.example/3", "BelumDicoba Juga"),
    ]
    page = FakePage([(429, None, "")])
    partial, pesan, blocked, gagal = _jalankan_fase_detail(page, rows)

    # loop BERHENTI: hanya baris pertama yang diminta
    assert page.goto_urls == ["https://scholar.example/1"]

    # kegagalan baris yang sedang diproses tetap dicatat
    assert partial is True
    assert blocked is False
    assert pesan is not None
    assert "429" in pesan
    assert len(gagal) == 1
    assert gagal[0]["reason"] == "detail_fetch_error"
    assert gagal[0]["index"] == 0
    assert gagal[0]["raw_title"] == "Kena 429"
    assert gagal[0]["detail_url"] == "https://scholar.example/1"

    # SEMUA row keluar dengan lengkapnya key DETAIL_KEYS (None),
    # metadata profil tidak disentuh
    for baris in rows:
        assert set(DETAIL_KEYS) <= set(baris.keys())
        assert all(baris[k] is None for k in DETAIL_KEYS)
        assert set(KUNCI_BARIS) <= set(baris.keys())
    assert rows[0]["title"] == "Kena 429"
    assert rows[0]["venue"] == "Venue Profil"
    assert rows[0]["citations"] == 5
    assert rows[1]["title"] == "BelumDicoba"
    assert rows[2]["title"] == "BelumDicoba Juga"


def test_fase_detail_http_500_lanjut_ke_baris_berikutnya():
    """Status HTTP lain (bukan 429) tetap LANJUT ke baris berikutnya."""
    rows = [
        _baris_profil("https://scholar.example/1", "HTTP 500"),
        _baris_profil("https://scholar.example/2", "Sukses"),
    ]
    page = FakePage([
        (500, None, ""),
        (None, None, _HTML_DETAIL_OK),
    ])
    partial, pesan, blocked, gagal = _jalankan_fase_detail(page, rows)

    # tetap lanjut: kedua baris diminta
    assert page.goto_urls == [
        "https://scholar.example/1", "https://scholar.example/2",
    ]
    assert partial is True
    assert pesan is not None
    assert "detail HTTP 500" in pesan
    assert [e["index"] for e in gagal] == [0]
    # baris kedua tetap berhasil diproses
    assert rows[1]["detail_title"] == "Judul Detail"


def test_status_run_partial_success_setelah_429():
    """Profil ada + fase detail berhenti karena 429 -> PARTIAL_SUCCESS.

    Menjalankan jalur 429 yang sebenarnya lalu menyusun status lewat
    _classify_status dengan semantik penggabungan persis seperti di
    scrape_scholar: `partial = partial or hasil_partial`,
    `blocked = blocked or hasil_blocked` (kedua flag awal False karena
    paging profil lancar).
    """
    rows = [
        _baris_profil("https://scholar.example/1", "Kena 429"),
        _baris_profil("https://scholar.example/2", "BelumDicoba"),
    ]
    page = FakePage([(429, None, "")])
    partial, pesan, blocked, gagal = _jalankan_fase_detail(page, rows)

    status = _scrape_mod._classify_status(
        has_data=bool(rows),
        blocked=False or blocked,
        network_error=False,
        parse_error=False,
        expected_empty=False,
        partial=False or partial,
    )
    assert status == "PARTIAL_SUCCESS"


def test_url_blokir_kasus_dasar():
    """_url_blokir: sorry-page Google dikenali; varian lain tidak."""
    # positif: host Google + path /sorry/
    assert _scrape_mod._url_blokir(
        "https://www.google.com/sorry/index?continue=x") is True
    assert _scrape_mod._url_blokir(
        "https://google.co.id/sorry/index") is True
    assert _scrape_mod._url_blokir(
        "https://scholar.google.co.id/sorry/") is True
    # negatif: URL publikasi Scholar normal
    assert _scrape_mod._url_blokir(
        "https://scholar.google.co.id/citations?view_op=view_citation"
        "&user=xyz") is False
    # negatif: host lain dengan path serupa tidak boleh salah deteksi
    assert _scrape_mod._url_blokir("https://example.com/sorry/") is False
    assert _scrape_mod._url_blokir("https://notgoogle.com/sorry/") is False
    # negatif: kasus kosong
    assert _scrape_mod._url_blokir("") is False
    assert _scrape_mod._url_blokir(None) is False


def test_fase_detail_sorry_page_hentikan_loop():
    """Redirect sorry-page (URL final) -> detail_blocked + break.

    Varian sorry-page run 160 tidak memuat marker HTML, jadi deteksi
    mengandalkan URL final. Metadata baris sukses sebelumnya tetap
    utuh; baris sesudahnya tidak diminta (tanpa retry).
    """
    url_sorry = ("https://www.google.com/sorry/index?continue="
                 "https%3A%2F%2Fscholar.google.co.id%2Fcitations")
    rows = [
        _baris_profil("https://scholar.example/1", "Sukses"),
        _baris_profil("https://scholar.example/2", "Kena Sorry"),
        _baris_profil("https://scholar.example/3", "BelumDicoba"),
    ]
    page = FakePage([
        (None, None, _HTML_DETAIL_OK, "https://scholar.example/1"),
        # URL sorry: wait_for_selector & content tidak pernah dipakai
        (None, None, "", url_sorry),
        # baris ke-3 sengaja tanpa paket: kalau dicoba, IndexError.
    ])
    partial, pesan, blocked, gagal = _jalankan_fase_detail(page, rows)

    # berhenti setelah baris ke-2: baris ke-3 tidak diminta
    assert page.goto_urls == [
        "https://scholar.example/1", "https://scholar.example/2",
    ]

    # baris sukses pertama dipertahankan (invarian PERTAHANAN)
    assert rows[0]["detail_title"] == "Judul Detail"
    assert rows[0]["detail_venue"] == "Tech Journal"

    # kontrak entri & flag - sama seperti blok captcha
    assert partial is True
    assert blocked is True
    assert pesan is not None
    assert "sorry" in pesan.lower()
    assert "429" not in pesan
    assert len(gagal) == 1
    assert gagal[0]["reason"] == "detail_blocked"
    assert gagal[0]["index"] == 1
    assert gagal[0]["raw_title"] == "Kena Sorry"
    assert gagal[0]["detail_url"] == "https://scholar.example/2"

    # baris terblokir & belum dicoba: struktur None utuh
    for baris in (rows[1], rows[2]):
        assert set(DETAIL_KEYS) <= set(baris.keys())
        assert all(baris[k] is None for k in DETAIL_KEYS)


def test_fase_detail_429_dahulukan_atas_sorry():
    """HTTP 429 + URL sorry: jalur 429 lama menang, sorry tidak menimpa."""
    url_sorry = "https://www.google.com/sorry/index?continue=x"
    rows = [_baris_profil("https://scholar.example/1", "Kena 429")]
    page = FakePage([(429, None, "", url_sorry)])
    partial, pesan, blocked, gagal = _jalankan_fase_detail(page, rows)

    # semantik 429 P29 dipertahankan persis
    assert page.goto_urls == ["https://scholar.example/1"]
    assert blocked is False  # 429 bukan captcha/blok klasik
    assert partial is True
    assert pesan is not None
    assert "429" in pesan
    assert "sorry" not in pesan.lower()
    assert len(gagal) == 1
    assert gagal[0]["reason"] == "detail_fetch_error"
    assert all(rows[0][k] is None for k in DETAIL_KEYS)


def test_fase_detail_timeout_selector_url_sorry():
    """Jalur sekunder: timeout selector + URL sorry -> detail_blocked.

    Varian HTML tanpa marker captcha (pola run 160) tetap terdeteksi
    lewat URL final pada blok except wait_for_selector; pesan sorry,
    bukan captcha.
    """
    url_sorry = "https://www.google.com/sorry/index?continue=x"
    rows = [_baris_profil("https://scholar.example/1", "Timeout Sorry")]
    timeout = TimeoutError("Timeout 20000ms exceeded")
    page = FakePage([(None, timeout, _HTML_DETAIL_OK, url_sorry)])
    partial, pesan, blocked, gagal = _jalankan_fase_detail(page, rows)

    assert blocked is True
    assert partial is True
    assert pesan is not None
    assert "captcha" not in pesan
    assert "sorry" in pesan.lower()
    assert len(gagal) == 1
    assert gagal[0]["reason"] == "detail_blocked"
    assert all(rows[0][k] is None for k in DETAIL_KEYS)


# ============================================================
# FASE DAFTAR: DETEKSI SORRY-PAGE (P49)
# ============================================================
# scrape_scholar mengimpor async_playwright DI DALAM fungsinya, jadi
# fake dipasang lewat monkeypatch pada modul playwright.async_api.
# Cakupan fake: goto, status resp, url final, wait_for_selector,
# content, locator #gsc_bpf_more, wait_for_timeout, browser.close.

_URL_SORRY = (
    "https://www.google.com/sorry/index?continue="
    "https%3A%2F%2Fscholar.google.co.id%2Fcitations%3Fuser%3DUJI"
)

# HTML sorry-style tanpa satupun marker detect_block (pola run 160):
# TANPA "unusual traffic", "not a robot", id="captcha", "sorry/index",
# "detected unusual".
_HTML_LISTING_BERSIH = (
    "<html><body><p>Halaman netral tanpa marker blokir.</p></body></html>"
)


class _FakeRespList:
    def __init__(self, status=200):
        self.status = status


class _FakeMorePaging:
    """Locator #gsc_bpf_more tiruan (tampil / nonaktif / klik).

    Opsional: `url_setelah_klik` meniru navigasi yang terjadi saat klik
    (dipakai tes sorry-page saat paging); halaman dirangkai otomatis
    oleh _FakePageList.
    """

    def __init__(self, tampil=True, disabled=False, klik_error=None,
                 url_setelah_klik=None):
        self._tampil = tampil
        self._disabled = disabled
        self._klik_error = klik_error
        self.klik_dipanggil = 0
        self.page: "_FakePageList | None" = None
        self.url_setelah_klik: "str | None" = url_setelah_klik

    async def is_visible(self):
        return self._tampil

    async def get_attribute(self, nama):
        if nama == "disabled" and self._disabled:
            return ""
        return None

    async def click(self):
        if self._klik_error is not None:
            raise self._klik_error
        self.klik_dipanggil += 1
        if self.url_setelah_klik is not None and self.page is not None:
            self.page.url = self.url_setelah_klik


class _FakePageList:
    """Halaman tiruan untuk scrape_scholar (fase daftar).

    - url_akhir: nilai page.url setelah goto sukses (default = tujuan).
    - url_saat_selector: page.url di-set tepat saat wait_for_selector
      berjalan (meniru redirect yang selesai di tengah wait).
    - daftar_html: antrean nilai content(); kehabisan -> AssertionError.
    """

    def __init__(self, *, url_akhir=None, goto_error=None,
                 goto_status=200, selector_error=None,
                 url_saat_selector=None, daftar_html=(), more=None):
        self.url = ""
        self.goto_urls = []
        self._url_akhir = url_akhir
        self._goto_error = goto_error
        self._goto_status = goto_status
        self._selector_error = selector_error
        self._url_saat_selector = url_saat_selector
        self._daftar_html = list(daftar_html)
        self._more = more if more is not None else _FakeMorePaging(
            tampil=False,
        )
        self._more.page = self  # rangkai untuk url_setelah_klik
        self.timeouts = []

    async def goto(self, url, wait_until=None, timeout=None):
        self.goto_urls.append(url)
        if self._goto_error is not None:
            raise self._goto_error
        self.url = self._url_akhir if self._url_akhir is not None else url
        return _FakeRespList(self._goto_status)

    async def wait_for_selector(self, selector, timeout=None):
        if self._url_saat_selector is not None:
            self.url = self._url_saat_selector
        if self._selector_error is not None:
            raise self._selector_error

    async def content(self):
        if not self._daftar_html:
            raise AssertionError("content() dipanggil melebihi antrean fake")
        return self._daftar_html.pop(0)

    def locator(self, selector):
        return self._more

    async def wait_for_timeout(self, ms):
        self.timeouts.append(ms)


class _FakeContextList:
    def __init__(self, page):
        self._page = page

    async def new_page(self):
        return self._page


class _FakeBrowserList:
    def __init__(self, page):
        self._page = page
        self.close_dipanggil = 0

    async def new_context(self, **kwargs):
        return _FakeContextList(self._page)

    async def close(self):
        self.close_dipanggil += 1


class _FakePlaywrightList:
    def __init__(self, browser):
        class _Chromium:
            async def launch(self, **kwargs):
                return browser

        self.chromium = _Chromium()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


async def _jalankan_listing(monkeypatch, page):
    """Jalankan scrape_scholar offline (tanpa browser, tanpa jaringan)."""
    import playwright.async_api as _async_api

    browser = _FakeBrowserList(page)
    monkeypatch.setattr(
        _async_api, "async_playwright", lambda: _FakePlaywrightList(browser),
    )
    envelope = await _scrape_mod.scrape_scholar(
        "UJI", headless=True, verbose=False, ambil_detail=False,
    )
    return envelope, browser


def test_classify_status_tanpa_data_blocked():
    """blocked tanpa data -> BLOCKED, bukan TIMEOUT."""
    assert _scrape_mod._classify_status(
        has_data=False, blocked=True, network_error=False,
        parse_error=False, expected_empty=False, partial=False,
    ) == "BLOCKED"


def test_listing_sorry_url_awal_hasilkan_blocked(monkeypatch):
    """goto berstatus 200 dengan URL akhir sorry -> BLOCKED, tanpa 429.

    Varian nyata run 160: sorry-page umumnya berstatus 200 sehingga
    cek status HTTP tidak terpicu; deteksi mengandalkan URL final.
    """
    page = _FakePageList(url_akhir=_URL_SORRY)
    envelope, browser = asyncio.run(_jalankan_listing(monkeypatch, page))

    assert envelope["status"] == "BLOCKED"
    assert envelope["records_fetched"] == 0
    assert envelope["data"] is None  # kontrak: run fatal tanpa data
    msg = envelope["error_message"] or ""
    assert "sorry" in msg.lower()
    assert "429" not in msg
    # cleanup browser mengikuti pola yang sudah ada; tanpa retry
    assert browser.close_dipanggil >= 1
    assert page.goto_urls == [_scrape_mod.build_url("UJI")]


def test_listing_sorry_timeout_selector_bukan_timeout_status(monkeypatch):
    """Selector timeout + HTML bersih + URL sorry -> BLOCKED, bukan TIMEOUT.

    Jalur sekunder: URL normal saat cek status, redirect selesai di
    tengah wait_for_selector; HTML tanpa marker (pola run 160).
    """
    page = _FakePageList(
        url_saat_selector=_URL_SORRY,
        selector_error=TimeoutError("Timeout 20000ms exceeded"),
        daftar_html=[_HTML_LISTING_BERSIH],
    )
    envelope, browser = asyncio.run(_jalankan_listing(monkeypatch, page))

    assert envelope["status"] == "BLOCKED"
    msg = envelope["error_message"] or ""
    assert "sorry" in msg.lower()
    assert "tabel #gsc_a_b tidak muncul" not in msg
    assert "429" not in msg
    assert browser.close_dipanggil >= 1


def test_listing_sorry_saat_paging_pertahankan_data_batch(monkeypatch):
    """Sorry-page setelah klik 'Tampilkan lainnya' -> PARTIAL_SUCCESS.

    Data batch pertama tetap utuh dan blocked aktif (terlihat dari
    loop berhenti setelah satu klik); tanpa klaim HTTP 429.
    """
    more = _FakeMorePaging(tampil=True, url_setelah_klik=_URL_SORRY)
    page = _FakePageList(
        daftar_html=[
            _html_profil_sintetis("Venue Uji"),
            _HTML_LISTING_BERSIH,
        ],
        more=more,
    )

    envelope, _ = asyncio.run(_jalankan_listing(monkeypatch, page))

    assert envelope["status"] == "PARTIAL_SUCCESS"
    assert envelope["records_fetched"] == 1  # data batch 1 dipertahankan
    assert envelope["data"]["rows"][0]["title"] == "Judul Uji"
    msg = envelope["error_message"] or ""
    assert "sorry" in msg.lower()
    assert "429" not in msg
    # loop berhenti tepat setelah satu klik (tidak lanjut batch berikut)
    assert more.klik_dipanggil == 1


def test_listing_normal_tetap_success(monkeypatch):
    """Profil normal tanpa blok -> SUCCESS, data utuh."""
    page = _FakePageList(
        daftar_html=[_html_profil_sintetis("Venue Normal")],
        more=_FakeMorePaging(tampil=False),  # tanpa tombol -> selesai
    )
    envelope, _ = asyncio.run(_jalankan_listing(monkeypatch, page))

    assert envelope["status"] == "SUCCESS"
    assert envelope["records_fetched"] == 1
    assert envelope["error_message"] is None
    assert envelope["data"]["rows"][0]["title"] == "Judul Uji"


def test_listing_http_429_teramati_perilaku_lama(monkeypatch):
    """429 yang benar-benar teramati: perilaku lama dipertahankan.

    Fatal tanpa data -> TIMEOUT dengan pesan "HTTP 429 dari server";
    pesan TIDAK diubah menjadi sorry-page.
    """
    page = _FakePageList(goto_status=429)
    envelope, _ = asyncio.run(_jalankan_listing(monkeypatch, page))

    assert envelope["status"] == "TIMEOUT"
    msg = envelope["error_message"] or ""
    assert "429" in msg
    assert "sorry" not in msg.lower()
    assert envelope["data"] is None


def test_fase_detail_parse_gagal(monkeypatch):
    """Parse exception -> detail_parse_error (dibedakan dari fetch)."""
    def _melempar(_html):
        raise ValueError("html rusak")

    monkeypatch.setattr(_scrape_mod, "parse_detail_html", _melempar)
    rows = [_baris_profil("https://scholar.example/1", "Parse Gagal")]
    page = FakePage([(None, None, "<html>normal</html>")])
    partial, pesan, blocked, gagal = _jalankan_fase_detail(page, rows)

    assert partial is True
    assert "detail parse gagal" in pesan
    assert "html rusak" in pesan
    assert len(gagal) == 1
    assert gagal[0]["reason"] == "detail_parse_error"
    assert gagal[0]["detail_url"] == "https://scholar.example/1"
    assert all(rows[0][k] is None for k in DETAIL_KEYS)


def test_fase_detail_block_entri_tunggal_dan_status():
    """Block di tengah: blocked+partial, TEPAT satu entri detail_blocked."""
    rows = [
        _baris_profil("https://scholar.example/1", "Sukses"),
        _baris_profil("https://scholar.example/2", "Terblokir"),
        _baris_profil("https://scholar.example/3", "BelumDicoba"),
    ]
    page = FakePage([
        (None, None, _HTML_DETAIL_OK),
        (None, None, _HTML_BLOKIR),
    ])
    partial, pesan, blocked, gagal = _jalankan_fase_detail(page, rows)

    assert blocked is True
    assert partial is True
    assert "captcha" in pesan

    assert len(gagal) == 1  # TEPAT satu: row yang terblokir saja
    entri = gagal[0]
    assert entri["reason"] == "detail_blocked"
    assert entri["index"] == 1
    assert entri["raw_title"] == "Terblokir"
    assert entri["detail_url"] == "https://scholar.example/2"
    # row sesudahnya TIDAK diberi entri (belum dicoba)
    assert all(e["index"] != 2 for e in gagal)


def test_fase_detail_block_saat_selector_timeout():
    """Jalur block lewat wait_for_selector timeout juga mencatat dirinya."""
    rows = [
        _baris_profil("https://scholar.example/1", "Terblokir"),
        _baris_profil("https://scholar.example/2", "BelumDicoba"),
    ]
    page = FakePage([
        (None, TimeoutError("selector habis waktu"), _HTML_BLOKIR),
    ])
    partial, pesan, blocked, gagal = _jalankan_fase_detail(page, rows)

    assert blocked is True
    assert partial is True
    assert "captcha" in pesan
    assert [e["reason"] for e in gagal] == ["detail_blocked"]
    assert gagal[0]["index"] == 0
    assert all(rows[1][k] is None for k in DETAIL_KEYS)
    assert len(page.goto_urls) == 1


def test_fase_detail_selector_timeout_non_block():
    """Timeout selector tanpa block -> detail_fetch_error, lanjut."""
    rows = [_baris_profil("https://scholar.example/1", "Timeout")]
    page = FakePage([(None, TimeoutError("selector habis"), "<html>oke</html>")])
    partial, pesan, blocked, gagal = _jalankan_fase_detail(page, rows)

    assert partial is True
    assert blocked is False
    assert "detail tidak termuat" in pesan
    assert gagal[0]["reason"] == "detail_fetch_error"
    assert gagal[0]["detail_url"] == "https://scholar.example/1"
    assert all(rows[0][k] is None for k in DETAIL_KEYS)


def test_fase_detail_sukses_penuh():
    """Semua row sukses: failures kosong, partial/blocked False, jitter jalan."""
    rows = [
        _baris_profil("https://scholar.example/1", "A"),
        _baris_profil("https://scholar.example/2", "B"),
    ]
    page = FakePage([
        (None, None, _HTML_DETAIL_OK),
        (None, None, _HTML_DETAIL_OK),
    ])
    partial, pesan, blocked, gagal = _jalankan_fase_detail(page, rows)

    assert gagal == []
    assert partial is False
    assert blocked is False
    assert pesan is None
    for baris in rows:
        assert baris["detail_title"] == "Judul Detail"
        assert baris["detail_venue"] == "Tech Journal"
    # jitter wait dipanggil sekali per baris sukses
    assert len(page.timeouts) == 2


def test_fase_detail_profil_tidak_berubah():
    """Semua jalur (sukses/gagal/url-kosong/belum-dicoba) profil utuh."""
    rows = [
        _baris_profil("https://scholar.example/1", "Sukses"),
        _baris_profil(None, "Tanpa URL"),
        _baris_profil("https://scholar.example/3", "Gagal Buka"),
        _baris_profil("https://scholar.example/4", "Terblokir"),
        _baris_profil("https://scholar.example/5", "BelumDicoba"),
    ]
    salinan = [{k: baris[k] for k in KUNCI_BARIS} for baris in rows]
    page = FakePage([
        (None, None, _HTML_DETAIL_OK),
        (RuntimeError("conn refused"), None, ""),
        (None, None, _HTML_BLOKIR),
    ])
    _jalankan_fase_detail(page, rows)

    for baris, asli in zip(rows, salinan):
        for kunci in KUNCI_BARIS:  # 11 kunci profil tidak tersentuh
            assert baris[kunci] == asli[kunci]


# ============================================================
# 15. parse_detail_html - label metadata alternatif (bilingual)
# ============================================================

def _blok_label(label, value):
    """Satu blok minimal div.gs_scl (label div.gsc_oci_field + value)."""
    return (
        '<div class="gs_scl">'
        f'<div class="gsc_oci_field">{label}</div>'
        f'<div class="gsc_oci_value">{value}</div>'
        "</div>"
    )


def _html_blok(*blok):
    return "<html><body>" + "".join(blok) + "</body></html>"


# Value sitasi ala fixture: anchor 'Dirujuk 5 kali' (href cites=, tanpa
# class) + histogram wrapper dgn anchor cites= lain berangka "7".
_VALUE_SITASI = (
    '<div style="margin-bottom:1em">'
    '<a href="https://scholar.google.co.id/scholar?oi=bibs&hl=id'
    '&cites=9&as_sdt=5">Dirujuk 5 kali</a></div>'
    '<div id="gsc_oci_graph_wrapper">'
    '<span class="gsc_oci_g_t">2020</span>'
    '<a class="gsc_oci_g_a" href="https://scholar.google.co.id/scholar'
    '?cites=9"><span class="gsc_oci_g_al">7</span></a>'
    '<span class="gsc_oci_g_t">2021</span></div>'
)


def _semua_detail_none_kecuali(detail, kunci):
    """Helper: semua DETAIL_KEYS None kecuali `kunci` (dan venue_label)."""
    for k in DETAIL_KEYS:
        if k in (kunci, "detail_venue_label"):
            continue
        assert detail[k] is None, f"{k} seharusnya None, dapat {detail[k]!r}"


@pytest.mark.parametrize("label, value, kunci, nilai", [
    ("Pengarang", "Budi; Ani", "detail_authors", "Budi; Ani"),
    ("Tanggal terbit", "2024/12/31", "detail_date", "2024/12/31"),
    ("Jurnal", "Tech Journal", "detail_venue", "Tech Journal"),
    ("Konferensi", "IEEE Conf", "detail_venue", "IEEE Conf"),
    ("Jilid", "5", "detail_volume", "5"),
    ("Terbitan", "2", "detail_issue", "2"),
    ("Halaman", "10-20", "detail_pages", "10-20"),
    ("Penerbit", "IEEE", "detail_publisher", "IEEE"),
    ("Total kutipan", _VALUE_SITASI, "detail_citations", "5"),
])
def test_label_indonesia_lama_masih_terpetakan(label, value, kunci, nilai):
    """Regresi: 9 label Indonesia fixture-proven tetap terpetakan benar."""
    detail = parse_detail_html(_html_blok(_blok_label(label, value)))
    assert detail[kunci] == nilai
    _semua_detail_none_kecuali(detail, kunci)
    if kunci == "detail_venue":
        assert detail["detail_venue_label"] == label
    else:
        assert detail["detail_venue_label"] is None


def test_label_deskripsi_indonesia_lama_masih_fallback():
    """Regresi: label 'Deskripsi' lama tetap terisi lewat fallback P6."""
    detail = parse_detail_html(_html_blok(_blok_label("Deskripsi", "Abstrak lama.")))
    assert detail["detail_description"] == "Abstrak lama."


@pytest.mark.parametrize("label, kunci, nilai", [
    ("penulis", "detail_authors", "Siti"),
    ("author(s)", "detail_authors", "Siti"),
    ("publication date", "detail_date", "2024/01/01"),
    ("date", "detail_date", "2024/01/01"),
    ("journal", "detail_venue", "J1"),
    ("conference", "detail_venue", "C1"),
    ("venue", "detail_venue", "V1"),
    ("volume", "detail_volume", "7"),
    ("vol", "detail_volume", "7"),
    ("issue", "detail_issue", "3"),
    ("nomor", "detail_issue", "3"),
    ("pages", "detail_pages", "1-2"),
    ("page", "detail_pages", "1-2"),
    ("page range", "detail_pages", "1-2"),
    ("publisher", "detail_publisher", "ACME"),
])
def test_alias_baru_terpetakan_ke_field_tepat(label, kunci, nilai):
    """Setiap alias baru -> field benar; field lain tetap None."""
    detail = parse_detail_html(_html_blok(_blok_label(label, nilai)))
    assert detail[kunci] == nilai
    _semua_detail_none_kecuali(detail, kunci)
    if label in ("journal", "conference"):
        assert detail["detail_venue_label"] == label
    else:
        assert detail["detail_venue_label"] is None


@pytest.mark.parametrize("label", [
    "  JURNAL  ", "Jurnal\xa0", "\xa0Jurnal", "\tjUrNaL\t",
])
def test_label_venue_kapitalisasi_dan_whitespace(label):
    """Kapitalisasi + whitespace luar/interior/NBSP tetap cocok."""
    detail = parse_detail_html(_html_blok(_blok_label(label, "Tech Journal")))
    assert detail["detail_venue"] == "Tech Journal"
    assert detail["detail_venue_label"] == label.strip()


def test_label_total_kutipan_dengan_tab():
    """'Total\\tkutipan' (tab interior) tetap cocok -> jalur sitasi P3."""
    detail = parse_detail_html(_html_blok(_blok_label("Total\tkutipan", _VALUE_SITASI)))
    assert detail["detail_citations"] == "5"


@pytest.mark.parametrize("label", ["Sumber", "ISBN"])
def test_label_tidak_dikenal_semua_none(label):
    """Label di luar LABEL_DETAIL -> semua field None (tanpa menebak)."""
    detail = parse_detail_html(_html_blok(_blok_label(label, "X")))
    for k in DETAIL_KEYS:
        assert detail[k] is None, f"{k} seharusnya None"


@pytest.mark.parametrize("label", ["Book", "Series", "Number"])
def test_label_ditolak_semua_none(label):
    """Book/Series/Number sengaja tidak dipetakan (lihat docstring)."""
    detail = parse_detail_html(_html_blok(_blok_label(label, "X")))
    for k in DETAIL_KEYS:
        assert detail[k] is None, f"{k} seharusnya None"


def test_alias_cited_by_lewat_jalur_p3():
    """'Cited by' memakai ekstraktor P3: angka anchor, bukan histogram."""
    detail = parse_detail_html(_html_blok(_blok_label("Cited by", _VALUE_SITASI)))
    assert detail["detail_citations"] == "5"  # bukan "7" (histogram), bukan teks
    # 'Total kutipan' tetap sama persis jalurnya
    detail2 = parse_detail_html(_html_blok(_blok_label("Total kutipan", _VALUE_SITASI)))
    assert detail2["detail_citations"] == "5"


def test_deskripsi_utama_menang_di_atas_abstract():
    """#gsc_oci_descr tetap prioritas walau blok 'Abstract' ada."""
    html = (
        "<html><body>"
        '<div id="gsc_oci_descr">TeksUtama</div>'
        + _blok_label("Abstract", "FallbackAbstract")
        + "</body></html>"
    )
    assert parse_detail_html(html)["detail_description"] == "TeksUtama"


@pytest.mark.parametrize("label, nilai", [
    ("Abstract", "Cats are fluffy."),
    ("Abstrak", "Kucing itu lucu."),
    ("Description", "Teks penjelas uji."),
    ("Ringkasan", "Inti dari makalah ini."),
])
def test_deskripsi_fallback_alias_bahasa(label, nilai):
    """Fallback P6 menerima alias Inggris/alternatif; label tak bocor."""
    detail = parse_detail_html(_html_blok(_blok_label(label, nilai)))
    assert detail["detail_description"] == nilai
    assert label not in detail["detail_description"]


def test_deskripsi_dua_blok_fallback_pertama_menang():
    """Dua blok fallback -> blok PERTAMA yang menang."""
    html = _html_blok(
        _blok_label("Abstrak", "Pertama."),
        _blok_label("Abstract", "Kedua."),
    )
    assert parse_detail_html(html)["detail_description"] == "Pertama."


def test_venue_label_kategori_bilingual():
    """Journal -> venue+label; Venue -> venue saja; Book/Series -> kosong."""
    detail = parse_detail_html(_html_blok(_blok_label("Journal", "Tech Journal")))
    assert detail["detail_venue"] == "Tech Journal"
    assert detail["detail_venue_label"] == "Journal"

    detail = parse_detail_html(_html_blok(_blok_label("Venue", "Grand Ballroom")))
    assert detail["detail_venue"] == "Grand Ballroom"
    assert detail["detail_venue_label"] is None

    for label in ("Book", "Series"):
        detail = parse_detail_html(_html_blok(_blok_label(label, "X")))
        assert detail["detail_venue"] is None
        assert detail["detail_venue_label"] is None


@pytest.mark.parametrize("label, kunci", [
    ("Journal", "detail_venue"),
    ("Penerbit", "detail_publisher"),
    ("volume", "detail_volume"),
])
def test_value_kosong_field_tetap_none(label, kunci):
    """Value div kosong -> field tetap None (bukan string kosong)."""
    detail = parse_detail_html(_html_blok(_blok_label(label, "")))
    assert detail[kunci] is None


def test_label_detail_tanpa_duplikat_lintas_field():
    """LABEL_DETAIL: satu sumber kebenaran, tanpa alias ganda."""
    label_detail = _scrape_mod.LABEL_DETAIL
    semua = []
    for kunci, daftar in label_detail.items():
        assert kunci in DETAIL_KEYS, kunci
        assert daftar, f"{kunci} tanpa alias"
        # setiap alias sudah dalam bentuk ternormalisasi
        for alias in daftar:
            assert " ".join(alias.split()).lower() == alias, alias
        semua.extend(daftar)
    # tanpa duplikat LINTAS field (dua field tidak boleh klaim sama)
    assert len(semua) == len(set(semua)), "alias ganda antar field"
    # konstanta keputusan ada dan konsisten dengan LABEL_DETAIL
    assert set(_scrape_mod.LABEL_VENUE_TIPE) <= set(label_detail["detail_venue"])
    assert "deskripsi" not in semua  # fallback deskripsi di luar pemetaan


# ============================================================
# 16. Penguatan audit P10 - jalur DOI (tanpa mengubah produksi)
# ============================================================

def test_ekstrak_doi_pola_hanya_bentuk_huruf_kecil_10():
    """Guard & regex HANYA bentuk literal '10.' (dua digit nol).

    Tautan DOI nyata selalu memakai bentuk lowercase '10.'; varian
    huruf-O ('1O.') bukan DOI dan tidak didukung. Regex TIDAK diubah
    (tidak case-insensitive) - tes ini mendokumentasikan keputusan.
    """
    assert _ekstrak_doi(
        "https://example.org/doi/1O.1145/3055635", None) == (None, None)
    doi, _ = _ekstrak_doi("https://example.org/doi/10.1145/3055635", None)
    assert doi == "10.1145/3055635"


def test_ekstrak_doi_pertama_menang_antara_dua_kandidat():
    """.search = match PERTAMA; kandidat kedua diabaikan.

    Spasi memisahkan dua kandidat sehingga suffix match pertama berhenti
    sebelum kandidat kedua. Tanpa pemisah (suffix melarut kandidat
    kedua), hasilnya over-capture terdokumentasi: SATU match yang dimulai
    dari kandidat PERTAMA.
    """
    doi, konflik = _ekstrak_doi(
        "https://example.org/10.1111/aaaa 10.2222/bbbb", None)
    assert doi == "10.1111/aaaa"  # kandidat kedua tidak dipakai
    assert konflik is None

    doi2, _ = _ekstrak_doi(
        "https://example.org/10.1111/aaaa-10.2222/bbbb", None)
    assert doi2 == "10.1111/aaaa-10.2222/bbbb"  # over-capture, 1 match


def test_ekstrak_doi_suffix_banyak_slash_diterima_utuh():
    """Suffix mengandung slash ('10.x/a/b/c') diterima utuh - pola
    memperbolehkan '/' di bagian suffix."""
    doi, konflik = _ekstrak_doi("https://example.org/10.1000/a/b/c", None)
    assert doi == "10.1000/a/b/c"
    assert konflik is None


# HTML detail dengan link ber-DOI untuk uji jalur sukses FakePage.
_HTML_DETAIL_DOI_P10 = (
    "<html><body>"
    '<div id="gsc_oci_title">Judul P10</div>'
    '<a class="gsc_oci_title_link" '
    'href="https://doi.org/10.5555/p10.0001">sumber</a>'
    '<div class="gs_scl"><div class="gsc_oci_field">Jurnal</div>'
    '<div class="gsc_oci_value">Tech Journal</div></div>'
    "</body></html>"
)


def test_fase_detail_sukses_mempertahankan_detail_doi():
    """Jalur sukses FakePage: pre-fill detail_doi=None tertimpa DOI
    hasil parse (row.update(detail)); kontrak P7 lain tetap; row tanpa
    URL tetap detail_doi=None."""
    rows = [
        _baris_profil("https://scholar.example/1", "Sukses DOI"),
        _baris_profil(None, "Tanpa URL"),
    ]
    page = FakePage([(None, None, _HTML_DETAIL_DOI_P10)])
    partial, pesan, blocked, gagal = _jalankan_fase_detail(page, rows)

    # row sukses: DOI terisi dari href title link, key P7 tetap
    assert rows[0]["detail_doi"] == "10.5555/p10.0001"
    assert rows[0]["detail_link"] == "https://doi.org/10.5555/p10.0001"
    assert rows[0]["detail_title"] == "Judul P10"
    assert rows[0]["detail_venue"] == "Tech Journal"
    assert partial is False
    assert pesan is None
    assert blocked is False

    # row tanpa URL: tetap None (pre-fill) + entri failure P7
    assert rows[1]["detail_doi"] is None
    assert len(gagal) == 1
    assert gagal[0]["reason"] == "detail_url_tidak_tersedia"


# ============================================================
# 17. P11 — query string & batas over-capture
# Dukungan aman DOI di query string (aturan baru P11 di
# _doi_dari_url): potong di '&' pertama, first-match, unquote,
# prioritas path di atas query, fragment TETAP ditolak, ekstensi
# berkas TIDAK dipotong (konsisten dengan path).
# ============================================================

def test_ekstrak_doi_query_dengan_parameter_lain():
    """?page=2&doi=10.1000/abc&lang=id -> '10.1000/abc'.

    Dipotong di '&' pertama: '&lang=id' TIDAK ikut terseret ke suffix.
    """
    assert _scrape_mod._doi_dari_url(
        "https://example.org/redirect?page=2&doi=10.1000/abc&lang=id"
    ) == "10.1000/abc"


def test_ekstrak_doi_query_over_capture_dipotong_di_ampersand():
    """Proteksi over-capture: '10.x/y&return=1' -> '10.x/y'."""
    assert _scrape_mod._doi_dari_url(
        "https://x.org/r?doi=10.1000/abc&return=1") == "10.1000/abc"


@pytest.mark.parametrize("url", [
    "https://example.org/redirect?next=%2Fdoi%2F10.1000%2Fabc",
    "https://example.org/redirect?doi=10.1000%2Fabc",
])
def test_ekstrak_doi_query_percent_encoded_terdecode(url):
    """Percent-encode di query ter-decode dulu, lalu pattern dicari."""
    assert _scrape_mod._doi_dari_url(url) == "10.1000/abc"


def test_ekstrak_doi_prioritas_path_di_atas_query():
    """Path BER-DOI + query berisi DOI BEDA -> DOI path menang; query
    diabaikan tanpa konflik (prioritas path aturan P11)."""
    doi, konflik = _ekstrak_doi(
        "https://example.org/10.1111/aaaa?doi=10.2222/bbbb", None)
    assert doi == "10.1111/aaaa"
    assert konflik is None
    # _doi_dari_url juga menghormati prioritas path
    assert _scrape_mod._doi_dari_url(
        "https://example.org/10.1111/aaaa?doi=10.2222/bbbb") == "10.1111/aaaa"


@pytest.mark.parametrize("url", [
    "https://example.org/redirect?page=2&lang=id",  # query tanpa DOI
    "https://example.org/artikel/123",              # URL polos tanpa '10.'
])
def test_ekstrak_doi_query_tanpa_doi_none(url):
    """Query tanpa DOI / URL polos -> None (tanpa kandidat)."""
    assert _scrape_mod._doi_dari_url(url) is None
    assert _ekstrak_doi(url, None) == (None, None)


def test_ekstrak_doi_dua_kandidat_di_query_pertama_menang():
    """Dua kandidat DOI dalam query -> kandidat PERTAMA (first-match,
    konsisten dengan path; didokumentasikan)."""
    assert _scrape_mod._doi_dari_url(
        "https://x.org/r?u=10.1111/aaaa&doi=10.2222/bbbb") == "10.1111/aaaa"


def test_ekstrak_doi_fragment_ditolak():
    """#fragment TIDAK didukung: 'https://x.org/page#doi=10.1000/abc'
    -> None (fragment dibuang sebelum pencarian; aturan P11)."""
    assert _scrape_mod._doi_dari_url(
        "https://x.org/page#doi=10.1000/abc") is None
    assert _ekstrak_doi(
        "https://x.org/page#doi=10.1000/abc", None) == (None, None)


def test_ekstrak_doi_ekstensi_di_query_over_capture_dipertahankan():
    """'?doi=10.1000/abc.pdf' -> '10.1000/abc.pdf' TANPA pemotongan
    ekstensi berkas - konsisten dengan jalur path (batasan yang
    terdokumentasi; membedakan berkas dari suffix DOI sah tidak andal)."""
    assert _scrape_mod._doi_dari_url(
        "https://example.org/r?doi=10.1000/abc.pdf") == "10.1000/abc.pdf"


def test_ekstrak_doi_dua_sumber_query_identik_satu_doi():
    """Kedua URL query-DOI identik -> satu DOI, tanpa konflik."""
    doi, konflik = _ekstrak_doi(
        "https://a.org/r?doi=10.1000/abc",
        "https://b.org/r?doi=10.1000/abc")
    assert doi == "10.1000/abc"
    assert konflik is None


def test_ekstrak_doi_query_dan_path_sama_lewat_kunci_banding():
    """Satu query-DOI + satu path-DOI sama -> satu DOI (kunci banding
    mengenali keduanya sebagai DOI yang sama)."""
    doi, konflik = _ekstrak_doi(
        "https://a.org/redirect?doi=10.1000/abc",
        "https://b.org/10.1000/abc")
    assert doi == "10.1000/abc"
    assert konflik is None


def test_ekstrak_doi_query_dan_path_beda_konflik():
    """Query-DOI + path-DOI BERBEDA -> None + konflik (mekanisme dua
    URL P3 tidak berubah)."""
    doi, konflik = _ekstrak_doi(
        "https://a.org/redirect?doi=10.1000/abc",
        "https://b.org/10.2222/bbbb")
    assert doi is None
    assert konflik == {
        "detail_link_doi": "10.1000/abc",
        "detail_pdf_doi": "10.2222/bbbb",
    }
