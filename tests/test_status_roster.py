"""Test untuk fitur kolom status roster + pembaca RAW incremental.

Menguji (lihat nomor kebutuhan di rencana):
  1.  pastikan_kolom_status: menambah kolom `status` + `pemeriksaan`,
      mengisi sel kosong ("belum"/"sudah"), mempertahankan isi lain.
  10. ubah_status_excel: hanya sel target yang berubah.
  11. baca_daftar membaca kolom `status` (case-insensitive, nilai
      asing -> "", kolom `Status Aktif` TIDAK ikut tertangkap).
  12. baca_judul_tersimpan / ids_riwayat_lengkap: logika SQL diuji
      dengan conn palsu - TIDAK ada database sungguhan.

Tidak ada Google Scholar, tidak ada PostgreSQL, tidak ada browser.
Cara menjalankan:
    python -m pytest tests/test_status_roster.py -q
"""

import sys
from pathlib import Path

# --- Bootstrap import (sama dengan test_run_batch.py) ---------------
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import openpyxl  # noqa: E402
import pytest  # noqa: E402

from src.loading import postgres as pg  # noqa: E402
from src.pipeline import run_batch  # noqa: E402


# ============================================================
# Helper Excel
# ============================================================

def buat_excel(path, header, baris):
    """Tulis workbook Sheet1 berisi `header` + `baris`, kembalikan str path."""
    wb = openpyxl.Workbook()
    ws = wb.active
    assert ws is not None
    ws.title = "Sheet1"
    ws.append(header)
    for b in baris:
        ws.append(b)
    wb.save(str(path))
    wb.close()
    return str(path)


def baca_workbook(path):
    """Baca workbook -> (wb, ws Sheet1); pemanggil yang wb.close()."""
    wb = openpyxl.load_workbook(str(path))
    return wb, wb["Sheet1"]


HEADER_STATUS = ["No", "NAMA", "scholar_id", "Status Scraping", "pemeriksaan"]


# ============================================================
# 1. pastikan_kolom_status
# ============================================================

def test_pastikan_kolom_status_menambah_kolom_belum(tmp_path, monkeypatch):
    """Roster tanpa kolom status -> kolom `status` + `pemeriksaan`
    ditambahkan; sel kosong diisi `belum` (riwayat DB kosong); kolom
    dan data lama tetap utuh."""
    xlsx = buat_excel(tmp_path / "dosen.xlsx",
                      ["No", "NAMA", "scholar_id"],
                      [[1, "Budi Santoso", "ID_A"],
                       [2, "Siti Aminah", "ID_B"]])
    monkeypatch.setattr(run_batch, "_statistik_dari_db",
                        lambda ids, url=None: {})

    run_batch.pastikan_kolom_status(xlsx)

    wb, ws = baca_workbook(xlsx)
    try:
        header = [c.value for c in ws[1]]
        assert header == ["No", "NAMA", "scholar_id", "Status Scraping",
                          "pemeriksaan"]
        assert header.count("Status Scraping") == 1
        assert "status" not in header
        assert ws.cell(row=2, column=4).value == "Belum"
        assert ws.cell(row=3, column=4).value == "Belum"
        assert ws.cell(row=2, column=5).value == "belum_diperiksa"
        assert ws.cell(row=3, column=5).value == "belum_diperiksa"
        # Data lama utuh.
        assert ws.cell(row=2, column=2).value == "Budi Santoso"
        assert ws.cell(row=3, column=3).value == "ID_B"
    finally:
        wb.close()


def test_pastikan_kolom_status_riwayat_lengkap_jadi_sudah(
        tmp_path, monkeypatch):
    """Dosen dengan run RAW SUCCESS (ids_riwayat_lengkap) -> sel
    statusnya `sudah`; dosen lain tetap `belum`."""
    xlsx = buat_excel(tmp_path / "dosen.xlsx",
                      ["No", "NAMA", "scholar_id"],
                      [[1, "Budi Santoso", "ID_A"],
                       [2, "Siti Aminah", "ID_B"]])
    monkeypatch.setattr(run_batch, "_statistik_dari_db",
                        lambda ids, url=None: {"ID_A": {"status_run": "SUCCESS"},
                                               "ID_B": {"status_run": "TIMEOUT"}})

    run_batch.pastikan_kolom_status(xlsx)

    wb, ws = baca_workbook(xlsx)
    try:
        assert ws.cell(row=2, column=4).value == "Sudah"
        assert ws.cell(row=3, column=4).value == "Belum"
    finally:
        wb.close()


