"""
Isi ulang kolom volume / pages di core.publications dari data raw.

============================================================
PERINGATAN YANG WAJIB DIBACA SEBELUM MENJALANKAN
============================================================

Script ini BAKAL KEHILANGAN work-nya kalau pipeline utama
dijalankan ulang pada author yang sama.

Kenapa: _stmt_sisip_publikasi() di src/loading/postgres.py
(sekitar baris 1108-1112) menulis ULANG setiap kolom non-kunci dari
EXCLUDED saat INSERT ... ON CONFLICT DO UPDATE menang - artinya
"run terakhir menang", applies untuk volume dan pages juga. Padahal
mapper di to_core_rows() selalu mengeluarkan None untuk kedua kolom
itu, karena row["extra"] memang tidak dipecah di sana.

Akibatnya:

    pipeline --isi--> core (volume=NULL, pages=NULL)
    backfill --isi--> core (volume='3',   pages='45-60')   <- kerja kita
    pipeline --lagi--> core (volume=NULL, pages=NULL)      <- HILANG

Jadi urutan yang benar: pipeline dulu sampai selesai, baru script ini.
Kalau pipeline dijalankan lagi setelahnya, cukup jalankan script ini
lagi; tidak ada data yang perlu diperbaiki secara manual.

Keputusan ini sudah disepakati: loader TIDAK diubah. Kalau nanti
maunya selesai, perbaikannya di sisi loader (mis. pakai COALESCE
untuk volume dan pages), bukan di file ini.

============================================================
CARA KERJA
============================================================

    1. Baca envelope dari raw.scholar_scrape_result yang statusnya
       SUCCESS atau PARTIAL_SUCCESS.
    2. Ambil raw_envelope -> data -> rows dari JSONB.
    3. Untuk tiap baris, panggil pecah_extra_volume_pages() pada
       field `extra`.
    4. Kalau berhasil, UPDATE core.publications berdasarkan natural
       key yang sama dengan yang dipakai loader: (title,
       publication_date).

Kenapa natural key-nya harus PERSIS sama dengan loader: kalau
normalisasi judul atau tanggal diulang di sini dengan kode sendiri,
nilainya bisa berbeda sedikit dari yang ditulis loader, UPDATE-nya
lalu tidak mencocokkan baris mana pun, dan data hilang tanpa error.
Karena itu file ini IMPOR helper normalisasi yang sama, bukan
menyalin logikanya (lihat blok import di bawah).

SELURUH run berjalan dalam SATU transaksi: commit di akhir, rollback
kalau ada kesalahan. --dry-run melakukan seluruh bacaan dan parsing
lalu rollback, jadi hasilnya bisa diperiksa tanpa perubahan apa pun.

Cara menjalankan
----------------
    # lihat dulu dampaknya, tanpa menulis apa pun
    python src/enrichment/backfill_volume_pages.py --dry-run

    # uji pada potongan kecil
    python src/enrichment/backfill_volume_pages.py --dry-run --maks 20

    # benar-benar menulis (butuh database sudah ada core-nya)
    python src/enrichment/backfill_volume_pages.py

    # lihat laporan pola yang tidak dikenali
    python src/enrichment/backfill_volume_pages.py --log laporan.txt

Butuh paket: sqlalchemy, psycopg, python-dotenv. Database harus
sudah punya core.* dan raw.* (sql/ddl/*.sql sudah dijalankan).
"""

import argparse
import collections
import sys
from pathlib import Path

# --- Bootstrap import -------------------------------------------------
# Supaya file ini jalan sebagai skrip dari root proyek.
_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# Windows default (cp1252) bisa crash saat print karakter non-ASCII
# dari isi kolom `extra` yang gagal diurai.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        pass

import sqlalchemy as sa  # noqa: E402

from src.loading.postgres import (  # noqa: E402
    PUBLICATIONS,
    RAW_SCRAPE_RESULT,
    LoaderError,
    _mulai_transaksi,
    connect,
)

