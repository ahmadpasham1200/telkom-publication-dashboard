"""Test untuk src/pipeline/run_pipeline.py (tanpa DB/jaringan/browser).

Fokus P13: RAW-only adalah DEFAULT pipeline.

    - parse_args: default raw_only=True; --load-core menyetel False.
    - main(): default TIDAK membuka koneksi core sama sekali;
      --load-core yang membukanya.
    - scrape_scholar, load_scholar_run, dan connect selalu
      di-patch (monkeypatch) -> tidak ada browser, tidak ada
      Playwright, tidak ada PostgreSQL.
    - --referensi-dosen (P58): file Excel identitas pemilik profil
      diteruskan ke loader sebagai baris_referensi; file hilang /
      tidak terbaca menonaktifkan verifikasi OTOMATIS tanpa
      menggagalkan run (baris_referensi=None, rc tetap 0).

Cara menjalankan:
    python -m pytest tests/test_run_pipeline.py -q
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

from openpyxl import Workbook  # noqa: E402

from src.pipeline import run_pipeline  # noqa: E402


def _envelope_sukses():
    """Envelope sukses minimal: data ada, rows kosong (tanpa print tabel)."""
    return {
        "run_id": None,
        "status": "OK",
        "records_fetched": 1,
        "records_failed": 0,
        "data": {"scholar_id": "X", "name": "X Profil", "h_index": 1,
                 "rows": []},
    }


def _buat_excel_referensi(path, header, baris):
    """Tulis workbook Sheet1 berisi `header` + `baris` (untuk test P58).

    Sengaja pakai openpyxl langsung (bukan baca_daftar) supaya test
    tidak saling bergantung pada fungsi yang sedang diuji di
    run_pipeline.
    """
    wb = Workbook()
    ws = wb.active
    # Type stub openpyxl menandai `active` sebagai Optional[Worksheet],
    # padahal Workbook baru selalu punya sheet.
    assert ws is not None
    ws.title = "Sheet1"
    ws.append(header)
    for b in baris:
        ws.append(b)
    wb.save(str(path))
    wb.close()
    return str(path)


# ============================================================
# parse_args: default RAW-only, --load-core pengecual eksplisit
# ============================================================

def test_run_pipeline_parse_args_default_raw_only():
    """Default raw_only=True; --load-core -> False; --raw-only eksplisit
    (kini redundan) tetap valid dan tetap True."""
    assert run_pipeline.parse_args(["--id", "X"]).raw_only is True
    assert run_pipeline.parse_args(
        ["--id", "X", "--load-core"]).raw_only is False
    assert run_pipeline.parse_args(
        ["--id", "X", "--raw-only"]).raw_only is True


# ============================================================
# main(): default berhenti setelah RAW
# ============================================================

def test_run_pipeline_default_tidak_buka_koneksi_core(monkeypatch, tmp_path):
    """Default (tanpa flag): load_scholar_run menerima raw_only=True,
    conn=None, dan connect() TIDAK pernah dipanggil -> staging/core
    tidak disentuh. rc == 0. Referensi identitas yang file-nya hilang
    diteruskan sebagai None (verifikasi mati otomatis, run lanjut)."""
    tercatat = {}

    async def scrape_palsu(*args, **kwargs):
        return _envelope_sukses()

    def connect_dilarang(*args, **kwargs):
        tercatat["connect_dipanggil"] = True
        raise AssertionError("koneksi core tidak boleh dibuka")

    def load_palsu(envelope, scholar_id=None, conn=None, raw_only=None,
                   baris_referensi=None):
        tercatat["raw_only"] = raw_only
        tercatat["conn"] = conn
        tercatat["baris_referensi"] = baris_referensi
        return {"status": run_pipeline.STATUS_RAW_ONLY,
                "raw_result_id": 1, "run_id": None}

    monkeypatch.setattr(run_pipeline, "scrape_scholar", scrape_palsu)
    monkeypatch.setattr(run_pipeline, "connect", connect_dilarang)
    monkeypatch.setattr(run_pipeline, "load_scholar_run", load_palsu)

    # Path referensi yang tidak ada: verifikasi identitas mati
    # otomatis tanpa menggagalkan run.
    rc = run_pipeline.main([
        "--id", "X",
        "--referensi-dosen", str(tmp_path / "hilang.xlsx"),
    ])

    assert rc == 0
    assert tercatat["raw_only"] is True
    # Koneksi core tidak pernah dibuka (flag + conn=None).
    assert tercatat.get("connect_dipanggil") is not True
    assert tercatat["conn"] is None
    # File hilang -> loader menerima None (semua penulis belum
    # terverifikasi), bukan crash.
    assert tercatat["baris_referensi"] is None


def test_run_pipeline_load_core_membuka_koneksi(monkeypatch, tmp_path):
    """--load-core: connect() DIPANGGIL (koneksi core dibuka),
    load_scholar_run menerima raw_only=False -> mode lama (isi
    staging/core). rc == 0."""
    tercatat = {}

    async def scrape_palsu(*args, **kwargs):
        return _envelope_sukses()

    class KoneksiPalsu:
        def close(self):
            tercatat["ditutup"] = True

    def connect_palsu(*args, **kwargs):
        tercatat["connect_dipanggil"] = True
        return KoneksiPalsu()

    def load_palsu(envelope, scholar_id=None, conn=None, raw_only=None,
                   baris_referensi=None):
        tercatat["raw_only"] = raw_only
        tercatat["conn"] = conn
        tercatat["baris_referensi"] = baris_referensi
        return {"status": run_pipeline.STATUS_LOADED,
                "raw_result_id": 1, "run_id": None, "author_id": 7}

    monkeypatch.setattr(run_pipeline, "scrape_scholar", scrape_palsu)
    monkeypatch.setattr(run_pipeline, "connect", connect_palsu)
    monkeypatch.setattr(run_pipeline, "load_scholar_run", load_palsu)

    rc = run_pipeline.main([
        "--id", "X", "--load-core",
        "--referensi-dosen", str(tmp_path / "hilang.xlsx"),
    ])

    assert rc == 0
    assert tercatat["connect_dipanggil"] is True
    assert isinstance(tercatat["conn"], KoneksiPalsu)
    assert tercatat["raw_only"] is False
    # Koneksi core ditutup rapi di blok finally.
    assert tercatat.get("ditutup") is True


# ============================================================
# P58: referensi identitas pemilik profil (--referensi-dosen)
# ============================================================

def test_run_pipeline_parse_args_default_referensi_dosen():
    """Default --referensi-dosen menunjuk ke Excel daftar dosen."""
    args = run_pipeline.parse_args(["--id", "X"])
    assert args.referensi_dosen == "data dosen/Data dosen.xlsx"
    # Path eksplisit dari user dihormati.
    args = run_pipeline.parse_args(
        ["--id", "X", "--referensi-dosen", "lain.xlsx"])
    assert args.referensi_dosen == "lain.xlsx"


def _pasang_patch(monkeypatch, tercatat, statistik=None):
    """Patch scrape_scholar/load_scholar_run/connect (offline total).

    `statistik` adalah dict yang dikembalikan load_palsu; default
    raw-only sukses.
    """
    if statistik is None:
        statistik = {"status": run_pipeline.STATUS_RAW_ONLY,
                     "raw_result_id": 1, "run_id": None}

    async def scrape_palsu(*args, **kwargs):
        return _envelope_sukses()

    def connect_dilarang(*args, **kwargs):
        raise AssertionError("koneksi core tidak boleh dibuka")

    def load_palsu(envelope, scholar_id=None, conn=None, raw_only=None,
                   baris_referensi=None):
        tercatat["scholar_id"] = scholar_id
        tercatat["raw_only"] = raw_only
        tercatat["baris_referensi"] = baris_referensi
        return statistik

    monkeypatch.setattr(run_pipeline, "scrape_scholar", scrape_palsu)
    monkeypatch.setattr(run_pipeline, "connect", connect_dilarang)
    monkeypatch.setattr(run_pipeline, "load_scholar_run", load_palsu)


def test_run_pipeline_referensi_dosen_diteruskan_ke_loader(tmp_path,
                                                           monkeypatch):
    """Excel referensi valid -> loader menerima baris daftar persis;
    rc == 0."""
    xlsx = _buat_excel_referensi(
        tmp_path / "referensi.xlsx",
        ["No", "NAMA", "scholar_id"],
        [[1, "Budi Santoso", "ID_A"], [2, "Siti Aminah", "ID_B"]])
    tercatat = {}
    _pasang_patch(monkeypatch, tercatat)

    rc = run_pipeline.main(["--id", "ID_A", "--referensi-dosen", xlsx])

    assert rc == 0
    assert tercatat["baris_referensi"] == [
        {"no": 1, "nama": "Budi Santoso", "scholar_id": "ID_A",
         "prodi": ""},
        {"no": 2, "nama": "Siti Aminah", "scholar_id": "ID_B",
         "prodi": ""},
    ]
    assert tercatat["scholar_id"] == "ID_A"


def test_run_pipeline_referensi_dosen_file_tidak_ada(tmp_path, monkeypatch,
                                                     capsys):
    """Path referensi tidak ada -> baris_referensi None, run LANJUT
    (rc == 0), peringatan tercetak."""
    tercatat = {}
    _pasang_patch(monkeypatch, tercatat)

    rc = run_pipeline.main([
        "--id", "X",
        "--referensi-dosen", str(tmp_path / "tidak_ada.xlsx"),
    ])

    assert rc == 0
    assert tercatat["baris_referensi"] is None
    keluaran = capsys.readouterr().out
    assert "TIDAK DIPAKAI" in keluaran
    assert "belum_terverifikasi" in keluaran


def test_run_pipeline_referensi_dosen_file_rusak(tmp_path, monkeypatch,
                                                 capsys):
    """File bukan Excel valid (teks .xlsx palsu) atau Excel tanpa
    kolom scholar_id -> baris_referensi None, run tetap rc == 0."""
    palsu = tmp_path / "palsu.xlsx"
    palsu.write_text("ini bukan excel\n", encoding="utf-8")
    tanpa_kolom = _buat_excel_referensi(
        tmp_path / "tanpa_kolom.xlsx",
        ["No", "NAMA"], [[1, "Budi"]])

    for path in (str(palsu), str(tanpa_kolom)):
        tercatat = {}
        _pasang_patch(monkeypatch, tercatat)

        rc = run_pipeline.main(["--id", "X", "--referensi-dosen", path])

        assert rc == 0
        assert tercatat["baris_referensi"] is None
        keluaran = capsys.readouterr().out
        assert "TIDAK DIPAKAI" in keluaran


def test_run_pipeline_ringkasan_cetak_statistik_author(tmp_path, monkeypatch,
                                                        capsys):
    """Statistik loader MEMUALI kunci author_* -> blok AUTHOR (RAW)
    tercetak dengan semua angkanya."""
    tercatat = {}
    _pasang_patch(monkeypatch, tercatat, statistik={
        "status": run_pipeline.STATUS_RAW_ONLY,
        "raw_result_id": 1, "run_id": None,
        "author_total": 5,
        "author_pemilik_profil": 1,
        "author_bukan_pemilik": 3,
        "author_belum_terverifikasi": 1,
        "publikasi_daftar_terpotong": 2,
        "publikasi_tanpa_penulis": 0,
        "penyimpanan_author": "belum_aktif",
    })

    rc = run_pipeline.main([
        "--id", "X",
        "--referensi-dosen", str(tmp_path / "tidak_ada.xlsx"),
    ])

    assert rc == 0
    keluaran = capsys.readouterr().out
    assert "AUTHOR (RAW)" in keluaran
    assert "author_total             : 5" in keluaran
    assert "author_pemilik_profil    : 1" in keluaran
    assert "author_bukan_pemilik     : 3" in keluaran
    assert "author_belum_terverifikasi: 1" in keluaran
    assert "publikasi_daftar_terpotong: 2" in keluaran
    assert "publikasi_tanpa_penulis  : 0" in keluaran
    assert "belum_aktif" in keluaran


def test_run_pipeline_ringkasan_tanpa_kunci_author_tidak_crash(tmp_path,
                                                                monkeypatch,
                                                                capsys):
    """Statistik TANPA kunci author_* (loader lama) -> tidak crash,
    blok AUTHOR (RAW) tidak dicetak."""
    tercatat = {}
    _pasang_patch(monkeypatch, tercatat, statistik={
        "status": run_pipeline.STATUS_RAW_ONLY,
        "raw_result_id": 1, "run_id": None,
    })

    rc = run_pipeline.main([
        "--id", "X",
        "--referensi-dosen", str(tmp_path / "tidak_ada.xlsx"),
    ])

    assert rc == 0
    keluaran = capsys.readouterr().out
    assert "AUTHOR (RAW)" not in keluaran


# ============================================================
# G2-F: kontrak 9 kunci blok AUTHOR (RAW) dan jalur fatal CLI
# ============================================================

def test_run_pipeline_ringkasan_cetak_semua_sembilan_kunci_author(
        tmp_path, monkeypatch, capsys):
    """G2-F §9(5): blok AUTHOR (RAW) mencetak SELURUH 9 kunci kontrak,
    termasuk author_disimpan, author_dilewati_konflik, dan
    penyimpanan_author - bukan hanya kunci perhitungan."""
    tercatat = {}
    _pasang_patch(monkeypatch, tercatat, statistik={
        "status": run_pipeline.STATUS_RAW_ONLY,
        "raw_result_id": 1, "run_id": None,
        "author_total": 5,
        "author_pemilik_profil": 1,
        "author_bukan_pemilik": 3,
        "author_belum_terverifikasi": 1,
        "publikasi_daftar_terpotong": 2,
        "publikasi_tanpa_penulis": 0,
        "author_disimpan": 4,
        "author_dilewati_konflik": 1,
        "penyimpanan_author": "tersimpan",
    })

    rc = run_pipeline.main([
        "--id", "X",
        "--referensi-dosen", str(tmp_path / "tidak_ada.xlsx"),
    ])

    assert rc == 0
    keluaran = capsys.readouterr().out
    assert "AUTHOR (RAW)" in keluaran
    assert "author_total             : 5" in keluaran
    assert "author_pemilik_profil    : 1" in keluaran
    assert "author_bukan_pemilik     : 3" in keluaran
    assert "author_belum_terverifikasi: 1" in keluaran
    assert "publikasi_daftar_terpotong: 2" in keluaran
    assert "publikasi_tanpa_penulis  : 0" in keluaran
    # Tiga kunci kontrak penyimpanan (G2-B/D4) ikut tercetak.
    assert "author_disimpan          : 4" in keluaran
    assert "author_dilewati_konflik  : 1" in keluaran
    assert "penyimpanan_author       : tersimpan" in keluaran


def test_run_pipeline_gagal_load_tidak_cetak_sukses_palsu(tmp_path,
                                                           monkeypatch,
                                                           capsys):
    """G2-F §6: load_scholar_run melempar LoaderError (transaksi raw
    gagal/ter-rollback) -> rc == 1, pesan GAGAL load tercetak, dan
    TIDAK ada blok AUTHOR (RAW) maupun klaim sukses palsu."""
    tercatat = {}

    async def scrape_palsu(*args, **kwargs):
        return _envelope_sukses()

    def connect_dilarang(*args, **kwargs):
        raise AssertionError("koneksi core tidak boleh dibuka")

    def load_gagal(envelope, scholar_id=None, conn=None, raw_only=None,
                   baris_referensi=None):
        tercatat["raw_only"] = raw_only
        raise run_pipeline.LoaderError("envelope tidak tersimpan")

    monkeypatch.setattr(run_pipeline, "scrape_scholar", scrape_palsu)
    monkeypatch.setattr(run_pipeline, "connect", connect_dilarang)
    monkeypatch.setattr(run_pipeline, "load_scholar_run", load_gagal)

    rc = run_pipeline.main([
        "--id", "X",
        "--referensi-dosen", str(tmp_path / "tidak_ada.xlsx"),
    ])

    assert rc == 1
    # Default tetap raw-only bahkan saat gagal.
    assert tercatat.get("raw_only") is True
    keluaran = capsys.readouterr().out
    assert "GAGAL load" in keluaran
    # Tidak ada ringkasan sukses / blok AUTHOR untuk transaksi gagal.
    assert "AUTHOR (RAW)" not in keluaran
    assert "Baris masuk core" not in keluaran
