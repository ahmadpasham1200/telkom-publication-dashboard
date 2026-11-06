"""
Pipeline BATCH: scrape Google Scholar untuk BANYAK dosen sekaligus.

File ini tidak melakukan scraping maupun loading apa pun. Ia hanya
membaca daftar dosen dari Excel, memanggil run_pipeline.main() satu
per satu, dan mencatat hasil tiap dosen ke file progress CSV supaya
batch bisa dihentikan lalu dilanjutkan tanpa mengulang yang sudah
beres.

Alur
----
    1. Baca daftar dosen dari Excel (--file). Kolom `scholar_id`
       wajib ada; baris dengan scholar_id kosong tidak bisa
       diproses.
    2. Baris tanpa scholar_id dicatat DILEWATI di file progress
       dan TIDAK PERNAH dikirim ke scraper.
    3. Terapkan --mulai (potong baris awal) dan --lanjut (lewati
       yang statusnya sudah terminal di progress).
    4. Panggil run_pipeline.main(["--id", <uid>, ...]) per dosen.
       Setiap panggilan = 1 browser Playwright + 1 koneksi DB, dan
       tiap dosen ATOMIC: loader commit raw dulu lalu core, jadi
       kegagalan satu dosen tidak mempengaruhi dosen lain.
    5. Hasil tiap dosen langsung di-append + flush ke progress CSV.
    6. Ringkasan: total, dilewati, sukses, gagal, plus daftar
       dosen yang GAGAL saja.

Kenapa file progress di-flush setiap baris
------------------------------------------
Batch ini bisa jalan puluhan menit dan sering dihentikan pakai
Ctrl+C. Kalau baris progress ditahan di buffer, semua yang sudah
jalan hilang begitu proses mati - persis yang membuat --lanjut
tidak berguna. Karena itu setiap record ditulis lalu f.flush().

Exit code (main() mengembalikan int)
-------------------------------------
    0   = semua dosen yang diproses berhasil (atau tidak ada yang
          perlu diproses).
    1   = ada minimal satu dosen gagal (GAGAL_LOAD / GAGAL_SCRAPE /
          GAGAL).
    2   = gagal membaca daftar (file tidak ada / kolom scholar_id
          tidak ada).
    130 = KeyboardInterrupt; ringkasan tetap dicetak.

Exit code run_pipeline yang dibaca: 0 -> OK_RAW (default) atau OK
(--load-core), 1 -> GAGAL_LOAD, 2 -> GAGAL_SCRAPE, 3 -> GAGAL_SCRAPE
(HTTP 429 rate limit; batch BERHENTI PENUH setelah dosen itu -
dosen berikutnya tidak dipanggil, jeda antar dosen dilewati).
Exit code 130
berarti interupsi: batch berhenti
rapi TANPA menulis record untuk dosen itu. Exception tak terduga
yang bocor dari run_pipeline.main -> GAGAL. SystemExit (dilempar
argparse lewat sys.exit, dan BUKAN Exception) juga ditangkap per
dosen di loop - lihat blok `except SystemExit` - supaya satu dosen
yang gagal parse tidak mematikan seluruh batch. Status DILEWATI
dipakai untuk baris tanpa profil.

Cara menjalankan
----------------
    python src/pipeline/run_batch.py --file "data/dosen/Data dosen.xlsx"
    python src/pipeline/run_batch.py --file ... --lanjut --jeda 10
    python src/pipeline/run_batch.py --file ... --mulai 50 --ulang
    python src/pipeline/run_batch.py --file ... --load-core

Default batch adalah RAW-ONLY: tiap dosen berhenti setelah envelope
tersimpan di raw (status OK_RAW di progress). Staging/core TIDAK
diisi otomatis; pakai --load-core untuk kembali ke mode lama (OK =
core terisi).

Catatan: `--batch` di CLI ini BUKAN nama fitur batch runner; itu
batas klik "Tampilkan lainnya" yang diteruskan ke run_pipeline
(lihat run_pipeline.py). Nama "batch" untuk batch runner ada di
nama file ini, bukan di nama argumen.

Butuh paket: openpyxl (baca Excel), plus seluruh dependensi
run_pipeline (playwright, beautifulsoup4, sqlalchemy, psycopg,
python-dotenv).
"""

import argparse
import asyncio
import csv
import sys
import time
from datetime import datetime
from pathlib import Path

# --- Bootstrap import -------------------------------------------------
# Supaya file ini jalan baik sebagai skrip
# (`python src/pipeline/run_batch.py`) maupun sebagai modul
# (`python -m src.pipeline.run_batch`), keduanya dari root proyek.
_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# Windows default (cp1252) bisa crash saat print nama dosen
# beraksen atau pesan error dari psycopg.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        pass

from openpyxl import load_workbook  # noqa: E402

from src.pipeline import run_pipeline  # noqa: E402

# ============================================================
# KONSTANTA PROGRESS
# ============================================================

# Header file progress. Jangan diubah seenaknya: file yang sudah
# ada di lapangan harus tetap terbaca oleh baca_progress().
KOLOM_PROGRESS = ["scholar_id", "nama", "status", "exit_code", "waktu"]

STATUS_OK = "OK"
STATUS_OK_RAW = "OK_RAW"
STATUS_GAGAL_LOAD = "GAGAL_LOAD"
STATUS_GAGAL_SCRAPE = "GAGAL_SCRAPE"
STATUS_DILEWATI = "DILEWATI"
STATUS_GAGAL = "GAGAL"

# Status yang berarti baris ini sudah selesai dan --lanjut boleh
# melewatkannya. GAGAL sengaja TIDAK ada di sini: kegagalan tak
# terduga harus dicoba ulang oleh --lanjut.
STATUS_TERMINAL = frozenset({
    STATUS_OK, STATUS_OK_RAW, STATUS_GAGAL_LOAD, STATUS_GAGAL_SCRAPE,
    STATUS_DILEWATI,
})

DEFAULT_PROGRESS = "data dosen/progress_batch.csv"

# Pemetaan exit code run_pipeline.main() -> status progress.
# 130 sengaja tidak ada di sini: artinya interupsi, dan interupsi
# menghentikan seluruh batch, bukan mencatat satu dosen gagal.
# 3 = HTTP 429 rate limit: dicatat GAGAL_SCRAPE, dan SELURUH batch
# berhenti (lihat penanganan exit 3 di loop main()).
_PETA_STATUS = {
    0: STATUS_OK,
    1: STATUS_GAGAL_LOAD,
    2: STATUS_GAGAL_SCRAPE,
    3: STATUS_GAGAL_SCRAPE,  # 3 = HTTP 429 rate limit
}


def _status_dari_exit(exit_code, raw_only):
    """Petakan exit code run_pipeline.main() ke status progress.

    Mode raw-only memetakan exit 0 ke OK_RAW (raw tersimpan, core
    TIDAK terisi) supaya tidak disamakan dengan OK (core terisi).
    """
    if exit_code == 0:
        return STATUS_OK_RAW if raw_only else STATUS_OK
    return _PETA_STATUS.get(exit_code, STATUS_GAGAL)


