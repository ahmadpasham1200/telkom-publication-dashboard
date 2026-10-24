"""Test untuk src/loading/postgres.py (tanpa PostgreSQL, tanpa Playwright).

Cakupan file ini dua bagian:

1. Unit `_bentuk_pendek(pendek, lengkap)` - penerjemah nama pendek
   Scholar ("TAD Kuntjoro") ke nama lengkap pemilik profil ("Tri Agus
   Djoko Kuntjoro"). Fungsi ini adalah satu-satunya kebijakan baru di
   loader, jadi seluruh tabel COCOK dan TOLAK plus kasus tepinya diuji
   eksak di sini.

2. Integrasi `load_core_rows()` dengan koneksi KELIRUAN (fake) dan
   store author palsu - tanpa database. Yang dibuktikan: tautan yang
   namanya bentuk pendek pemilik menempel di author_id PEMILIK;
   co-author yang tidak dikenal TIDAK membuat author baru (migrasi
   14) - tautannya disimpan dengan author_id NULL + author_name,
   sedangkan co-author yang sudah ada di cache memakai author_id
   lamanya.

Kenapa fake-nya dua objek terpisah:
  - `PostgresAuthorStore` DI-MONKEYPATCH (kontraknya cuma 2 method:
    daftar_author() dan buat_author()), supaya INSERT author bisa
    diamati tanpa SQL.
  - Koneksi palsu hanya perlu `in_transaction()`, `begin()`, dan
    `execute()`; yang WAJIB mengembalikan nilai hanya INSERT
    publications (RETURNING publication_id). sisanya diabaikan.

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

from sqlalchemy import UniqueConstraint  # noqa: E402
from sqlalchemy.dialects import postgresql  # noqa: E402

from src.loading import postgres  # noqa: E402
from src.loading.postgres import _bentuk_pendek  # noqa: E402
from src.loading.postgres import _varian_nama_pemilik  # noqa: E402


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
# 1b. UNIT: _varian_nama_pemilik
# ============================================================
# Aturan nama-varian pemilik KHUSUS jalur daftar penulis halaman
# detail. Kasus nyata: publikasi 48 menulis pemilik sebagai
# "Tri Agus D Kuntjoro" sehingga tautan 48|67 hilang dan author
# tiruan 3315 tercipta setelah rewrite kebijakan 3j.


@pytest.mark.parametrize("nama, lengkap", [
    # Kasus nyata regressi publikasi 48: potong-per-kata tengah.
    ("Tri Agus D Kuntjoro", PEMILIK_LENGKAP),
    # Beda HANYA huruf besar-kecil tetap cocok (casefold).
    ("tri agus d kuntjoro", "TRI AGUS DJOKO Kuntjoro"),
    # Awalan yang lebih panjang tetap awalan ("Djo" dari "Djoko").
    ("Tri Agus Djo Kuntjoro", PEMILIK_LENGKAP),
])
def test_varian_pemilik_cocok(nama, lengkap):
    """Varian potong-per-kata dengan jumlah kata sama dikenali."""
    assert _varian_nama_pemilik(nama, lengkap) is True


@pytest.mark.parametrize("nama, lengkap", [
    # Dua kasus negatif yang diminta: belakang sama tapi isi beda.
    ("Tri Budi Kuntjoro", PEMILIK_LENGKAP),
    ("Dwi Agus Kuntjoro", PEMILIK_LENGKAP),
    # Jumlah kata berbeda -> bukan urusan aturan ini (milik
    # _bentuk_pendek), jadi ditolak di sini.
    ("TAD Kuntjoro", PEMILIK_LENGKAP),
    # Jumlah kata SAMA tapi kata tengah bukan awalan ("Z" vs "Djoko").
    # Tanpa uji ini, aturan prefix tidak benar-benar teruji.
    ("Tri Agus Z Kuntjoro", PEMILIK_LENGKAP),
    # Belakang berbeda -> ditolak walau depannya sama persis.
    ("Tri Agus D Santoso", PEMILIK_LENGKAP),
    # Nama persis sama: resolve biasa sudah menemukannya.
    (PEMILIK_LENGKAP, PEMILIK_LENGKAP),
    # Bukan pemilik profil sama sekali.
    ("Dwi Edi Setyawan", PEMILIK_LENGKAP),
])
def test_varian_pemilik_ditolak(nama, lengkap):
    """Varian yang tidak memenuhi syarat ketat tidak pernah cocok."""
    assert _varian_nama_pemilik(nama, lengkap) is False


@pytest.mark.parametrize("nama, lengkap", [
    # Input kosong / None / cuma spasi ditolak di kedua sisi.
    (None, PEMILIK_LENGKAP),
    ("Tri Agus D Kuntjoro", None),
    ("", PEMILIK_LENGKAP),
    ("Tri Agus D Kuntjoro", ""),
    ("   ", PEMILIK_LENGKAP),
    ("Tri Agus D Kuntjoro", "   "),
])
def test_varian_pemilik_kasus_tepi(nama, lengkap):
    """Input kosong ditolak tanpa melempar."""
    assert _varian_nama_pemilik(nama, lengkap) is False


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
    buat_author(nama, scholar_id=None, program_study=None). ID baru
    naik dari 1; nama yang sudah ada di `existing` tidak pernah
    dibuat ulang. `program_study` dicatat terpisah di
    rekaman["prodi"] supaya tes bisa memeriksa sumbernya tanpa
    mengubah bentuk tuple rekaman["dibuat"] (yang dipakai tes lama).
    """
    class StorePalsu:
        def __init__(self, conn):
            self._conn = conn

        def daftar_author(self):
            return list(existing)

        def buat_author(self, nama, scholar_id=None, program_study=None):
            if any(nama.casefold() == n.casefold() for _, n in existing):
                # Meniru ON CONFLICT: nama yang sudah ada kembali id
                # lamanya, bukan baris kedua.
                return next(i for i, n in existing if n.casefold() == nama.casefold())
            rekaman["dibuat"].append((nama, scholar_id))
            rekaman["prodi"].append((nama, program_study))
            rekaman["berikut"] += 1
            return rekaman["berikut"]

    return StorePalsu


class _HasilPalsu:
    """Hasil execute() palsu: .all() untuk SELECT, .scalar_one() RETURNING.

    `scalar_one_or_none()` dipakai write_raw_scrape_result() untuk
    membaca RETURNING result_id; sama dengan .scalar_one() pada fake
    ini (None kalau tidak ada nilai yang diberikan).
    """

    def __init__(self, baris=(), scalar=None):
        self._baris = list(baris)
        self._scalar = scalar

    def all(self):
        return self._baris

    def scalar_one(self):
        return self._scalar

    def scalar_one_or_none(self):
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

    Satu-satunya nilai WAJIB dikembalikan adalah publication_id dari
    INSERT core.publications (RETURNING publication_id); sisanya
    (publication_authors, metrics) dicatat untuk asersi lalu diabaikan.
    """

    def __init__(self):
        self.transaksi: _TransaksiPalsu | None = None
        self._id_publikasi = 0
        # (publication_id, author_id, author_order, author_name, author_type)
        self.tautan = []
        # DELETE repoint kini berdasar (publication_id, author_order)
        # saja - author_id tidak lagi bagian identitas baris (migrasi
        # 14), jadi tidak ada di WHERE dan tidak ikut dicatat.
        self.dihapus = []  # (publication_id, author_order)
        # (author_id, program_study) per UPDATE core.authors
        # (_stmt_isi_program_study_author) - dicatat untuk asersi
        # tes program_study; INSERT author lewat store dipalsukan,
        # jadi satu-satunya pernyataan authors di koneksi tiruan
        # ini adalah UPDATE itu.
        self.prodi_author = []

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
        if tabel.name == "authors":
            # UPDATE program_study pemilik (fill-only-when-NULL).
            # Nama param hasil compile: coalesce_1 + author_id_1
            # (lihat _stmt_isi_program_study_author).
            stmt_type = type(stmt).__name__.lower()
            if "update" in stmt_type:
                nilai = stmt.compile(dialect=postgresql.dialect()).params
                self.prodi_author.append((
                    nilai.get("author_id_1"),
                    nilai.get("coalesce_1"),
                ))
            return _HasilPalsu()
        if tabel.name == "publication_authors":
            # Deteksi DELETE vs INSERT berdasarkan tipe statement.
            stmt_type = type(stmt).__name__.lower()
            nilai = stmt.compile(dialect=postgresql.dialect()).params
            if "delete" in stmt_type:
                # Jangan memilih nilai dengan `or`: nilai 0 (author_order
                # 0-based, migrasi 14) sah tapi dianggap hilang oleh `or`.
                # Pilih kunci berdasarkan keberadaannya; nama kunci hasil
                # compile bisa polos atau berakhiran _1 (tergantung bentuk
                # WHERE). Kunci yang memang tidak ada - mis. DELETE
                # _stmt_hapus_semua_tautan (jalur detail) yang hanya
                # memfilter publication_id - tetap None seperti toleransi
                # perilaku lama.
                def _ambil(*nama):
                    for kunci in nama:
                        if kunci in nilai:
                            return nilai[kunci]
                    return None
                self.dihapus.append((
                    _ambil("publication_id", "publication_id_1"),
                    _ambil("author_order", "author_order_1"),
                ))
                return _HasilPalsu()
            self.tautan.append((
                nilai["publication_id"],
                nilai["author_id"],
                nilai["author_order"],
                nilai.get("author_name"),
                nilai.get("author_type"),
            ))
        return _HasilPalsu()


# Penanda skrip: INSERT publikasi mengembalikan None (mensimulasikan
# ON CONFLICT DO NOTHING kena baris lama pada retry).
_KONFLIK = object()


class _KoneksiRawPalsu:
    """Koneksi tiruan khusus write_raw_scrape_result (P58 + G2-B).

    STATEMENT-AWARE: setiap execute() dicatat (nama tabel, jenis
    statement insert/select, params hasil compile dialect postgresql)
    untuk asersi tes, dan hasil per statement bisa DISKRIP:

      skrip_publikasi  antrian hasil INSERT publikasi per baris:
                       angka = id yang dikembalikan; _KONFLIK = None
                       (konflik, mensimulasikan baris lama); kosong =
                       id auto (1001, 1002, ...).
      skrip_fallback   antrian hasil SELECT fallback id publikasi
                       (angka atau None); kosong = None.
      skrip_author     antrian hasil INSERT author: True = sukses
                       (id auto); False = konflik (None); kosong =
                       sukses (id auto).
      galat_author     Exception yang dilempar saat INSERT author
                       berikutnya (mensimulasikan kegagalan database).

    Sengaja tidak menyentuh database sungguhan.
    """

    def __init__(self, result_id=11):
        self.transaksi: _TransaksiPalsu | None = None
        self.result_id = result_id
        self.tabel_dieksekusi = []  # nama tabel per execute(), berurutan
        self.perintah = []  # dict {tabel, jenis, params} per execute()
        self.skrip_publikasi = []
        self.skrip_fallback = []
        self.skrip_author = []
        self.galat_author: Exception | None = None
        self._id_publikasi_auto = 1000
        self._id_author_auto = 5000

    def in_transaction(self):
        return False

    def begin(self):
        self.transaksi = _TransaksiPalsu()
        return self.transaksi

    def execute(self, stmt, *args, **kwargs):
        compiled = stmt.compile(dialect=postgresql.dialect())
        tipe = type(stmt).__name__.lower()
        if "select" in tipe:
            nama = stmt.selected_columns[0].table.name
            jenis = "select"
        else:
            nama = stmt.table.name
            jenis = "insert"
        self.tabel_dieksekusi.append(nama)
        self.perintah.append({
            "tabel": nama,
            "jenis": jenis,
            "params": dict(compiled.params),
        })

        if nama == "scholar_scrape_result":
            return _HasilPalsu(scalar=self.result_id)
        if nama == "scholar_publication":
            if jenis == "select":
                # SELECT fallback resolusi id publikasi (D1).
                return _HasilPalsu(
                    scalar=(self.skrip_fallback.pop(0)
                            if self.skrip_fallback else None))
            if self.skrip_publikasi:
                nilai = self.skrip_publikasi.pop(0)
                if nilai is _KONFLIK:
                    return _HasilPalsu(scalar=None)
                return _HasilPalsu(scalar=nilai)
            self._id_publikasi_auto += 1
            return _HasilPalsu(scalar=self._id_publikasi_auto)
        if nama == "scholar_publication_author":
            if self.galat_author is not None:
                galat, self.galat_author = self.galat_author, None
                raise galat
            sukses = (self.skrip_author.pop(0)
                      if self.skrip_author else True)
            if not sukses:
                # ON CONFLICT DO NOTHING: RETURNING kosong.
                return _HasilPalsu(scalar=None)
            self._id_author_auto += 1
            return _HasilPalsu(scalar=self._id_author_auto)
        return _HasilPalsu()


def _params_tabel(conn, nama, jenis=None):
    """Kumpulkan params hasil compile tiap execute() pada tabel tertentu.

    `jenis` menyaring "insert"/"select"; None = semua. Urutan execute
    dipertahankan, supaya tes bisa memeriksa urutan pernyataan.
    """
    return [p["params"] for p in conn.perintah
            if p["tabel"] == nama and (jenis is None or p["jenis"] == jenis)]


def _core(pemilik, tautan, scholar_id="8kDg_v4AAAAJ", sumber=None):
    """Bangun dict 5 bucket persis kontrak to_core_rows().

    `sumber` (opsional) mengisi publications[i]["author_source"],
    yaitu penanda keputusan 3j: "detail" (otoritatif) atau daftar
    (None/tidak diisi). Default None = jalur daftar, sama seperti
    test-test lama di file ini.

    Tiap tautan memuat author_name, author_order, publication_index,
    dan author_type (penulis_pertama untuk urutan 0, sisanya
    co_author) - kunci persis keluaran mapper lane.
    """
    publications = [
        {"title": "Publikasi Satu", "publication_date": "2024-05-01"},
        {"title": "Publikasi Dua", "publication_date": "2023-01-15"},
    ]
    if sumber is not None:
        for baris in publications:
            baris["author_source"] = sumber
    return {
        "authors": {"name": pemilik, "scholar_id": scholar_id},
        "publications": publications,
        # Pemetaan ke publikasi dipertahankan dari versi lama fixture
        # agar cakupan tes tidak berubah: penulis PERTAMA menunjuk
        # publikasi indeks 1, penulis KEDUA indeks 0, lalu
        # bergantian. author_order kini 0-based sesuai kontrak
        # mapper; karena itu penulis_pertama menempel pada urutan 0.
        "publication_authors": [
            {
                "publication_index": 1 if urut % 2 == 0 else 0,
                "author_name": nama,
                "author_order": urut,
                "author_type": ("penulis_pertama" if urut == 0
                                else "co_author"),
            }
            for urut, nama in enumerate(tautan)
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


def _jalankan(monkeypatch, pemilik, tautan, existing=(), sumber=None,
              baris_referensi=None):
    """Jalankan load_core_rows dengan store & koneksi palsu.

    `sumber` diteruskan ke _core() sebagai author_source publikasi
    (None = jalur daftar, "detail" = jalur halaman detail).
    `baris_referensi` diteruskan ke load_core_rows sebagai sumber
    program_study pemilik profil.
    """
    rekaman = {
        "dibuat": [],
        # (nama, program_study) per author yang benar-benar dibuat;
        # dipakai tes program_study tanpa mengubah bentuk "dibuat".
        "prodi": [],
        # ID baru naik dari atas ID yang sudah ada, supaya tidak pernah
        # bentrok dengan baris lama yang ikut dimuat ke cache.
        "berikut": max((i for i, _ in existing), default=0),
    }
    monkeypatch.setattr(postgres, "PostgresAuthorStore",
                        _store_palsu(rekaman, existing))
    conn = _KoneksiPalsu()
    statistik = postgres.load_core_rows(
        _core(pemilik, tautan, sumber=sumber), conn=conn, run_id="run-uji",
        baris_referensi=baris_referensi)
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


def test_co_author_tidak_dikenal_tanpa_author_baru(monkeypatch):
    """Co-author yang bukan pemilik TIDAK dibuat lagi (migrasi 14).

    Tautannya tetap disimpan, tapi author_id-nya NULL - tanpa
    pemanggilan buat_author sama sekali untuk nama co-author.
    """
    statistik, conn, rekaman = _jalankan(
        monkeypatch, PEMILIK_LENGKAP,
        ["TAD Kuntjoro", "AS Albana", "MIR Riansyah"])

    pemilik_id = statistik["author_id"]
    id_pendek, id_albana, id_riansyah = [t[1] for t in conn.tautan]

    # Nama pendek pemilik -> tertaut ke pemilik.
    assert id_pendek == pemilik_id
    # Co-author tidak dikenal -> author_id NULL, bukan author baru.
    assert id_albana is None
    assert id_riansyah is None
    # Tapi nama + perannya tetap tersimpan apa adanya.
    assert [t[3] for t in conn.tautan] == [
        "TAD Kuntjoro", "AS Albana", "MIR Riansyah"]
    assert [t[4] for t in conn.tautan] == [
        "penulis_pertama", "co_author", "co_author"]

    # Satu-satunya author yang dibuat: pemilik profil itu sendiri.
    assert rekaman["dibuat"] == [(PEMILIK_LENGKAP, "8kDg_v4AAAAJ")]
    assert statistik["authors_created"] == 1
    # Seluruh tautan tetap terhitung ter-sisip.
    assert statistik["publication_authors_inserted"] == 3


def test_co_author_dikenal_di_cache_memakai_id_lama(monkeypatch):
    """Co-author yang sudah ada di core.authors memakai id lamanya.

    Cache diisi dari tabel saat mulai; nama yang dikenal resolve ke
    author_id yang sudah ada - tanpa INSERT, tanpa author_id NULL.
    """
    statistik, conn, rekaman = _jalankan(
        monkeypatch, PEMILIK_LENGKAP, ["TAD Kuntjoro", "AS Albana"],
        existing=[(67, PEMILIK_LENGKAP), (88, "AS Albana")])

    assert conn.tautan[0][1] == 67
    assert conn.tautan[1][1] == 88
    assert conn.tautan[1][3] == "AS Albana"
    assert rekaman["dibuat"] == []
    assert statistik["authors_created"] == 0
    assert statistik["publication_authors_repointed"] == 0


def test_bentuk_pendek_yang_sudah_jadi_author_direwrite_ke_pemilik(monkeypatch):
    """Nama pendek yang sudah menempel di author lain (run sebelum fix)
    dialihkan ke author_id pemilik. Link lama dihapus untuk
    publikasi+urutan yang sama (identitas: publication_id +
    author_order - migrasi 14).
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
    assert conn.dihapus == [(2, 0)]


