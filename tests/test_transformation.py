"""Test untuk src/transformation/publications.py.

Cakupan file ini sengaja hanya parser kolom `extra`
(pecah_extra_volume_pages). to_core_rows() TIDAK diuji di sini -
mapper itu punya kontraknya sendiri dan punya file test-nya sendiri;
mencampurnya ke satu file hanya membuat dua kontrak berbeda
saling menutupi.

Yang diuji dan paling dijaga adalah kontrak anti-tebakan: parser
tidak pernah mengisi sebagian. Untuk SETIAP pola yang ditolak, kedua
field (volume dan pages) harus None. Karena itu tabel penolakan punya
test sendiri yang mengulang seluruh tabel, bukan sekadar ikut dicek
di tiap kasus.

Cara menjalankan:
    python -m pytest tests/test_transformation.py -q
"""

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

from src.transformation.publications import (  # noqa: E402
    pecah_extra_volume_pages,
)


# ============================================================
# POLA 1: "VOL(ISSUE), PAGES"
# ============================================================
#
# Nomor issue sengaja dibuang: core tidak punya kolom issue, yang
# disimpan hanya volume dan pages.

@pytest.mark.parametrize("extra, volume, pages", [
    ("3(2), 45-60", "3", "45-60"),
    # Spasi sebelum kurung tetap diterima.
    ("3 (2), 45-60", "3", "45-60"),
    # Issue huruf/angka campuran, dan issue angka Romawi.
    ("3(S1), 45-60", "3", "45-60"),
    ("3(IV), 1-12", "3", "1-12"),
    # Pemisah titik koma sama sahnya dengan koma.
    ("3(2); 45-60", "3", "45-60"),
    # Volume berdigit banyak - VARCHAR(50) masih aman.
    ("1234567890(2), 1-2", "1234567890", "1-2"),
])
def test_pola_volume_issue_pages(extra, volume, pages):
    """Pola VOL(ISSUE), PAGES terurai jadi volume + pages."""
    hasil = pecah_extra_volume_pages(extra)
    assert hasil.volume == volume
    assert hasil.pages == pages
    assert hasil.alasan is None


# ============================================================
# POLA 2: "VOL, PAGES"
# ============================================================

@pytest.mark.parametrize("extra, volume, pages", [
    ("15, 234-245", "15", "234-245"),
    ("12, 45", "12", "45"),
    # Satu halaman saja sah: kolomnya VARCHAR, bukan INTEGER.
    ("7, 100", "7", "100"),
    # Pemisah titik koma.
    ("15; 234-245", "15", "234-245"),
    # Volume berdigit banyak.
    ("1000, 1-999", "1000", "1-999"),
])
def test_pola_volume_pages(extra, volume, pages):
    """Pola VOL, PAGES terurai jadi volume + pages."""
    hasil = pecah_extra_volume_pages(extra)
    assert hasil.volume == volume
    assert hasil.pages == pages
    assert hasil.alasan is None


# ============================================================
# POLA 3: "PAGES" saja (volume memang tidak ada di sumber)
# ============================================================

@pytest.mark.parametrize("extra, pages", [
    ("45-60", "45-60"),
    ("234", "234"),
    # Label halaman bergaya e-journal dan nomor artikel.
    ("e12345", "e12345"),
    ("S1234", "S1234"),
    ("e1234-1239", "e1234-1239"),
    # Huruf besar tetap diterima (polanya IGNORECASE).
    ("E12345", "E12345"),
])
def test_pola_halaman_saja(extra, pages):
    """Pola PAGES saja menghasilkan volume=None dan pages terisi."""
    hasil = pecah_extra_volume_pages(extra)
    assert hasil.volume is None
    assert hasil.pages == pages
    assert hasil.alasan is None


# ============================================================
# TIPOGRAFI: Scholar memang sering mengirim karakter aneh
# ============================================================
#
# Ditulis sebagai escape \u... dengan sengaja: kalau literal NBSP
# atau en dash ikut tersalin ke berkas ini, karakter itu akan
# hilang diam-diam saat berkas ini disalin atau disunting, dan
# testnya tetap hijau padahal tidak lagi menguji apa pun.

@pytest.mark.parametrize("extra, volume, pages", [
    # NBSP (U+00A0) di antara koma dan halaman.
    ("3(2),\u00a045-60", "3", "45-60"),
    ("15,\u00a0234-245", "15", "234-245"),
    # En dash (U+2013) di dalam range halaman.
    ("15, 234\u2013245", "15", "234-245"),
    ("45\u201360", None, "45-60"),
    # Em dash (U+2014) dan minus sign (U+2212).
    ("15, 234\u2014245", "15", "234-245"),
    ("15, 234\u2212245", "15", "234-245"),
    # Spasi sempit: narrow NBSP (U+202F), thin space (U+2009),
    # dan figure space (U+2007).
    ("3(2),\u202f45-60", "3", "45-60"),
    ("3(2),\u200945-60", "3", "45-60"),
    ("3(2),\u200745-60", "3", "45-60"),
    # Spasi ganda harus ikut dirapikan, bukan hanya diganti.
    ("3(2),  45-60", "3", "45-60"),
    ("3(2),\t45-60", "3", "45-60"),
    ("  3(2), 45-60  ", "3", "45-60"),
])
def test_tipografi_diratakan(extra, volume, pages):
    """Karakter non-ASCII dari Scholar diratakan sebelum diurai."""
    hasil = pecah_extra_volume_pages(extra)
    assert hasil.volume == volume
    assert hasil.pages == pages
    assert hasil.alasan is None


