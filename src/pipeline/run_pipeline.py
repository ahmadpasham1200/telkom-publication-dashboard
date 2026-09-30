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
        ),
    )
    p.add_argument("--id", dest="uid", default=SCHOLAR_ID_DEFAULT,
                   help="Google Scholar user ID (bagian setelah ?user=). "
                        "Nilai ini juga dipakai sebagai scholar_id saat load.")
    p.add_argument("--headed", action="store_true",
                   help="Tampilkan browser (berguna kalau kena captcha)")
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


def main(argv=None):
    args = parse_args(argv)

    _garis("PIPELINE SCRAPE -> POSTGRESQL")
    print("  Scholar ID : {}".format(args.uid))
    print("  Mode       : {}".format("headed" if args.headed else "headless"))
    print("  Batas batch: {}".format(args.batch))

    # ---------- TAHAP 1: SCRAPE ----------
    _garis("TAHAP 1 - SCRAPE")
    try:
        envelope = asyncio.run(scrape_scholar(
            args.uid,
            headless=not args.headed,
            verbose=not args.diam,
            max_batches=args.batch,
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
        # Hanya SATU koneksi yang dibuka di sini, untuk core.
        # Koneksi raw dibuka sendiri oleh loader (raw_conn=None),
        # jadi keduanya dijamin objek berbeda - syarat wajib agar
        # commit raw tidak ikut ter-rollback bersama core.
        conn_core = connect(url=args.url, echo=args.echo_sql)
        statistik = load_scholar_run(
            envelope,
            scholar_id=args.uid,   # dari argumen pemanggil, bukan envelope
            conn=conn_core,
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
