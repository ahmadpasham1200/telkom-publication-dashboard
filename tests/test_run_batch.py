"""Test untuk src/pipeline/run_batch.py.

File ini menguji batch runner yang memanggil run_pipeline.main()
banyak kali. Scrape asli, Playwright, dan PostgreSQL TIDAK pernah
dipakai di sini:

    - run_pipeline.main  di-patch (monkeypatch) -> tidak ada browser.
    - _statistik_dari_db di-patch                -> tidak ada database.
    - --jeda 0                                  -> tidak ada sleep.

Yang dijaga adalah kontrak tiga fungsi murni (baca_daftar,
baca_progress, tulis_progress) plus perilaku loop main(): baris
tanpa scholar_id tidak boleh pernah sampai ke scraper.

Cara menjalankan:
    python -m pytest tests/test_run_batch.py -q
"""

import argparse
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

import openpyxl  # noqa: E402
import pytest  # noqa: E402

from src.pipeline import run_batch  # noqa: E402


# ============================================================
# Fixture Excel (dibuat on-the-fly di tmp_path)
# ============================================================

HEADER_KELOMPOK = ["No", "NAMA", "Status Aktif", "PRODI", "NIP", "scholar_id"]


def buat_excel(path, header, baris):
    """Tulis workbook Sheet1 berisi `header` + `baris`, kembalikan str path."""
    wb = openpyxl.Workbook()
    ws = wb.active
    # Type stub openpyxl menandai `active` sebagai Optional[Worksheet],
    # padahal Workbook baru selalu punya sheet. Assert ini menghilangkan
    # 3 error LSP palsu di fungsi ini tanpa mengubah perilaku.
    assert ws is not None
    ws.title = "Sheet1"
    ws.append(header)
    for b in baris:
        ws.append(b)
    wb.save(str(path))
    wb.close()
    return str(path)


# ============================================================
# baca_daftar
# ============================================================

def test_baca_daftar_campuran_id_dan_kosong(tmp_path):
    """Campuran ID valid + kosong: jumlah dan nilai harus persis.

    Header sengaja ditulis `Scholar_ID` (bukan `scholar_id`) untuk
    membuktikan deteksinya case-insensitive.
    """
    xlsx = buat_excel(tmp_path / "dosen.xlsx", HEADER_KELOMPOK, [
        [1, "Budi Santoso", "Aktif", "Informatika", 111, "AAA_id-1"],
        [2, "Siti Aminah", "Aktif", "Informatika", 222, None],
        [3, "Andi Wijaya", "Cuti", "Teknik Elektro", 333, "   "],
        [4, "Rina Marlina", "Aktif", "Teknik Logistik", 444, "CCC_id-3"],
    ])

    hasil = run_batch.baca_daftar(xlsx)

    assert len(hasil) == 4
    assert hasil[0] == {"no": 1, "nama": "Budi Santoso", "scholar_id": "AAA_id-1"}
    # Sel kosong (None) dan sel berisi spasi saja -> "".
    assert hasil[1]["scholar_id"] == ""
    assert hasil[1]["nama"] == "Siti Aminah"
    assert hasil[2]["scholar_id"] == ""
    # Baris setelah sel kosong tetap terbaca, tidak ikut "tersedak".
    assert hasil[3] == {"no": 4, "nama": "Rina Marlina", "scholar_id": "CCC_id-3"}


def test_baca_daftar_tanpa_kolom_scholar_id(tmp_path):
    """Kolom scholar_id tidak ada -> error yang menyebut kolom itu."""
    xlsx = buat_excel(tmp_path / "tanpa.xlsx",
                      ["No", "NAMA", "NIP"],
                      [[1, "Budi Santoso", 111]])

    with pytest.raises((SystemExit, argparse.ArgumentTypeError)) as excinfo:
        run_batch.baca_daftar(xlsx)

    pesan = str(excinfo.value)
    # Pesan wajib memuat nama kolomnya...
    assert "scholar_id" in pesan
    # ...dan menyebut kolom apa yang harus ditambahkan.
    assert "tambahkan" in pesan.lower()


def test_baca_daftar_file_tidak_ada(tmp_path):
    """File tak ada -> OSError, bukan diam-diam mengembalikan [].

    Supaya typo path tidak membuat batch "sukses" tanpa memproses
    siapa pun.
    """
    with pytest.raises(OSError):
        run_batch.baca_daftar(str(tmp_path / "hilang.xlsx"))


