"""Test untuk src/loading/postgres.py (tanpa PostgreSQL, tanpa Playwright).

Cakupan file ini dua bagian:

1. Unit `_bentuk_pendek(pendek, lengkap)` - penerjemah nama pendek
   Scholar ("TAD Kuntjoro") ke nama lengkap pemilik profil ("Tri Agus
   Djoko Kuntjoro"). Fungsi ini adalah satu-satunya kebijakan baru di
   loader, jadi seluruh tabel COCOK dan TOLAK plus kasus tepinya diuji
   eksak di sini.

2. Integrasi `load_core_rows()` dengan koneksi KELIRUAN (fake) dan
   store author palsu - tanpa database. Yang dibuktikan: tautan yang
   namanya bentuk pendek pemilik menempel di author_id PEMILIK dan
   tidak membuat author tiruan, sedangkan co-author sungguhan tetap
   membuat author baru.

Kenapa fake-nya dua objek terpisah:
  - `PostgresAuthorStore` DI-MONKEYPATCH (kontraknya cuma 2 method:
    daftar_author() dan buat_author()), supaya INSERT author bisa
    diamati tanpa SQL.
  - Koneksi palsu hanya perlu `in_transaction()`, `begin()`, dan
    `execute()`; yang WAJIB mengembalikan nilai hanya INSERT
    publications (RETURNING publications_id). sisanya diabaikan.

Cara menjalankan:
    python -m pytest tests/test_database.py -q
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

from sqlalchemy.dialects import postgresql  # noqa: E402

from src.loading import postgres  # noqa: E402
from src.loading.postgres import _bentuk_pendek  # noqa: E402


# ============================================================
# 1. UNIT: _bentuk_pendek
# ============================================================

PEMILIK_LENGKAP = "Tri Agus Djoko Kuntjoro"


@pytest.mark.parametrize("pendek, lengkap", [
    # Tiga contoh yang terbukti ada di database (3/3 dosen sama pola).
    ("TAD Kuntjoro", "Tri Agus Djoko Kuntjoro"),
    ("DE Setyawan", "Dwi Edi Setyawan"),
    ("MI Riansyah", "Moch. Iskandar Riansyah"),
    # Inisial seluruh kata, termasuk kata belakang. Bentuk ini nyata
    # di data ("MIR Riansyah") dan harus ikut cocok.
    ("MIR Riansyah", "Moch. Iskandar Riansyah"),
    # Pola yang sama untuk nama empat kata: T+A+D+K lalu belakangnya.
    ("TADK Kuntjoro", "Tri Agus Djoko Kuntjoro"),
    # Beda HANYA huruf besar-kecil tetap cocok (casefold).
    ("de setyawan", "DWI EDI Setyawan"),
    ("tad kuntjoro", "TRI AGUS DJOKO Kuntjoro"),
    # Spasi ganda di nama dirapikan dulu oleh _normalisasi_nama.
    ("TAD   Kuntjoro", "Tri  Agus   Djoko Kuntjoro"),
])
def test_bentuk_pendek_cocok(pendek, lengkap):
    """Bentuk pendek yang sah dikenali sebagai inisial nama lengkap."""
    assert _bentuk_pendek(pendek, lengkap) is True


@pytest.mark.parametrize("pendek, lengkap", [
    # Belakang berbeda -> inisial apa pun tidak menolong.
    ("TAD Kuntjoro", "Abduh Sayid Albana"),
    ("TAD Kuntjoro", "Tri Agus Djoko Santoso"),
    ("AS Albana", "Tri Agus Djoko Kuntjoro"),
    # Tanpa inisial sama sekali: awalan kosong != "tad".
    ("Kuntjoro", "Tri Agus Djoko Kuntjoro"),
    # Inisial kurang / kelebihan dibanding nama lengkap.
    ("TA Kuntjoro", "Tri Agus Djoko Kuntjoro"),
    ("TADJK Kuntjoro", "Tri Agus Djoko Kuntjoro"),
    # Nama yang casefold-nya SAMA dengan lengkap: tidak perlu
    # penerjemahan (resolve biasa sudah menemukannya).
    ("Tri Agus Djoko Kuntjoro", "Tri Agus Djoko Kuntjoro"),
    ("tri agus djoko kuntjoro", "Tri Agus Djoko Kuntjoro"),
    # Nama pemilik profil yang sebenarnya berbeda orang.
    ("DE Setyawan", "Tri Agus Djoko Kuntjoro"),
])
def test_bentuk_pendek_ditolak(pendek, lengkap):
    """Bentuk yang bukan inisial nama lengkap tidak pernah cocok."""
    assert _bentuk_pendek(pendek, lengkap) is False


@pytest.mark.parametrize("pendek, lengkap", [
    # Input kosong / None / cuma spasi ditolak di kedua sisi.
    (None, PEMILIK_LENGKAP),
    ("TAD Kuntjoro", None),
    ("", PEMILIK_LENGKAP),
    ("TAD Kuntjoro", ""),
    ("   ", PEMILIK_LENGKAP),
    ("TAD Kuntjoro", "   "),
    # Kasus tepi: pendek satu kata.
    ("TAD", PEMILIK_LENGKAP),           # belakangnya bukan "Kuntjoro"
    ("Kuntjoro", PEMILIK_LENGKAP),      # belakang cocok, tapi tanpa inisial
    # Kasus tepi: lengkap satu kata - tidak ada inisial sama sekali,
    # jadi aturan ini memang tidak boleh dipakai.
    ("Kuntjoro", "Kuntjoro"),
    ("kuntjoro", "Kuntjoro"),
    ("X Kuntjoro", "Kuntjoro"),
])
def test_bentuk_pendek_kasus_tepi(pendek, lengkap):
    """Input kosong, nama satu kata, dan nama identik ditolak."""
    assert _bentuk_pendek(pendek, lengkap) is False


# ============================================================
# 2. INTEGRASI: load_core_rows dengan fake store + fake conn
# ============================================================
#
# Store palsu sengaja dibuat lewat pabrik: load_core_rows yang
# menginstansiasi PostgresAuthorStore sendiri, jadi yang disuntikkan
# cukup kelasnya (monkeypatch), sementara REKAMAN dibuat di luar
# supaya test bisa membaca isi "tabel"-nya setelah run selesai.


def _store_palsu(rekaman, existing=()):
    """Kelas store palsu dengan kontrak persis AuthorResolver.

    Kontrak (lihat PostgresAuthorStore): daftar_author() dan
    buat_author(nama, scholar_id=None). ID baru naik dari 1; nama yang
    sudah ada di `existing` tidak pernah dibuat ulang.
    """
    class StorePalsu:
        def __init__(self, conn):
            self._conn = conn

        def daftar_author(self):
            return list(existing)

        def buat_author(self, nama, scholar_id=None):
            if any(nama.casefold() == n.casefold() for _, n in existing):
                # Meniru ON CONFLICT: nama yang sudah ada kembali id
                # lamanya, bukan baris kedua.
                return next(i for i, n in existing if n.casefold() == nama.casefold())
            rekaman["dibuat"].append((nama, scholar_id))
            rekaman["berikut"] += 1
            return rekaman["berikut"]

    return StorePalsu


class _HasilPalsu:
    """Hasil execute() palsu: .all() untuk SELECT, .scalar_one() RETURNING."""

    def __init__(self, baris=(), scalar=None):
        self._baris = list(baris)
        self._scalar = scalar

    def all(self):
        return self._baris

    def scalar_one(self):
        return self._scalar


class _TransaksiPalsu:
    def __init__(self):
        self.committed = False
        self.rolled_back = False

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True


class _KoneksiPalsu:
    """Koneksi tiruan yang cukup untuk load_core_rows().

    Satu-satunya nilai WAJIB dikembalikan adalah publications_id dari
    INSERT core.publications (RETURNING publications_id); sisanya
    (publication_authors, metrics) dicatat untuk asersi lalu diabaikan.
    """

    def __init__(self):
        self.transaksi: _TransaksiPalsu | None = None
        self._id_publikasi = 0
        self.tautan = []  # (publication_id, author_id, author_order)
        self.dihapus = []  # (publication_id, author_id, author_order)

    def in_transaction(self):
        return False

    def begin(self):
        self.transaksi = _TransaksiPalsu()
        return self.transaksi

    def execute(self, stmt, *args, **kwargs):
        tabel = getattr(stmt, "table", None)
        if tabel is None:
            # SELECT. Tidak terjadi selama store dipalsukan, tapi
            # kembalikan hasil kosong supaya gagal dengan anggun.
            return _HasilPalsu()
        if tabel.name == "publications":
            self._id_publikasi += 1
            return _HasilPalsu(scalar=self._id_publikasi)
        if tabel.name == "publication_authors":
            # Deteksi DELETE vs INSERT berdasarkan tipe statement.
            stmt_type = type(stmt).__name__.lower()
            nilai = stmt.compile(dialect=postgresql.dialect()).params
            if "delete" in stmt_type:
                self.dihapus.append((
                    nilai.get("publication_id") or nilai.get("publication_id_1"),
                    nilai.get("author_id") or nilai.get("author_id_1"),
                    nilai.get("author_order") or nilai.get("author_order_1"),
                ))
                return _HasilPalsu()
            self.tautan.append((
                nilai["publication_id"],
                nilai["author_id"],
                nilai["author_order"],
            ))
        return _HasilPalsu()


def _core(pemilik, tautan, scholar_id="8kDg_v4AAAAJ"):
    """Bangun dict 5 bucket persis kontrak to_core_rows()."""
    return {
        "authors": {"name": pemilik, "scholar_id": scholar_id},
        "publications": [
            {"title": "Publikasi Satu", "publication_date": "2024-05-01"},
            {"title": "Publikasi Dua", "publication_date": "2023-01-15"},
        ],
        "publication_authors": [
            {
                "publication_index": urut % 2,
                "author_name": nama,
                "author_order": urut,
            }
            for urut, nama in enumerate(tautan, start=1)
        ],
        "author_metrics": {
            "source": "google_scholar",
            "h_index": 12,
            "score": 1.5,
            "index_name": "h5-index",
            "retrieved_at": "2024-06-01T00:00:00Z",
        },
        "publication_metrics": [
            {
                "source": "google_scholar",
                "citation_count": 7,
                "index_name": "h5-index",
                "score": 1.0,
                "retrieved_at": "2024-06-01T00:00:00Z",
            },
        ],
    }


def _jalankan(monkeypatch, pemilik, tautan, existing=()):
    """Jalankan load_core_rows dengan store & koneksi palsu."""
    rekaman = {
        "dibuat": [],
        # ID baru naik dari atas ID yang sudah ada, supaya tidak pernah
        # bentrok dengan baris lama yang ikut dimuat ke cache.
        "berikut": max((i for i, _ in existing), default=0),
    }
    monkeypatch.setattr(postgres, "PostgresAuthorStore",
                        _store_palsu(rekaman, existing))
    conn = _KoneksiPalsu()
    statistik = postgres.load_core_rows(
        _core(pemilik, tautan), conn=conn, run_id="run-uji")
    return statistik, conn, rekaman


def test_tautan_nama_pendek_pemilik_menempel_di_pemilik(monkeypatch):
    """'TAD Kuntjoro' resolve ke author_id PEMILIK, tanpa author baru.

    Ini inti bugnya: tanpa penerjemahan, resolver membuat author
    tiruan dan baris pemilik berakhir dengan nol relasi.
    """
    statistik, conn, rekaman = _jalankan(
        monkeypatch, PEMILIK_LENGKAP, ["TAD Kuntjoro"])

    pemilik_id = statistik["author_id"]
    assert pemilik_id is not None
    assert len(conn.tautan) == 1
    assert conn.tautan[0][1] == pemilik_id
    # Satu-satunya author yang dibuat adalah pemilik profil itu sendiri.
    assert rekaman["dibuat"] == [(PEMILIK_LENGKAP, "8kDg_v4AAAAJ")]
    assert statistik["authors_created"] == 1
    assert statistik["publication_authors_inserted"] == 1
    assert statistik["status"] == postgres.STATUS_LOADED
    assert conn.transaksi is not None
    assert conn.transaksi.committed is True


def test_pemilik_sudah_ada_di_tabel_tidak_dibuat_ulang(monkeypatch):
    """Skripsi bug yang asli: pemilik sudah ada (id=67, relasi=0).

    Cache diisi dari tabel, pemilik resolve ke baris lamanya, dan
    tautan nama pendek menempel ke baris lamanya - tanpa INSERT apa
    pun.
    """
    statistik, conn, rekaman = _jalankan(
        monkeypatch, PEMILIK_LENGKAP, ["TAD Kuntjoro"],
        existing=[(67, PEMILIK_LENGKAP)])

    assert statistik["author_id"] == 67
    assert conn.tautan[0][1] == 67
    assert rekaman["dibuat"] == []
    assert statistik["authors_created"] == 0


def test_co_author_bukan_pemilik_tetap_dibuat_baru(monkeypatch):
    """Co-author yang bukan bentuk pendek pemilik tetap author baru."""
    statistik, conn, rekaman = _jalankan(
        monkeypatch, PEMILIK_LENGKAP,
        ["TAD Kuntjoro", "AS Albana", "MIR Riansyah"])

    pemilik_id = statistik["author_id"]
    id_pendek, id_albana, id_riansyah = [t[1] for t in conn.tautan]

    # Nama pendek pemilik -> tertaut ke pemilik.
    assert id_pendek == pemilik_id
    # Co-author sungguhan -> author sendiri, bukan pemilik.
    assert id_albana != pemilik_id
    assert id_riansyah != pemilik_id
    assert id_albana != id_riansyah

    assert [nama for nama, _ in rekaman["dibuat"]] == [
        PEMILIK_LENGKAP, "AS Albana", "MIR Riansyah"]
    assert statistik["authors_created"] == 3


    assert statistik["authors_created"] == 3


def test_bentuk_pendek_yang_sudah_jadi_author_direwrite_ke_pemilik(monkeypatch):
    """Nama pendek yang sudah jadi author tiruan (run sebelum fix)
    dialihkan ke author_id pemilik. Link lama dihapus untuk
    publikasi+urutan yang sama.
    """
    statistik, conn, rekaman = _jalankan(
        monkeypatch, PEMILIK_LENGKAP, ["TAD Kuntjoro"],
        existing=[(67, PEMILIK_LENGKAP), (78, "TAD Kuntjoro")])

    pemilik_id = statistik["author_id"]
    assert pemilik_id == 67
    assert conn.tautan[0][1] == pemilik_id
    assert rekaman["dibuat"] == []
    assert statistik["authors_created"] == 0
    assert statistik["publication_authors_repointed"] == 1
    assert conn.dihapus == [(2, 78, 1)]


def test_repoint_kasus_khodijah_amiroh(monkeypatch):
    """Kasus nyata: pemilik id=238, tiruan K Amiroh id=179 sudah ada."""
    statistik, conn, rekaman = _jalankan(
        monkeypatch, "Khodijah Amiroh", ["K Amiroh"],
        existing=[(238, "Khodijah Amiroh"), (179, "K Amiroh")])

    assert statistik["author_id"] == 238
    assert conn.tautan[0][1] == 238
    assert conn.dihapus == [(2, 179, 1)]
    assert statistik["publication_authors_repointed"] == 1
    assert rekaman["dibuat"] == []


def test_tidak_ada_delete_bila_lama_id_none(monkeypatch):
    """Nama belum pernah ada di cache → tidak ada DELETE."""
    statistik, conn, rekaman = _jalankan(
        monkeypatch, PEMILIK_LENGKAP, ["TAD Kuntjoro"],
        existing=[(67, PEMILIK_LENGKAP)])

    assert statistik["author_id"] == 67
    assert conn.tautan[0][1] == 67
    assert conn.dihapus == []
    assert statistik["publication_authors_repointed"] == 0


def test_tidak_ada_delete_bila_bentuk_pendek_false(monkeypatch):
    """Bentuk pendek tidak cocok → lama_id == author_id → tidak DELETE."""
    statistik, conn, rekaman = _jalankan(
        monkeypatch, PEMILIK_LENGKAP, ["AS Albana"],
        existing=[(67, PEMILIK_LENGKAP), (88, "AS Albana")])

    assert statistik["author_id"] == 67
    assert conn.tautan[0][1] == 88
    assert conn.dihapus == []
    assert statistik["publication_authors_repointed"] == 0
