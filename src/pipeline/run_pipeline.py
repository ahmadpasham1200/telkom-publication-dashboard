"""
Pipeline CLI: scrape Google Scholar -> simpan ke PostgreSQL.

Penghubung antara scraper (src/ingestion/) dan loader
(src/loading/). File ini tidak/logika scraping maupun pemetaan
kolom; ia hanya mengurutkan dua tahap itu dan mencetak ringkasannya.

Alur
----
    1. Ambil --id dari command line.
    2. Panggil scrape_scholar(id) -> envelope.
    3. Buka koneksi PostgreSQL (kredensial dari .env, variabel
       POSTGRES_*).
    4. load_scholar_run(envelope, scholar_id=id, conn=...).

    Loader-lah yang menangani raw-dulu-dan-commit, upsert, dan
    transaksi core. File ini tidak perlu tahu detail itu.

Kenapa run yang GAGAL tetap diteruskan ke loader
-------------------------------------------------
Run fatal (BLOCKED / TIMEOUT / NO_DATA / PARSING_ERROR) tetap
dikirim ke load_scholar_run. Itu disengaja: envelope-nya ditulis
ke raw.scholar_scrape_result dan di-commit sebelum core disentuh,
jadi "profil mana yang gagal dan kenapa" tetap tercatat. Kalau
file ini berhenti di langkah 2 saat scraping gagal, tabel raw
justru tidak akan pernah berisi record kegagalan - persis hal
yang membuatnya berguna. Itu inti dari desain raw.

Cara menjalankan
----------------
    python src/pipeline/run_pipeline.py --id 8kDg_v4AAAAJ
    python -m src.pipeline.run_pipeline --id 8kDg_v4AAAAJ --headed
    python src/pipeline/run_pipeline.py --id 8kDg_v4AAAAJ --tanpa-detail
    python src/pipeline/run_pipeline.py --id 8kDg_v4AAAAJ --load-core

DEFAULT: pipeline BERHENTI setelah envelope tersimpan dan ter-commit
di raw.scholar_scrape_result (RAW-only). Transformasi dan loading ke
staging/core TIDAK dijalankan otomatis; enrichment/backfill juga
tidak. Staging/core hanya diisi kalau --load-core dinyatakan
eksplisit - pilihan sadar, karena core adalah tujuan akhir.

Butuh paket: playwright, beautifulsoup4, sqlalchemy, psycopg,
python-dotenv. Untuk browser: python -m playwright install chromium
"""

import argparse
import asyncio
import sys
from pathlib import Path

# --- Bootstrap import -------------------------------------------------
# Supaya file ini jalan baik sebagai skrip
# (`python src/pipeline/run_pipeline.py`) maupun sebagai modul
# (`python -m src.pipeline.run_pipeline`), keduanya dari root proyek.
_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# Windows default (cp1252) bisa crash saat print karakter non-ASCII
# dari judul publikasi atau pesan error psycopg.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        pass

from src.ingestion.scrape_scholar_playwright import (  # noqa: E402
    MAX_BATCHES,
    SCHOLAR_ID_DEFAULT,
    print_table,
    scrape_scholar,
)
from src.loading.postgres import (  # noqa: E402
    STATUS_LOADED,
    STATUS_RAW_ONLY,
    STATUS_SKIPPED,
    LoaderError,
    connect,
    load_scholar_run,
)