# ============================================================
# baca_progress / tulis_progress
# ============================================================

def test_progress_roundtrip(tmp_path):
    """Tulis 2 record, baca balik -> dapat 2 key + isi benar."""
    path = tmp_path / "progress.csv"

    run_batch.tulis_progress(str(path), {
        "scholar_id": "ID_A", "nama": "Budi Santoso",
        "status": "OK", "exit_code": 0, "waktu": "",
    })
    run_batch.tulis_progress(str(path), {
        "scholar_id": "ID_B", "nama": "Siti Aminah",
        "status": "GAGAL_LOAD", "exit_code": 1, "waktu": "",
    })

    hasil = run_batch.baca_progress(str(path))

    assert set(hasil) == {"ID_A", "ID_B"}
    assert hasil["ID_A"]["status"] == "OK"
    assert hasil["ID_A"]["nama"] == "Budi Santoso"
    assert hasil["ID_B"]["status"] == "GAGAL_LOAD"
    assert hasil["ID_B"]["exit_code"] == "1"
    # waktu kosong -> diisi otomatis ISO-8601.
    assert len(hasil["ID_A"]["waktu"]) >= 19

    # Header persis dan tanpa CR (lineterminator="\n").
    teks = Path(path).read_text(encoding="utf-8")
    baris = teks.splitlines()
    assert baris[0] == "scholar_id,nama,status,exit_code,waktu"
    assert "\r" not in teks
    assert len(baris) == 3  # header + 2 record


def test_tulis_progress_langsung_terbaca(tmp_path):
    """Baris harus sudah ada di file begitu tulis_progress selesai."""
    path = tmp_path / "progress.csv"

    run_batch.tulis_progress(str(path), {
        "scholar_id": "FLUSH_ID", "nama": "Budi",
        "status": "OK", "exit_code": 0, "waktu": "",
    })

    # Dibaca langsung dari disk, tanpa proses apa pun di antaranya.
    teks = Path(path).read_text(encoding="utf-8")
    assert "FLUSH_ID" in teks
    assert "Budi" in teks


def test_baca_progress_file_belum_ada(tmp_path):
    """Progress belum pernah dibuat -> {}, bukan error."""
    assert run_batch.baca_progress(str(tmp_path / "belum.csv")) == {}


# ============================================================
# main(): loop batch
# ============================================================

def _pasang_patch_jalan_sendiri(monkeypatch, hasil, panggilan):
    """Patch run_pipeline.main (panggilan terekam, kode dari `hasil`).

    `hasil` adalah daftar exit code yang dipakai berurutan; begitu
    habis, memakai kode terakhir.
    """
    kode = iter(hasil)

    def run_pipeline_palsu(argv):
        panggilan.append(list(argv))
        try:
            return next(kode)
        except StopIteration:
            return hasil[-1]

    monkeypatch.setattr(run_batch.run_pipeline, "main", run_pipeline_palsu)
    # Supaya test tidak pernah menyentuh PostgreSQL.
    monkeypatch.setattr(run_batch, "_statistik_dari_db",
                        lambda ids, url=None: {})


def _nilai(argv, nama):
    """Ambil nilai argumen dari argv run_pipeline.

    Mendukung bentuk `--nama=nilai` (dipakai _argv_run_pipeline
    sejak perbaikan bug scholar_id berawalan `-`) dan bentuk lama
    dua token `--nama nilai`.
    """
    for i, tok in enumerate(argv):
        if tok == nama:
            return argv[i + 1]
        if tok.startswith(nama + "="):
            return tok.split("=", 1)[1]
    raise AssertionError("{} tidak ada di argv {}".format(nama, argv))


def _id_dipanggil(panggilan):
    return [_nilai(argv, "--id") for argv in panggilan]