def test_repoint_kasus_khodijah_amiroh(monkeypatch):
    """Kasus nyata: pemilik id=238, tiruan K Amiroh id=179 sudah ada."""
    statistik, conn, rekaman = _jalankan(
        monkeypatch, "Khodijah Amiroh", ["K Amiroh"],
        existing=[(238, "Khodijah Amiroh"), (179, "K Amiroh")])

    assert statistik["author_id"] == 238
    assert conn.tautan[0][1] == 238
    assert conn.dihapus == [(2, 0)]
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


# ============================================================
# 3. INTEGRASI: _varian_nama_pemilik di jalur halaman detail
# ============================================================
# Regressi nyata: publikasi 48 (sumber detail) menulis pemilik
# sebagai "Tri Agus D Kuntjoro". Tanpa aturan varian, resolver
# memakai author tiruan 3315 dan tautan 48|67 hilang.


def test_varian_pemilik_jalur_detail_menempel_di_pemilik(monkeypatch):
    """Jalur detail: 'Tri Agus D Kuntjoro' -> author_id PEMILIK (67).

    Author tiruan 3315 yang sudah ada TIDAK dipakai dan TIDAK
    dihapus; hanya tautannya yang tidak lagi menunjuk ke sana.
    """
    statistik, conn, rekaman = _jalankan(
        monkeypatch, PEMILIK_LENGKAP, ["Tri Agus D Kuntjoro"],
        existing=[(67, PEMILIK_LENGKAP), (3315, "Tri Agus D Kuntjoro")],
        sumber="detail")

    assert statistik["author_id"] == 67
    assert len(conn.tautan) == 1
    assert conn.tautan[0][1] == 67
    assert conn.tautan[0][1] != 3315
    # Tidak ada author baru: varian diterjemahkan ke pemilik.
    assert rekaman["dibuat"] == []
    assert statistik["authors_created"] == 0
    assert statistik["publication_authors_inserted"] == 1


def test_varian_pemilik_tidak_berlaku_di_jalur_daftar(monkeypatch):
    """Jalur daftar: aturan varian TIDAK dipakai (hanya _bentuk_pendek).

    'Tri Agus D Kuntjoro' bukan bentuk pendek, jadi tetap menempel
    ke author yang sudah ada (3315) - bukan dipaksa ke pemilik.
    Ini membuktikan aturan varian memang terbatas pada jalur detail.
    """
    statistik, conn, rekaman = _jalankan(
        monkeypatch, PEMILIK_LENGKAP, ["Tri Agus D Kuntjoro"],
        existing=[(67, PEMILIK_LENGKAP), (3315, "Tri Agus D Kuntjoro")],
        sumber=None)

    assert statistik["author_id"] == 67
    assert conn.tautan[0][1] == 3315
    assert rekaman["dibuat"] == []
    assert statistik["authors_created"] == 0


def test_bentuk_pendek_tetap_berlaku_di_jalur_detail(monkeypatch):
    """Jalur detail: _bentuk_pendek tetap dipakai (aturan digabung).

    'TAD Kuntjoro' (nama pendek halaman daftar) yang muncul di
    jalur detail tetap diterjemahkan ke pemilik.
    """
    statistik, conn, rekaman = _jalankan(
        monkeypatch, PEMILIK_LENGKAP, ["TAD Kuntjoro"],
        existing=[(67, PEMILIK_LENGKAP), (78, "TAD Kuntjoro")],
        sumber="detail")

    assert statistik["author_id"] == 67
    assert conn.tautan[0][1] == 67
    assert rekaman["dibuat"] == []
    assert statistik["authors_created"] == 0


# ============================================================
# 4. KOLOM BARU: author_name / author_type di
#    core.publication_authors, dan program_study pemilik di
#    core.authors (dari Excel referensi) - tanpa database.
# ============================================================


def test_tautan_membawa_author_name_dan_author_type(monkeypatch):
    """author_name + author_type mapper diteruskan apa adanya ke INSERT.

    Keduanya kini kolom NYATA di core.publication_authors; nilai yang
    disimpan adalah salinan langsung dari dict tautan mapper (bukan
    hasil resolusi nama).
    """
    statistik, conn, rekaman = _jalankan(
        monkeypatch, PEMILIK_LENGKAP, ["TAD Kuntjoro", "AS Albana"])

    assert len(conn.tautan) == 2
    # Urutan pertama = penulis_pertama, sisanya co_author (kosakata
    # sama dengan author_role raw.scholar_publication_author).
    assert conn.tautan[0][3] == "TAD Kuntjoro"
    assert conn.tautan[0][4] == "penulis_pertama"
    assert conn.tautan[1][3] == "AS Albana"
    assert conn.tautan[1][4] == "co_author"


def test_stmt_sisip_publication_authors_kolom_dan_on_conflict():
    """INSERT memuat author_name/author_type; target unik per-posisi."""
    stmt = postgres._stmt_sisip_publication_authors(
        10, 20, 0, author_name="Budi Santoso", author_type="penulis_pertama")
    compiled = stmt.compile(dialect=postgresql.dialect())
    assert compiled.params["author_name"] == "Budi Santoso"
    assert compiled.params["author_type"] == "penulis_pertama"
    sql = str(compiled)
    # Target ON CONFLICT (migrasi 14): unique (publication_id,
    # author_order) - bukan lagi tripel dengan author_id yang kini
    # bisa NULL.
    assert "ON CONFLICT (publication_id, author_order) DO NOTHING" in sql
    assert "(publication_id, author_id, author_order)" not in sql


def test_stmt_sisip_publication_authors_tanpa_nama_tetap_null():
    """Tanpa author_name/author_type (panggilan lama) keduanya NULL."""
    stmt = postgres._stmt_sisip_publication_authors(10, 20, 1)
    compiled = stmt.compile(dialect=postgresql.dialect())
    assert compiled.params["author_name"] is None
    assert compiled.params["author_type"] is None


def test_stmt_sisip_publication_authors_author_id_null(monkeypatch):
    """author_id NULL (co-author tidak dikenal) tetap sah di INSERT."""
    statistik, conn, rekaman = _jalankan(
        monkeypatch, PEMILIK_LENGKAP, ["TAD Kuntjoro", "AS Albana"])

    # Tautan kedua: author_id None diteruskan apa adanya (bukan
    # diganti 0 / string "NULL" / dilempar).
    assert conn.tautan[1][1] is None
    assert conn.tautan[1][3] == "AS Albana"


def test_stmt_hapus_tautan_identitas_publikasi_dan_urutan():
    """DELETE repoint berdasar (publication_id, author_order) saja."""
    stmt = postgres._stmt_hapus_tautan(12, 3)
    compiled = stmt.compile(dialect=postgresql.dialect())
    sql = str(compiled)
    assert "WHERE" in sql
    assert "publication_id" in sql
    assert "author_order" in sql
    # author_id TIDAK ada di WHERE - tidak lagi bagian identitas.
    assert "author_id" not in sql
    # Nama param hasil compile memakai sufiks _1 (bentrok dengan
    # nama kolom) - lihat pola sama di _stmt_isi_program_study_author.
    assert compiled.params["publication_id_1"] == 12
    assert compiled.params["author_order_1"] == 3


def test_stmt_sisip_publication_authors_pk_surrogate_dan_unik():
    """Mirror: PK surrogate publication_author_id + unique per-posisi."""
    tabel = postgres.PUBLICATION_AUTHORS
    pk = [kolom.name for kolom in tabel.primary_key.columns]
    assert pk == ["publication_author_id"]
    # author_id nullable: co-author tidak dikenal disimpan NULL.
    assert tabel.c.author_id.nullable is True
    # Ada UniqueConstraint (publication_id, author_order) - identitas
    # baris pasca-migrasi 14, sekaligus target ON CONFLICT INSERT.
    batas_unik = [
        batas for batas in tabel.constraints
        if isinstance(batas, UniqueConstraint)
    ]
    assert len(batas_unik) == 1
    assert [kolom.name for kolom in batas_unik[0].columns] == [
        "publication_id", "author_order"]


def test_stmt_sisip_publikasi_returning_publication_id():
    """RETURNING memakai kolom publication_id (rename dari
    publications_id), bukan nama lama."""
    baris = {"title": "Judul", "publication_year": 2024}
    stmt = postgres._stmt_sisip_publikasi(baris)
    sql = str(stmt.compile(dialect=postgresql.dialect()))
    assert "RETURNING core.publications.publication_id" in sql
    assert "publications_id" not in sql


def test_program_study_pemilik_dari_referensi_terverifikasi(monkeypatch):
    """Referensi valid (id cocok + nama cocok eksak) -> program_study
    pemilik terisi; co-author tetap None."""
    referensi = [{
        "no": 1,
        "nama": PEMILIK_LENGKAP,
        "scholar_id": "8kDg_v4AAAAJ",
        "prodi": "Teknik Informatika",
    }]
    statistik, conn, rekaman = _jalankan(
        monkeypatch, PEMILIK_LENGKAP, ["TAD Kuntjoro", "AS Albana"],
        baris_referensi=referensi)

    # Sejak migrasi 14 hanya pemilik yang dibuat - co-author tidak
    # dikenal disimpan author_id NULL tanpa author baru, jadi prodi
    # tercatat hanya untuk pemilik.
    assert rekaman["prodi"] == [
        (PEMILIK_LENGKAP, "Teknik Informatika"),
    ]
    assert statistik["author_id"] is not None
    # Tautan co-author tetap ada dengan author_id NULL.
    assert conn.tautan[1][1] is None