class KolomScholarIdHilang(SystemExit, argparse.ArgumentTypeError):
    """Kolom `scholar_id` tidak ditemukan di file Excel.

    Turunan ganda SystemExit + argparse.ArgumentTypeError supaya
    penanganannya sah di dua konteks: dari dalam main() ia bisa
    ditangkap sebagai error keluaran (atau lolos ke interpreter
    sebagai exit code 1 dengan pesan ke stderr), dan dari konteks
    argparse ia terbaca seperti error argumen biasa. Pesannya selalu
    memuat teks `scholar_id` plus kolom yang harus ditambahkan.
    """


# ============================================================
# BACA DAFTAR DARI EXCEL
# ============================================================

def _teks_nilai(nilai):
    """Normalisasi sel Excel -> string; kosong (None/putih) -> ""."""
    if nilai is None:
        return ""
    if isinstance(nilai, float) and nilai.is_integer():
        # 123.0 jangan berubah jadi "123.0".
        nilai = int(nilai)
    return str(nilai).strip()


def baca_daftar(file_path: str) -> list:
    """Baca daftar dosen dari Excel -> list dict per baris DATA.

    Tiap dict berisi `no`, `nama`, `scholar_id`, `prodi`, dan
    `status` (string hasil strip; sel kosong/None/putih -> ""). Kolom
    `scholar_id` WAJIB ada; kolom `prodi` dan `status` OPSIONAL -
    kalau tidak ada di header, semua baris mendapat "". `status`
    hanya mengenal nilai "belum"/"sudah" (case-insensitive); nilai
    lain -> "". Semuanya dideteksi case-insensitive terhadap baris
    pertama (header). Baris yang seluruhnya kosong dilewati.

    Melempar KolomScholarIdHilang kalau kolom `scholar_id` tidak
    ada, dan OSError kalau file-nya tidak bisa dibuka.
    """
    try:
        wb = load_workbook(filename=str(file_path), read_only=True,
                           data_only=True)
    except OSError:
        raise
    except Exception as exc:
        # File bukan Excel valid (atau corrupt): laporkan sebagai
        # kegagalan membaca daftar, bukan traceback panjang.
        raise OSError(
            "File {} tidak bisa dibaca sebagai Excel: {}".format(
                file_path, exc)) from exc

    try:
        if "Sheet1" in wb.sheetnames:
            ws = wb["Sheet1"]
        else:
            ws = wb[wb.sheetnames[0]]

        baris_iter = ws.iter_rows(values_only=True)
        header = next(baris_iter, None)
        if header is None:
            raise KolomScholarIdHilang(
                "File {} tidak punya baris header, jadi kolom `scholar_id` "
                "tidak ditemukan. Tambahkan kolom `scholar_id` (ID Google "
                "Scholar dari bagian ?user= URL profil) pada baris header "
                "file Excel itu.".format(file_path))

        judul = [("" if c is None else str(c).strip().lower())
                 for c in header]

        if "scholar_id" not in judul:
            raise KolomScholarIdHilang(
                "Kolom `scholar_id` tidak ditemukan di {}. Kolom yang ada: "
                "{}. Tambahkan kolom `scholar_id` (diisi ID Google Scholar "
                "dari bagian ?user= URL profil) pada file Excel itu."
                .format(file_path, ", ".join(
                    [str(c).strip() for c in header if c is not None]) or
                    "(tidak ada)"))

        # `no`, `nama`, dan `prodi` dicari case-insensitive juga.
        # `no`/`nama` jatuh ke posisi kolom pertama dan kedua kalau
        # tidak ada; `prodi` bersifat OPSIONAL - tanpa kolomnya semua
        # baris mendapat "" (bukan error), karena prodi hanya
        # pelengkap identitas pemilik profil.
        #
        # `status` juga OPSIONAL (kolom roster untuk jalur
        # incremental). Pencariannya EXACT MATCH setelah strip+lower,
        # jadi kolom `Status Aktif` yang sudah ada di roster asli
        # TIDAK ikut tertangkap sebagai `status`.
        idx_scholar = judul.index("scholar_id")
        idx_no = judul.index("no") if "no" in judul else 0
        idx_nama = judul.index("nama") if "nama" in judul else 1
        idx_prodi = judul.index("prodi") if "prodi" in judul else None
        idx_status = (judul.index("status scraping")
                      if "status scraping" in judul else
                      judul.index("status") if "status" in judul else None)

        def _sel(baris, idx):
            if idx is None or idx >= len(baris):
                return ""
            return baris[idx]

        def _status_roster(baris):
            """Sel kolom `status` -> "belum"/"sudah", selain itu "".

            Nilai dibaca case-insensitive ("Belum"/"SUDAH" sah), tapi
            apa pun yang bukan persis `belum`/`sudah` (mis. "aktif",
            sel kosong, atau kolom tidak ada) jatuh ke "" - dosen
            tanpa penanda jelas diperlakukan sebagai belum diproses.
            """
            if not _teks_nilai(_sel(baris, idx_scholar)):
                return "belum"
            nilai = _teks_nilai(_sel(baris, idx_status)).lower()
            return nilai if nilai in ("belum", "sudah") else ""

        hasil = []
        for baris in baris_iter:
            # Baris kosong total (sering muncul di Excel) dilewati,
            # bukan dihitung sebagai dosen tanpa profil.
            if all(c is None or str(c).strip() == "" for c in baris):
                continue
            hasil.append({
                "no": _sel(baris, idx_no),
                "nama": _teks_nilai(_sel(baris, idx_nama)),
                "scholar_id": _teks_nilai(_sel(baris, idx_scholar)),
                "prodi": _teks_nilai(_sel(baris, idx_prodi)),
                "status": _status_roster(baris),
            })
        return hasil
    finally:
        wb.close()


# ============================================================
# KOLOM STATUS ROSTER (Excel)
# ============================================================
#
# Roster Excel ("Data dosen.xlsx") dipakai sebagai sumber mode
# per dosen: kolom `Status Scraping` berisi "Belum"/"Sudah". Dosen "sudah"
# diproses INCREMENTAL (hanya publikasi baru), dosen lain lewat
# jalur awal (run_pipeline penuh). Dua fungsi di bawah menulis
# kolom itu tanpa pernah menyentuh kolom/data lain.

def _pilih_sheet(wb):
    """Sheet roster: Sheet1 kalau ada, else sheet pertama.

    Sama dengan pilihan baca_daftar, supaya kolom yang ditulis di
    sini pasti dibaca oleh baca_daftar.
    """
    if "Sheet1" in wb.sheetnames:
        return wb["Sheet1"]
    return wb[wb.sheetnames[0]]


def _header_map(ws):
    """Map nama header baris 1 (strip+lower) -> nomor kolom 1-based.

    Nama duplikat: yang pertama menang (konsisten dengan
    judul.index() di baca_daftar).
    """
    hasil = {}
    for cell in ws[1]:
        if cell.value is None:
            continue
        nama = str(cell.value).strip().lower()
        if nama and nama not in hasil:
            hasil[nama] = cell.column
    return hasil