def test_main_loop_kosong_tidak_dipanggil(tmp_path, monkeypatch):
    """1 ID valid, 1 kosong, 1 valid -> scraper hanya 2x, status benar."""
    xlsx = buat_excel(tmp_path / "dosen.xlsx", HEADER_KELOMPOK, [
        [1, "Budi Santoso", "Aktif", "Informatika", 111, "ID_A"],
        [2, "Siti Aminah", "Aktif", "Informatika", 222, None],
        [3, "Andi Wijaya", "Aktif", "Teknik Elektro", 333, "ID_B"],
    ])
    progress = tmp_path / "progress.csv"
    panggilan = []
    _pasang_patch_jalan_sendiri(monkeypatch, [0, 1], panggilan)

    rc = run_batch.main([
        "--file", xlsx,
        "--progress", str(progress),
        "--jeda", "0",
    ])

    # Scraper dipanggil tepat 2x, untuk 2 ID valid, berurutan.
    assert _id_dipanggil(panggilan) == ["ID_A", "ID_B"]
    assert "" not in _id_dipanggil(panggilan)
    # argv ke run_pipeline selalu membawa --id=..., --diam, --tampil=0.
    for argv in panggilan:
        assert argv[0].startswith("--id=")
        assert "--diam" in argv
        assert _nilai(argv, "--tampil") == "0"

    data = run_batch.baca_progress(str(progress))
    assert data["ID_A"]["status"] == "OK"
    assert data["ID_A"]["exit_code"] == "0"
    assert data["ID_B"]["status"] == "GAGAL_LOAD"
    assert data["ID_B"]["exit_code"] == "1"
    # Baris tanpa profil tercatat DILEWATI, bukan dipanggil.
    assert data[""]["status"] == "DILEWATI"

    # Ada kegagalan -> exit code batch 1.
    assert rc == 1


def test_main_tampil_diteruskan_bila_diset(tmp_path, monkeypatch):
    """--tampil dari user diteruskan apa adanya (bukan dipaksa 0)."""
    xlsx = buat_excel(tmp_path / "dosen.xlsx", HEADER_KELOMPOK, [
        [1, "Budi Santoso", "Aktif", "Informatika", 111, "ID_A"],
    ])
    panggilan = []
    _pasang_patch_jalan_sendiri(monkeypatch, [0], panggilan)

    rc = run_batch.main([
        "--file", xlsx,
        "--progress", str(tmp_path / "p.csv"),
        "--jeda", "0",
        "--tampil", "7",
        "--batch", "3",
    ])

    argv = panggilan[0]
    assert _nilai(argv, "--tampil") == "7"
    assert _nilai(argv, "--batch") == "3"
    assert rc == 0


def test_main_lanjut_skip_yang_sudah_terminal(tmp_path, monkeypatch):
    """--lanjut: status OK di progress -> tidak dipanggil lagi."""
    xlsx = buat_excel(tmp_path / "dosen.xlsx", HEADER_KELOMPOK, [
        [1, "Budi Santoso", "Aktif", "Informatika", 111, "ID_A"],
        [2, "Andi Wijaya", "Aktif", "Teknik Elektro", 333, "ID_B"],
    ])
    progress = tmp_path / "progress.csv"
    run_batch.tulis_progress(str(progress), {
        "scholar_id": "ID_A", "nama": "Budi Santoso",
        "status": "OK", "exit_code": 0, "waktu": "",
    })
    panggilan = []
    _pasang_patch_jalan_sendiri(monkeypatch, [0], panggilan)

    rc = run_batch.main([
        "--file", xlsx, "--progress", str(progress),
        "--jeda", "0", "--lanjut",
    ])

    assert _id_dipanggil(panggilan) == ["ID_B"]
    assert rc == 0


def test_main_ulang_paksa_semua(tmp_path, monkeypatch):
    """--ulang mengalahkan --lanjut: yang sudah OK tetap dipanggil."""
    xlsx = buat_excel(tmp_path / "dosen.xlsx", HEADER_KELOMPOK, [
        [1, "Budi Santoso", "Aktif", "Informatika", 111, "ID_A"],
        [2, "Andi Wijaya", "Aktif", "Teknik Elektro", 333, "ID_B"],
    ])
    progress = tmp_path / "progress.csv"
    run_batch.tulis_progress(str(progress), {
        "scholar_id": "ID_A", "nama": "Budi Santoso",
        "status": "OK", "exit_code": 0, "waktu": "",
    })
    panggilan = []
    _pasang_patch_jalan_sendiri(monkeypatch, [0], panggilan)

    rc = run_batch.main([
        "--file", xlsx, "--progress", str(progress),
        "--jeda", "0", "--lanjut", "--ulang",
    ])

    assert _id_dipanggil(panggilan) == ["ID_A", "ID_B"]
    assert rc == 0