# Status run yang berarti scraping-nya tidak menghasilkan apa pun.
# Ini BUKAN kegagalan pipeline: record-nya tetap disimpan di raw.
STATUS_RUN_GAGAL = {"BLOCKED", "TIMEOUT", "NO_DATA", "PARSING_ERROR"}


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="Scrape satu profil Google Scholar lalu muat ke PostgreSQL.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Contoh:\n"
            "  python src/pipeline/run_pipeline.py --id 8kDg_v4AAAAJ\n"
            "  python -m src.pipeline.run_pipeline --id 8kDg_v4AAAAJ --headed\n"
            "  python src/pipeline/run_pipeline.py --id 8kDg_v4AAAAJ --load-core\n"
            "\n"
            "Default: RAW-ONLY - berhenti setelah simpan ke\n"
            "raw.scholar_scrape_result; transformasi dan loading ke\n"
            "staging/core tidak dijalankan.\n"
            "--load-core: kebalikannya - lanjutkan ke staging/core.\n"
            "\n"
            "Status run:\n"
            "  SUCCESS         scraping selesai tanpa penanda partial.\n"
            "  PARTIAL_SUCCESS ada data tersimpan, tapi proses ditandai\n"
            "                  parsial (mis. batas --batch tercapai,\n"
            "                  sebagian halaman detail gagal). Periksa\n"
            "                  error_message dan failures di envelope RAW.\n"
            "Mencapai batas --batch (batas klik 'Tampilkan lainnya') bisa\n"
            "menghasilkan PARTIAL_SUCCESS walaupun records_failed=0 dan\n"
            "failures=[]. Di mode raw-only, PARTIAL_SUCCESS tetap\n"
            "tersimpan ke raw dan exit code tetap 0.\n"
        ),
    )
    p.add_argument("--id", dest="uid", default=SCHOLAR_ID_DEFAULT,
                   help="Google Scholar user ID (bagian setelah ?user=). "
                        "Nilai ini juga dipakai sebagai scholar_id saat load.")
    p.add_argument("--headed", action="store_true",
                   help="Tampilkan browser (berguna kalau kena captcha)")
    p.add_argument("--tanpa-detail", action="store_true",
                   help="Lewati pengambilan halaman detail per publikasi "
                        "(default: detail diambil; lihat risiko block di "
                        "scrape_scholar_playwright.py)")
    p.add_argument("--batch", type=int, default=MAX_BATCHES,
                   help="Batas klik 'Tampilkan lainnya' (default %d)" % MAX_BATCHES)
    p.add_argument("--tampil", type=int, default=20,
                   help="Jumlah baris yang ditampilkan sebelum load (default 20)")
    p.add_argument("--diam", action="store_true",
                   help="Matikan progress scraper di terminal")
    p.add_argument("--url", default=None,
                   help="URL koneksi PostgreSQL lengkap, menggantikan .env "
                        "(berguna untuk test).")
    p.add_argument("--echo-sql", action="store_true",
                   help="Cetak SQL yang dijalankan")
    p.add_argument("--raw-only", action="store_true", default=True,
                   help="Hanya scrape dan simpan ke tabel raw; TIDAK "
                        "menjalankan transformasi/loading ke staging/core "
                        "(INI DEFAULT)")
    p.add_argument("--load-core", dest="raw_only", action="store_false",
                   help="Kebalikan --raw-only: lanjutkan transformasi dan "
                        "loading ke staging/core (mode lama)")
    p.add_argument("--referensi-dosen",
                   default="data dosen/Data dosen.xlsx",
                   help="File Excel referensi identitas pemilik profil "
                        "(kolom `nama` + `scholar_id`; dibaca dengan "
                        "openpyxl). Bila file tidak ada atau tidak bisa "
                        "dibaca, verifikasi identitas dimatikan otomatis "
                        "(semua penulis -> belum_terverifikasi) dan run "
                        "TETAP berjalan - bukan kegagalan "
                        "(default: %(default)s)")
    return p.parse_args(argv)


def _garis(judul):
    print("\n" + "=" * 60)
    print(judul)
    print("=" * 60)


def _ringkas_run(envelope):
    """Cetak status run hasil scraping, sebelum menyentuh database."""
    data = envelope.get("data") or {}
    print("  Status run        : {}".format(envelope.get("status") or "(kosong)"))
    print("  records_fetched   : {}".format(envelope.get("records_fetched")))
    print("  records_failed    : {}".format(envelope.get("records_failed")))
    print("  run_id            : {}".format(envelope.get("run_id") or "(kosong)"))
    if data:
        print("  Nama profil       : {}".format(data.get("name") or "(tidak ada)"))
        print("  scholar_id envelope: {}".format(data.get("scholar_id") or "(tidak ada)"))
        print("  h-index           : {}".format(data.get("h_index")))
        print("  baris di envelope : {}".format(len(data.get("rows") or [])))
        if envelope.get("status") in STATUS_RUN_GAGAL:
            print("  Pesan error       : {}".format(envelope.get("error_message") or "(tidak ada)"))
    else:
        print("  (tidak ada data; run ini tetap dicatat di tabel raw)")