# Kenapa helper BERAWAL UNDERSCORE di sini ikut diimpor: nama-nama itu
# sengaja private di modul aslinya, tapi menyalin logikanya ke file
# ini justru berisiko. UPDATE di bawah harus menghasilkan nilai kunci
# yang byte-identical dengan yang ditulis loader, jadi judul dan
# tanggal harus melewati _rapikan_teks dan _normalisasi_tahun yang
# sama. Duplikasi di sini berarti satu jalur bisa diam-diam berbeda
# dari jalur lain, dan akibatnya UPDATE tidak cocok tanpa error.
# Satu-satunya konsekuensinya: kalau helper di publications.py
# berubah, file ini ikut berubah - dan itu justru yang benar.
from src.transformation.publications import (  # noqa: E402
    _normalisasi_tahun,
    _rapikan_teks,
    pecah_extra_volume_pages,
)

# Status run yang isinya layak diproses. Run fatal (BLOCKED /
# TIMEOUT / NO_DATA / PARSING_ERROR) tidak punya rows sama sekali,
# jadi membacanya cuma menambah ruang untuk salah baca.
STATUS_DIBACA = ("SUCCESS", "PARTIAL_SUCCESS")

# Alasan penolakan parser (lihat pecah_extra_volume_pages). Keduanya
# sengaja dibedakan dan TIDAK digabung: "EXTRA_KOSONG" berarti Scholar
# tidak punya data pada baris itu, sedangkan "POLA_TIDAK_DIKENAL"
# berarti ada teksnya tapi polanya belum dikenal. Menghitung yang
# pertama sebagai kegagalan akan membuat persentase kegagalan terlihat
# lebih buruk daripada kenyataannya.
ALASAN_EXTRA_KOSONG = "EXTRA_KOSONG"
ALASAN_GAGAL_POLA = "POLA_TIDAK_DIKENAL"

# Jumlah contoh pola gagal yang dicetak ke terminal. Lapisanannya
# lengkap ada di berkas log; samples hanya supaya cepat dilihat mata.
CONTOH_GAGAL = 5

# Nama berkas log default (di direktori kerja sekarang).
LOG_DEFAULT = "laporan_gagal_parse_extra.txt"

# Field per item di dalam raw_envelope -> data -> rows.
FIELD_EXTRA = "extra"
FIELD_JUDUL = "title"
FIELD_TAHUN = "year"


# ============================================================
# PERNYATAAN SQL
# ============================================================

def _stmt_baca_raw(maks=None):
    """SELECT result_id, status, raw_envelope dari tabel raw.

    TIDAK ada kolom `rows` di tabel ini: daftar publikasi hidup di
    dalam JSONB, di raw_envelope -> data -> rows. Karena itu kolom
    yang diambil apa adanya, dan penguraiannya diserahkan ke
    _ambil_rows() yang sengaja defensif terhadap JSON rusak.

    `maks` membatasi jumlah envelope yang diproses - dipakai untuk
    menguji potongan kecil tanpa membaca seluruh histori.
    """
    stmt = (
        sa.select(
            RAW_SCRAPE_RESULT.c.result_id,
            RAW_SCRAPE_RESULT.c.status,
            RAW_SCRAPE_RESULT.c.raw_envelope,
        )
        .where(RAW_SCRAPE_RESULT.c.status.in_(STATUS_DIBACA))
        .order_by(RAW_SCRAPE_RESULT.c.result_id)
    )
    return stmt.limit(maks) if maks else stmt