def test_pastikan_kolom_status_db_gagal_tidak_mengubah_status(tmp_path, monkeypatch):
    """DB gagal -> status yang sudah ada tidak diubah massal."""
    xlsx = buat_excel(tmp_path / "dosen.xlsx",
                      ["No", "NAMA", "scholar_id"],
                      [[1, "Budi Santoso", "ID_A"]])

    def db_meledak(ids, url=None):
        raise RuntimeError("DB down")

    monkeypatch.setattr(run_batch, "_statistik_dari_db", db_meledak)

    run_batch.pastikan_kolom_status(xlsx)  # tidak melempar

    wb, ws = baca_workbook(xlsx)
    try:
        assert ws.cell(row=2, column=4).value is None
        assert ws.cell(row=2, column=5).value == "belum_diperiksa"
    finally:
        wb.close()


@pytest.mark.parametrize("latest", ["TIMEOUT", "PARTIAL_SUCCESS", "RATE_LIMITED"])
def test_status_run_terbaru_non_success_menghasilkan_belum(
        tmp_path, monkeypatch, latest):
    xlsx = buat_excel(tmp_path / "dosen.xlsx", HEADER_STATUS,
                      [[1, "Budi", "ID_A", "Sudah", ""]])
    monkeypatch.setattr(run_batch, "_statistik_dari_db",
                        lambda ids, url=None: {
                            "ID_A": {"status_run": latest}})

    run_batch.pastikan_kolom_status(xlsx)

    wb, ws = baca_workbook(xlsx)
    try:
        assert ws.cell(row=2, column=4).value == "Belum"
        assert [c.value for c in ws[1]].count("Status Scraping") == 1
        assert "status" not in [str(c.value).lower() for c in ws[1]]
    finally:
        wb.close()


def test_status_scraping_dikenali_dan_id_kosong_belum(tmp_path):
    xlsx = buat_excel(tmp_path / "dosen.xlsx",
                      ["No", "NAMA", "scholar_id", "Status Scraping"],
                      [[1, "Budi", "ID_A", "Sudah"],
                       [2, "Siti", None, None]])

    hasil = run_batch.baca_daftar(xlsx)

    assert [r["status"] for r in hasil] == ["sudah", "belum"]


def test_kolom_status_lama_tidak_dipakai_jika_kolom_kanonis_ada(
        tmp_path, monkeypatch):
    xlsx = buat_excel(tmp_path / "dosen.xlsx",
                      ["No", "scholar_id", "Status Scraping", "status"],
                      [[1, "ID_A", "Belum", "Sudah"]])
    monkeypatch.setattr(run_batch, "_statistik_dari_db",
                        lambda ids, url=None: {
                            "ID_A": {"status_run": "TIMEOUT"}})

    run_batch.pastikan_kolom_status(xlsx)

    wb, ws = baca_workbook(xlsx)
    try:
        headers = [c.value for c in ws[1]]
        assert headers[2] == "Status Scraping"
        assert headers[3] == "Status Scraping (legacy)"
        assert ws.cell(row=2, column=3).value == "Belum"
        assert ws.cell(row=2, column=4).value == "Sudah"
    finally:
        wb.close()


def test_pembaruan_status_satu_scholar_id_tidak_mengubah_lainnya(tmp_path):
    xlsx = buat_excel(tmp_path / "dosen.xlsx",
                      ["No", "NAMA", "scholar_id", "Status Scraping"],
                      [[1, "Budi", "ID_A", "Belum"],
                       [2, "Siti", "ID_B", "Sudah"]])

    assert run_batch.ubah_status_excel(xlsx, "ID_A", status="sudah")

    wb, ws = baca_workbook(xlsx)
    try:
        assert ws.cell(row=2, column=4).value == "Sudah"
        assert ws.cell(row=3, column=4).value == "Sudah"
    finally:
        wb.close()


