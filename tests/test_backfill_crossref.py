"""Test gerbang tahun di backfill_crossref.

Satu test yang diminta: gerbang TAHUN_TIDAK_COCOK tetap aktif walau
publication_date NULL (baris lama setelah migrasi 06), karena
tahun_awal kini berasal dari publication_year.
"""

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src.enrichment.backfill_crossref import (  # noqa: E402
    ALASAN_TAHUN_TIDAK_COCOK,
    _nilai_item,
    _normalisasi_judul,
    _tahun_publikasi,
)


def test_gerbang_tahun_menolak_saat_publication_date_null():
    """Kandidat Crossref beda tahun > 1 ditolak walau publication_date NULL.

    Setelah migrasi 06, publication_date baris lama NULL sehingga
    _tahun_publikasi(tanggal) mengembalikan None. Tahun_awal yang
    dipakai gerbang tahun harus datang dari publication_year; test ini
    meniru persis turunan tahun_awal di main() dan membuktikan gerbang
    tetap menolak kandidat yang tahunnya meleset lebih dari
    SELISIH_TAHUN_MAKS (1).
    """
    tanggal = None          # publication_date NULL setelah migrasi 06
    tahun = 2020            # publication_year terisi
    tahun_awal = tahun if tahun is not None else _tahun_publikasi(tanggal)
    assert tahun_awal == 2020

    judul_baku = _normalisasi_judul("Contoh Judul Publikasi")
    item = {
        "DOI": "10.1000/xyz123",
        "title": ["Contoh Judul Publikasi"],
        "publisher": "IEEE",
        # tahun terbit 2015, beda 5 tahun dari tahun_awal 2020
        "issued": {"date-parts": [[2015]]},
    }
    nilai = _nilai_item(item, judul_baku, tahun_awal, batas_skor=0.90)
    assert nilai["diterima"] is False
    assert nilai["alasan"] == ALASAN_TAHUN_TIDAK_COCOK