def _stmt_perbarui_publikasi():
    """UPDATE core.publications SET volume, pages untuk satu natural key.

    Bentuknya setara dengan:

        UPDATE core.publications
           SET volume = :volume, pages = :pages
         WHERE title = :judul
           AND publication_date IS NOT DISTINCT FROM CAST(:tanggal AS date)
        RETURNING publications_id

    Kenapa IS NOT DISTINCT FROM dan BUKAN `=`:
    batasan uniknya adalah UNIQUE NULLS NOT DISTINCT (title,
    publication_date) (lihat sql/ddl/03_create_raw_scrape_result.sql),
    jadi publikasi tanpa tanggal yang sah ikut tercakup: NULL-nya
    harus dicocokkan sebagai NULL. Dengan operator `=` biasa,
    perbandingan dengan NULL menghasilkan NULL - bukan true - sehingga
    setiap publikasi tanpa tanggal akan dilewati diam-diam dan
    dihitung sebagai "tidak cocok".

    Kenapa tanggalnya di-CAST: kolomnya bertipe DATE, sementara
    _normalisasi_tahun() mengembalikan string "YYYY-01-01". Kalau
    `tanggal` None, CAST-nya jadi SQL NULL, dan itu justru yang
    dipakai IS NOT DISTINCT FROM untuk mencocokkan publication_date
    yang NULL.
    """
    return (
        sa.update(PUBLICATIONS)
        .where(
            PUBLICATIONS.c.title == sa.bindparam("judul"),
            PUBLICATIONS.c.publication_date.is_not_distinct_from(
                sa.cast(sa.bindparam("tanggal"), sa.Date)
            ),
        )
        .values(
            volume=sa.bindparam("volume"),
            pages=sa.bindparam("pages"),
        )
        .returning(PUBLICATIONS.c.publications_id)
    )


# ============================================================
# BANTUAN
# ============================================================

def _garis(judul):
    print("\n" + "=" * 60)
    print(judul)
    print("=" * 60)


def _peringatan():
    """Peringatan yang harus muncul sebelum operator menekan commit."""
    print()
    print("  !! PERINGATAN !!")
    print("  Menjalankan run_pipeline.py untuk author yang sama SETELAH")
    print("  script ini akan mengembalikan volume dan pages ke NULL:")
    print("  _stmt_sisip_publikasi() menulis ulang setiap kolom non-kunci")
    print("  dari EXCLUDED, sedangkan mapper selalu mengirim None untuk")
    print("  volume dan pages. Urutan yang benar: pipeline dulu, lalu")
    print("  script ini. Kalau pipeline dijalankan lagi, jalankan juga")
    print("  lagi script ini.")


def _statistik_kosong():
    """Dict penghitung dengan semua kunci terisi nol.

    Yang DIHITUNG bergantian: total_cek + extra_kosong + gagal_pola +
    tidak_cocok + berhasil = total_cek.
    """
    return {
        "total_cek": 0,
        "extra_kosong": 0,
        "berhasil": 0,
        "gagal_pola": 0,
        "tidak_cocok": 0,
    }


def _ambil_rows(envelope):
    """Ambil daftar `rows` dari satu envelope, secara defensif.

    JSONB bisa berisi apa saja tergantung versi scraper, jadi tidak
    ada satu pun langkah di sini yang boleh melempar error:
    envelope bukan dict, `data` hilang, `rows` bukan list - semuanya
    dianggap "tidak ada baris" supaya satu run rusak tidak
    menghentikan pemrosesan run yang lain.
    """
    data = envelope.get("data") if isinstance(envelope, dict) else None
    rows = data.get("rows") if isinstance(data, dict) else None
    return rows if isinstance(rows, list) else []


def _tulis_log(path, gagal):
    """Tulis daftar lengkap `extra` yang gagal diurai ke berkas.

    `gagal` adalah collections.Counter: kuncinya sudah dirapikan
    teksnya (jadi varian spasi tidak terhitung sebagai pola berbeda),
    nilainya adalah berapa kali pola itu muncul. Berkas ditulis mode
    "w", jadi isinya selalu milik run ini - sisa berkas dari run
    sebelumnya tidak mungkin terbaca seolah-olah pola yang masih
    gagal sekarang.
    """
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("# Pola `extra` yang tidak dikenali parser.\n")
        fh.write("# Satu baris per pola unik: <jumlah kemunculan>x <teks>\n")
        fh.write("# Volume/pages untuk baris-baris ini TIDAK diubah.\n")
        if not gagal:
            fh.write("# (tidak ada pola gagal pada run ini)\n")
        for teks, jumlah in sorted(gagal.items(), key=lambda item: (-item[1], item[0])):
            fh.write("{}x {}\n".format(jumlah, teks))
    return path


# ============================================================
# CLI
# ============================================================