def test_pastikan_kolom_status_nilai_lama_tidak_ditimpa(tmp_path,
                                                        monkeypatch):
    """Sel status yang SUDAH terisi tidak pernah ditimpa; hanya sel
    kosong yang diisi."""
    xlsx = buat_excel(tmp_path / "dosen.xlsx", HEADER_STATUS,
                      [[1, "Budi Santoso", "ID_A", "Sudah", ""],
                       [2, "Siti Aminah", "ID_B", "", ""]])
    monkeypatch.setattr(run_batch, "_statistik_dari_db",
                        lambda ids, url=None: {"ID_A": {"status_run": "SUCCESS"},
                                               "ID_B": {"status_run": "TIMEOUT"}})

    run_batch.pastikan_kolom_status(xlsx)

    wb, ws = baca_workbook(xlsx)
    try:
        # Budi tetap "sudah" (tidak ditimpa jadi "belum").
        assert ws.cell(row=2, column=4).value == "Sudah"
        # Siti (kosong) diisi "belum".
        assert ws.cell(row=3, column=4).value == "Belum"
        # Pemeriksaan Budi tetap kosong? Tidak - kosong diisi.
        assert ws.cell(row=2, column=5).value == "belum_diperiksa"
    finally:
        wb.close()


def test_pastikan_kolom_status_file_terkunci_tidak_crash(tmp_path,
                                                         monkeypatch):
    """File tidak bisa ditulis (terbuka di Excel) -> peringatan,
    tanpa crash."""
    xlsx = buat_excel(tmp_path / "dosen.xlsx",
                      ["No", "NAMA", "scholar_id"],
                      [[1, "Budi Santoso", "ID_A"]])
    monkeypatch.setattr(pg, "ids_riwayat_lengkap",
                        lambda ids, url=None: set())

    wb_asli = openpyxl.load_workbook(xlsx)

    def save_terkunci(*a, **kw):
        raise PermissionError("file terbuka di Excel")

    wb_asli.save = save_terkunci

    def load_palsu(filename, **kw):
        return wb_asli

    monkeypatch.setattr(run_batch, "load_workbook", load_palsu)

    run_batch.pastikan_kolom_status(xlsx)  # tidak melempar
    wb_asli.close()


def test_pastikan_kolom_status_tanpa_kolom_scholar_id(tmp_path,
                                                      monkeypatch):
    """Tanpa kolom scholar_id -> tidak ada yang diubah, tidak crash
    (baca_daftar nanti yang melaporkan kolom wajib hilang)."""
    xlsx = buat_excel(tmp_path / "dosen.xlsx", ["No", "NAMA"],
                      [[1, "Budi Santoso"]])
    monkeypatch.setattr(pg, "ids_riwayat_lengkap",
                        lambda ids, url=None: set())

    run_batch.pastikan_kolom_status(xlsx)  # tidak melempar

    wb, ws = baca_workbook(xlsx)
    try:
        header = [c.value for c in ws[1]]
        assert header == ["No", "NAMA"]  # tidak berubah
    finally:
        wb.close()


# ============================================================
# 10. ubah_status_excel: integritas sel lain
# ============================================================