def test_program_study_none_tanpa_referensi(monkeypatch):
    """baris_referensi None -> program_study None, perilaku lama."""
    statistik, conn, rekaman = _jalankan(
        monkeypatch, PEMILIK_LENGKAP, ["TAD Kuntjoro"])

    assert rekaman["prodi"] == [(PEMILIK_LENGKAP, None)]


def test_program_study_none_bila_verifikasi_gagal(monkeypatch):
    """Nama di referensi tidak cocok dengan nama profil -> None,
    tanpa penebakan (gerbang K8 yang sama dengan jalur raw)."""
    referensi = [{
        "no": 1,
        "nama": "Orang Lain",
        "scholar_id": "8kDg_v4AAAAJ",
        "prodi": "Teknik Informatika",
    }]
    statistik, conn, rekaman = _jalankan(
        monkeypatch, PEMILIK_LENGKAP, ["TAD Kuntjoro"],
        baris_referensi=referensi)

    assert rekaman["prodi"] == [(PEMILIK_LENGKAP, None)]


def test_stmt_buat_author_program_study_dan_coalesce():
    """INSERT memuat program_study; DO UPDATE memakai COALESCE
    supaya NULL baru tidak menghapus nilai lama."""
    stmt = postgres._stmt_buat_author(
        "Budi", scholar_id="ID1", program_study="Teknik Informatika")
    compiled = stmt.compile(dialect=postgresql.dialect())
    assert compiled.params["program_study"] == "Teknik Informatika"
    sql = str(compiled).replace(" ", "")
    # COALESCE di DO UPDATE SET untuk program_study (dan scholar_id
    # yang sudah ada sejak sebelumnya).
    assert "program_study=coalesce" in sql
    assert "scholar_id=coalesce" in sql


def test_stmt_buat_author_tanpa_program_study_tetap_kolom_null():
    """Panggilan tanpa program_study (co-author) -> NULL, dan
    DO UPDATE tetap COALESCE (NULL tidak menimpa nilai lama)."""
    stmt = postgres._stmt_buat_author("Budi")
    compiled = stmt.compile(dialect=postgresql.dialect())
    assert compiled.params["program_study"] is None
    assert "program_study=coalesce" in str(compiled).replace(" ", "")


def test_stmt_isi_program_study_author_coalesce_saat_null():
    """UPDATE isi-saat-NULL: COALESCE(program_study lama, nilai baru)
    - nilai lama yang non-NULL tidak pernah tertimpa."""
    stmt = postgres._stmt_isi_program_study_author(
        67, "Teknik Informatika")
    compiled = stmt.compile(dialect=postgresql.dialect())
    sql = str(compiled).replace(" ", "")
    assert "UPDATEcore.authorsSETprogram_study=coalesce(core.authors.program_study" in sql
    assert "WHEREcore.authors.author_id=" in sql
    assert compiled.params["coalesce_1"] == "Teknik Informatika"
    assert compiled.params["author_id_1"] == 67


def test_pemilik_cache_hit_tetap_menerima_program_study(monkeypatch):
    """Pemilik SUDAH ADA di tabel (cache hit) -> UPDATE tetap
    dieksekusi, jadi prodi Excel tidak hilang percuma.

    Ini celah yang ditambal _stmt_isi_program_study_author: pada
    cache hit resolver tidak memanggil buat_author sama sekali,
    jadi tanpa UPDATE ini prodi tidak pernah sampai ke database -
    dan di production pemilik hampir selalu sudah ada.
    """
    referensi = [{
        "no": 1,
        "nama": PEMILIK_LENGKAP,
        "scholar_id": "8kDg_v4AAAAJ",
        "prodi": "Teknik Informatika",
    }]
    statistik, conn, rekaman = _jalankan(
        monkeypatch, PEMILIK_LENGKAP, ["TAD Kuntjoro"],
        existing=[(67, PEMILIK_LENGKAP)],
        baris_referensi=referensi)

    # Resolver: pemilik dari cache, tanpa INSERT author baru.
    assert statistik["author_id"] == 67
    assert rekaman["dibuat"] == []
    # TAPI UPDATE program_study tetap terbang ke koneksi, dengan
    # author_id pemilik dan prodi yang lolos verifikasi.
    assert conn.prodi_author == [(67, "Teknik Informatika")]
    # Tautan tetap menempel ke pemilik (perilaku lama utuh).
    assert conn.tautan[0][1] == 67


def test_pemilik_baru_dibuat_juga_mengirim_update_program_study(monkeypatch):
    """Pemilik BARU dibuat: UPDATE ikut dieksekusi (no-op di DB
    karena kolomnya sudah terisi lewat UPSERT) - jalur seragam,
    tanpa cabang khusus."""
    referensi = [{
        "no": 1,
        "nama": PEMILIK_LENGKAP,
        "scholar_id": "8kDg_v4AAAAJ",
        "prodi": "Teknik Informatika",
    }]
    statistik, conn, rekaman = _jalankan(
        monkeypatch, PEMILIK_LENGKAP, ["TAD Kuntjoro"],
        baris_referensi=referensi)

    assert statistik["author_id"] == 1  # id baru dari store palsu
    assert conn.prodi_author == [(1, "Teknik Informatika")]


def test_tanpa_program_study_tidak_mengirim_update(monkeypatch):
    """program_study None (tanpa referensi / verifikasi gagal) ->
    statement UPDATE TIDAK dieksekusi sama sekali.

    Sudah tercakup sebagian oleh test_program_study_none_* di
    atas; ini memastikannya lewat sisi koneksi tiruan juga.
    """
    statistik, conn, rekaman = _jalankan(
        monkeypatch, PEMILIK_LENGKAP, ["TAD Kuntjoro"])
    assert conn.prodi_author == []

    referensi_gagal = [{
        "no": 1,
        "nama": "Orang Lain",
        "scholar_id": "8kDg_v4AAAAJ",
        "prodi": "Teknik Informatika",
    }]
    statistik2, conn2, rekaman2 = _jalankan(
        monkeypatch, PEMILIK_LENGKAP, ["TAD Kuntjoro"],
        baris_referensi=referensi_gagal)
    assert conn2.prodi_author == []


# ============================================================
# load_scholar_run: mode raw-only
# ============================================================

def _envelope_uji(data=None, status="SUCCESS"):
    """Envelope 9 kunci minimal untuk load_scholar_run."""
    return {
        "run_id": "run-uji-1",
        "source": "google_scholar",
        "started_at": "2026-10-10T00:00:00Z",
        "finished_at": "2026-10-10T00:01:00Z",
        "status": status,
        "records_fetched": 1,
        "records_failed": 0,
        "error_message": None,
        "data": data,
    }


def _pasti_tidak_dipanggil(*args, **kwargs):
    raise AssertionError("fungsi ini tidak boleh dipanggil di mode raw-only")


def test_load_scholar_run_raw_only_menyimpan_raw_dan_berhenti(monkeypatch):
    """raw_only: write_raw_scrape_result dipanggil, to_core_rows dan
    load_core_rows TIDAK dipanggil, status RAW_ONLY."""
    panggilan = {"raw": 0}

    def raw_palsu(envelope, raw_conn=None, scholar_id=None, scraped_at=None,
                  baris_referensi=None, keluaran_author=None):
        panggilan["raw"] += 1
        return 42

    monkeypatch.setattr(postgres, "write_raw_scrape_result", raw_palsu)
    monkeypatch.setattr(postgres, "to_core_rows", _pasti_tidak_dipanggil)
    monkeypatch.setattr(postgres, "load_core_rows", _pasti_tidak_dipanggil)

    statistik = postgres.load_scholar_run(
        _envelope_uji(data={"name": "Budi", "rows": []}),
        scholar_id="ID_A",
        raw_only=True,
    )

    assert panggilan["raw"] == 1
    assert statistik["status"] == postgres.STATUS_RAW_ONLY
    assert statistik["status"] != postgres.STATUS_LOADED
    assert statistik["raw_result_id"] == 42
    assert statistik["run_id"] == "run-uji-1"


def test_load_scholar_run_raw_only_tanpa_koneksi_core(monkeypatch):
    """raw_only tidak butuh koneksi core: conn=None diterima."""
    monkeypatch.setattr(postgres, "write_raw_scrape_result",
                        lambda envelope, raw_conn=None, scholar_id=None,
                               scraped_at=None, baris_referensi=None,
                               keluaran_author=None: 7)
    monkeypatch.setattr(postgres, "to_core_rows", _pasti_tidak_dipanggil)
    monkeypatch.setattr(postgres, "load_core_rows", _pasti_tidak_dipanggil)

    statistik = postgres.load_scholar_run(
        _envelope_uji(data={"name": "Budi", "rows": []}),
        scholar_id="ID_A",
        conn=None,
        raw_only=True,
    )
    assert statistik["status"] == postgres.STATUS_RAW_ONLY


def test_load_scholar_run_raw_only_mengabaikan_koneksi_core(monkeypatch):
    """raw_only: conn yang sama dengan raw_conn tidak ditolak (conn tak dipakai)."""
    koneksi = object()
    monkeypatch.setattr(postgres, "write_raw_scrape_result",
                        lambda envelope, raw_conn=None, scholar_id=None,
                               scraped_at=None, baris_referensi=None,
                               keluaran_author=None: 7)
    monkeypatch.setattr(postgres, "to_core_rows", _pasti_tidak_dipanggil)
    monkeypatch.setattr(postgres, "load_core_rows", _pasti_tidak_dipanggil)

    statistik = postgres.load_scholar_run(
        _envelope_uji(data={"name": "Budi", "rows": []}),
        scholar_id="ID_A",
        conn=koneksi,
        raw_conn=koneksi,
        raw_only=True,
    )
    assert statistik["status"] == postgres.STATUS_RAW_ONLY


def test_load_scholar_run_raw_only_gagal_raw_tidak_dilaporkan_sukses(monkeypatch):
    """Kegagalan write_raw_scrape_result -> LoaderError, bukan RAW_ONLY."""
    def raw_gagal(envelope, raw_conn=None, scholar_id=None, scraped_at=None,
                  baris_referensi=None, keluaran_author=None):
        raise postgres.LoaderError("gagal menyimpan raw")

    monkeypatch.setattr(postgres, "write_raw_scrape_result", raw_gagal)
    monkeypatch.setattr(postgres, "to_core_rows", _pasti_tidak_dipanggil)
    monkeypatch.setattr(postgres, "load_core_rows", _pasti_tidak_dipanggil)

    with pytest.raises(postgres.LoaderError):
        postgres.load_scholar_run(
            _envelope_uji(data={"name": "Budi", "rows": []}),
            scholar_id="ID_A",
            raw_only=True,
        )


def test_load_scholar_run_raw_only_run_fatal_mengembalikan_status_scraper(monkeypatch):
    """Run fatal (data None) di raw-only: raw tetap ditulis, tetapi status
    yang dikembalikan adalah status scraper ASLI (BLOCKED), bukan RAW_ONLY -
    raw tersimpan bukan berarti scraping sukses."""
    panggilan = {"raw": 0}

    def raw_palsu(envelope, raw_conn=None, scholar_id=None, scraped_at=None,
                  baris_referensi=None, keluaran_author=None):
        panggilan["raw"] += 1
        return 42

    monkeypatch.setattr(postgres, "write_raw_scrape_result", raw_palsu)
    monkeypatch.setattr(postgres, "to_core_rows", _pasti_tidak_dipanggil)
    monkeypatch.setattr(postgres, "load_core_rows", _pasti_tidak_dipanggil)

    statistik = postgres.load_scholar_run(
        _envelope_uji(data=None, status="BLOCKED"),
        scholar_id="ID_A",
        raw_only=True,
    )
    assert panggilan["raw"] == 1
    assert statistik["status"] == "BLOCKED"
    assert statistik["status"] != postgres.STATUS_RAW_ONLY
    assert statistik["raw_result_id"] == 42


@pytest.mark.parametrize("status_scraper", [
    "BLOCKED", "TIMEOUT", "NO_DATA", "PARSING_ERROR",
])
def test_load_scholar_run_raw_only_status_fatal_mengembalikan_status_asli(
        monkeypatch, status_scraper):
    """Semua status fatal di raw-only: raw ditulis, status = status scraper
    asli (bukan RAW_ONLY), to_core_rows/load_core_rows tidak dipanggil."""
    panggilan = {"raw": 0}

    def raw_palsu(envelope, raw_conn=None, scholar_id=None, scraped_at=None,
                  baris_referensi=None, keluaran_author=None):
        panggilan["raw"] += 1
        return 42

    monkeypatch.setattr(postgres, "write_raw_scrape_result", raw_palsu)
    monkeypatch.setattr(postgres, "to_core_rows", _pasti_tidak_dipanggil)
    monkeypatch.setattr(postgres, "load_core_rows", _pasti_tidak_dipanggil)

    statistik = postgres.load_scholar_run(
        _envelope_uji(data=None, status=status_scraper),
        scholar_id="ID_A",
        raw_only=True,
    )
    assert panggilan["raw"] == 1
    assert statistik["status"] == status_scraper
    assert statistik["status"] != postgres.STATUS_RAW_ONLY
    assert statistik["raw_result_id"] == 42
    assert statistik["run_id"] == "run-uji-1"


def test_load_scholar_run_raw_only_partial_success_tetap_raw_only(monkeypatch):
    """PARTIAL_SUCCESS di raw-only: ada data -> sukses untuk mode ini,
    status tetap RAW_ONLY."""
    monkeypatch.setattr(postgres, "write_raw_scrape_result",
                        lambda envelope, raw_conn=None, scholar_id=None,
                               scraped_at=None, baris_referensi=None,
                               keluaran_author=None: 42)
    monkeypatch.setattr(postgres, "to_core_rows", _pasti_tidak_dipanggil)
    monkeypatch.setattr(postgres, "load_core_rows", _pasti_tidak_dipanggil)

    statistik = postgres.load_scholar_run(
        _envelope_uji(data={"name": "Budi", "rows": []},
                      status="PARTIAL_SUCCESS"),
        scholar_id="ID_A",
        raw_only=True,
    )
    assert statistik["status"] == postgres.STATUS_RAW_ONLY
    assert statistik["raw_result_id"] == 42