def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description=(
            "Isi ulang kolom volume dan pages di core.publications "
            "dengan mengurai kolom `extra` dari raw.scholar_scrape_result."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Contoh:\n"
            "  python src/enrichment/backfill_volume_pages.py --dry-run\n"
            "  python src/enrichment/backfill_volume_pages.py --dry-run --maks 20\n"
            "  python src/enrichment/backfill_volume_pages.py --maks 100\n"
            "  python src/enrichment/backfill_volume_pages.py "
            "--log laporan.txt\n"
            "\n"
            "Ingat: jalankan pipeline sampai selesai DULU, lalu script ini.\n"
            "Menjalankan pipeline lagi akan mengembalikan volume/pages\n"
            "ke NULL - lihat peringatan di ringkasan keluaran.\n"
        ),
    )
    p.add_argument("--dry-run", action="store_true",
                   help="Baca dan urai semuanya, lalu ROLLBACK. "
                        "Tidak ada yang ditulis ke database.")
    p.add_argument("--maks", type=int, default=None,
                   help="Batas jumlah envelope raw yang diproses "
                        "(default: semua). Berguna untuk menguji "
                        "potongan kecil.")
    p.add_argument("--log", default=LOG_DEFAULT,
                   help="Berkas laporan pola `extra` yang gagal "
                        "diurai (default: %s)" % LOG_DEFAULT)
    p.add_argument("--url", default=None,
                   help="URL koneksi PostgreSQL lengkap, menggantikan "
                        ".env (berguna untuk test).")
    p.add_argument("--echo-sql", action="store_true",
                   help="Cetak SQL yang dijalankan")
    return p.parse_args(argv)


# ============================================================
# UTAMA
# ============================================================