def pastikan_kolom_status(path, url=None):
    """Sinkronkan kolom kanonis ke status RAW terbaru per scholar_id.

    Header lama `status` diubah namanya bila kolom kanonis belum ada.
    Kegagalan query RAW tidak mengubah status dosen yang memiliki ID.
    Baris tanpa scholar_id ditetapkan `Belum`.
    """
    try:
        wb = load_workbook(str(path))
    except Exception as exc:
        print("  (peringatan) Tidak bisa membuka {} untuk inisialisasi "
              "kolom status: {}: {}".format(
                  path, type(exc).__name__, exc))
        return

    try:
        ws = _pilih_sheet(wb)
        header = _header_map(ws)
        idx_scholar = header.get("scholar_id")
        if idx_scholar is None:
            # Tanpa kolom scholar_id tidak ada baris yang bisa
            # diinisialisasi; baca_daftar nanti yang melaporkan
            # kolom wajib itu hilang (exit 2).
            return

        kolom_bebas = 1 + max(header.values())
        kolom_status = header.get("status scraping")
        if kolom_status is None and "status" in header:
            kolom_status = header["status"]
            ws.cell(row=1, column=kolom_status, value="Status Scraping")
        elif kolom_status is not None and "status" in header:
            # Pertahankan kolom lama, tetapi keluarkan dari nama kanonis.
            ws.cell(row=1, column=header["status"],
                    value="Status Scraping (legacy)")
        elif kolom_status is None:
            kolom_status = kolom_bebas
            ws.cell(row=1, column=kolom_status, value="Status Scraping")
            kolom_bebas += 1
        if "pemeriksaan" in header:
            kolom_pemeriksaan = header["pemeriksaan"]
        else:
            kolom_pemeriksaan = kolom_bebas
            ws.cell(row=1, column=kolom_pemeriksaan, value="pemeriksaan")

        # Satu query untuk semua ID, lalu sinkronkan status run terbaru.
        scholar_ids = []
        for r_idx in range(2, ws.max_row + 1):
            sid = _teks_nilai(ws.cell(row=r_idx, column=idx_scholar).value)
            if sid:
                scholar_ids.append(sid)

        statistik = None
        if scholar_ids:
            try:
                statistik = _statistik_dari_db(scholar_ids, url=url)
            except Exception as exc:
                print("  (peringatan) Tidak bisa membaca status RAW "
                      "terbaru; status dosen ber-ID tidak diubah: {}: {}"
                      .format(type(exc).__name__, exc))

        for r_idx in range(2, ws.max_row + 1):
            sid = _teks_nilai(ws.cell(row=r_idx, column=idx_scholar).value)
            if not sid:
                ws.cell(row=r_idx, column=kolom_status, value="Belum")
            elif statistik is not None:
                terbaru = statistik.get(sid, {}).get("status_run")
                ws.cell(row=r_idx, column=kolom_status,
                        value="Sudah" if terbaru == "SUCCESS" else "Belum")
            if _teks_nilai(ws.cell(
                    row=r_idx, column=kolom_pemeriksaan).value) == "":
                ws.cell(row=r_idx, column=kolom_pemeriksaan,
                        value="belum_diperiksa")

        try:
            wb.save(str(path))
        except OSError as exc:
            # PermissionError turunan OSError; file terbuka di Excel
            # adalah kasus paling umum. Batch tetap jalan; kolom
            # status hanya belum terinisialisasi.
            print("  (peringatan) Tidak bisa menyimpan {} (mis. file "
                  "terbuka di Excel): {}".format(path, exc))
    finally:
        wb.close()


def ubah_status_excel(path, scholar_id, status=None, pemeriksaan=None):
    """Ubah sel status/pemeriksaan SATU dosen di roster Excel.

    Mencari baris dengan scholar_id yang cocok (strip), menulis sel
    kolom `status` dan/atau `pemeriksaan` (kolom dibuat bila belum
    ada dan nilainya diberikan), lalu save. Kolom dan data lain
    tidak disentuh. Return True bila berhasil; False bila file
    tidak bisa ditulis (peringatan dicetak, batch lanjut) atau
    scholar_id tidak ditemukan.
    """
    if status is None and pemeriksaan is None:
        return True

    try:
        wb = load_workbook(str(path))
    except Exception as exc:
        print("  (peringatan) Tidak bisa membuka {}: {}: {}".format(
            path, type(exc).__name__, exc))
        return False

    try:
        ws = _pilih_sheet(wb)
        header = _header_map(ws)
        idx_scholar = header.get("scholar_id")
        if idx_scholar is None:
            print("  (peringatan) Kolom scholar_id tidak ada di {}; "
                  "status/pemeriksaan tidak diubah.".format(path))
            return False

        kolom_bebas = 1 + max(header.values())
        kolom_status = header.get("status scraping")
        if kolom_status is None and "status" in header:
            kolom_status = header["status"]
            ws.cell(row=1, column=kolom_status, value="Status Scraping")
        elif kolom_status is not None and "status" in header:
            ws.cell(row=1, column=header["status"],
                    value="Status Scraping (legacy)")
        if kolom_status is None and status is not None:
            kolom_status = kolom_bebas
            ws.cell(row=1, column=kolom_status, value="Status Scraping")
            kolom_bebas += 1
        if kolom_status is None:
            # Kolom status tidak ada dan tidak diminta: tidak akan
            # ditulis; 0 hanyalah pengisi tipe (tidak pernah dipakai).
            kolom_status = 0
        kolom_pemeriksaan = header.get("pemeriksaan")
        if kolom_pemeriksaan is None and pemeriksaan is not None:
            kolom_pemeriksaan = kolom_bebas
            ws.cell(row=1, column=kolom_pemeriksaan, value="pemeriksaan")
        if kolom_pemeriksaan is None:
            kolom_pemeriksaan = 0

        target = str(scholar_id).strip()
        if status is not None:
            nilai_status = str(status).strip().lower()
            status = {"sudah": "Sudah", "belum": "Belum"}.get(
                nilai_status, status)
        ketemu = False
        for r_idx in range(2, ws.max_row + 1):
            sid = _teks_nilai(ws.cell(row=r_idx, column=idx_scholar).value)
            if sid != target:
                continue
            ketemu = True
            if status is not None:
                ws.cell(row=r_idx, column=kolom_status, value=status)
            if pemeriksaan is not None:
                ws.cell(row=r_idx, column=kolom_pemeriksaan,
                        value=pemeriksaan)
            break

        if not ketemu:
            print("  (peringatan) scholar_id {} tidak ditemukan di {}; "
                  "status/pemeriksaan tidak diubah.".format(
                      scholar_id, path))
            return False

        try:
            wb.save(str(path))
        except OSError as exc:
            print("  (peringatan) Tidak bisa menyimpan {} (mis. file "
                  "terbuka di Excel): {}".format(path, exc))
            return False
        return True
    finally:
        wb.close()


# ============================================================
# FILE PROGRESS (CSV)
# ============================================================

def _pecah_baris(baris_mentah, delimiter):
    """Pecah satu baris CSV dengan csv.reader -> list field.

    csv.reader dipakai (bukan str.split) karena ia menghormati tanda
    kutip, jadi baris yang kebetulan mengandung delimiter di dalam
    kutip tidak ikut terbelah.
    """
    return next(csv.reader([baris_mentah], delimiter=delimiter))