def _baca_referensi(path):
    """Baca baris referensi identitas dari Excel daftar dosen.

    Mengembalikan (baris, alasan): `baris` = list dict hasil
    baca_daftar() kalau file terbaca, atau None; `alasan` = pesan
    singkat kenapa tidak dipakai ("" kalau berhasil).

    Impor run_batch dilakukan LAZY di dalam fungsi, bukan di atas
    file: run_batch mengimpor run_pipeline di level modul, jadi
    impor timbal-balik di atas file akan memicu siklus import.

    File hilang / tidak terbaca / tanpa kolom `scholar_id` BUKAN
    kegagalan di sini: verifikasi identitas dimatikan otomatis (semua
    penulis -> belum_terverifikasi) dan run tetap lanjut.
    """
    from src.pipeline.run_batch import KolomScholarIdHilang, baca_daftar

    try:
        return baca_daftar(path), ""
    except (OSError, KolomScholarIdHilang) as exc:
        # KolomScholarIdHilang turunan SystemExit (bukan Exception),
        # jadi ia harus ditangkap eksplisit di sini; kalau lolos ia
        # akan mematikan seluruh run lewat sys.exit.
        return None, str(exc)


def main(argv=None):
    args = parse_args(argv)

    # Referensi identitas pemilik profil dibaca SEBELUM header
    # dicetak supaya baris "Referensi identitas" bisa menampilkan
    # jumlah baris / status pemakaian. Gagal membaca bukan fatal -
    # lihat _baca_referensi.
    baris_referensi, alasan_referensi = _baca_referensi(args.referensi_dosen)

    _garis("PIPELINE SCRAPE -> POSTGRESQL")
    print("  Scholar ID : {}".format(args.uid))
    print("  Mode       : {}".format("headed" if args.headed else "headless"))
    print("  Detail     : {}".format(
        "TIDAK diambil (--tanpa-detail)" if args.tanpa_detail else "diambil"))
    print("  Batas batch: {}".format(args.batch))
    print("  Raw-only   : {}".format(
        "YA (default; berhenti setelah raw)" if args.raw_only
        else "tidak (--load-core; isi staging/core)"))
    if baris_referensi is not None:
        print("  Referensi identitas : {} ({} baris)".format(
            args.referensi_dosen, len(baris_referensi)))
    else:
        print("  Referensi identitas : {} (TIDAK DIPAKAI - {})".format(
            args.referensi_dosen, alasan_referensi))
        print("Referensi identitas tidak dipakai: {}. Semua penulis akan "
              "berstatus belum_terverifikasi. Run tetap lanjut."
              .format(alasan_referensi))

    # ---------- TAHAP 1: SCRAPE ----------
    _garis("TAHAP 1 - SCRAPE")
    try:
        envelope = asyncio.run(scrape_scholar(
            args.uid,
            headless=not args.headed,
            verbose=not args.diam,
            max_batches=args.batch,
            ambil_detail=not args.tanpa_detail,
        ))
    except KeyboardInterrupt:
        print("\nDibatalkan pengguna. Tidak ada yang ditulis ke database.")
        return 130
    except Exception as exc:
        # Scraper melempar sebelum envelope terbentuk, jadi tidak ada
        # yang bisa disimpan ke raw. Itu batasnya: tabel raw hanya
        # bisa merekam run yang sudah punya envelope.
        print("\nGAGAL di tahap scrape: {}".format(exc))
        print("Tidak ada envelope, jadi tidak ada yang bisa disimpan ke raw.")
        return 2

    _ringkas_run(envelope)
    rows = (envelope.get("data") or {}).get("rows") or []
    if rows and args.tampil > 0:
        print()
        print_table(rows, limit=args.tampil)

    # ---------- TAHAP 2: LOAD ----------
    _garis("TAHAP 2 - LOAD KE POSTGRESQL")
    conn_core = None
    try:
        # Koneksi core hanya dibuka kalau --load-core (bukan default).
        # Mode raw-only (default) tidak menyentuh staging/core sama
        # sekali, jadi koneksi ini tidak diperlukan (loader membuka
        # koneksi raw sendiri).
        if not args.raw_only:
            conn_core = connect(url=args.url, echo=args.echo_sql)
        statistik = load_scholar_run(
            envelope,
            scholar_id=args.uid,   # dari argumen pemanggil, bukan envelope
            conn=conn_core,
            raw_only=args.raw_only,
            baris_referensi=baris_referensi,  # None -> belum_terverifikasi
        )
    except LoaderError as exc:
        print("\nGAGAL load: {}".format(exc))
        print("Run ini TIDAK masuk core. Envelope mentah aman di raw "
              "(kalau tahap raw sempat berhasil), jadi bisa diperiksa "
              "dan dimuat ulang nanti.")
        return 1
    except Exception as exc:
        print("\nGAGAL karena kesalahan tak terduga: {}: {}".format(
            type(exc).__name__, exc))
        print("Contoh penyebab: PostgreSQL belum jalan, port salah, "
              "kredensial .env salah, atau tabel belum dibuat (jalankan "
              "sql/ddl/*.sql dulu).")
        return 1
    finally:
        if conn_core is not None:
            conn_core.close()

    # ---------- RINGKASAN ----------
    _garis("RINGKASAN")
    print("  Scholar ID         : {}".format(args.uid))
    print("  Status run         : {}".format(envelope.get("status")))
    print("  records_fetched    : {}".format(envelope.get("records_fetched")))
    print("  records_failed     : {}".format(envelope.get("records_failed")))
    print()
    print("  Status load        : {}".format(statistik.get("status")))
    print("  raw_result_id      : {}".format(statistik.get("raw_result_id")))
    print("  run_id             : {}".format(statistik.get("run_id") or "(kosong)"))
    if statistik.get("reason"):
        print("  Alasan             : {}".format(statistik["reason"]))

    # Statistik identitas penulis (dihitung loader dari referensi
    # Excel + verifikasi pemilik profil; disimpan ke
    # raw.scholar_publication_author sejak G2-B). Loader lama tanpa
    # kunci ini tidak dicetak apa-apa (semua .get() -> aman).
    if "author_total" in statistik:
        print()
        print("  AUTHOR (RAW)")
        print("    author_total             : {}".format(
            statistik.get("author_total")))
        print("    author_pemilik_profil    : {}".format(
            statistik.get("author_pemilik_profil")))
        print("    author_bukan_pemilik     : {}".format(
            statistik.get("author_bukan_pemilik")))
        print("    author_belum_terverifikasi: {}".format(
            statistik.get("author_belum_terverifikasi")))
        print("    publikasi_daftar_terpotong: {}".format(
            statistik.get("publikasi_daftar_terpotong")))
        print("    publikasi_tanpa_penulis  : {}".format(
            statistik.get("publikasi_tanpa_penulis")))
        print("    author_disimpan          : {}".format(
            statistik.get("author_disimpan")))
        print("    author_dilewati_konflik  : {}".format(
            statistik.get("author_dilewati_konflik")))
        print("    penyimpanan_author       : {}".format(
            statistik.get("penyimpanan_author")))

    if statistik.get("status") == STATUS_LOADED:
        print()
        print("  Baris masuk core   : YA (transaksi sudah di-commit)")
        print("    author_id        : {}".format(statistik.get("author_id")))
        print("    authors_created  : {}".format(statistik.get("authors_created")))
        print("    authors_reused   : {}".format(statistik.get("authors_reused")))
        print("    publications     : {}".format(statistik.get("publications_inserted")))
        print("    relasi author    : {}".format(
            statistik.get("publication_authors_inserted")))
        print("    metrics          : {}".format(statistik.get("metrics_inserted")))
        print()
        print("  Selesai.")
        return 0

    if statistik.get("status") == STATUS_RAW_ONLY:
        print()
        print("  Baris masuk core   : TIDAK - mode raw-only.")
        print("  Yang tersimpan     : envelope lengkap di "
              "raw.scholar_scrape_result (result_id={}), sudah di-commit."
              .format(statistik.get("raw_result_id")))
        print("  Transformasi/load  : tidak dijalankan (staging/core "
              "tidak disentuh).")
        return 0

    if args.raw_only and statistik.get("status") in STATUS_RUN_GAGAL:
        print()
        print("  Baris masuk core   : TIDAK - mode raw-only.")
        print("  Yang tersimpan     : envelope lengkap di "
              "raw.scholar_scrape_result (result_id={}), sudah di-commit."
              .format(statistik.get("raw_result_id")))
        print("  Scraping           : TIDAK sukses (status={})."
              .format(statistik.get("status")))
        print("  Transformasi/load  : tidak dijalankan (staging/core "
              "tidak disentuh).")
        print("  Exit code          : 2 (GAGAL_SCRAPE) - raw tersimpan "
              "bukan berarti scraping sukses.")
        return 2

    if statistik.get("status") == STATUS_SKIPPED:
        print()
        print("  Baris masuk core   : TIDAK - run fatal tidak punya data.")
        print("  Yang tersimpan     : envelope lengkap di "
              "raw.scholar_scrape_result (result_id={}), sudah di-commit."
              .format(statistik.get("raw_result_id")))
        print("  Arti 'SKIPPED'    : bukan kegagalan pipeline. Scraping "
              "yang bermasalah justru ikut terdokumentasi.")
        return 0

    print()
    print("  Baris masuk core   : TIDAK (status load tidak dikenali: "
          "{!r})".format(statistik.get("status")))
    return 1


if __name__ == "__main__":
    sys.exit(main())