# ============================================================
# EXTRA_KOSONG: ini BUKAN kegagalan parse
# ============================================================
#
# Dibedakan dari POLA_TIDAK_DIKENAL dengan sengaja. Scholar memang
# tidak punya data pada baris itu; mencampurkannya dengan kegagalan
# pola akan membuat persentase kegagalan terlihat lebih buruk dari
# kenyataannya.

@pytest.mark.parametrize("extra", [
    None,
    "",
    "   ",
    "\t\n ",
])
def test_extra_kosong_bukan_kegagalan(extra):
    """Kotak kosong menghasilkan EXTRA_KOSONG, bukan pola tak dikenal."""
    hasil = pecah_extra_volume_pages(extra)
    assert hasil.alasan == "EXTRA_KOSONG"
    assert hasil.volume is None
    assert hasil.pages is None


# ============================================================
# TABEL PENOLAKAN
# ============================================================

POLA_DITOLAK = [
    # Volume angka Romawi - belum bisa dibedakan dari nomor issue.
    ("volume_romawi", "III, 45-60"),
    # Prefiks prosa.
    ("prefiks_prosa", "Vol. 3, 45-60"),
    # Issue bertoken dengan spasi di dalamnya.
    ("issue_ber_spasi", "3(Suppl 1), 45-60"),
    # Volume tanpa halaman: TIDAK boleh diisi sebagian.
    ("volume_tanpa_halaman", "3(2)"),
    # Tiga bagian: tidak ditebak mana volume dan mana halaman.
    ("tiga_bagian", "3(2), 45-60, 2019"),
    ("tiga_bagian_polos", "3, 2, 45-60"),
    # Free-text umum dari Scholar.
    ("in_press", "in press"),
    # Range halaman rusak.
    ("range_setengah", "45-"),
    ("range_setengah_2", "-60"),
    # Digit non-ASCII: ditolak, sama seperti sikap _TAHUN_POLA.
    ("digit_non_ascii", "٣(٢), 45-60"),
    ("halaman_non_ascii", "٤٥-٦٠"),
    # Salah satu bagian kosong setelah pemisah.
    ("bagian_kosong", "3, "),
    ("bagian_kosong_2", ", 45-60"),
    # Halaman yang bukan angka.
    ("bukan_angka", "45-60 hal"),
]

_ID_DITOLAK = [nama for nama, _ in POLA_DITOLAK]


@pytest.mark.parametrize("nama, extra", POLA_DITOLAK, ids=_ID_DITOLAK)
def test_pola_ditolak(nama, extra):
    """Pola yang tidak dikenal ditolak dengan alasan yang tepat."""
    hasil = pecah_extra_volume_pages(extra)
    assert hasil.alasan == "POLA_TIDAK_DIKENAL"


@pytest.mark.parametrize("nama, extra", POLA_DITOLAK, ids=_ID_DITOLAK)
def test_penolakan_tidak_mengisi_sebagian(nama, extra):
    """Kontrak anti-tebakan: saat ditolak, KEDUA field wajib None.

    Ini test paling penting di file ini. Kalau parser suatu saat
    diisi ulang sebagian (volume terkena tapi pages tidak), test ini
    langsung merah - dan itu memang yang dikehendaki: kolom VARCHAR
    yang terisi separuh akan dibaca downstream seolah-olah itu data
    sumber yang sah.
    """
    hasil = pecah_extra_volume_pages(extra)
    assert hasil.volume is None, (
        "volume tidak boleh terisi pada input yang ditolak: {!r} -> {!r}"
        .format(extra, hasil)
    )
    assert hasil.pages is None, (
        "pages tidak boleh terisi pada input yang ditolak: {!r} -> {!r}"
        .format(extra, hasil)
    )
    assert hasil.alasan == "POLA_TIDAK_DIKENAL"


@pytest.mark.parametrize("extra", [extra for _, extra in POLA_DITOLAK]
                         + [None, "", "   "])
def test_alasan_selalu_terisi_ketika_gagal(extra):
    """Setiap kegagalan WAJIB punya alasan, tidak pernah diam-diam."""
    hasil = pecah_extra_volume_pages(extra)
    assert hasil.alasan in ("EXTRA_KOSONG", "POLA_TIDAK_DIKENAL")


# ============================================================
# KONTRAK UMUM
# ============================================================

def test_nilai_kembalikan_punya_tiga_field():
    """Namedtuple punya field persis: volume, pages, alasan."""
    hasil = pecah_extra_volume_pages("3(2), 45-60")
    assert hasil._fields == ("volume", "pages", "alasan")
    # Bisa di-unpack persis seperti tuple biasa.
    volume, pages, alasan = hasil
    assert (volume, pages, alasan) == ("3", "45-60", None)


def test_hasil_bersifat_tupel():
    """Nilai balik boleh dipakai sebagai key dict (dibutuhkan log)."""
    hasil = pecah_extra_volume_pages("45-60")
    assert hasil == (None, "45-60", None)
    assert hash(hasil) == hash((None, "45-60", None))


@pytest.mark.parametrize("extra", [
    "3(2), 45-60",
    "15, 234-245",
    "45-60",
    "III, 45-60",
    None,
])
def test_ulang_panggil_hasil_sama(extra):
    """Parser murni: input sama -> output sama, berapa kali pun."""
    assert (pecah_extra_volume_pages(extra)
            == pecah_extra_volume_pages(extra))