def test_main_mulai_lewati_baris_awal(tmp_path, monkeypatch):
    """--mulai 2: baris data pertama tidak diproses sama sekali."""
    xlsx = buat_excel(tmp_path / "dosen.xlsx", HEADER_KELOMPOK, [
        [1, "Budi Santoso", "Aktif", "Informatika", 111, "ID_A"],
        [2, "Siti Aminah", "Aktif", "Informatika", 222, "ID_B"],
        [3, "Andi Wijaya", "Aktif", "Teknik Elektro", 333, "ID_C"],
    ])
    panggilan = []
    _pasang_patch_jalan_sendiri(monkeypatch, [0], panggilan)

    rc = run_batch.main([
        "--file", xlsx, "--progress", str(tmp_path / "p.csv"),
        "--jeda", "0", "--mulai", "2",
    ])

    assert _id_dipanggil(panggilan) == ["ID_B", "ID_C"]
    assert rc == 0


def test_main_exception_persatu_dosen_tidak_mematikan_batch(tmp_path, monkeypatch):
    """Exception tak terduga di dosen 1 -> dosen 2 tetap jalan, status GAGAL."""
    xlsx = buat_excel(tmp_path / "dosen.xlsx", HEADER_KELOMPOK, [
        [1, "Budi Santoso", "Aktif", "Informatika", 111, "ID_A"],
        [2, "Andi Wijaya", "Aktif", "Teknik Elektro", 333, "ID_B"],
    ])
    progress = tmp_path / "progress.csv"
    panggilan = []

    def meledak(argv):
        panggilan.append(list(argv))
        raise RuntimeError("database meledak")

    monkeypatch.setattr(run_batch.run_pipeline, "main", meledak)
    monkeypatch.setattr(run_batch, "_statistik_dari_db",
                        lambda ids, url=None: {})

    rc = run_batch.main([
        "--file", xlsx, "--progress", str(progress),
        "--jeda", "0",
    ])

    # Kedua dosen tetap dicoba.
    assert _id_dipanggil(panggilan) == ["ID_A", "ID_B"]
    data = run_batch.baca_progress(str(progress))
    assert data["ID_A"]["status"] == "GAGAL"
    assert data["ID_B"]["status"] == "GAGAL"
    assert rc == 1


def test_main_tanpa_kolom_scholar_id_keluar_dengan_kode_2(tmp_path, monkeypatch):
    """Excel tanpa kolom scholar_id -> main tidak memanggil scraper."""
    xlsx = buat_excel(tmp_path / "dosen.xlsx", ["No", "NAMA"], [[1, "Budi"]])
    panggilan = []
    _pasang_patch_jalan_sendiri(monkeypatch, [0], panggilan)

    rc = run_batch.main([
        "--file", xlsx, "--progress", str(tmp_path / "p.csv"),
        "--jeda", "0",
    ])

    assert panggilan == []
    assert rc == 2


def test_parse_args_wajibkan_file():
    """--file wajib; argumen batch tidak boleh memakai nama --batch untuk
    fitur lain -- --batch di sini memang diteruskan ke run_pipeline."""
    with pytest.raises(SystemExit):
        run_batch.parse_args([])
    args = run_batch.parse_args(["--file", "x.xlsx"])
    assert args.file == "x.xlsx"
    assert args.jeda == 8.0
    assert args.mulai == 1
    assert args.progress == "data dosen/progress_batch.csv"
    assert args.lanjut is False and args.ulang is False


# ============================================================
# Bug 1: scholar_id berawalan `-` (mis. -zWuOpYAAAAJ)
# ============================================================

def test_argv_id_awalan_minus_diterima_parse_args_asli():
    """--id=... (bentuk `=`) untuk uid berawalan `-` LULUS parse asli.

    Bentuk dua token `--id -zWuOpYAAAAJ` membuat argparse mengira
    `-zWuOpYAAAAJ` adalah flag -> SystemExit(2). Bentuk `=` tidak.
    Dibuktikan dengan memanggil run_pipeline.parse_args() yang asli,
    bukan sekadar mengecek string.
    """
    uid = "-zWuOpYAAAAJ"
    args = run_batch.parse_args(["--file", "x.xlsx"])
    argv = run_batch._argv_run_pipeline(args, uid)

    # Bentuk `=` dipakai untuk --id.
    assert any(tok.startswith("--id=") for tok in argv)
    assert "--id={}".format(uid) in argv

    # Parse ASLI run_pipeline: tidak boleh melempar SystemExit.
    parsed = run_batch.run_pipeline.parse_args(argv)
    assert parsed.uid == uid