def baca_progress(path: str) -> dict:
    """Baca CSV progress -> map `scholar_id` -> baris (dict).

    File yang belum ada atau masih kosong -> {}. Baris kembar
    (mis. banyak dosen DILEWATI berbagi scholar_id "") dipakai
    yang terakhir, karena kuncinya memang scholar_id.

    Delimiter dipilih PER BARIS, bukan per file. Kenapa: file
    progress pernah dibuka/di-save ulang oleh Excel dengan locale
    Indonesia (pemisah `;`), dan hasilnya file MIXED - header dan
    sebagian baris memakai `;`, baris lain (yang ditulis ulang oleh
    batch ini dengan tulis_progress) memakai `,`. Deteksi per-file
    (lihat header saja) GAGAL pada kasus ini: header `;` dipilih,
    lalu baris koma yang tidak mengandung `;` sama sekali terbaca
    sebagai SATU field utuh, kuncinya jadi kalimat lengkap, dan
    scholar_id asli tidak pernah muncul - --lanjut diam-diam
    mengulang dosen yang sudah jalan. Jangan hapus pemilihan per
    baris ini.

    Aturan per baris data:
      1. Nama field ditentukan header (delimiter header dipilih
         yang menghasilkan field terbanyak; seri -> koma, format
         penulis tulis_progress).
      2. Tiap baris dipecah dengan `;` dan dengan `,`; dipakai yang
         jumlah fieldnya SAMA dengan jumlah nama field. Kalau
         keduanya sama-sama cocok -> koma (format penulis). Baris
         koma yang dipecah `;` menghasilkan 1 field, jadi otomatis
         kalah hitungan - tidak perlu heuristik lain.
      3. Kalau tidak ada yang cocok -> baris rusak, DIABAIKAN
         (tidak dilempar, tidak masuk hasil).
      4. Baris yang seluruh field kosong (mis. `;;;;`) diabaikan.
    """
    p = Path(path)
    if not p.exists() or p.stat().st_size == 0:
        return {}
    hasil = {}
    with p.open("r", encoding="utf-8", newline="") as f:
        baris_header = f.readline()
        if not baris_header.strip():
            return {}

        header_koma = _pecah_baris(baris_header, ",")
        header_titik = _pecah_baris(baris_header, ";")
        if len(header_titik) > len(header_koma):
            nama_field = header_titik
        else:
            nama_field = header_koma
        jumlah_field = len(nama_field)

        for baris_mentah in f:
            pecah_koma = _pecah_baris(baris_mentah, ",")
            pecah_titik = _pecah_baris(baris_mentah, ";")

            if (len(pecah_titik) == jumlah_field
                    and len(pecah_koma) != jumlah_field):
                field = pecah_titik
            else:
                # Koma menang seri (format penulis) dan menang kalau
                # hanya koma yang cocok.
                field = pecah_koma

            if len(field) != jumlah_field:
                # Baris rusak: jumlah kolom tidak cocok header.
                continue
            if not any((v or "").strip() for v in field):
                # Baris kosong total (mis. ";;;;") diabaikan.
                continue

            baris = dict(zip(nama_field, field))
            kunci = baris.get("scholar_id")
            hasil["" if kunci is None else kunci] = baris
    return hasil


def tulis_progress(path: str, record: dict) -> None:
    """Append 1 baris ke CSV progress lalu FLUSH.

    Flush-nya wajib: batch bisa mati lewat Ctrl+C kapan saja, dan
    baris yang tertahan di buffer akan hilang - membuat --lanjut
    kehilangan pekerjaan yang sudah jalan. Header ditulis hanya
    kalau file belum ada/ masih kosong. `waktu` diisi otomatis
    ISO-8601 bila record tidak membawanya.
    """
    p = Path(path)
    if p.parent != Path(""):
        p.parent.mkdir(parents=True, exist_ok=True)

    perlu_header = (not p.exists()) or p.stat().st_size == 0
    with p.open("a", encoding="utf-8", newline="") as f:
        penulis = csv.DictWriter(f, fieldnames=KOLOM_PROGRESS,
                                 lineterminator="\n",
                                 extrasaction="ignore")
        if perlu_header:
            penulis.writeheader()

        baris = {}
        for kolom in KOLOM_PROGRESS:
            nilai = record.get(kolom, "")
            if nilai is None:
                nilai = ""
            baris[kolom] = nilai
        if not str(baris["waktu"]).strip():
            baris["waktu"] = datetime.now().isoformat(timespec="seconds")
        penulis.writerow(baris)
        f.flush()


# ============================================================
# PILIHAN OPSIONAL: BACA DB UNTUK MEMBEDAKAN LOADED vs SKIPPED
# ============================================================

def _statistik_dari_db(scholar_ids, url=None) -> dict:
    """Baca raw.scholar_scrape_result untuk run terakhir tiap scholar.

    Kembalikan map scholar_id -> dict {status_run, ada_baris}.
    `ada_baris` membedakan LOADED (envelope punya baris, core
    kemungkinan terisi) vs SKIPPED (envelope tanpa baris).

    Sifatnya penambah nilai: kalau database tidak bisa diakses,
    pemanggil menangkap exceptionnya dan melewatinya dengan
    peringatan. Fungsi ini tidak pernah dipanggil dari test
    (di-patch agar test tidak butuh PostgreSQL).
    """
    if not scholar_ids:
        return {}
    import sqlalchemy as sa
    from src.loading.postgres import connect

    sql = sa.text(
        "SELECT DISTINCT ON (scholar_id) "
        "       scholar_id, status, "
        "       jsonb_array_length("
        "           coalesce(raw_envelope->'data'->'rows', '[]'::jsonb)"
        "       ) AS n_rows "
        "FROM raw.scholar_scrape_result "
        "WHERE scholar_id IN :ids "
        "ORDER BY scholar_id, scraped_at DESC"
    ).bindparams(sa.bindparam("ids", expanding=True))

    conn = connect(url=url)
    try:
        hasil = {}
        for sid, status_run, n_rows in conn.execute(
                sql, {"ids": list(scholar_ids)}):
            hasil[sid] = {
                "status_run": status_run,
                "ada_baris": bool(n_rows),
            }
        return hasil
    finally:
        conn.close()


# ============================================================
# CLI
# ============================================================