def test_load_scholar_run_mode_lama_tetap_dua_tahap(monkeypatch):
    """Tanpa raw_only: to_core_rows + load_core_rows tetap dipanggil."""
    panggilan = {"raw": 0, "transform": 0, "core": 0}

    def raw_palsu(envelope, raw_conn=None, scholar_id=None, scraped_at=None,
                  baris_referensi=None, keluaran_author=None):
        panggilan["raw"] += 1
        return 42

    def transform_palsu(data, retrieved_at=None):
        panggilan["transform"] += 1
        return {"publications": []}

    def core_palsu(core, conn=None, run_id=None, profil=None,
                   baris_referensi=None):
        panggilan["core"] += 1
        stat = postgres._stats_kosong()
        stat["status"] = postgres.STATUS_LOADED
        return stat

    monkeypatch.setattr(postgres, "write_raw_scrape_result", raw_palsu)
    monkeypatch.setattr(postgres, "to_core_rows", transform_palsu)
    monkeypatch.setattr(postgres, "load_core_rows", core_palsu)

    statistik = postgres.load_scholar_run(
        _envelope_uji(data={"name": "Budi", "rows": []}),
        scholar_id="ID_A",
    )
    assert panggilan["raw"] == 1
    assert panggilan["transform"] == 1
    assert panggilan["core"] == 1
    assert statistik["status"] == postgres.STATUS_LOADED
    assert statistik["raw_result_id"] == 42


def test_load_scholar_run_run_fatal_mode_lama_tetap_skipped(monkeypatch):
    """Tanpa raw_only, run fatal (data None) -> SKIPPED_NO_DATA."""
    monkeypatch.setattr(postgres, "write_raw_scrape_result",
                        lambda envelope, raw_conn=None, scholar_id=None,
                               scraped_at=None, baris_referensi=None,
                               keluaran_author=None: 42)
    monkeypatch.setattr(postgres, "to_core_rows", _pasti_tidak_dipanggil)
    monkeypatch.setattr(postgres, "load_core_rows", _pasti_tidak_dipanggil)

    statistik = postgres.load_scholar_run(
        _envelope_uji(data=None, status="TIMEOUT"),
        scholar_id="ID_A",
    )
    assert statistik["status"] == postgres.STATUS_SKIPPED
    assert statistik["raw_result_id"] == 42


# ============================================================
# P58 + G2-B - integrasi helper author (P57) ke
# write_raw_scrape_result dan load_scholar_run: komputasi record
# author dan PENYIMPANAN AKTIF ke raw.scholar_publication_author
# (DDL sql/ddl/10; keputusan D1-D4). Memakai koneksi tiruan
# _KoneksiRawPalsu; TIDAK PERNAH menyentuh database.
# ============================================================

# Sembilan kunci statistik author yang dijanjikan keluaran_author dan
# digabung additif ke statistik load_scholar_run.
_KUNCI_STATISTIK_AUTHOR = (
    "author_total",
    "author_pemilik_profil",
    "author_bukan_pemilik",
    "author_belum_terverifikasi",
    "publikasi_daftar_terpotong",
    "publikasi_tanpa_penulis",
    "author_disimpan",
    "author_dilewati_konflik",
    "penyimpanan_author",
)


def test_write_raw_author_dari_detail_authors():
    """1) detail_authors terisi -> record dari detail, sumber "detail",
    author_total benar, nama persis tanpa rekayasa."""
    conn = _KoneksiRawPalsu()
    keluaran = {}
    envelope = _envelope_uji(data={
        "name": "Tri Agus Djoko Kuntjoro",
        "rows": [{
            "title": "Publikasi Satu",
            "authors": "TAD Kuntjoro, Siti Aminah",
            "detail_authors": "Tri Agus Djoko Kuntjoro, Siti Aminah",
        }],
    })
    result_id = postgres.write_raw_scrape_result(
        envelope, "8kDg_v4AAAAJ", raw_conn=conn, keluaran_author=keluaran)

    assert result_id == 11
    records = keluaran["records"]
    assert len(records) == 1
    assert [r["author_name"] for r in records[0]] == [
        "Tri Agus Djoko Kuntjoro", "Siti Aminah"]
    assert all(r["sumber"] == "detail" for r in records[0])
    assert all(r["daftar_terpotong"] is False for r in records[0])
    statistik = keluaran["statistik"]
    assert statistik["author_total"] == 2
    assert statistik["penyimpanan_author"] == "tersimpan"
    assert statistik["author_disimpan"] == 2
    assert statistik["author_dilewati_konflik"] == 0


def test_write_raw_author_fallback_daftar_saats_detail_kosong():
    """2) detail_authors None (gaya 429/blokir) -> fallback daftar,
    sumber "daftar", nama persis dari kolom authors, tanpa nama
    karangan."""
    conn = _KoneksiRawPalsu()
    keluaran = {}
    envelope = _envelope_uji(data={
        "name": "Budi Santoso",
        "rows": [{
            "title": "Publikasi Satu",
            "authors": "Budi Santoso, Siti Aminah",
            "detail_authors": None,
        }],
    })
    postgres.write_raw_scrape_result(
        envelope, "ID_A", raw_conn=conn, keluaran_author=keluaran)

    records = keluaran["records"]
    assert len(records) == 1
    assert [r["author_name"] for r in records[0]] == [
        "Budi Santoso", "Siti Aminah"]
    assert all(r["sumber"] == "daftar" for r in records[0])
    assert keluaran["statistik"]["author_total"] == 2


def test_write_raw_author_daftar_terpotong_terhitung():
    """3) Daftar berelipsis "A B, C D, ..." -> elipsis dibuang dari
    nama, daftar_terpotong True, publikasi_daftar_terpotong = 1."""
    conn = _KoneksiRawPalsu()
    keluaran = {}
    envelope = _envelope_uji(data={
        "name": "Budi Santoso",
        "rows": [{
            "title": "Publikasi Satu",
            "authors": "Andi Budi, Citra Dewi, ...",
            "detail_authors": None,
        }],
    })
    postgres.write_raw_scrape_result(
        envelope, "ID_A", raw_conn=conn, keluaran_author=keluaran)

    records = keluaran["records"][0]
    assert [r["author_name"] for r in records] == ["Andi Budi", "Citra Dewi"]
    assert all(r["daftar_terpotong"] is True for r in records)
    statistik = keluaran["statistik"]
    assert statistik["publikasi_daftar_terpotong"] == 1
    assert statistik["author_total"] == 2


def test_write_raw_author_pemilik_profil_terverifikasi():
    """4) baris_referensi valid + nama profil cocok eksak -> tepat satu
    record pemilik_profil dengan scholar_id run; statistik cocok."""
    conn = _KoneksiRawPalsu()
    keluaran = {}
    envelope = _envelope_uji(data={
        "name": "Tri Agus Djoko Kuntjoro",
        "rows": [{
            "title": "Publikasi Satu",
            "detail_authors": "Siti Aminah, Tri Agus Djoko Kuntjoro",
        }],
    })
    referensi = [{
        "no": 1,
        "nama": "Tri Agus Djoko Kuntjoro",
        "scholar_id": "8kDg_v4AAAAJ",
    }]
    postgres.write_raw_scrape_result(
        envelope, "8kDg_v4AAAAJ", raw_conn=conn,
        baris_referensi=referensi, keluaran_author=keluaran)

    records = keluaran["records"][0]
    assert [r["profile_owner_status"] for r in records] == [
        "bukan_pemilik_profil", "pemilik_profil"]
    assert records[1]["scholar_id"] == "8kDg_v4AAAAJ"
    assert records[0]["scholar_id"] is None
    statistik = keluaran["statistik"]
    assert statistik["author_pemilik_profil"] == 1
    assert statistik["author_bukan_pemilik"] == 1
    assert statistik["author_belum_terverifikasi"] == 0


def test_write_raw_author_referensi_id_tidak_ditemukan():
    """5) Referensi ada tapi id run tidak ditemukan -> semua record
    belum_terverifikasi, nol pemilik, belum == total."""
    conn = _KoneksiRawPalsu()
    keluaran = {}
    envelope = _envelope_uji(data={
        "name": "Tri Agus Djoko Kuntjoro",
        "rows": [{
            "title": "Publikasi Satu",
            "detail_authors": "Tri Agus Djoko Kuntjoro, Siti Aminah",
        }],
    })
    referensi = [{"no": 1, "nama": "Orang Lain", "scholar_id": "ID_LAIN"}]
    postgres.write_raw_scrape_result(
        envelope, "8kDg_v4AAAAJ", raw_conn=conn,
        baris_referensi=referensi, keluaran_author=keluaran)

    records = keluaran["records"][0]
    assert all(r["profile_owner_status"] == "belum_terverifikasi"
               for r in records)
    assert all(r["scholar_id"] is None for r in records)
    statistik = keluaran["statistik"]
    assert statistik["author_pemilik_profil"] == 0
    assert statistik["author_belum_terverifikasi"] == statistik["author_total"]


def test_write_raw_author_run_fatal_data_none():
    """6) data=None (run fatal) -> records [], semua hitungan 0,
    penyimpanan_author "tidak_disimpan"; hanya envelope yang
    dieksekusi (tidak ada tabel terstruktur yang disentuh)."""
    conn = _KoneksiRawPalsu()
    keluaran = {}
    envelope = _envelope_uji(data=None, status="BLOCKED")
    result_id = postgres.write_raw_scrape_result(
        envelope, "ID_A", raw_conn=conn, keluaran_author=keluaran)

    assert result_id == 11
    assert keluaran["records"] == []
    statistik = keluaran["statistik"]
    for kunci in _KUNCI_STATISTIK_AUTHOR:
        assert kunci in statistik
    assert statistik["author_total"] == 0
    assert statistik["author_pemilik_profil"] == 0
    assert statistik["author_bukan_pemilik"] == 0
    assert statistik["author_belum_terverifikasi"] == 0
    assert statistik["publikasi_daftar_terpotong"] == 0
    assert statistik["publikasi_tanpa_penulis"] == 0
    assert statistik["author_disimpan"] == 0
    assert statistik["author_dilewati_konflik"] == 0
    assert statistik["penyimpanan_author"] == "tidak_disimpan"
    # Run fatal: hanya envelope; tidak ada tabel terstruktur.
    assert conn.tabel_dieksekusi == ["scholar_scrape_result"]


def test_write_raw_author_pola_execute_lama_ditambah_insert_author():
    """7) keluaran_author terisi dan pola execute TETAP pola lama +
    INSERT author (G2-B, D2: transaksi yang sama, tanpa tabel baru di
    luar pola): 1 baris publikasi 2 penulis = result + scholar_author
    + publikasi + 2 author; tanpa tabel di luar empat tabel raw."""
    conn = _KoneksiRawPalsu()
    keluaran = {}
    envelope = _envelope_uji(data={
        "name": "Budi Santoso",
        "rows": [{
            "title": "Publikasi Satu",
            "authors": "Budi Santoso, Siti Aminah",
        }],
    })
    result_id = postgres.write_raw_scrape_result(
        envelope, "ID_A", raw_conn=conn, keluaran_author=keluaran)

    assert result_id == 11
    assert conn.transaksi is not None
    assert conn.transaksi.committed is True
    assert conn.tabel_dieksekusi == [
        "scholar_scrape_result", "scholar_author", "scholar_publication",
        "scholar_publication_author", "scholar_publication_author"]
    # Komputasi author mengisi keluaran tanpa menggeser pola execute.
    assert keluaran["statistik"]["author_total"] == 2
    assert len(keluaran["records"][0]) == 2


def test_load_scholar_run_raw_only_statistik_author_menyatu(monkeypatch):
    """8) raw_only: sembilan kunci author dari write_raw_scrape_result
    DIGABUNG ADDITIF ke statistik hasil; status & kunci lama tidak
    berubah. Writer tiruan di sini memilih "belum_aktif" - nilai itu
    sah sebagai jalur tiruan, bukan status penyimpanan sungguhan."""
    def raw_palsu(envelope, raw_conn=None, scholar_id=None, scraped_at=None,
                  baris_referensi=None, keluaran_author=None):
        if isinstance(keluaran_author, dict):
            keluaran_author["records"] = [[]]
            keluaran_author["statistik"] = {
                "author_total": 2,
                "author_pemilik_profil": 1,
                "author_bukan_pemilik": 1,
                "author_belum_terverifikasi": 0,
                "publikasi_daftar_terpotong": 0,
                "publikasi_tanpa_penulis": 0,
                "author_disimpan": 1,
                "author_dilewati_konflik": 1,
                "penyimpanan_author": "belum_aktif",
            }
        return 42

    monkeypatch.setattr(postgres, "write_raw_scrape_result", raw_palsu)
    monkeypatch.setattr(postgres, "to_core_rows", _pasti_tidak_dipanggil)
    monkeypatch.setattr(postgres, "load_core_rows", _pasti_tidak_dipanggil)

    statistik = postgres.load_scholar_run(
        _envelope_uji(data={"name": "Budi", "rows": []}),
        scholar_id="ID_A",
        raw_only=True,
    )
    # Kunci lama TIDAK berubah oleh penggabungan author.
    assert statistik["status"] == postgres.STATUS_RAW_ONLY
    assert statistik["raw_result_id"] == 42
    assert statistik["run_id"] == "run-uji-1"
    # Sembilan kunci author hadir dengan nilai dari write_raw_scrape_result.
    for kunci in _KUNCI_STATISTIK_AUTHOR:
        assert kunci in statistik
    assert statistik["author_total"] == 2
    assert statistik["author_pemilik_profil"] == 1
    assert statistik["author_disimpan"] == 1
    assert statistik["author_dilewati_konflik"] == 1
    assert statistik["penyimpanan_author"] == "belum_aktif"