def test_argv_argumen_lain_tetap_benar():
    """Konversi ke bentuk `=` tidak merusak argumen lain.

    --diam/--headed/--echo-sql tetap flag; --tampil/--batch/--url
    ikut bentuk `=` dan tetap terbaca benar oleh parse asli.
    """
    args = run_batch.parse_args([
        "--file", "x.xlsx",
        "--headed", "--batch", "5", "--tampil", "9",
        "--url", "postgresql://u:p@h:5432/db", "--echo-sql",
    ])
    argv = run_batch._argv_run_pipeline(args, "ID_A")

    assert argv[0] == "--id=ID_A"
    assert "--diam" in argv
    assert "--headed" in argv
    assert "--echo-sql" in argv
    assert _nilai(argv, "--tampil") == "9"
    assert _nilai(argv, "--batch") == "5"
    assert _nilai(argv, "--url") == "postgresql://u:p@h:5432/db"

    parsed = run_batch.run_pipeline.parse_args(argv)
    assert parsed.uid == "ID_A"
    assert parsed.tampil == 9
    assert parsed.batch == 5
    assert parsed.url == "postgresql://u:p@h:5432/db"
    assert parsed.headed is True
    assert parsed.echo_sql is True


def test_argv_tampil_default_nol():
    """Tanpa --tampil dari user, argv membawa --tampil=0."""
    args = run_batch.parse_args(["--file", "x.xlsx"])
    argv = run_batch._argv_run_pipeline(args, "ID_A")
    assert _nilai(argv, "--tampil") == "0"
    assert run_batch.run_pipeline.parse_args(argv).tampil == 0


# ============================================================
# Bug 2: SystemExit (dilempar argparse) tidak boleh mematikan batch
# ============================================================

def test_main_systemexit_tidak_mematikan_batch(tmp_path, monkeypatch):
    """SystemExit(code=2) di dosen 1 -> GAGAL_SCRAPE, dosen 2 tetap jalan.

    SystemExit adalah BaseException, bukan Exception, jadi tanpa
    penanganan khusus ia lolos dari `except Exception` dan mematikan
    seluruh batch. Kode 2 masuk jalur _PETA_STATUS seperti biasa
    (sama seperti run_pipeline.main() yang mengembalikan 2).
    """
    xlsx = buat_excel(tmp_path / "dosen.xlsx", HEADER_KELOMPOK, [
        [1, "Budi Santoso", "Aktif", "Informatika", 111, "ID_A"],
        [2, "Andi Wijaya", "Aktif", "Teknik Elektro", 333, "ID_B"],
    ])
    progress = tmp_path / "progress.csv"
    panggilan = []
    hitungan = {"n": 0}

    def systemexit_2(argv):
        panggilan.append(list(argv))
        hitungan["n"] += 1
        if hitungan["n"] == 1:
            raise SystemExit(2)
        return 0

    monkeypatch.setattr(run_batch.run_pipeline, "main", systemexit_2)
    monkeypatch.setattr(run_batch, "_statistik_dari_db",
                        lambda ids, url=None: {})

    rc = run_batch.main([
        "--file", xlsx, "--progress", str(progress),
        "--jeda", "0",
    ])

    # Kedua dosen tetap dicoba - batch tidak mati di dosen 1.
    assert _id_dipanggil(panggilan) == ["ID_A", "ID_B"]
    data = run_batch.baca_progress(str(progress))
    # Kode 2 -> _PETA_STATUS -> GAGAL_SCRAPE (bukan OK).
    assert data["ID_A"]["status"] == run_batch.STATUS_GAGAL_SCRAPE
    assert data["ID_A"]["exit_code"] == "2"
    assert data["ID_B"]["status"] == run_batch.STATUS_OK
    # Ada kegagalan -> exit code batch 1.
    assert rc == 1