def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="Scrape daftar dosen dari Excel, satu per satu, "
                    "lalu catat hasilnya ke file progress CSV.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Exit code:\n"
            "  0   semua dosen yang diproses berhasil\n"
            "  1   ada dosen gagal (GAGAL_LOAD/GAGAL_SCRAPE/GAGAL);\n"
            "      batch JUGA BERHENTI PENUH bila ada dosen kena rate\n"
            "      limit HTTP 429 (exit code anak 3): dosen berikutnya\n"
            "      tidak dipanggil dan jeda antar dosen dilewati\n"
            "  2   gagal membaca daftar (file / kolom scholar_id)\n"
            "  130 dihentikan pengguna (Ctrl+C)\n"
            "\n"
            "Contoh:\n"
            "  python src/pipeline/run_batch.py --file \"data/dosen/Data dosen.xlsx\"\n"
            "  python src/pipeline/run_batch.py --file ... --lanjut --jeda 10\n"
            "  python src/pipeline/run_batch.py --file ... --mulai 50 --ulang\n"
            "  python src/pipeline/run_batch.py --file ... --load-core\n"
            "\n"
            "Default: RAW-ONLY - berhenti setelah simpan ke raw\n"
            "(status OK_RAW). --load-core: lanjutkan ke staging/core.\n"
            "\n"
            "Status progress:\n"
            "  OK_RAW  raw tersimpan, core tidak diisi (exit 0). Ini\n"
            "          mencakup run SUCCESS maupun PARTIAL_SUCCESS.\n"
            "          OK_RAW terminal: --lanjut MELEWATInya.\n"
            "PARTIAL_SUCCESS artinya ada data berhasil diambil tapi\n"
            "proses ditandai parsial (mis. batas --batch tercapai).\n"
            "Cek error_message dan failures di envelope RAW.\n"
            "Rate limit HTTP 429 (status RATE_LIMITED, exit anak 3)\n"
            "menghentikan SELURUH batch: record dosen itu tetap\n"
            "ditulis (GAGAL_SCRAPE, exit 3), raw tersimpan, tidak ada\n"
            "retry - lanjutkan nanti dengan --lanjut.\n"
            "Kalau hasil perlu dilengkapi: --ulang memaksa SEMUA dosen\n"
            "diulang (mengalahkan --lanjut); atau jalankan ulang satu\n"
            "profil dengan run_pipeline --id <SCHOLAR_ID>. Perbedaan:\n"
            "--ulang mengulang seluruh daftar; --id hanya satu profil.\n"
        ),
    )
    p.add_argument("--file", required=True,
                   help="Path file Excel daftar dosen (kolom `scholar_id` "
                        "wajib ada; baris kosong = DILEWATI).")
    p.add_argument("--jeda", type=float, default=180.0,
                   help="Jeda antar dosen dalam detik (default 180.0)")
    p.add_argument("--lanjut", action="store_true",
                   help="Lewati dosen yang statusnya di file progress sudah "
                        "terminal (OK, GAGAL_LOAD, GAGAL_SCRAPE, DILEWATI)")
    p.add_argument("--ulang", action="store_true",
                   help="Paksa ulang semua dosen, mengalahkan --lanjut")
    p.add_argument("--mulai", type=int, default=1,
                   help="Mulai dari baris data ke-N, 1-based dihitung dari "
                        "baris DATA pertama, bukan header (default 1)")
    p.add_argument("--progress", default=DEFAULT_PROGRESS,
                   help="Path file progress CSV (default: %(default)s)")
    # --- diteruskan ke run_pipeline.main() ---
    p.add_argument("--headed", action="store_true",
                   help="[diteruskan] Tampilkan browser")
    p.add_argument("--batch", type=int, default=None,
                   help="[diteruskan] Batas klik 'Tampilkan lainnya' di "
                        "run_pipeline (bukan jumlah dosen)")
    p.add_argument("--diam", action="store_true",
                   help="[diteruskan] Matikan progress scraper (selalu "
                        "dinyalakan untuk batch, flag ini dipertahankan "
                        "agar antarmuka tetap sama)")
    p.add_argument("--tampil", type=int, default=None,
                   help="[diteruskan] Jumlah baris tabel yang dicetak "
                        "run_pipeline per dosen (default 0 = tidak mencetak)")
    p.add_argument("--url", default=None,
                   help="[diteruskan] URL koneksi PostgreSQL lengkap")
    p.add_argument("--echo-sql", action="store_true",
                   help="[diteruskan] Cetak SQL yang dijalankan")
    p.add_argument("--raw-only", action="store_true", default=True,
                   help="[diteruskan] Hanya scrape dan simpan ke raw; "
                        "tidak loading ke staging/core (INI DEFAULT)")
    p.add_argument("--load-core", dest="raw_only", action="store_false",
                   help="[diteruskan] Kebalikan --raw-only: lanjutkan "
                        "transformasi/loading ke staging/core (mode lama)")
    return p.parse_args(argv)


def _argv_run_pipeline(args, uid: str) -> list:
    """Susun argv untuk run_pipeline.main() untuk satu dosen.

    Selalu membawa `--id` dan `--diam`; `--tampil 0` dipasang bila
    user tidak menyetel --tampil, supaya batch tidak dibanjiri tabel
    puluhan dosen.

    SEMUA argumen nilai memakai bentuk `--nama=nilai`, bukan dua
    token terpisah. Ini perbaikan bug, bukan selera gaya: ada
    scholar_id yang berawalan minus (mis. `-zWuOpYAAAAJ`; pola
    `^[A-Za-z0-9_-]{7}AAAAJ$` memang mengizinkan `-`). Dengan dua
    token, `--id -zWuOpYAAAAJ` dibaca argparse sebagai opsi, bukan
    nilai -> `error: argument --id: expected one argument` ->
    sys.exit(2) -> batch mati tepat di dosen itu. Bentuk
    `--id=-zWuOpYAAAAJ` aman karena tokennya diawali `--id=`,
    bukan `-`. Konsisten diterapkan ke `--id`, `--batch`,
    `--tampil`, `--url`, dan `--referensi-dosen`;
    `--headed`/`--diam`/`--echo-sql` memang flag tanpa nilai.
    """
    argv = ["--id={}".format(uid), "--diam"]
    if args.headed:
        argv.append("--headed")
    if args.batch is not None:
        argv.append("--batch={}".format(args.batch))
    argv.append("--tampil={}".format(
        0 if args.tampil is None else args.tampil))
    # Referensi identitas pemilik profil: batch SELALU memakai file
    # Excel yang sama dengan yang di-scrape (--file) - kolom
    # `nama` + `scholar_id` di situ adalah identitas pemilik profil.
    # Bentuk `=` dipakai karena path bisa mengandung spasi
    # ("data/dosen/Data dosen.xlsx").
    argv.append("--referensi-dosen={}".format(args.file))
    if args.url:
        argv.append("--url={}".format(args.url))
    if args.echo_sql:
        argv.append("--echo-sql")
    # Mode diungkapkan EKSPLISIT ke anak, apa pun default-nya, supaya
    # perilaku anak tidak bergantung pada default yang bisa berubah:
    # raw_only (default P13) -> --raw-only; --load-core -> --load-core.
    if args.raw_only:
        argv.append("--raw-only")
    else:
        argv.append("--load-core")
    return argv


def _garis(judul):
    print("\n" + "=" * 60)
    print(judul)
    print("=" * 60)