def test_load_scholar_run_tanpa_baris_referensi_nol_pemilik():
    """9) baris_referensi default None: jalur asli tetap jalan dengan
    koneksi tiruan; statistik author hadir dengan nol pemilik (semua
    record belum_terverifikasi)."""
    conn_raw = _KoneksiRawPalsu()
    statistik = postgres.load_scholar_run(
        _envelope_uji(data={
            "name": "Tri Agus Djoko Kuntjoro",
            "rows": [{
                "title": "Publikasi Satu",
                "authors": "TAD Kuntjoro, Siti Aminah",
                "detail_authors": None,
            }],
        }),
        scholar_id="8kDg_v4AAAAJ",
        raw_conn=conn_raw,
        raw_only=True,
    )
    assert statistik["status"] == postgres.STATUS_RAW_ONLY
    for kunci in _KUNCI_STATISTIK_AUTHOR:
        assert kunci in statistik
    assert statistik["author_total"] == 2
    assert statistik["author_pemilik_profil"] == 0
    assert statistik["author_belum_terverifikasi"] == 2
    assert statistik["author_disimpan"] == 2
    assert statistik["author_dilewati_konflik"] == 0
    assert statistik["penyimpanan_author"] == "tersimpan"
    # Pola eksekusi: pola lama + INSERT author per record (2 baris).
    assert conn_raw.tabel_dieksekusi == [
        "scholar_scrape_result", "scholar_author", "scholar_publication",
        "scholar_publication_author", "scholar_publication_author"]


# ============================================================
# PRODI PEMILIK PROFIL DI raw.scholar_author (offline, koneksi
# tiruan): params INSERT profil membawa kolom prodi hasil gerbang
# K8 yang sama dengan verifikasi identitas. Tanpa database sungguhan.
# ============================================================

def test_write_raw_prodi_terverifikasi_masuk_params_insert_profil():
    """Referensi lolos gerbang K8 -> params INSERT profil memuat
    `prodi` dari Excel; kolom lain tidak berubah."""
    conn = _KoneksiRawPalsu()
    envelope = _envelope_uji(data={
        "name": "Budi Santoso",
        "rows": [{
            "title": "Publikasi Satu",
            "authors": "Budi Santoso, Siti Aminah",
        }],
    })
    referensi = [
        {"no": 1, "nama": "Budi Santoso", "scholar_id": "ID_A",
         "prodi": "Informatika"},
    ]
    postgres.write_raw_scrape_result(
        envelope, "ID_A", raw_conn=conn, baris_referensi=referensi)

    params_profil = _params_tabel(conn, "scholar_author", "insert")[0]
    assert params_profil["prodi"] == "Informatika"
    assert params_profil["scholar_id"] == "ID_A"
    assert params_profil["nama"] == "Budi Santoso"
    assert params_profil["urutan"] == 0


def test_write_raw_prodi_none_tanpa_referensi_dan_gagal_verifikasi():
    """Tanpa referensi ATAU id/nama tidak cocok ATAU sel prodi
    kosong -> prodi None (NULL) di params INSERT profil - tanpa
    tebakan, tanpa fuzzy."""
    envelope = _envelope_uji(data={
        "name": "Budi Santoso",
        "rows": [{"title": "Publikasi Satu", "authors": "Budi Santoso"}],
    })

    # (a) Tanpa referensi sama sekali.
    conn = _KoneksiRawPalsu()
    postgres.write_raw_scrape_result(envelope, "ID_A", raw_conn=conn)
    assert _params_tabel(
        conn, "scholar_author", "insert")[0]["prodi"] is None

    # (b) Id cocok tapi nama berbeda -> gagal verifikasi -> None.
    conn = _KoneksiRawPalsu()
    referensi = [{"no": 1, "nama": "Orang Lain", "scholar_id": "ID_A",
                  "prodi": "Informatika"}]
    postgres.write_raw_scrape_result(
        envelope, "ID_A", raw_conn=conn, baris_referensi=referensi)
    assert _params_tabel(
        conn, "scholar_author", "insert")[0]["prodi"] is None

    # (c) Lolos verifikasi tapi sel prodi kosong -> None.
    conn = _KoneksiRawPalsu()
    referensi = [{"no": 1, "nama": "Budi Santoso", "scholar_id": "ID_A",
                  "prodi": ""}]
    postgres.write_raw_scrape_result(
        envelope, "ID_A", raw_conn=conn, baris_referensi=referensi)
    assert _params_tabel(
        conn, "scholar_author", "insert")[0]["prodi"] is None


# ============================================================
# G2-B - KONTRAK D1-D4 penyimpanan author (lanjutan tes di atas):
# resolusi raw_publication_id, transaksi yang sama, INSERT idempoten
# ON CONFLICT, dan statistik hasil. Koneksi tiruan _KoneksiRawPalsu
# yang bisa diskrip; TIDAK PERNAH menyentuh database.
# ============================================================

def test_g2b_id_publikasi_returning_diteruskan_ke_author():
    """1) id publikasi dari RETURNING INSERT -> diteruskan persis ke
    argumen raw_publication_id INSERT author tiap record."""
    conn = _KoneksiRawPalsu()
    keluaran = {}
    envelope = _envelope_uji(data={
        "name": "Budi Santoso",
        "rows": [{"title": "Publikasi Satu",
                  "authors": "Budi Santoso, Siti Aminah"}],
    })
    # Skrip: INSERT publikasi baris 0 mengembalikan id 777.
    conn.skrip_publikasi = [777]

    postgres.write_raw_scrape_result(
        envelope, "ID_A", raw_conn=conn, keluaran_author=keluaran)

    params_author = _params_tabel(conn, "scholar_publication_author")
    assert len(params_author) == 2
    assert all(p["raw_publication_id"] == 777 for p in params_author)
    assert keluaran["statistik"]["author_disimpan"] == 2


def test_g2b_returning_kosong_select_fallback_dipakai():
    """2) RETURNING publikasi None (konflik retry) -> SELECT fallback
    dieksekusi dengan WHERE (result_id, urutan) yang benar, dan hasil
    fallback itulah yang dipakai untuk INSERT author."""
    conn = _KoneksiRawPalsu()
    keluaran = {}
    envelope = _envelope_uji(data={
        "name": "Budi Santoso",
        "rows": [{"title": "Publikasi Satu", "authors": "Budi Santoso"}],
    })
    conn.skrip_publikasi = [_KONFLIK]  # INSERT kena konflik -> None
    conn.skrip_fallback = [555]        # SELECT menemukan id baris lama

    postgres.write_raw_scrape_result(
        envelope, "ID_A", raw_conn=conn, keluaran_author=keluaran)

    # SELECT fallback dieksekusi tepat satu kali, dengan kunci yang
    # PERSIS dipakai konflik publikasi: result_id + urutan baris itu.
    # (Nama bind param memakai sufiks numerik SQLAlchemy -
    # result_id_1/urutan_1 - jadi yang diuji adalah NILAINYA.)
    pilih = _params_tabel(conn, "scholar_publication", jenis="select")
    assert len(pilih) == 1
    assert sorted(pilih[0].values()) == [0, 11]  # urutan=0, result_id=11
    # Hasil fallback itulah yang dipakai - bukan None, bukan id karangan.
    params_author = _params_tabel(conn, "scholar_publication_author")
    assert params_author[0]["raw_publication_id"] == 555
    statistik = keluaran["statistik"]
    assert statistik["penyimpanan_author"] == "tersimpan"
    assert statistik["author_disimpan"] == 1


def test_g2b_fallback_juga_kosong_loadererror_dan_rollback():
    """3) RETURNING None dan SELECT fallback juga None -> LoaderError
    eksplisit (publikasi baris 0 tidak bisa diresolusi) + rollback
    total transaksi; tidak ada klaim 'tersimpan' yang lolos."""
    conn = _KoneksiRawPalsu()
    keluaran = {}
    envelope = _envelope_uji(data={
        "name": "Budi Santoso",
        "rows": [{"title": "Publikasi Satu", "authors": "Budi Santoso"}],
    })
    conn.skrip_publikasi = [_KONFLIK]
    conn.skrip_fallback = [None]

    with pytest.raises(postgres.LoaderError) as tangkap:
        postgres.write_raw_scrape_result(
            envelope, "ID_A", raw_conn=conn, keluaran_author=keluaran)

    assert conn.transaksi is not None
    assert conn.transaksi.rolled_back is True
    assert conn.transaksi.committed is False
    # Pesan menyebut publikasi baris yang gagal diresolusi id-nya.
    assert "tidak bisa diresolusi" in str(tangkap.value)
    assert "baris 0" in str(tangkap.value)
    # Tidak ada klaim "tersimpan" pada dict yang bocor via referensi.
    assert keluaran["statistik"].get("penyimpanan_author") != "tersimpan"


def test_g2b_stmt_sisip_author_kolom_dan_on_conflict():
    """4) Compile test: builder baru memakai kolom skema yang benar,
    ON CONFLICT pada kunci UNIQUE DDL 10, dan RETURNING id-nya."""
    from src.loading import postgres as pgmod

    stmt = pgmod._stmt_sisip_raw_publication_author(
        raw_publication_id=42, author_order=1,
        author_name="Siti Aminah", author_role="co_author",
        scholar_id=None, profile_owner_status="belum_terverifikasi",
        sumber="daftar", daftar_terpotong=True)
    compiled = stmt.compile(dialect=postgresql.dialect())
    sql = str(compiled)

    assert "scholar_publication_author" in sql
    # Kolom skema yang benar semuanya ikut INSERT.
    for kolom in ("raw_publication_id", "author_order", "author_name",
                  "author_role", "scholar_id", "profile_owner_status",
                  "sumber", "daftar_terpotong"):
        assert kolom in sql, "kolom {} hilang dari INSERT".format(kolom)
    # ON CONFLICT persis di kunci UNIQUE (raw_publication_id,
    # author_order); TANPA UPDATE apa pun.
    assert ("ON CONFLICT (raw_publication_id, author_order) DO NOTHING"
            in sql)
    assert "UPDATE" not in sql.upper().replace("ON CONFLICT", "")
    # RETURNING id baris (pembeda sukses vs konflik).
    assert "raw_publication_author_id" in sql
    # Nilai diteruskan persis, termasuk None untuk scholar_id.
    assert compiled.params["raw_publication_id"] == 42
    assert compiled.params["author_order"] == 1
    assert compiled.params["author_name"] == "Siti Aminah"
    assert compiled.params["author_role"] == "co_author"
    assert compiled.params["scholar_id"] is None
    assert compiled.params["profile_owner_status"] == "belum_terverifikasi"
    assert compiled.params["sumber"] == "daftar"
    assert compiled.params["daftar_terpotong"] is True


def test_g2b_nilai_author_di_values_urutan_peran_nama_status():
    """5-7) Di VALUES INSERT author: author_order 0-based + author_role
    murni fungsi urutan; author_name PERSIS (multi-token, tidak
    dibalik); profile_owner_status TERPISAH dari author_role (co_author
    boleh pemilik_profil)."""
    conn = _KoneksiRawPalsu()
    keluaran = {}
    envelope = _envelope_uji(data={
        "name": "Siti Aminah",
        "rows": [{
            "title": "Publikasi Satu",
            "detail_authors": "Budi Santoso, Siti Aminah, "
                              "Tri Agus Djoko Kuntjoro",
        }],
    })
    referensi = [{"no": 1, "nama": "Siti Aminah", "scholar_id": "ID_A"}]

    postgres.write_raw_scrape_result(
        envelope, "ID_A", raw_conn=conn,
        baris_referensi=referensi, keluaran_author=keluaran)

    baris = _params_tabel(conn, "scholar_publication_author")
    assert len(baris) == 3
    # 5) urutan 0-based; peran murni fungsi urutan tampil.
    assert [b["author_order"] for b in baris] == [0, 1, 2]
    assert [b["author_role"] for b in baris] == [
        "penulis_pertama", "co_author", "co_author"]
    # 6) nama persis multi-token, tidak dibalik, tanpa recasing.
    assert [b["author_name"] for b in baris] == [
        "Budi Santoso", "Siti Aminah", "Tri Agus Djoko Kuntjoro"]
    # 7) pemilik profil ada di urutan 1 (co_author): dua dimensi
    #    berbeda yang tidak saling menentukan (K7).
    assert [b["profile_owner_status"] for b in baris] == [
        "bukan_pemilik_profil", "pemilik_profil", "bukan_pemilik_profil"]


def test_g2b_scholar_id_hanya_untuk_pemilik_terverifikasi():
    """8) scholar_id None untuk record belum/bukan pemilik; terisi
    HANYA untuk record pemilik_profil terverifikasi (baris_referensi
    tiruan); tidak pernah id karangan."""
    conn = _KoneksiRawPalsu()
    keluaran = {}
    envelope = _envelope_uji(data={
        "name": "Tri Agus Djoko Kuntjoro",
        "rows": [{
            "title": "Publikasi Satu",
            "detail_authors": "Siti Aminah, Tri Agus Djoko Kuntjoro",
        }],
    })
    referensi = [{"no": 1, "nama": "Tri Agus Djoko Kuntjoro",
                  "scholar_id": "8kDg_v4AAAAJ"}]

    postgres.write_raw_scrape_result(
        envelope, "8kDg_v4AAAAJ", raw_conn=conn,
        baris_referensi=referensi, keluaran_author=keluaran)

    baris = _params_tabel(conn, "scholar_publication_author")
    assert baris[0]["scholar_id"] is None
    assert baris[0]["profile_owner_status"] == "bukan_pemilik_profil"
    assert baris[1]["scholar_id"] == "8kDg_v4AAAAJ"
    assert baris[1]["profile_owner_status"] == "pemilik_profil"