def test_ubah_status_excel_hanya_sel_target(tmp_path):
    """ubah_status_excel hanya mengubah sel pemeriksaan dosen target;
    semua kolom/sel lain tetap persis."""
    xlsx = buat_excel(tmp_path / "dosen.xlsx", HEADER_STATUS,
                      [[1, "Budi Santoso", "ID_A", "belum",
                        "belum_diperiksa"],
                       [2, "Siti Aminah", "ID_B", "sudah",
                        "belum_diperiksa"]])

    ok = run_batch.ubah_status_excel(xlsx, "ID_B", pemeriksaan="berhasil")

    assert ok is True
    wb, ws = baca_workbook(xlsx)
    try:
        # Baris Budi (bukan target) utuh.
        assert ws.cell(row=2, column=2).value == "Budi Santoso"
        assert ws.cell(row=2, column=3).value == "ID_A"
        assert ws.cell(row=2, column=4).value == "belum"
        assert ws.cell(row=2, column=5).value == "belum_diperiksa"
        # Baris Siti: hanya pemeriksaan yang berubah.
        assert ws.cell(row=3, column=2).value == "Siti Aminah"
        assert ws.cell(row=3, column=3).value == "ID_B"
        assert ws.cell(row=3, column=4).value == "sudah"
        assert ws.cell(row=3, column=5).value == "berhasil"
    finally:
        wb.close()


def test_ubah_status_excel_membuat_kolom_status(tmp_path):
    """Kolom `status` belum ada -> dibuat saat status diberikan;
    data lain tidak tersentuh."""
    xlsx = buat_excel(tmp_path / "dosen.xlsx",
                      ["No", "NAMA", "scholar_id"],
                      [[1, "Budi Santoso", "ID_A"]])

    ok = run_batch.ubah_status_excel(xlsx, "ID_A", status="sudah")

    assert ok is True
    wb, ws = baca_workbook(xlsx)
    try:
        header = [c.value for c in ws[1]]
        assert header == ["No", "NAMA", "scholar_id", "Status Scraping"]
        assert ws.cell(row=2, column=4).value == "Sudah"
        assert ws.cell(row=2, column=2).value == "Budi Santoso"
    finally:
        wb.close()


def test_ubah_status_excel_id_tidak_ditemukan(tmp_path):
    """scholar_id tidak ada di roster -> False, file tidak berubah."""
    xlsx = buat_excel(tmp_path / "dosen.xlsx", HEADER_STATUS,
                      [[1, "Budi Santoso", "ID_A", "belum",
                        "belum_diperiksa"]])
    sebelum = Path(xlsx).read_bytes()

    ok = run_batch.ubah_status_excel(xlsx, "ID_ZZ", status="sudah")

    assert ok is False
    assert Path(xlsx).read_bytes() == sebelum


def test_ubah_status_excel_file_terkunci(tmp_path, monkeypatch):
    """File tidak bisa ditulis -> False + peringatan, tanpa crash."""
    xlsx = buat_excel(tmp_path / "dosen.xlsx", HEADER_STATUS,
                      [[1, "Budi Santoso", "ID_A", "belum",
                        "belum_diperiksa"]])

    wb_asli = openpyxl.load_workbook(xlsx)

    def save_terkunci(*a, **kw):
        raise PermissionError("file terbuka di Excel")

    wb_asli.save = save_terkunci

    def load_palsu(filename, **kw):
        return wb_asli

    monkeypatch.setattr(run_batch, "load_workbook", load_palsu)

    ok = run_batch.ubah_status_excel(xlsx, "ID_A", status="sudah")

    assert ok is False
    wb_asli.close()


# ============================================================
# 11. baca_daftar membaca kolom `status`
# ============================================================

def test_baca_daftar_status_case_insensitive(tmp_path):
    """`Belum`/`SUDAH` -> `belum`/`sudah`; nilai asing & kosong -> ''."""
    xlsx = buat_excel(tmp_path / "dosen.xlsx",
                      ["No", "NAMA", "scholar_id", "STATUS"],
                      [[1, "Budi", "ID_A", "Belum"],
                       [2, "Siti", "ID_B", "SUDAH"],
                       [3, "Andi", "ID_C", "aktif"],
                       [4, "Rina", "ID_D", None]])

    hasil = run_batch.baca_daftar(xlsx)

    assert [r["status"] for r in hasil] == ["belum", "sudah", "", ""]


def test_baca_daftar_tanpa_kolom_status(tmp_path):
    """Kolom status tidak ada -> semua baris mendapat ''."""
    xlsx = buat_excel(tmp_path / "dosen.xlsx",
                      ["No", "NAMA", "scholar_id"],
                      [[1, "Budi", "ID_A"], [2, "Siti", "ID_B"]])

    hasil = run_batch.baca_daftar(xlsx)

    assert [r["status"] for r in hasil] == ["", ""]