def test_main_systemexit_tanpa_kode_angka_jadi_gagal(tmp_path, monkeypatch):
    """SystemExit() tanpa kode angka -> GAGAL, batch tetap lanjut."""
    xlsx = buat_excel(tmp_path / "dosen.xlsx", HEADER_KELOMPOK, [
        [1, "Budi Santoso", "Aktif", "Informatika", 111, "ID_A"],
        [2, "Andi Wijaya", "Aktif", "Teknik Elektro", 333, "ID_B"],
    ])
    progress = tmp_path / "progress.csv"
    panggilan = []
    hitungan = {"n": 0}

    def systemexit_polos(argv):
        panggilan.append(list(argv))
        hitungan["n"] += 1
        if hitungan["n"] == 1:
            raise SystemExit()
        return 0

    monkeypatch.setattr(run_batch.run_pipeline, "main", systemexit_polos)
    monkeypatch.setattr(run_batch, "_statistik_dari_db",
                        lambda ids, url=None: {})

    rc = run_batch.main([
        "--file", xlsx, "--progress", str(progress),
        "--jeda", "0",
    ])

    assert _id_dipanggil(panggilan) == ["ID_A", "ID_B"]
    data = run_batch.baca_progress(str(progress))
    assert data["ID_A"]["status"] == run_batch.STATUS_GAGAL
    assert data["ID_A"]["exit_code"] == ""
    assert data["ID_B"]["status"] == run_batch.STATUS_OK
    assert rc == 1


def test_main_systemexit_130_menghentikan_batch(tmp_path, monkeypatch):
    """SystemExit(130) TETAP menghentikan batch (perilaku Ctrl+C).

    Dosen 1 melempar SystemExit(130) -> batch berhenti rapi, dosen
    itu TIDAK dicatat (biar --lanjut mengulangnya), dosen 2 tidak
    pernah dipanggil, dan main() mengembalikan 130.
    """
    xlsx = buat_excel(tmp_path / "dosen.xlsx", HEADER_KELOMPOK, [
        [1, "Budi Santoso", "Aktif", "Informatika", 111, "ID_A"],
        [2, "Andi Wijaya", "Aktif", "Teknik Elektro", 333, "ID_B"],
    ])
    progress = tmp_path / "progress.csv"
    panggilan = []

    def systemexit_130(argv):
        panggilan.append(list(argv))
        raise SystemExit(130)

    monkeypatch.setattr(run_batch.run_pipeline, "main", systemexit_130)
    monkeypatch.setattr(run_batch, "_statistik_dari_db",
                        lambda ids, url=None: {})

    rc = run_batch.main([
        "--file", xlsx, "--progress", str(progress),
        "--jeda", "0",
    ])

    assert _id_dipanggil(panggilan) == ["ID_A"]
    # Dosen yang terputus tidak dicatat.
    data = run_batch.baca_progress(str(progress))
    assert "ID_A" not in data
    assert "ID_B" not in data
    assert rc == 130


# ============================================================
# Bug 3: progress CSV yang di-save Excel (delimiter `;`)
# ============================================================

def test_baca_progress_delimiter_titik_koma(tmp_path):
    """File ber-delimiter `;` terbaca benar; baris kosong diabaikan;
    duplikat memakai baris terakhir."""
    path = tmp_path / "progress.csv"
    path.write_text(
        "scholar_id;nama;status;exit_code;waktu\n"
        "ID_A;Budi;OK;0;2026-01-01T00:00:00\n"
        ";;;;\n"
        "ID_A;Budi;GAGAL_LOAD;1;2026-01-01T00:00:01\n"
        "ID_B;Siti;OK;0;2026-01-01T00:00:02\n",
        encoding="utf-8",
    )

    hasil = run_batch.baca_progress(str(path))

    # Baris ";;;;" tidak boleh masuk hasil.
    assert set(hasil) == {"ID_A", "ID_B"}
    # Duplikat ID_A: yang terakhir menang.
    assert hasil["ID_A"]["status"] == "GAGAL_LOAD"
    assert hasil["ID_A"]["exit_code"] == "1"
    assert hasil["ID_B"]["status"] == "OK"
    assert hasil["ID_B"]["nama"] == "Siti"