def _proses_incremental(args, uid, baris_referensi):
    """Jalur INCREMENTAL untuk dosen berstatus `sudah` di roster.

    Berbeda dari jalur awal (run_pipeline.main penuh), jalur ini:
      1. Membaca judul yang SUDAH tersimpan di RAW untuk uid itu
         (baca_judul_tersimpan; union semua run RAW).
      2. Menyerahkan himpunan judul itu (sudah dinormalisasi) ke
         scrape_scholar lewat parameter `judul_sudah_tersimpan`,
         sehingga scraper hanya mengambil detail publikasi BARU.
      3. Menyimpan envelope dengan load_scholar_run, persis
         TAHAP 2 run_pipeline (raw-only default; koneksi core
         hanya dibuka bila --load-core).

    Kolom `status` roster TIDAK PERNAH diubah di jalur ini (tetap
    "sudah" walau run fatal/429); yang diperbarui hanya kolom
    `pemeriksaan`: berhasil / parsial / gagal.

    Kembalikan (status, exit_code_simpan, pesan_rate_limit) yang
    kompatibel dengan ekor penulisan record loop main();
    `pesan_rate_limit` berisi envelope error_message bila status
    RATE_LIMITED (HTTP 429), else "". Exit code 3 = sinyal rate
    limit: loop main() menghentikan SELURUH batch setelah record
    dosen ini ditulis. KeyboardInterrupt DILEPAS ke pemanggil
    (berhenti rapi seperti jalur awal). Kegagalan membaca judul
    tersimpan TIDAK pernah dilanjutkan scraping: tanpa himpunan
    judul lama, scraper bisa mengambil ulang detail publikasi
    lama - jadi langsung GAGAL_SCRAPE + pemeriksaan "gagal".
    """
    # ---------- 1 & 2: impor lazy + judul tersimpan ----------
    try:
        from src.ingestion.scrape_scholar_playwright import (
            MAX_BATCHES, normalisasi_judul, scrape_scholar)
        from src.loading.postgres import baca_judul_tersimpan
        judul_raw = baca_judul_tersimpan(uid, url=args.url)
        judul_set = {normalisasi_judul(j) for j in judul_raw}
    except Exception as exc:
        print("  (peringatan) Tidak bisa membaca judul tersimpan untuk "
              "{}: {}: {}".format(uid, type(exc).__name__, exc))
        print("  Jalur incremental dibatalkan (tanpa himpunan judul "
              "lama, scraping penuh berisiko mengambil ulang detail "
              "publikasi lama).")
        ubah_status_excel(args.file, uid, pemeriksaan="gagal")
        ubah_status_excel(args.file, uid, status="Belum")
        return (STATUS_GAGAL_SCRAPE, 2, "")

    # ---------- 3: scrape incremental ----------
    max_batches = args.batch if args.batch is not None else MAX_BATCHES
    envelope = asyncio.run(scrape_scholar(
        uid,
        headless=not args.headed,
        verbose=not args.diam,
        max_batches=max_batches,
        judul_sudah_tersimpan=judul_set,
    ))

    # ---------- 4: load (cermin TAHAP 2 run_pipeline) ----------
    conn_core = None
    try:
        if not args.raw_only:
            from src.loading.postgres import connect
            conn_core = connect(url=args.url, echo=args.echo_sql)
        from src.loading.postgres import load_scholar_run
        load_scholar_run(
            envelope,
            scholar_id=uid,  # dari pemanggil, bukan dari envelope
            conn=conn_core,
            raw_only=args.raw_only,
            baris_referensi=baris_referensi,
        )
    except Exception as exc:
        # LoaderError maupun kesalahan tak terduga: sama seperti
        # run_pipeline, keduanya berarti GAGAL_LOAD (exit 1).
        print("  GAGAL load (incremental): {}: {}".format(
            type(exc).__name__, exc))
        ubah_status_excel(args.file, uid, pemeriksaan="gagal")
        ubah_status_excel(args.file, uid, status="Belum")
        return (STATUS_GAGAL_LOAD, 1, "")
    finally:
        if conn_core is not None:
            conn_core.close()

    # ---------- 5 & 6: petakan hasil + tulis pemeriksaan ----------
    status_run = envelope.get("status")
    rows = (envelope.get("data") or {}).get("rows") or []
    pesan_rate_limit = ""
    if status_run == "SUCCESS":
        pemeriksaan = "berhasil"
        status = STATUS_OK_RAW if args.raw_only else STATUS_OK
        exit_code_simpan = 0
    elif status_run == "PARTIAL_SUCCESS":
        pemeriksaan = "parsial"
        status = STATUS_OK_RAW if args.raw_only else STATUS_OK
        exit_code_simpan = 0
    elif status_run == "RATE_LIMITED":
        # HTTP 429 (navigasi awal / paging / detail). Run fatal:
        # pemeriksaan "gagal" dan kolom kanonis menjadi "Belum".
        # Envelope - termasuk baris parsial yang sudah sempat
        # diambil saat 429 di fase detail - SUDAH tersimpan di raw
        # lewat load_scholar_run di atas (raw-first commit selalu
        # jalan untuk semua status). Exit code 3 = sinyal rate
        # limit; loop main() menghentikan seluruh batch. TIDAK ada
        # retry di mana pun.
        pemeriksaan = "gagal"
        status = STATUS_GAGAL_SCRAPE
        exit_code_simpan = 3
        pesan_rate_limit = envelope.get("error_message") or ""
        print("  RATE_LIMITED (HTTP 429): {}. Tidak ada retry; batch "
              "akan berhenti.".format(pesan_rate_limit or
                                      "(tanpa pesan error)"))
    else:
        # BLOCKED / TIMEOUT / NO_DATA / PARSING_ERROR maupun status
        # tak dikenal: run fatal.
        pemeriksaan = "gagal"
        status = STATUS_GAGAL_SCRAPE
        exit_code_simpan = 2

    ubah_status_excel(args.file, uid,
                      status="Sudah" if status_run == "SUCCESS" else "Belum",
                      pemeriksaan=pemeriksaan)
    print("  Incremental: {} publikasi baru; pemeriksaan {}.".format(
        len(rows), pemeriksaan))
    return (status, exit_code_simpan, pesan_rate_limit)


def _catat_keluaran_terakhir(nama, uid, hasil, exit_code):
    print("  -> {} ({}): {} exit={}".format(
        nama, uid, hasil, exit_code if exit_code != "" else "-"))


def _cetak_berhenti_rate_limit(uid, pesan_error, ringkasan, url=None):
    """Cetak pesan berhenti batch karena HTTP 429 (rate limit).

    Memuat: scholar_id yang kena 429, pesan error scraper bila
    tersedia (menyebut fase: navigasi awal / paging / detail),
    jumlah profil yang sudah diproses (len(ringkasan), termasuk
    dosen yang kena 429), dan hitungan sukses. Hitungan sukses
    DIVERIFIKASI ke raw.scholar_scrape_result bila DB bisa dibaca;
    bila tidak, dilaporkan JUJUR sebagai "tercatat" (tidak
    diverifikasi) - tidak pernah mengklaim penyimpanan yang tidak
    diperiksa.
    """
    STATUS_SUKSES = frozenset({STATUS_OK, STATUS_OK_RAW})
    sukses = [x for x in ringkasan if x[0]["status"] in STATUS_SUKSES]
    ids_sukses = [sid for _, _, sid in sukses]
    print()
    print("  BERHENTI: HTTP 429 (rate limit) dari Google Scholar saat "
          "memproses {}.".format(uid))
    if pesan_error:
        print("  Pesan scraper : {}".format(pesan_error))
    print("  Profil diproses sebelum berhenti : {}".format(
        len(ringkasan)))
    if ids_sukses:
        try:
            db = _statistik_dari_db(ids_sukses, url=url)
            terverifikasi = len([sid for sid in ids_sukses if sid in db])
            print("  Sukses terverifikasi di RAW      : {}/{} "
                  "(record run ada di raw.scholar_scrape_result)".format(
                      terverifikasi, len(ids_sukses)))
        except Exception as exc:
            print("  Tercatat sukses (OK/OK_RAW)      : {} (TIDAK "
                  "diverifikasi ke RAW - baca DB gagal: {})".format(
                      len(sukses), type(exc).__name__))
    else:
        print("  Tercatat sukses (OK/OK_RAW)      : 0")
    print("  Tidak ada retry otomatis. Jalankan lagi dengan --lanjut "
          "untuk melanjutkan sisanya.")