def main(argv=None):
    args = parse_args(argv)

    _garis("BACKFILL VOLUME / PAGES DARI RAW")
    print("  Mode       : {}".format("dry-run (rollback)" if args.dry_run
                                     else "TULIS (commit)"))
    print("  Status raw : {}".format(", ".join(STATUS_DIBACA)))
    print("  Batas run  : {}".format(args.maks if args.maks else "semua"))
    print("  Berkas log : {}".format(args.log))
    _peringatan()

    statistik = _statistik_kosong()
    gagal = collections.Counter()
    conn = None
    trans = None

    try:
        conn = connect(url=args.url, echo=args.echo_sql)
        # BEGIN di luar try berikutnya dengan sengaja: kalau gagal,
        # tidak ada transaksi yang perlu di-rollback dan pesan
        # errornya harus naik apa adanya.
        try:
            trans = _mulai_transaksi(conn, untuk="backfill_volume_pages()")
        except Exception:
            conn.close()
            raise

        stmt_perbarui = _stmt_perbarui_publikasi()
        for hasil in conn.execute(_stmt_baca_raw(args.maks)):
            rows = _ambil_rows(hasil.raw_envelope)
            if not rows:
                continue
            for item in rows:
                # Abaikan item yang bukan dict atau judulnya kosong:
                # tanpa judul tidak ada natural key untuk dicocokkan,
                # jadi tidak ada yang bisa ditulis.
                if not isinstance(item, dict):
                    continue
                judul = _rapikan_teks(item.get(FIELD_JUDUL))
                if judul is None:
                    continue

                statistik["total_cek"] += 1
                tanggal = _normalisasi_tahun(item.get(FIELD_TAHUN))

                urai = pecah_extra_volume_pages(item.get(FIELD_EXTRA))
                if urai.alasan == ALASAN_EXTRA_KOSONG:
                    # Bukan kegagalan: Scholar memang tidak memberi
                    # volume/halaman untuk baris ini.
                    statistik["extra_kosong"] += 1
                    continue
                if urai.alasan == ALASAN_GAGAL_POLA:
                    statistik["gagal_pola"] += 1
                    # Kuncinya teks yang sudah dirapikan supaya varian
                    # spasi tidak tercatat sebagai pola berbeda; teks
                    # mentahnya tetap ada di raw untuk penelusuran.
                    gagal[_rapikan_teks(item.get(FIELD_EXTRA)) or ""] += 1
                    continue

                publications_id = conn.execute(
                    stmt_perbarui,
                    {
                        "judul": judul,
                        "tanggal": tanggal,
                        "volume": urai.volume,
                        "pages": urai.pages,
                    },
                ).scalar_one_or_none()
                if publications_id is None:
                    # Parse-nya benar, tapi tidak ada baris core dengan
                    # natural key itu. Artinya kunci kita BERBEDA dari
                    # yang ditulis loader (normalisasi judul/tahun yang
                    # bergeser) - atau barisnya memang belum pernah
                    # masuk core. Dicatat terpisah karena ini
                    # kehilangan data tanpa error sama sekali.
                    statistik["tidak_cocok"] += 1
                    continue
                statistik["berhasil"] += 1

        if args.dry_run:
            trans.rollback()
        else:
            trans.commit()
    except KeyboardInterrupt:
        if trans is not None and trans.is_active:
            trans.rollback()
        print("\nDibatalkan pengguna. Transaksi dibatalkan, tidak ada "
              "yang ditulis ke database.")
        return 130
    except LoaderError as exc:
        if trans is not None and trans.is_active:
            trans.rollback()
        print("\nGAGAL: {}".format(exc))
        return 1
    except Exception as exc:
        if trans is not None and trans.is_active:
            trans.rollback()
        print("\nGAGAL karena kesalahan tak terduga: {}: {}".format(
            type(exc).__name__, exc))
        print("Contoh penyebab: PostgreSQL belum jalan, port salah, "
              "kredensial .env salah, atau tabel belum dibuat (jalankan "
              "sql/ddl/*.sql dulu).")
        return 1
    finally:
        if conn is not None:
            try:
                if trans is not None and trans.is_active:
                    trans.rollback()
            finally:
                conn.close()

    # ---------- RINGKASAN ----------
    _garis("RINGKASAN")
    print("  Total diperiksa      : {}".format(statistik["total_cek"]))
    print("  Berhasil di-update   : {}".format(statistik["berhasil"]))
    print("  Extra kosong         : {}  (bukan kegagalan)".format(
        statistik["extra_kosong"]))
    print("  Pola tidak dikenal   : {}".format(statistik["gagal_pola"]))
    print("  Tidak cocok di core  : {}".format(statistik["tidak_cocok"]))
    print()
    print("  Transaksi            : {}".format(
        "ROLLBACK (dry-run, tidak ada yang ditulis)"
        if args.dry_run else "COMMIT (perubahan sudah tersimpan)"))

    if statistik["tidak_cocok"]:
        print()
        print("  !! PERINGATAN KERAS !!")
        print("  {} baris berhasil diurai tapi TIDAK ADA di core dengan".format(
            statistik["tidak_cocok"]))
        print("  natural key (title, publication_date) yang sama. Data itu")
        print("  hilang tanpa error. Penyebab paling mungkin: normalisasi")
        print("  judul atau tahun berbeda dari yang ditulis loader, atau")
        print("  barisnya belum pernah masuk core. Cek dengan:")
        print("    SELECT title, publication_date FROM core.publications")
        print("    WHERE title LIKE '%<potongan judul>%';")

    if statistik["gagal_pola"]:
        print()
        print("  Contoh pola yang gagal ({} pertama dari {} pola unik):"
              .format(min(CONTOH_GAGAL, len(gagal)), len(gagal)))
        for teks in sorted(gagal)[:CONTOH_GAGAL]:
            print("    - {!r}  ({}x)".format(teks, gagal[teks]))
    else:
        print()
        print("  Tidak ada pola `extra` yang gagal diurai.")

    try:
        _tulis_log(args.log, gagal)
        print()
        print("  Laporan pola gagal   : {}".format(args.log))
    except OSError as exc:
        print()
        print("  GAGAL menulis berkas log {}: {}".format(args.log, exc))

    if args.dry_run:
        print()
        print("  Ini dry-run: tidak ada perubahan di database.")
    else:
        print()
        print("  Selesai. Jangan jalankan pipeline untuk author yang sama")
        print("  tanpa menjalankan ulang script ini.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