def test_baca_progress_koma_normal_regresi(tmp_path):
    """File koma normal tetap terbaca seperti dulu (regresi)."""
    path = tmp_path / "progress.csv"
    path.write_text(
        "scholar_id,nama,status,exit_code,waktu\n"
        "ID_A,Budi,OK,0,2026-01-01T00:00:00\n"
        ",,,,\n"
        "ID_B,Siti,GAGAL_LOAD,1,2026-01-01T00:00:01\n",
        encoding="utf-8",
    )

    hasil = run_batch.baca_progress(str(path))

    assert set(hasil) == {"ID_A", "ID_B"}
    assert hasil["ID_A"]["status"] == "OK"
    assert hasil["ID_B"]["status"] == "GAGAL_LOAD"


def test_baca_progress_mixed_delimiter(tmp_path):
    """Kasus asli: header `;` + baris `;` DAN baris `,` dalam satu file.

    Deteksi delimiter per-file (lihat header saja) gagal di sini:
    header `;` dipilih, baris koma terbaca sebagai satu field utuh,
    scholar_id asli hilang, dan --lanjut diam-diam mengulang dosen
    yang sudah jalan. Pemilihan per baris harus menemukan SEMUA
    scholar_id, termasuk yang berdelimiter koma.
    """
    path = tmp_path / "progress.csv"
    path.write_text(
        "scholar_id;nama;status;exit_code;waktu\n"
        ";;;;\n"
        "et_PgM0AAAAJ;Khodijah Amiroh;OK;0;2026-10-07T03:12:50\n"
        "8kDg_v4AAAAJ,Tri Agus Djoko Kuntjoro,OK,0,2026-10-07T03:40:00\n"
        "VdPx-dgAAAAJ,Dwi Edi Setyawan,OK,0,2026-10-07T03:41:00\n"
        "GHtaGbQAAAAJ,Moch. Iskandar Riansyah,OK,0,2026-10-07T03:42:00\n"
        "et_PgM0AAAAJ;Khodijah Amiroh;GAGAL_LOAD;1;2026-10-07T03:50:00\n",
        encoding="utf-8",
    )

    hasil = run_batch.baca_progress(str(path))

    # Semua scholar_id terbaca - termasuk 3 yang dulu "TIDAK ADA".
    assert set(hasil) == {
        "et_PgM0AAAAJ", "8kDg_v4AAAAJ", "VdPx-dgAAAAJ", "GHtaGbQAAAAJ",
    }
    # Baris koma terbaca dengan nama yang benar.
    assert hasil["8kDg_v4AAAAJ"]["nama"] == "Tri Agus Djoko Kuntjoro"
    assert hasil["8kDg_v4AAAAJ"]["status"] == "OK"
    assert hasil["VdPx-dgAAAAJ"]["nama"] == "Dwi Edi Setyawan"
    assert hasil["GHtaGbQAAAAJ"]["nama"] == "Moch. Iskandar Riansyah"
    # Baris `;;;;` tidak masuk hasil (tidak ada kunci kosong).
    assert "" not in hasil
    # Duplikat et_PgM0AAAAJ: baris terakhir menang.
    assert hasil["et_PgM0AAAAJ"]["status"] == "GAGAL_LOAD"
    assert hasil["et_PgM0AAAAJ"]["exit_code"] == "1"


def test_baca_progress_baris_rusak_diabaikan(tmp_path):
    """Baris yang jumlah kolomnya tidak cocok header -> diabaikan,
    tanpa exception, dan tidak merusak baris lain."""
    path = tmp_path / "progress.csv"
    path.write_text(
        "scholar_id,nama,status,exit_code,waktu\n"
        "ID_A,Budi,OK,0,2026-01-01T00:00:00\n"
        "ID_B,Siti,OK,0,2026-01-01T00:00:00,EXTRA\n"
        "ID_C,Andi,OK,0\n"
        "ID_D,Rina,OK,0,2026-01-01T00:00:00\n",
        encoding="utf-8",
    )

    hasil = run_batch.baca_progress(str(path))

    # ID_B (6 kolom) dan ID_C (4 kolom) diabaikan; ID_A dan ID_D utuh.
    assert set(hasil) == {"ID_A", "ID_D"}
    assert hasil["ID_A"]["nama"] == "Budi"
    assert hasil["ID_D"]["nama"] == "Rina"