def main(argv=None):
    args = parse_args(argv)
    if args.mulai < 1:
        args.mulai = 1
    if args.jeda < 0:
        args.jeda = 0.0

    _garis("BATCH SCRAPE -> POSTGRESQL")
    print("  File Excel : {}".format(args.file))
    print("  Progress   : {}".format(args.progress))
    print("  Jeda       : {} detik".format(args.jeda))
    print("  Mulai      : baris data ke-{}".format(args.mulai))
    print("  Mode       : {}".format(
        "lanjut (lewati yang sudah terminal)" if args.lanjut and not args.ulang
        else "ulang (paksa semua)" if args.ulang
        else "dari awal"))

    # ---------- 0. INISIALISASI KOLOM STATUS ROSTER ----------
    # Kolom `status`/`pemeriksaan` dibuat (bila belum ada) SEBELUM
    # baca_daftar, supaya nilai yang terbaca sudah final. Gagal di
    # sini tidak fatal: peringatan dicetak, batch tetap jalan.
    pastikan_kolom_status(args.file, url=args.url)

    # ---------- 1. BACA DAFTAR ----------
    try:
        daftar = baca_daftar(args.file)
    except KolomScholarIdHilang as exc:
        # Flush stdout dulu: kalau output di-pipe, stderr tanpa flush
        # bisa muncul sebelum banner di atas.
        sys.stdout.flush()
        print("GAGAL membaca daftar: {}".format(exc), file=sys.stderr)
        return 2
    except OSError as exc:
        sys.stdout.flush()
        print("GAGAL membaca daftar: {}".format(exc), file=sys.stderr)
        return 2

    n_belum = sum(1 for r in daftar if (r.get("status") or "") == "belum")
    n_sudah = sum(1 for r in daftar if (r.get("status") or "") == "sudah")
    print("  Status roster : {} belum, {} sudah".format(n_belum, n_sudah))

    progress = baca_progress(args.progress)
    total = len(daftar)
    tanpa_profil = [r for r in daftar if not r["scholar_id"]]
    print("  Baris data : {}".format(total))
    print("  Tanpa profil Scholar: {}".format(len(tanpa_profil)))

    # ---------- 2. DILEWATI (tanpa profil) ----------
    # Sengaja dilakukan SEBELUM filter --mulai: semua dosen tanpa
    # profil tetap terdokumentasi di progress, apa pun --mulai.
    for r in tanpa_profil:
        sudah = any(
            rec.get("status") == STATUS_DILEWATI and
            rec.get("nama") == r["nama"]
            for rec in progress.values()
        )
        if sudah:
            continue
        record = {
            "scholar_id": "",
            "nama": r["nama"],
            "status": STATUS_DILEWATI,
            "exit_code": "",
            "waktu": "",
        }
        tulis_progress(args.progress, record)
        progress[""] = record
        print("  DILEWATI  : {} (tidak punya profil Scholar)".format(
            r["nama"]))

    # ---------- 3 & 4. FILTER --mulai / --lanjut ----------
    kandidat = []
    dilewati_lanjut = 0
    for nomor, r in enumerate(daftar, start=1):  # nomor baris DATA, 1-based
        if not r["scholar_id"]:
            continue  # sudah dicatat DILEWATI di atas
        if nomor < args.mulai:
            continue
        if args.lanjut and not args.ulang:
            rec = progress.get(r["scholar_id"])
            if rec and rec.get("status") in STATUS_TERMINAL:
                dilewati_lanjut += 1
                print("  LEWAT     : {} ({}) sudah {}".format(
                    r["nama"], r["scholar_id"], rec.get("status")))
                continue
        kandidat.append(r)

    print("  Akan diproses: {}".format(len(kandidat)))
    if dilewati_lanjut:
        print("  Dilewati (--lanjut): {}".format(dilewati_lanjut))

    # ---------- 5. LOOP PER DOSEN ----------
    ringkasan = []  # (record, nama, scholar_id)
    dipanggil = []  # scholar_id yang benar-benar masuk run_pipeline
    terputus = False
    # HTTP 429 (exit code 3): SELURUH batch berhenti setelah record
    # dosen yang kena 429 ditulis - dosen berikutnya tidak dipanggil
    # dan jeda antar dosen dilewati (meneruskan 429 hanya akan
    # menghasilkan 429 lagi).
    berhenti_rate_limit = False
    uid_rate_limit = None
    pesan_rate_limit = ""
    n = len(kandidat)

    try:
        for idx, r in enumerate(kandidat):
            uid = r["scholar_id"]
            print("\n[{}/{}] {} ({})".format(idx + 1, n, r["nama"], uid))
            dipanggil.append(uid)

            # Mode per dosen ditentukan kolom `status` roster, BUKAN
            # --ulang: --ulang hanya mengalahkan --lanjut (skip CSV
            # progress), tidak memaksa dosen "sudah" di-scrape penuh.
            status_roster = (r.get("status") or "").strip().lower()
            if status_roster == "sudah":
                # ---------- jalur INCREMENTAL ----------
                try:
                    status, exit_code_simpan, pesan_rl = \
                        _proses_incremental(args, uid, daftar)
                    if exit_code_simpan == 3:
                        # RATE_LIMITED: batch berhenti penuh.
                        berhenti_rate_limit = True
                        uid_rate_limit = uid
                        pesan_rate_limit = pesan_rl
                except KeyboardInterrupt:
                    # Interupsi di dalam scraper incremental: berhenti
                    # rapi, sama seperti jalur awal.
                    terputus = True
                    break
                except SystemExit as exc:
                    # Sama seperti jalur awal: SystemExit adalah
                    # BaseException, bukan Exception; satu dosen
                    # bermasalah tidak boleh mematikan batch.
                    if exc.code == 130:
                        terputus = True
                        break
                    if isinstance(exc.code, int):
                        status = _status_dari_exit(exc.code, args.raw_only)
                        exit_code_simpan = exc.code
                        if exc.code == 3:
                            # HTTP 429 lewat SystemExit(3): batch
                            # berhenti penuh setelah record ditulis.
                            berhenti_rate_limit = True
                            uid_rate_limit = uid
                    else:
                        status = STATUS_GAGAL
                        exit_code_simpan = ""
                    print("  SystemExit({!r}) dari jalur incremental "
                          "ditangkap; batch tetap lanjut.".format(exc.code))
                except Exception as exc:
                    status = STATUS_GAGAL
                    exit_code_simpan = ""
                    print("  GAGAL tak terduga: {}: {}".format(
                        type(exc).__name__, exc))
            else:
                # ---------- jalur AWAL (perilaku lama) ----------
                argv_anak = _argv_run_pipeline(args, uid)
                try:
                    exit_code = run_pipeline.main(argv_anak)
                except KeyboardInterrupt:
                    # Interupsi di dalam run_pipeline: berhenti rapi,
                    # jangan catat apa pun (dosen ini bisa jadi setengah
                    # jalan; biar --lanjut mengulangnya nanti).
                    terputus = True
                    break
                except SystemExit as exc:
                    # SystemExit harus ditangkap DI TINGKAT INI, sejajar
                    # Exception, karena ia BaseException - BUKAN Exception.
                    # argparse memanggil sys.exit() (melempar SystemExit)
                    # saat gagal parse, jadi kalau hanya `except
                    # Exception` yang ada, SystemExit lolos dari loop,
                    # lolos dari try luar, dan mematikan SELURUH batch di
                    # tengah jalan (persis yang terjadi saat satu
                    # scholar_id membuat run_pipeline error parse).
                    # Kontraknya dipertahankan: satu dosen bermasalah
                    # tidak boleh mematikan dosen berikutnya.
                    if exc.code == 130:
                        # Sama seperti KeyboardInterrupt: ini permintaan
                        # berhenti, bukan kegagalan satu dosen.
                        terputus = True
                        break
                    if isinstance(exc.code, int):
                        # Kode keluaran angka -> perlakukan seperti
                        # run_pipeline.main() yang mengembalikan kode itu,
                        # ikut jalur _PETA_STATUS seperti biasa.
                        exit_code = exc.code
                        status = _status_dari_exit(exit_code, args.raw_only)
                        exit_code_simpan = exit_code
                        if exit_code == 3:
                            # HTTP 429 lewat SystemExit(3): batch
                            # berhenti penuh setelah record ditulis.
                            berhenti_rate_limit = True
                            uid_rate_limit = uid
                        print("  SystemExit(code={}) dari run_pipeline "
                              "ditangkap; batch tetap lanjut.".format(
                                  exit_code))
                    else:
                        # sys.exit() tanpa kode angka (None) atau kode
                        # aneh -> tidak ada exit code yang bisa dipetakan,
                        # perlakukan seperti kegagalan tak terduga.
                        status = STATUS_GAGAL
                        exit_code_simpan = ""
                        print("  SystemExit tanpa kode angka ({!r}) dari "
                              "run_pipeline; dicatat GAGAL.".format(
                                  exc.code))
                except Exception as exc:
                    # Exception tak terduga TIDAK boleh mematikan batch:
                    # satu dosen bermasalah, dosen berikutnya tetap jalan.
                    status = STATUS_GAGAL
                    exit_code_simpan = ""
                    print("  GAGAL tak terduga: {}: {}".format(
                        type(exc).__name__, exc))
                else:
                    if exit_code == 130:
                        # run_pipeline menangkap KeyboardInterrupt sendiri.
                        terputus = True
                        break
                    status = _status_dari_exit(exit_code, args.raw_only)
                    exit_code_simpan = exit_code
                    if exit_code == 3:
                        # HTTP 429 (rate limit): batch berhenti penuh
                        # setelah record dosen ini ditulis; jeda antar
                        # dosen TIDAK dijalankan.
                        berhenti_rate_limit = True
                        uid_rate_limit = uid

                # Perbarui hanya baris dosen yang baru selesai diproses.
                if exit_code_simpan != 0:
                    ubah_status_excel(args.file, uid, status="Belum")
                else:
                    # Exit 0 mencakup SUCCESS dan PARTIAL_SUCCESS.
                    # Hanya status RAW terbaru SUCCESS yang berarti Sudah.
                    try:
                        stat = _statistik_dari_db([uid], url=args.url)
                        roster_status = (
                            "Sudah" if stat.get(uid, {}).get(
                                "status_run") == "SUCCESS" else "Belum")
                        if ubah_status_excel(
                                args.file, uid, status=roster_status):
                            print("  Status Scraping: {} -> {}."
                                  .format(uid, roster_status))
                    except Exception as exc:
                        print("  (peringatan) Tidak bisa memperbarui "
                              "Status Scraping untuk {}: {}: {}".format(
                                  uid, type(exc).__name__, exc))

            record = {
                "scholar_id": uid,
                "nama": r["nama"],
                "status": status,
                "exit_code": exit_code_simpan,
                "waktu": "",
            }
            tulis_progress(args.progress, record)
            progress[uid] = record
            ringkasan.append((record, r["nama"], uid))
            _catat_keluaran_terakhir(r["nama"], uid, status,
                                     exit_code_simpan)

            if berhenti_rate_limit:
                # HTTP 429: hentikan SELURUH batch. Record dosen yang
                # kena 429 SUDAH ditulis di atas; jeda antar dosen
                # sengaja DILEWATI (tidak ada dosen berikutnya yang
                # layak menunggu jeda).
                _cetak_berhenti_rate_limit(
                    uid_rate_limit, pesan_rate_limit, ringkasan,
                    url=args.url)
                break

            # Jeda antar iterasi; baris terakhir tidak perlu.
            if idx < n - 1:
                time.sleep(args.jeda)
    except KeyboardInterrupt:
        terputus = True

    # ---------- RINGKASAN ----------
    _garis("RINGKASAN BATCH")
    STATUS_SUKSES = frozenset({STATUS_OK, STATUS_OK_RAW})
    sukses = [x for x in ringkasan if x[0]["status"] in STATUS_SUKSES]
    gagal = [x for x in ringkasan if x[0]["status"] not in STATUS_SUKSES]

    print("  Total baris data      : {}".format(total))
    print("  Dilewati (tanpa profil): {}".format(len(tanpa_profil)))
    print("  Dilewati (--lanjut)    : {}".format(dilewati_lanjut))
    print("  Dipanggil scraper      : {}".format(len(dipanggil)))
    print("  Sukses (OK)            : {}".format(
        len([x for x in sukses if x[0]["status"] == STATUS_OK])))
    print("  Sukses (OK_RAW)        : {}".format(
        len([x for x in sukses if x[0]["status"] == STATUS_OK_RAW])))
    print("  Gagal                  : {}".format(len(gagal)))

    # Penambah nilai: kalau DB bisa dibaca, tunjukkan mana yang
    # LOADED vs SKIPPED. Gagal membaca DB jangan pernah crash.
    if sukses:
        try:
            db = _statistik_dari_db(
                [sid for _, _, sid in sukses], url=args.url)
        except Exception as exc:
            print("  (peringatan) Tidak bisa membaca "
                  "raw.scholar_scrape_result: {}: {}".format(
                      type(exc).__name__, exc))
            print("           Ringkasan LOADED/SKIPPED dilewati.")
        else:
            if db:
                loaded = [sid for sid, v in db.items() if v["ada_baris"]]
                skipped = [sid for sid, v in db.items()
                           if not v["ada_baris"]]
                print()
                print("  Dari raw.scholar_scrape_result (run terakhir):")
                if args.raw_only:
                    print("    punya baris (raw tersimpan)      : {}"
                          .format(len(loaded)))
                    print("    tanpa baris (raw tanpa data)     : {}"
                          .format(len(skipped)))
                else:
                    print("    punya baris (kemungkinan LOADED) : {}"
                          .format(len(loaded)))
                    print("    tanpa baris (kemungkinan SKIPPED): {}"
                          .format(len(skipped)))
                for sid in skipped:
                    if args.raw_only:
                        print("      - {} (raw tanpa baris)".format(sid))
                    else:
                        print("      - {} (SKIPPED, envelope tanpa baris)"
                              .format(sid))
            else:
                print("  (peringatan) Tidak ada record di "
                      "raw.scholar_scrape_result untuk dosen yang OK.")

    if gagal:
        print()
        print("  DOSEN YANG GAGAL:")
        for record, nama, uid in gagal:
            print("    - {} ({}): {} exit={}".format(
                nama, uid, record["status"],
                record["exit_code"] if record["exit_code"] != "" else "-"))

    if terputus:
        print()
        print("  Dihentikan pengguna. Ringkasan di atas tetap valid; "
              "jalankan lagi dengan --lanjut untuk melanjutkan.")
        return 130

    if berhenti_rate_limit:
        # HTTP 429: ringkasan tetap dicetak (sudah di atas), lalu
        # batch keluar dengan kode 1 - BUKAN 130 (ini bukan interupsi
        # pengguna) dan BUKAN 0 (ada pekerjaan yang belum selesai).
        print()
        print("  Batch dihentikan karena HTTP 429 (rate limit). "
              "Ringkasan di atas tetap valid; jalankan lagi dengan "
              "--lanjut untuk melanjutkan sisanya.")

    if gagal:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