def test_baca_daftar_status_aktif_tidak_tertukar(tmp_path):
    """Kolom `Status Aktif` yang sudah ada di roster asli TIDAK boleh
    terbaca sebagai kolom `status` (exact match setelah strip+lower)."""
    xlsx = buat_excel(tmp_path / "dosen.xlsx",
                      ["No", "NAMA", "Status Aktif", "scholar_id"],
                      [[1, "Budi", "Aktif", "ID_A"],
                       [2, "Siti", "Cuti", "ID_B"]])

    hasil = run_batch.baca_daftar(xlsx)

    assert [r["status"] for r in hasil] == ["", ""]


# ============================================================
# 12. Pembaca RAW dengan conn palsu (tanpa database)
# ============================================================

class _FakeConn:
    """Conn tiruan: merekam SQL + params, mengembalikan baris tetap."""

    def __init__(self, rows):
        self._rows = rows
        self.eksekusi = []
        self.ditutup = False

    def execute(self, sql, params=None):
        self.eksekusi.append((str(sql), params))
        return list(self._rows)

    def close(self):
        self.ditutup = True


def test_baca_judul_tersimpan_sql_dan_filter(tmp_path, monkeypatch):
    """SQL menunjuk raw.scholar_scrape_result dengan jsonb_array_elements
    + filter scholar_id; judul None/kosong disaring; conn ditutup."""
    conn = _FakeConn([("Judul A",), (None,), ("   ",), ("Judul B",)])
    dipanggil_connect = []

    def connect_palsu(*a, **kw):
        dipanggil_connect.append(kw)
        return conn

    monkeypatch.setattr(pg, "connect", connect_palsu)

    hasil = pg.baca_judul_tersimpan("ID_A", url="postgresql://x")

    assert hasil == ["Judul A", "Judul B"]
    assert conn.ditutup is True
    assert dipanggil_connect == [{"url": "postgresql://x"}]
    assert len(conn.eksekusi) == 1
    sql, params = conn.eksekusi[0]
    assert "raw.scholar_scrape_result" in sql
    assert "jsonb_array_elements" in sql
    assert "scholar_id = :sid" in sql
    assert params == {"sid": "ID_A"}


def test_ids_riwayat_lengkap_sql_dan_hasil(tmp_path, monkeypatch):
    """SQL memfilter status = 'SUCCESS' dengan IN expanding; hasil
    berupa set; conn ditutup."""
    conn = _FakeConn([("ID_A",), ("ID_C",)])
    monkeypatch.setattr(pg, "connect", lambda *a, **kw: conn)

    hasil = pg.ids_riwayat_lengkap(["ID_A", "ID_B"], url=None)

    assert hasil == {"ID_A", "ID_C"}
    assert conn.ditutup is True
    assert len(conn.eksekusi) == 1
    sql, params = conn.eksekusi[0]
    assert "raw.scholar_scrape_result" in sql
    assert "status = 'SUCCESS'" in sql
    # Expanding bindparam dirender SQLAlchemy sebagai
    # `IN (__[POSTCOMPILE_ids])` pada str(sql).
    assert "scholar_id IN" in sql
    assert "ids" in sql
    assert params == {"ids": ["ID_A", "ID_B"]}


def test_ids_riwayat_lengkap_daftar_kosong_tanpa_db(tmp_path, monkeypatch):
    """Daftar kosong -> set() TANPA menyentuh database sama sekali."""

    def connect_larang(*a, **kw):
        raise AssertionError("connect dipanggil untuk daftar kosong")

    monkeypatch.setattr(pg, "connect", connect_larang)

    assert pg.ids_riwayat_lengkap([]) == set()


def test_pembaca_raw_terdaftar_di_all():
    """Dua pembaca baru harus ada di __all__ postgres."""
    assert "baca_judul_tersimpan" in pg.__all__
    assert "ids_riwayat_lengkap" in pg.__all__