def test_g2b_konflik_author_dihitung_dilewati_bukan_error():
    """9) INSERT author RETURNING None (konflik baris lama) ->
    author_dilewati_konflik naik; author_disimpan TIDAK naik; konflik
    bukan error, transaksi tetap ter-commit."""
    conn = _KoneksiRawPalsu()
    keluaran = {}
    envelope = _envelope_uji(data={
        "name": "Budi Santoso",
        "rows": [{"title": "Publikasi Satu",
                  "detail_authors": "Budi Santoso, Siti Aminah"}],
    })
    # Baris pertama sukses, baris kedua konflik.
    conn.skrip_author = [True, False]

    result_id = postgres.write_raw_scrape_result(
        envelope, "ID_A", raw_conn=conn, keluaran_author=keluaran)

    assert result_id == 11  # bukan error
    assert conn.transaksi is not None
    assert conn.transaksi.committed is True
    statistik = keluaran["statistik"]
    assert statistik["author_disimpan"] == 1
    assert statistik["author_dilewati_konflik"] == 1


def test_g2b_retry_penuh_tanpa_update_dan_nol_disimpan():
    """10) Retry penuh (publikasi konflik + SEMUA author konflik): tidak
    ada satu pun stmt UPDATE; nilai lama tidak tertimpa (semua
    dilewati); author_disimpan 0; penyimpanan_author tetap
    'tersimpan'."""
    conn = _KoneksiRawPalsu()
    keluaran = {}
    envelope = _envelope_uji(data={
        "name": "Budi Santoso",
        "rows": [{"title": "Publikasi Satu",
                  "detail_authors": "Budi Santoso, Siti Aminah"}],
    })
    conn.skrip_publikasi = [_KONFLIK]
    conn.skrip_fallback = [900]
    conn.skrip_author = [False, False]

    postgres.write_raw_scrape_result(
        envelope, "ID_A", raw_conn=conn, keluaran_author=keluaran)

    # Tidak ada UPDATE/delete sama sekali di seluruh transaksi.
    assert all(p["jenis"] in ("insert", "select") for p in conn.perintah)
    # Semua dilewati; tidak ada yang dianggap baris tertulis baru.
    statistik = keluaran["statistik"]
    assert statistik["author_disimpan"] == 0
    assert statistik["author_dilewati_konflik"] == 2
    assert statistik["penyimpanan_author"] == "tersimpan"
    # Author konflik merujuk id publikasi lama hasil fallback.
    baris = _params_tabel(conn, "scholar_publication_author")
    assert all(b["raw_publication_id"] == 900 for b in baris)


def test_g2b_galat_insert_author_rollback_total_dan_loadererror():
    """11) Exception dari INSERT author -> rollback total + LoaderError
    naik, dengan exception asli tetap terpasang sebagai penyebab."""
    conn = _KoneksiRawPalsu()
    keluaran = {}
    envelope = _envelope_uji(data={
        "name": "Budi Santoso",
        "rows": [{"title": "Publikasi Satu", "authors": "Budi Santoso"}],
    })
    conn.galat_author = RuntimeError("database lagi galau")

    with pytest.raises(postgres.LoaderError) as tangkap:
        postgres.write_raw_scrape_result(
            envelope, "ID_A", raw_conn=conn, keluaran_author=keluaran)

    assert conn.transaksi is not None
    assert conn.transaksi.rolled_back is True
    assert conn.transaksi.committed is False
    assert isinstance(tangkap.value.__cause__, RuntimeError)
    assert "database lagi galau" in str(tangkap.value)


def test_g2b_setelah_rollback_tidak_ada_klaim_tersimpan():
    """12) Setelah rollback fungsi melempar (mengembalikan APA PUN);
    tidak ada klaim 'tersimpan' yang lolos ke pemanggil, baik lewat
    write_raw_scrape_result maupun load_scholar_run."""
    conn = _KoneksiRawPalsu()
    keluaran = {}
    envelope = _envelope_uji(data={
        "name": "Budi Santoso",
        "rows": [{"title": "Publikasi Satu", "authors": "Budi Santoso"}],
    })
    conn.galat_author = RuntimeError("gagal di tengah")

    with pytest.raises(postgres.LoaderError):
        postgres.write_raw_scrape_result(
            envelope, "ID_A", raw_conn=conn, keluaran_author=keluaran)
    # Dict yang bocor lewat referensi TIDAK pernah mengklaim
    # "tersimpan" untuk transaksi yang ter-rollback.
    assert keluaran["statistik"].get("penyimpanan_author") != "tersimpan"

    # Jalur load_scholar_run: exception naik, tidak ada dict statistik.
    conn2 = _KoneksiRawPalsu()
    conn2.galat_author = RuntimeError("gagal di tengah")
    with pytest.raises(postgres.LoaderError):
        postgres.load_scholar_run(
            _envelope_uji(data={
                "name": "Budi Santoso",
                "rows": [{"title": "Publikasi Satu",
                          "authors": "Budi Santoso"}],
            }),
            scholar_id="ID_A", raw_conn=conn2, raw_only=True)
    assert conn2.transaksi is not None
    assert conn2.transaksi.rolled_back is True


def test_g2b_publikasi_tanpa_penulis_tanpa_insert_author():
    """13) Publikasi tanpa penulis (daftar kosong) -> TANPA INSERT
    author untuk baris itu; publikasi_tanpa_penulis tetap benar."""
    conn = _KoneksiRawPalsu()
    keluaran = {}
    envelope = _envelope_uji(data={
        "name": "Budi Santoso",
        "rows": [
            {"title": "Publikasi Kosong", "authors": None},
            {"title": "Publikasi Berpenulis", "authors": "Budi Santoso"},
        ],
    })

    postgres.write_raw_scrape_result(
        envelope, "ID_A", raw_conn=conn, keluaran_author=keluaran)

    baris = _params_tabel(conn, "scholar_publication_author")
    # Hanya baris berpenulis yang menghasilkan INSERT author.
    assert len(baris) == 1
    assert baris[0]["author_name"] == "Budi Santoso"
    statistik = keluaran["statistik"]
    assert statistik["publikasi_tanpa_penulis"] == 1
    assert statistik["author_total"] == 1
    assert statistik["author_disimpan"] == 1


def test_g2b_sembilan_kunci_di_semua_jalur_return(monkeypatch):
    """14) Sembilan kunci statistik author hadir di SEMUA titik keluar
    load_scholar_run: raw-only, SKIPPED (run fatal), dan loaded."""
    data_uji = {
        "name": "Tri Agus Djoko Kuntjoro",
        "rows": [{"title": "Publikasi Satu",
                  "detail_authors": "Tri Agus Djoko Kuntjoro"}],
    }

    # Jalur 1: raw-only, writer asli.
    statistik_raw = postgres.load_scholar_run(
        _envelope_uji(data=data_uji), scholar_id="8kDg_v4AAAAJ",
        raw_conn=_KoneksiRawPalsu(), raw_only=True)
    assert statistik_raw["status"] == postgres.STATUS_RAW_ONLY
    assert statistik_raw["penyimpanan_author"] == "tersimpan"
    assert statistik_raw["author_disimpan"] == 1

    # Jalur 2: SKIPPED (data None), writer asli.
    statistik_skip = postgres.load_scholar_run(
        _envelope_uji(data=None, status="BLOCKED"), scholar_id="ID_A",
        raw_conn=_KoneksiRawPalsu())
    assert statistik_skip["status"] == postgres.STATUS_SKIPPED
    assert statistik_skip["penyimpanan_author"] == "tidak_disimpan"
    assert statistik_skip["author_disimpan"] == 0

    # Jalur 3: loaded - writer asli untuk raw, load_core_rows tiruan.
    def core_palsu(core, conn=None, run_id=None, profil=None,
                   baris_referensi=None):
        stat = postgres._stats_kosong()
        stat["status"] = postgres.STATUS_LOADED
        return stat

    monkeypatch.setattr(postgres, "load_core_rows", core_palsu)
    statistik_loaded = postgres.load_scholar_run(
        _envelope_uji(data=data_uji), scholar_id="8kDg_v4AAAAJ",
        conn=object(), raw_conn=_KoneksiRawPalsu())
    assert statistik_loaded["status"] == postgres.STATUS_LOADED
    assert statistik_loaded["penyimpanan_author"] == "tersimpan"
    assert statistik_loaded["author_disimpan"] == 1

    for statistik in (statistik_raw, statistik_skip, statistik_loaded):
        for kunci in _KUNCI_STATISTIK_AUTHOR:
            assert kunci in statistik, (
                "kunci {} hilang di jalur {}".format(
                    kunci, statistik["status"]))


# ============================================================
# Kolom RAW detail_doi (Tahap 1) - tanpa database
# ============================================================

def test_kolom_raw_publication_memuat_detail_doi():
    from src.loading.postgres import KOLOM_RAW_PUBLICATION
    assert "detail_doi" in KOLOM_RAW_PUBLICATION


def test_stmt_sisip_raw_publication_menyertakan_detail_doi():
    from sqlalchemy.dialects import postgresql
    from src.loading import postgres as pgmod

    stmt = pgmod._stmt_sisip_raw_scholar_publication(
        1, 0, "X", {"title": "T", "detail_doi": "10.1145/3055635.3056657"})
    compiled = stmt.compile(dialect=postgresql.dialect())
    assert "detail_doi" in str(compiled)
    # Nilai DOI benar-benar diteruskan ke parameter bind insert RAW.
    assert "10.1145/3055635.3056657" in list(compiled.params.values())

    # Baris tanpa detail_doi tetap aman (menjadi NULL).
    stmt2 = pgmod._stmt_sisip_raw_scholar_publication(1, 0, "X", {"title": "T"})
    compiled2 = stmt2.compile(dialect=postgresql.dialect())
    assert compiled2.params.get("detail_doi") is None


def test_migrasi_ddl_detail_doi_aditif_dan_idempoten():
    from pathlib import Path
    akar = Path(__file__).resolve().parents[1]
    isi = (akar / "sql" / "ddl" / "07_add_raw_publication_detail_doi.sql").read_text(
        encoding="utf-8")
    assert "ADD COLUMN IF NOT EXISTS detail_doi" in isi
    assert "raw.scholar_publication" in isi


def test_ddl_04_raw_publication_punya_detail_doi_nullable():
    from pathlib import Path
    akar = Path(__file__).resolve().parents[1]
    isi = (akar / "sql" / "ddl" / "04_create_raw_structured.sql").read_text(
        encoding="utf-8")
    baris = [l for l in isi.splitlines() if "detail_doi" in l]
    assert baris, "kolom detail_doi harus ada di DDL 04"
    assert any("TEXT" in l for l in baris), "detail_doi harus bertipe TEXT"
    assert not any("NOT NULL" in l for l in baris), "detail_doi harus nullable"


def test_tidak_ada_kolom_doi_dari_metadata_tambahan():
    from src.loading.postgres import KOLOM_RAW_PUBLICATION
    kolom_doi = [k for k in KOLOM_RAW_PUBLICATION if "doi" in k.lower()]
    # Tepat satu kolom DOI. (Kolom "status" P24 ikut dalam tuple tapi
    # bukan kolom DOI dan bukan key sumber - nilainya dihitung.)
    assert kolom_doi == ["detail_doi"]


# ============================================================
# Kolom RAW bibliografi profil (TDD) - profile_volume / profile_issue /
# profile_pages, terpisah dari detail_* halaman detail. Tanpa database.
# ============================================================

def test_kolom_raw_publication_memuat_bibliografi_profil():
    from src.loading.postgres import KOLOM_RAW_PUBLICATION
    for kolom in ("profile_volume", "profile_issue", "profile_pages"):
        assert kolom in KOLOM_RAW_PUBLICATION, (
            f"{kolom} harus ada di KOLOM_RAW_PUBLICATION")


def test_stmt_sisip_raw_publication_memisahkan_profil_dan_detail():
    """Satu baris input: kunci profil -> kolom profile_*, kunci detail
    -> kolom detail_*. Keduanya hidup berdampingan dengan nilai
    BERBEDA, bukan saling menimpa."""
    from sqlalchemy.dialects import postgresql
    from src.loading import postgres as pgmod

    stmt = pgmod._stmt_sisip_raw_scholar_publication(
        1, 0, "X",
        {"title": "T", "volume": "5", "issue": "2", "pages": "23-32",
         "detail_volume": "9", "detail_pages": "53-58"},
    )
    compiled = stmt.compile(dialect=postgresql.dialect())
    # Metadata bibliografi profil (key parser volume/issue/pages)
    assert compiled.params.get("profile_volume") == "5"
    assert compiled.params.get("profile_issue") == "2"
    assert compiled.params.get("profile_pages") == "23-32"
    # Metadata halaman detail tetap di kolom detail_* masing-masing
    assert compiled.params.get("detail_volume") == "9"
    assert compiled.params.get("detail_pages") == "53-58"


def test_stmt_sisip_raw_publication_tanpa_key_profil_jadi_null():
    """Baris tanpa kunci profil, dan baris dengan kunci profil=None
    eksplisit, keduanya menghasilkan SQL NULL pada kolom profile_*."""
    from sqlalchemy.dialects import postgresql
    from src.loading import postgres as pgmod

    stmt = pgmod._stmt_sisip_raw_scholar_publication(
        1, 0, "X", {"title": "T"})
    compiled = stmt.compile(dialect=postgresql.dialect())
    for kolom in ("profile_volume", "profile_issue", "profile_pages"):
        assert compiled.params.get(kolom) is None

    stmt2 = pgmod._stmt_sisip_raw_scholar_publication(
        1, 0, "X",
        {"title": "T", "volume": None, "issue": None, "pages": None},
    )
    compiled2 = stmt2.compile(dialect=postgresql.dialect())
    for kolom in ("profile_volume", "profile_issue", "profile_pages"):
        assert compiled2.params.get(kolom) is None


def test_migrasi_ddl_08_bibliografi_profil_aditif_dan_idempoten():
    from pathlib import Path
    akar = Path(__file__).resolve().parents[1]
    isi = (akar / "sql" / "ddl"
           / "08_add_raw_publication_profile_metadata.sql").read_text(
        encoding="utf-8")
    assert "raw.scholar_publication" in isi
    for kolom in ("profile_volume", "profile_issue", "profile_pages"):
        assert f"ADD COLUMN IF NOT EXISTS {kolom}" in isi
    assert "TEXT" in isi
    assert "DROP COLUMN" not in isi


def test_ddl_04_raw_publication_punya_kolom_profil_nullable():
    from pathlib import Path
    akar = Path(__file__).resolve().parents[1]
    isi = (akar / "sql" / "ddl" / "04_create_raw_structured.sql").read_text(
        encoding="utf-8")
    for kolom in ("profile_volume", "profile_issue", "profile_pages"):
        baris = [l for l in isi.splitlines() if kolom in l]
        assert baris, f"kolom {kolom} harus ada di DDL 04"
        assert any("TEXT" in l for l in baris), (
            f"{kolom} harus bertipe TEXT")
        assert not any("NOT NULL" in l for l in baris), (
            f"{kolom} harus nullable")


# ============================================================
# Penguatan pemetaan metadata -> raw.scholar_publication
# (audit P9) - SEMUA tanpa database: hanya compile statement ke
# dialect postgresql, tidak pernah dieksekusi.
# ============================================================

# Cakupan lengkap kolom data KOLOM_RAW_PUBLICATION: urutan DAN
# keanggotaan persis seperti yang dideklarasikan (25 kolom; kolom
# terakhir "status" adalah turunan P24 yang dihitung loader).
KOLOM_RAW_PUBLICATION_DIHARAPKAN = (
    "title", "authors", "venue", "extra", "citations", "year",
    "publication_year", "detail_url", "detail_title", "detail_authors",
    "detail_date", "detail_venue", "detail_volume", "detail_issue",
    "detail_pages", "detail_publisher", "detail_description",
    "detail_citations", "detail_link", "detail_pdf_link", "detail_doi",
    "profile_volume", "profile_issue", "profile_pages", "status",
)

# Bukti sumber-ke-target per kolom: kolom -> key row parser SATUNYA
# yang menjadi sumber nilainya (21 kolom identitas + 3 alias profil =
# 24 entri; kolom "status" sengaja TIDAK dipemetaan karena nilainya
# DIHITUNG loader, bukan diambil dari key baris scraper).
PEMETAAN_SUMBER_KOLOM_RAW_PUBLICATION = {
    # Halaman daftar / profil (key identik dengan nama kolom)
    "title": "title",
    "authors": "authors",
    "venue": "venue",
    "extra": "extra",
    "citations": "citations",
    "year": "year",
    "publication_year": "publication_year",
    # Halaman detail - 14 key kontrak parse_detail_html (P8),
    # termasuk detail_url
    "detail_url": "detail_url",
    "detail_title": "detail_title",
    "detail_authors": "detail_authors",
    "detail_date": "detail_date",
    "detail_venue": "detail_venue",
    "detail_volume": "detail_volume",
    "detail_issue": "detail_issue",
    "detail_pages": "detail_pages",
    "detail_publisher": "detail_publisher",
    "detail_description": "detail_description",
    "detail_citations": "detail_citations",
    "detail_link": "detail_link",
    "detail_pdf_link": "detail_pdf_link",
    "detail_doi": "detail_doi",
    # Bibliografi profil (key pendek parser profil P2)
    "profile_volume": "volume",
    "profile_issue": "issue",
    "profile_pages": "pages",
}


def _baris_lengkap_kontrak_p9():
    """Baris row ENVELOPE lengkap sesuai kontrak scraper P2/P3/P8.

    Memuat SEMUA key nyata: profil halaman daftar + key bibliografi
    profil volume/issue/pages + 14 key halaman detail, plus dua key
    yang TIDAK boleh masuk ke INSERT raw (detail_venue_label hanya
    hidup di envelope JSONB; category bukan kolom raw). Nilai profil
    dan detail sengaja BERBEDA supaya percampuran ketahuan.
    """
    return {
        # -- profil / halaman daftar (P2) --
        "title": "Judul Paper",
        "authors": "A Penulis, B Penulis",
        "venue": "ACM Transactions on X",
        "extra": "Vol 5, 2024, pp. 23-32",
        "citations": "42",
        "year": "2024",
        "publication_year": 2024,
        "detail_url": "https://scholar.google.com/citations?view_op=view"
                      "_citation&citml=1",
        # -- bibliografi profil (P2): key pendek volume/issue/pages --
        "volume": "5",
        "issue": "2",
        "pages": "23-32",
        # -- halaman detail (P8): 14 key, termasuk detail_url --
        "detail_title": "Judul Paper (halaman detail)",
        "detail_authors": "A Penulis",
        "detail_date": "Mei 2024",
        "detail_venue": "ACM Trans. on X",
        "detail_volume": "99",
        "detail_issue": "7",
        "detail_pages": "53-58",
        "detail_publisher": "ACM",
        "detail_description": "Abstrak singkat.",
        "detail_citations": "43",
        "detail_link": "https://doi.org/10.1145/contoh",
        "detail_pdf_link": "https://example.org/paper.pdf",
        "detail_doi": "10.x/y",
        # -- key yang TIDAK boleh muncul di params INSERT --
        "detail_venue_label": "Jurnal",
        "category": "Jurnal",
        "index": 0,
    }


def test_kolom_raw_publication_cakupan_lengkap_25_kolom():
    """Cakupan lengkap: tuple 25 kolom persis (urutan + keanggotaan),
    memuat detail_doi, ketiga kolom profil, dan kolom turunan status
    di AKHIR; seluruhnya subset kolom tabel RAW_SCHOLAR_PUBLICATION."""
    from src.loading.postgres import (
        KOLOM_RAW_PUBLICATION, RAW_SCHOLAR_PUBLICATION)

    assert KOLOM_RAW_PUBLICATION == KOLOM_RAW_PUBLICATION_DIHARAPKAN
    assert len(KOLOM_RAW_PUBLICATION) == 25
    # Keanggotaan wajib dalam SATU cakupan
    assert "detail_doi" in KOLOM_RAW_PUBLICATION
    for kolom in ("profile_volume", "profile_issue", "profile_pages"):
        assert kolom in KOLOM_RAW_PUBLICATION
    # Kolom turunan P24 di urutan akhir
    assert KOLOM_RAW_PUBLICATION[-1] == "status"

    # Kolom data = subset kolom tabel (baca deklarasi tabel, bukan teks)
    kolom_tabel = set(RAW_SCHOLAR_PUBLICATION.c.keys())
    assert set(KOLOM_RAW_PUBLICATION) <= kolom_tabel
    # Identitas ditulis terpisah, bukan bagian dari KOLOM data
    for identitas in ("raw_publication_id", "result_id", "urutan",
                      "scholar_id"):
        assert identitas not in KOLOM_RAW_PUBLICATION
    # detail_venue_label sengaja TIDAK ada: hanya di envelope JSONB
    assert "detail_venue_label" not in KOLOM_RAW_PUBLICATION
    assert "detail_venue_label" not in kolom_tabel


def test_stmt_sisip_raw_publication_sumber_ke_target_end_to_end():
    """Baris uji lengkap kontrak scraper -> compiled INSERT: semua 25
    kolom muncul sebagai param (24 kolom sumber-key terpetakan ke key
    sumbernya masing-masing, plus status turunan yang dihitung),
    venue_label/key asing tidak bocor, profil dan detail terpisah."""
    from sqlalchemy.dialects import postgresql
    from src.loading import postgres as pgmod
    from src.loading.postgres import KOLOM_RAW_PUBLICATION

    baris = _baris_lengkap_kontrak_p9()
    stmt = pgmod._stmt_sisip_raw_scholar_publication(
        1, 0, "X", baris)
    # Hanya compile - TIDAK pernah dieksekusi ke database.
    compiled = stmt.compile(dialect=postgresql.dialect())

    # (e) Konsistensi kolom-param: 25 data + 3 identitas, tanpa
    # param kolom data ekstra.
    assert len(compiled.params) == 25 + 3
    assert set(compiled.params) == (
        set(KOLOM_RAW_PUBLICATION) | {"result_id", "urutan", "scholar_id"})

    # DOI dan pemisahan profil/detail dengan nilai BERBEDA
    assert compiled.params["detail_doi"] == "10.x/y"
    assert compiled.params["profile_volume"] == "5"     # key "volume"
    assert compiled.params["detail_volume"] == "99"     # key "detail_volume"
    assert compiled.params["profile_issue"] == "2"      # key "issue"
    assert compiled.params["detail_issue"] == "7"
    assert compiled.params["profile_pages"] == "23-32"  # key "pages"
    assert compiled.params["detail_pages"] == "53-58"

    # Baris lengkap (keempat field detail terisi) -> status turunan yes
    assert compiled.params["status"] == "yes"

    # Bukti sumber-ke-target PER KOLOM: 24 kolom sumber-key, satu per
    # satu (kolom status tidak dipemetaan - dihitung, bukan diambil
    # dari key baris).
    assert len(PEMETAAN_SUMBER_KOLOM_RAW_PUBLICATION) == 24
    assert "status" not in PEMETAAN_SUMBER_KOLOM_RAW_PUBLICATION
    for kolom, kunci in PEMETAAN_SUMBER_KOLOM_RAW_PUBLICATION.items():
        assert kunci in baris, f"key sumber {kunci} hilang dari baris uji"
        assert compiled.params[kolom] == baris[kunci], (
            f"kolom {kolom} harus diambil dari key {kunci}")

    # (D) detail_venue_label ada di baris TAPI tidak boleh jadi param
    assert "detail_venue_label" not in compiled.params
    with pytest.raises(KeyError):
        compiled.params["detail_venue_label"]
    # Key asing lain juga tidak muncul
    with pytest.raises(KeyError):
        compiled.params["category"]
    with pytest.raises(KeyError):
        compiled.params["index"]


def test_stmt_sisip_raw_publication_none_tetap_null_bukan_string():
    """Key dengan nilai None eksplisit -> param None (SQL NULL), bukan
    string "None"; guard teks memastikan literal pun tidak mengandung
    'None'."""
    from sqlalchemy.dialects import postgresql
    from src.loading import postgres as pgmod

    baris = {
        "title": None, "authors": None, "venue": None, "extra": None,
        "citations": None, "year": None, "publication_year": None,
        "detail_url": None, "detail_title": None, "detail_doi": None,
        "detail_citations": None, "detail_description": None,
        "volume": None, "issue": None, "pages": None,
    }
    stmt = pgmod._stmt_sisip_raw_scholar_publication(1, 0, "X", baris)
    compiled = stmt.compile(dialect=postgresql.dialect())
    for kolom in ("title", "detail_doi", "detail_citations",
                  "profile_volume", "profile_issue", "profile_pages"):
        assert compiled.params.get(kolom) is None
        assert compiled.params.get(kolom) != "None"
    # Guard: representasi SQL tidak pernah memuat kata "None"
    assert "None" not in str(compiled)
    # Render literal (compile saja, tanpa eksekusi): NULL tertulis
    # sebagai SQL NULL, bukan kata None
    sql_literal = str(stmt.compile(dialect=postgresql.dialect(),
                                   compile_kwargs={"literal_binds": True}))
    assert "None" not in sql_literal
    assert "NULL" in sql_literal


def test_stmt_sisip_raw_publication_key_hilang_default_null():
    """Key profil hilang dan sebagian key detail hilang -> param None
    (perilaku dict.get default), tanpa exception."""
    from sqlalchemy.dialects import postgresql
    from src.loading import postgres as pgmod

    baris = {"title": "T", "detail_citations": "43"}
    stmt = pgmod._stmt_sisip_raw_scholar_publication(1, 0, "X", baris)
    compiled = stmt.compile(dialect=postgresql.dialect())
    # Key profil tidak ada sama sekali -> NULL
    for kolom in ("profile_volume", "profile_issue", "profile_pages"):
        assert compiled.params.get(kolom) is None
    # Sebagian key detail tidak ada -> NULL; yang ada tetap utuh
    for kolom in ("detail_title", "detail_volume", "detail_doi",
                  "detail_link", "detail_pdf_link"):
        assert compiled.params.get(kolom) is None
    assert compiled.params.get("detail_citations") == "43"
    assert compiled.params.get("title") == "T"


def test_stmt_sisip_raw_publication_params_persis_sesuai_kolom():
    """Dengan baris SEPARSIAL sekalipun: semua kolom KOLOM_RAW_PUBLICATION
    muncul sebagai param dan tidak ada param kolom data ekstra di luar
    3 kolom identitas."""
    from sqlalchemy.dialects import postgresql
    from src.loading import postgres as pgmod
    from src.loading.postgres import KOLOM_RAW_PUBLICATION

    stmt = pgmod._stmt_sisip_raw_scholar_publication(
        7, 3, "SCHOLAR_X", {"title": "T", "volume": "5"})
    compiled = stmt.compile(dialect=postgresql.dialect())
    param_kolom = set(compiled.params) - {"result_id", "urutan",
                                          "scholar_id"}
    assert param_kolom == set(KOLOM_RAW_PUBLICATION)
    assert len(compiled.params) == len(KOLOM_RAW_PUBLICATION) + 3
    # Identitas berasal dari ARGUMEN, bukan dari baris
    assert compiled.params["result_id"] == 7
    assert compiled.params["urutan"] == 3
    assert compiled.params["scholar_id"] == "SCHOLAR_X"
    # Baris tidak boleh mengubah identitas lewat key semacam "urutan"
    stmt2 = pgmod._stmt_sisip_raw_scholar_publication(
        7, 3, "SCHOLAR_X", {"title": "T", "urutan": 99, "result_id": 99})
    compiled2 = stmt2.compile(dialect=postgresql.dialect())
    assert compiled2.params["result_id"] == 7
    assert compiled2.params["urutan"] == 3


# ============================================================
# P24 - kolom turunan status raw.scholar_publication: validator
# kelengkapan metadata, integrasi loader (compile-only, TANPA
# database), dan migrasi DDL aditif.
# ============================================================

# Empat field yang menjadi dasar keputusan status kelengkapan.
_FIELD_STATUS_LENGKAP = {
    "detail_volume": "5",
    "detail_issue": "2",
    "detail_pages": "23-32",
    "detail_description": "Abstrak singkat.",
}


def test_status_kelengkapan_metadata_empat_field_terisi_yes():
    """Keempat field terisi non-kosong -> 'yes'."""
    from src.loading.postgres import _status_kelengkapan_metadata

    assert _status_kelengkapan_metadata(dict(_FIELD_STATUS_LENGKAP)) == "yes"


def test_status_kelengkapan_metadata_nilai_asli_tidak_diubah():
    """Whitespace hanya DIDETEKSI; nilai asli baris tidak dipotong/
    diubah oleh validator."""
    from src.loading.postgres import _status_kelengkapan_metadata

    baris = {
        "detail_volume": " 5 ",
        "detail_issue": "2",
        "detail_pages": "23-32",
        "detail_description": "  Abstrak  ",
    }
    assert _status_kelengkapan_metadata(baris) == "yes"
    # Nilai asli utuh, termasuk spasi
    assert baris["detail_volume"] == " 5 "
    assert baris["detail_description"] == "  Abstrak  "


@pytest.mark.parametrize("kunci", sorted(_FIELD_STATUS_LENGKAP))
@pytest.mark.parametrize("nilai_kosong", [None, "", "   "])
def test_status_kelengkapan_metadata_satu_field_kosong_no(kunci,
                                                           nilai_kosong):
    """Salah satu dari empat field NULL / '' / whitespace-only -> 'no'."""
    from src.loading.postgres import _status_kelengkapan_metadata

    baris = dict(_FIELD_STATUS_LENGKAP)
    baris[kunci] = nilai_kosong
    assert _status_kelengkapan_metadata(baris) == "no"


def test_status_kelengkapan_metadata_semua_null_no():
    from src.loading.postgres import _status_kelengkapan_metadata

    baris = {kunci: None for kunci in _FIELD_STATUS_LENGKAP}
    assert _status_kelengkapan_metadata(baris) == "no"


def test_status_kelengkapan_metadata_key_hilang_no():
    """baris.get(...) -> key hilang dihitung sebagai tidak lengkap."""
    from src.loading.postgres import _status_kelengkapan_metadata

    # Dict kosong: keempat key hilang
    assert _status_kelengkapan_metadata({}) == "no"
    # Sebagian key hilang (tanpa key lain sama sekali)
    assert _status_kelengkapan_metadata({"detail_volume": "5"}) == "no"
    # Tiga lengkap, satu hilang
    baris = dict(_FIELD_STATUS_LENGKAP)
    del baris["detail_pages"]
    assert _status_kelengkapan_metadata(baris) == "no"


def test_kolom_raw_publication_memuat_status_di_akhir():
    """Kolom status ada di tuple KOLOM_RAW_PUBLICATION (urutan akhir),
    di tabel RAW_SCHOLAR_PUBLICATION, dan bukan kolom identitas."""
    from src.loading.postgres import (
        KOLOM_RAW_PUBLICATION, RAW_SCHOLAR_PUBLICATION)

    assert "status" in KOLOM_RAW_PUBLICATION
    assert KOLOM_RAW_PUBLICATION[-1] == "status"
    assert "status" in RAW_SCHOLAR_PUBLICATION.c.keys()
    # Identitas tetap terpisah
    for identitas in ("raw_publication_id", "result_id", "urutan",
                      "scholar_id"):
        assert identitas not in KOLOM_RAW_PUBLICATION


def test_stmt_sisip_raw_publication_status_yes_bila_empat_field_terisi():
    """Baris dgn keempat field detail terisi -> param status 'yes';
    status muncul di daftar param INSERT."""
    from sqlalchemy.dialects import postgresql
    from src.loading import postgres as pgmod
    from src.loading.postgres import KOLOM_RAW_PUBLICATION

    baris = dict(_FIELD_STATUS_LENGKAP)
    stmt = pgmod._stmt_sisip_raw_scholar_publication(1, 0, "X", baris)
    compiled = stmt.compile(dialect=postgresql.dialect())
    assert compiled.params.get("status") == "yes"
    assert "status" in compiled.params
    assert "status" in KOLOM_RAW_PUBLICATION
    # Metadata asli baris tidak diubah oleh penyusunan param
    assert baris == _FIELD_STATUS_LENGKAP


@pytest.mark.parametrize("kunci", sorted(_FIELD_STATUS_LENGKAP))
def test_stmt_sisip_raw_publication_status_no_bila_salah_satu_kosong(
        kunci):
    """Baris parsial (salah satu field detail hilang) -> status 'no'."""
    from sqlalchemy.dialects import postgresql
    from src.loading import postgres as pgmod

    baris = dict(_FIELD_STATUS_LENGKAP)
    del baris[kunci]
    stmt = pgmod._stmt_sisip_raw_scholar_publication(1, 0, "X", baris)
    compiled = stmt.compile(dialect=postgresql.dialect())
    assert compiled.params.get("status") == "no"


def test_stmt_sisip_raw_publication_status_tidak_dipengaruhi_profil():
    """profile_* lengkap TIDAK membuat status 'yes' kalau salah satu
    detail_* kosong; metadata profil dan detail tetap utuh di param."""
    from sqlalchemy.dialects import postgresql
    from src.loading import postgres as pgmod

    baris = dict(_FIELD_STATUS_LENGKAP)
    del baris["detail_issue"]                 # salah satu detail kosong
    baris.update({
        "volume": "5", "issue": "2", "pages": "23-32",  # profil lengkap
        "title": "Judul", "detail_publisher": "ACM",
    })
    salinan = dict(baris)
    stmt = pgmod._stmt_sisip_raw_scholar_publication(1, 0, "X", baris)
    compiled = stmt.compile(dialect=postgresql.dialect())
    # Profil lengkap tidak menaikkan status
    assert compiled.params.get("status") == "no"
    assert compiled.params.get("detail_issue") is None
    # Metadata profil tetap utuh di kolomnya
    assert compiled.params.get("profile_volume") == "5"
    assert compiled.params.get("profile_issue") == "2"
    assert compiled.params.get("profile_pages") == "23-32"
    # Metadata asli baris tidak diubah
    assert baris == salinan


def test_stmt_sisip_raw_publication_status_key_baris_di_override():
    """Key 'status' pada baris scraper DIABAikan: nilai selalu dihitung
    ulang dari kelengkapan field detail."""
    from sqlalchemy.dialects import postgresql
    from src.loading import postgres as pgmod

    # Baris lengkap tapi key status scraper bilang "no" -> tetap yes
    baris_lengkap = dict(_FIELD_STATUS_LENGKAP)
    baris_lengkap["status"] = "no"
    compiled = pgmod._stmt_sisip_raw_scholar_publication(
        1, 0, "X", baris_lengkap).compile(dialect=postgresql.dialect())
    assert compiled.params.get("status") == "yes"

    # Baris parsial tapi key status scraper bilang "yes" -> tetap no
    baris_parsial = dict(_FIELD_STATUS_LENGKAP)
    del baris_parsial["detail_description"]
    baris_parsial["status"] = "yes"
    compiled2 = pgmod._stmt_sisip_raw_scholar_publication(
        1, 0, "X", baris_parsial).compile(dialect=postgresql.dialect())
    assert compiled2.params.get("status") == "no"


def test_stmt_sisip_raw_publication_status_baris_kosong_no():
    """Baris tanpa field detail sama sekali -> status 'no' (bukan
    None), sementara kolom detail lain tetap NULL."""
    from sqlalchemy.dialects import postgresql
    from src.loading import postgres as pgmod

    stmt = pgmod._stmt_sisip_raw_scholar_publication(
        1, 0, "X", {"title": "T"})
    compiled = stmt.compile(dialect=postgresql.dialect())
    assert compiled.params.get("status") == "no"
    assert compiled.params.get("detail_volume") is None


def test_migrasi_ddl_09_status_aditif_dan_idempoten():
    from pathlib import Path
    akar = Path(__file__).resolve().parents[1]
    isi = (akar / "sql" / "ddl" / "09_add_raw_publication_status.sql"
           ).read_text(encoding="utf-8")
    assert "ADD COLUMN IF NOT EXISTS status TEXT" in isi
    assert "raw.scholar_publication" in isi
    # Aditif murni: tanpa UPDATE/backfill/DROP/perubahan kolom lain
    assert "DROP" not in isi
    assert "UPDATE" not in isi
    assert "DELETE" not in isi


def test_ddl_04_raw_publication_punya_status_nullable():
    """DDL 04 mendefinisikan kolom status TEXT tepat satu kali di
    tabel raw.scholar_publication, nullable."""
    from pathlib import Path
    akar = Path(__file__).resolve().parents[1]
    isi = (akar / "sql" / "ddl" / "04_create_raw_structured.sql").read_text(
        encoding="utf-8")
    baris_kolom = [
        l for l in isi.splitlines()
        if l.strip().startswith("status") and "TEXT" in l
    ]
    assert len(baris_kolom) == 1, (
        "kolom status harus didefinisikan tepat satu kali di DDL 04")
    assert "NOT NULL" not in baris_kolom[0], "status harus nullable"
    # Tabel lain (scholar_author) tidak ikut mendapat kolom status
    bagian_author = isi.split("raw.scholar_publication")[0]
    assert not any(
        l.strip().startswith("status")
        for l in bagian_author.splitlines()
    ), "kolom status hanya boleh ada di raw.scholar_publication"


# ============================================================
# P10 - kontrak parser -> parameter INSERT (jalur DOI, e2e
# lintas-file). parse_detail_html (scraper) -> baris -> compiled
# INSERT raw.scholar_publication (loader). TANPA database: hanya
# compile dialect postgresql, tidak pernah dieksekusi.
# ============================================================

# HTML detail sintetis: judul + link utama ber-DOI + PDF tanpa DOI +
# blok Deskripsi. Sumber DOI HANYA detail_link & detail_pdf_link.
_HTML_DETAIL_DOI_E2E = (
    "<html><body>"
    '<div id="gsc_oci_title">Judul E2E P10</div>'
    '<a class="gsc_oci_title_link" '
    'href="https://doi.org/10.1145/3055635.3056657">sumber</a>'
    '<div class="gsc_oci_title_ggi">'
    '<a href="https://example.org/paper.pdf">pdf</a></div>'
    '<div class="gs_scl"><div class="gsc_oci_field">Deskripsi</div>'
    '<div class="gsc_oci_value">Abstrak e2e P10.</div></div>'
    "</body></html>"
)


def _baris_e2e_p10(html):
    """Parse HTML detail -> baris envelope + key profil palsu (beda
    nilai dari field detail supaya percampuran ketahuan)."""
    from src.ingestion.scrape_scholar_playwright import parse_detail_html

    baris = parse_detail_html(html)
    baris.update({
        "title": "Judul Profil",
        "authors": "P Profil",
        "venue": "Venue Profil",
        "extra": "Vol 5, 2024, pp. 1-2",
        "citations": "42",
        "year": "2024",
        "publication_year": 2024,
        "detail_url": "https://scholar.example/view?id=X",
        "volume": "5",   # profil -> profile_volume
        "issue": "2",    # profil -> profile_issue
        "pages": "1-2",  # profil -> profile_pages
    })
    return baris


def test_parse_detail_html_ke_stmt_sisip_doi_end_to_end():
    """E2E parser->INSERT: detail_doi = DOI pada href title link; kedua
    URL sumber IDENTIK dgn nilai aslinya (tidak dimodifikasi); nilai DOI
    tidak tertukar dengan profile_*/field lain."""
    from src.loading import postgres as pgmod

    baris = _baris_e2e_p10(_HTML_DETAIL_DOI_E2E)
    stmt = pgmod._stmt_sisip_raw_scholar_publication(1, 0, "X", baris)
    compiled = stmt.compile(dialect=postgresql.dialect())

    doi = "10.1145/3055635.3056657"
    href = "https://doi.org/10.1145/3055635.3056657"
    pdf = "https://example.org/paper.pdf"

    # DOI dari href title link, utuh di kolomnya
    assert compiled.params["detail_doi"] == doi
    # Kedua URL sumber identik dengan nilai aslinya (tidak dimodifikasi
    # sepanjang alur parse -> param INSERT)
    assert compiled.params["detail_link"] == href
    assert compiled.params["detail_pdf_link"] == pdf
    # Nilai DOI hanya muncul di kolom detail_doi (tidak tertukar)
    for kolom, nilai in compiled.params.items():
        if nilai == doi:
            assert kolom == "detail_doi", f"DOI bocor ke kolom {kolom}"
    # Profil vs detail tidak tercampur
    assert compiled.params["profile_volume"] == "5"     # key "volume"
    assert compiled.params["detail_volume"] is None     # tanpa blok Jilid
    assert compiled.params["detail_description"] == "Abstrak e2e P10."
    assert compiled.params["detail_title"] == "Judul E2E P10"


# Dua URL dgn DOI BERBEDA -> parse -> konflik -> None.
_HTML_DETAIL_KONFLIK_E2E = (
    "<html><body>"
    '<div id="gsc_oci_title">Judul E2E P10</div>'
    '<a class="gsc_oci_title_link" '
    'href="https://doi.org/10.1145/3055635.3056657">a</a>'
    '<div class="gsc_oci_title_ggi">'
    '<a href="https://link.springer.com/chapter/10.1007/978-981-19-1804-9_34">'
    "b</a></div>"
    "</body></html>"
)


def test_parse_detail_html_konflik_doi_ke_stmt_sisip_null():
    """Dua URL beda DOI -> parse None -> param INSERT NULL; kedua URL
    sumber tetap identik (tidak dimodifikasi bahkan saat konflik)."""
    from src.loading import postgres as pgmod

    baris = _baris_e2e_p10(_HTML_DETAIL_KONFLIK_E2E)
    stmt = pgmod._stmt_sisip_raw_scholar_publication(1, 0, "X", baris)
    compiled = stmt.compile(dialect=postgresql.dialect())

    assert compiled.params["detail_doi"] is None
    assert compiled.params["detail_link"] == (
        "https://doi.org/10.1145/3055635.3056657")
    assert compiled.params["detail_pdf_link"] == (
        "https://link.springer.com/chapter/10.1007/978-981-19-1804-9_34")
    # Judul detail tetap terisi (parse sukses; hanya DOI yang null)
    assert compiled.params["detail_title"] == "Judul E2E P10"
