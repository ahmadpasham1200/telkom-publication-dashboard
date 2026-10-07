"""
Isi ulang (backfill) kolom doi dan publisher dari Crossref.

Skrip ini membaca publikasi yang doi-nya masih NULL di
core.publications, menanyakan judulnya ke Crossref REST API, lalu
menulis DOI (dan publisher, kalau Crossref menyediakannya) kembali ke
baris yang sama. Kolom lain - termasuk category - tidak pernah
disentuh: Crossref tidak mengenal taksonomi category milik dashboard
ini, jadi mengisinya dari sana hanya menghasilkan data salah.

Kenapa hanya baris yang doi IS NULL
----------------------------------
Query dengan WHERE doi IS NULL membuat skrip ini idempoten: baris yang
sudah terisi dilewati begitu saja pada run berikutnya, jadi aman
dipanggil ulang kapan saja tanpa risiko menimpa hasil sendiri.

Koreksi judul, dan kenapa skornya 0.90
--------------------------------------
Dua judul dibandingkan dengan difflib.SequenceMatcher, tapi keduanya
dibakukan lebih dulu (lihat _normalisasi_judul): huruf besar/kecil,
aksen (é -> e), tanda baca, dan spasi berlebih dibuang. Tanpa
normalisasi, perbedaan yang sekadar kosmetik menutupi kecocokan
yang benar dan membuat tingkat penolakan terasa jauh lebih tinggi
daripada kenyataannya.

Ambang standarnya 0.90, bukan 0.85. Alasannya: parameter
`query.bibliographic` milik Crossref adalah PENCARIAN relevansi yang
fuzzy, bukan lookup persis. Ia rutin mengembalikan item yang masuk
 akal - terkait topik, tapi paper yang berbeda sama sekali. Di 0.85
DOI asli menempel ke paper yang salah, dan kerusakan seperti itu
nyaris tidak mungkin ketahuan belakangan: DOI-nya valid, tautannya
hidup, hanya saja menunjuk paper yang bukan itu. Karena itu dua
gerbang tambahan ikut dipasang: gerbang tahun (tolak kalau tahun
terbit berbeda lebih dari 1) dan syarat DOI harus benar-benar ada.

Ambang bisa diubah lewat --batas-skor kalau mau bereksperimen.

Gerbang yang dipakai, berurutan
-------------------------------
    1. TIDAK_ADA_DOI     - item Crossref tidak punya field DOI.
    2. TIDAK_ADA_JUDUL   - judul (sisi kita atau sisi Crossref)
                           tidak bisa dibakukan jadi pembanding.
    3. TAHUN_TIDAK_COCOK - tahun terbit beda lebih dari
                           SELISIH_TAHUN_MAKS.
    4. SKOR_RENDAH       - skor kemiripan di bawah --batas-skor.

Yang TIDAK di-retry: HTTP 404 dan daftar `items` kosong. Keduanya
berarti "tidak ada di Crossref", bukan "Crossref sedang sibuk";
mengulangnya cuma membuang kuota rate limit tanpa mengubah hasil.

PERINGATAN: doi dan publisher bisa kembali NULL
-----------------------------------------------
Menjalankan ulang src/pipeline/run_pipeline.py untuk author yang sama
AKAN mengembalikan doi dan publisher ke NULL. Penyebabnya ada di
loader, bukan di skrip ini: _stmt_sisip_publikasi() di
src/loading/postgres.py menimpa seluruh kolom non-kunci dari EXCLUDED,
sedangkan mapper selalu mengirim None untuk kedua kolom itu -
seolah-olah belum pernah diisi. Ini perilaku yang sudah disadari dan
disetujui; loader sengaja tidak diubah. Kalau hasil backfill ini
dibutuhkan permanen, jalankan skrip ini SETELAH pipeline, bukan
sebelumnya.

Cara menjalankan
----------------
    python src/enrichment/backfill_crossref.py --dry-run --maks 10
    python src/enrichment/backfill_crossref.py --maks 20 --jeda 2.0
    python src/enrichment/backfill_crossref.py --batas-skor 0.95
    python src/enrichment/backfill_crossref.py --echo-sql
    python -m src.enrichment.backfill_crossref --maks 5

Alamat email kontak dibaca dari variabel CROSSREF_MAILTO; kalau tidak
diisi, dipakai nilai bawaan dan program mencetak peringatan. Email ini
dipakai dua tempat sekaligus, di parameter `mailto` dan di dalam
User-Agent, karena dua itulah yang diminta Crossref agar request-nya
dianggap "aman" (polite pool) dan tidak dibatasi lajunya.

Butuh paket: requests, sqlalchemy, psycopg, python-dotenv.
"""

import argparse
import difflib
import os
import re
import sys
import time
import unicodedata
from pathlib import Path

# --- Bootstrap import -------------------------------------------------
# Supaya file ini jalan baik sebagai skrip
# (`python src/enrichment/backfill_crossref.py`) maupun sebagai modul
# (`python -m src.enrichment.backfill_crossref`), keduanya dari root
# proyek.
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

import requests  # noqa: E402
import sqlalchemy as sa  # noqa: E402

from src.loading.postgres import (  # noqa: E402
    PUBLICATIONS,
    LoaderError,
    connect,
)


# ============================================================
# KONFIGURASI
# ============================================================

CROSSREF_URL = "https://api.crossref.org/works"

# Nama variabel .env yang dibaca untuk email kontak.
ENV_MAILTO = "CROSSREF_MAILTO"

# Bawaan kalau .env tidak punya CROSSREF_MAILTO.
MAILTO_DEFAULT = "ahmadpasham1200@gmail.com"

# Format yang diminta Crossref: nama tool + versi, lalu kontak di
# dalam kurung.
USER_AGENT_TEMPLATE = "telkom-publication-dashboard/1.0 (mailto:{email})"

# Hanya field yang dipakai (select membuat respons tetap kecil; field
# di sini sudah diverifikasi valid untuk endpoint /works).
SELECT_FIELDS = "DOI,title,publisher,issued"

TIMEOUT_DETIK = 20

# Crossref punya rate limit sendiri, tapi kita tetap menunggu sendiri
# supaya tidak menyalahgunakan kuota yang diberikan polite pool.
JEDA_DEFAULT = 1.5
JEDA_MINIMUM = 1.0

# Kode HTTP yang layak dicoba ulang: rate limit dan sisi server.
STATUS_RETRY = (429, 500, 502, 503, 504)
MAKS_COBA = 3
# Backoff tumbuh: percobaan ke-1 tidur 2 detik, ke-2 5 detik,
# ke-3 10 detik.
BACKOFF_DETIK = (2, 5, 10)

SKOR_MINIMUM = 0.90

# Selisih tahun yang masih dianggap "karya yang sama" (mis. edisi
# cetak vs online boleh beda setahun).
SELISIH_TAHUN_MAKS = 1

# Batas panjang kolom di core.publications (lihat
# sql/ddl/02_create_core_tables.sql). Dipakai untuk memotong nilai
# yang akan melebihi kolom, bukan untuk membiarkan PostgreSQL menolaknya.
PANJANG_DOI = 100
PANJANG_PUBLISHER = 255

# libpq membaca PGCONNECT_TIMEOUT dari environment. Tanpa itu,
# koneksi ke host yang tidak menjawab akan menggantung selamanya
# tanpa pesan - lebih buruk daripada gagal dengan pesan jelas. Tidak
# menimpa nilai yang sudah ada di environment pemanggil.
PGCONNECT_TIMEOUT_DETIK = "5"

# Status hasil satu permintaan ke Crossref.
HASIL_DITEMUKAN = "DITEMUKAN"
HASIL_TIDAK_DITEMUKAN = "TIDAK_DITEMUKAN"
HASIL_GAGAL = "GAGAL_REQUEST"

# Kode alasan penolakan (dicetak per alasan di ringkasan).
ALASAN_SKOR_RENDAH = "SKOR_RENDAH"
ALASAN_TAHUN_TIDAK_COCOK = "TAHUN_TIDAK_COCOK"
ALASAN_TIDAK_ADA_DOI = "TIDAK_ADA_DOI"
ALASAN_TIDAK_ADA_JUDUL = "TIDAK_ADA_JUDUL"

ALASAN_SEMUA = (
    ALASAN_SKOR_RENDAH,
    ALASAN_TAHUN_TIDAK_COCOK,
    ALASAN_TIDAK_ADA_DOI,
    ALASAN_TIDAK_ADA_JUDUL,
)

# Berapa banyak kasus yang ditampilkan sebagai contoh di ringkasan.
JUMLAH_CONTOH = 5


# ============================================================
# BANTUAN UMUM
# ============================================================

def _garis(judul):
    print("\n" + "=" * 60)
    print(judul)
    print("=" * 60)


def _potong(teks, panjang):
    """Batasi panjang teks agar muat di kolom VARCHAR."""
    teks = (teks or "").strip()
    if len(teks) <= panjang:
        return teks
    return teks[:panjang]


def _email_kontak():
    """Ambil email kontak dari .env, atau bawaan kalau tidak ada.

    Tidak pernah gagal: program hanya bergantung pada mailto untuk
    masuk polite pool, dan kehilangan email berarti kehilangan kuota yang
    lebih longgar - bukan kehilangan fungsi.
    """
    dari_env = (os.getenv(ENV_MAILTO) or "").strip()
    if dari_env:
        return dari_env, False
    return MAILTO_DEFAULT, True


# ============================================================
# NORMALISASI DAN SKOR
# ============================================================

def _normalisasi_judul(judul):
    """Ubah judul jadi bentuk baku yang layak dibandingkan.

        NFKD -> buang tanda diakritik -> casefold -> ganti semua
        karakter non-alfanumerik dengan spasi -> rapikan spasi.

    "Le Café" dan "le cafe." jadi dua string yang sama persis.
    Panggil sekali per baris, bukan sekali per perbandingan.
    """
    teks = unicodedata.normalize("NFKD", judul or "")
    teks = "".join(kar for kar in teks if not unicodedata.combining(kar))
    teks = teks.casefold()
    teks = re.sub(r"[^0-9a-z]+", " ", teks)
    return " ".join(teks.split())


def _skor_kecocokan(judul_baku, judul_lawan):
    """Kemiripan 0.0-1.0 antara dua judul yang sudah dibakukan.

    autojunk=False dimatikan supaya heuristik SequenceMatcher yang
    membuang karakter "populer" tidak ikut mengubah hasil untuk judul
    yang panjangnya tidak normal.
    """
    if not judul_baku or not judul_lawan:
        return 0.0
    return difflib.SequenceMatcher(
        None, judul_baku, judul_lawan, autojunk=False
    ).ratio()


def _tahun_crossref(item):
    """Tahun terbit dari Crossref, atau None kalau tidak terbaca.

    Struktur date-parts milik Crossref sering berubah bentuk
    (nested list, None, atau field yang tidak ada), jadi dibaca
    defensif: satu field hilang tidak boleh menggagalkan run.
    """
    try:
        return int(item["issued"]["date-parts"][0][0])
    except (KeyError, IndexError, TypeError, ValueError):
        return None


def _tahun_publikasi(tanggal):
    """Tahun dari kolom publication_date (DATE), atau None."""
    if tanggal is None:
        return None
    tahun = getattr(tanggal, "year", None)
    if tahun:
        return int(tahun)
    try:
        return int(str(tanggal)[:4])
    except (TypeError, ValueError):
        return None


# ============================================================
# KONEKSI KE CROSSREF
# ============================================================

def _header(email):
    return {"User-Agent": USER_AGENT_TEMPLATE.format(email=email)}


def _tidur(detik):
    if detik > 0:
        time.sleep(detik)


def cari_di_crossref(judul, email):
    """Tanya Crossref satu judul; kembalikan dict hasil.

    Parameter
    ---------
    judul : str
        Judul publikasi mentah, apa adanya dari database.
    email : str
        Email kontak, untuk polite pool.

    Kembalikan
    -----------
    dict dengan kunci:
        status : HASIL_DITEMUKAN | HASIL_TIDAK_DITEMUKAN | HASIL_GAGAL
        item   : item Crossref (hanya kalau status DITEMUKAN)
        pesan  : keterangan singkat kalau bukan DITEMUKAN
    """
    params = {
        "query.bibliographic": judul,
        "rows": 1,
        "select": SELECT_FIELDS,
        "mailto": email,
    }

    for percobaan in range(1, MAKS_COBA + 1):
        try:
            respons = requests.get(
                CROSSREF_URL,
                params=params,
                headers=_header(email),
                timeout=TIMEOUT_DETIK,
            )
        except requests.RequestException as exc:
            # Error koneksi (timeout, DNS, TLS) belum tentu berarti
            # Crossref sedang sibuk, jadi masih dicoba.
            if percobaan >= MAKS_COBA:
                return {
                    "status": HASIL_GAGAL,
                    "item": None,
                    "pesan": "koneksi ke Crossref gagal setelah {} "
                             "percobaan: {}".format(MAKS_COBA, exc),
                }
            _tidur(BACKOFF_DETIK[min(percobaan - 1, len(BACKOFF_DETIK) - 1)])
            continue

        if respons.status_code in STATUS_RETRY:
            if percobaan >= MAKS_COBA:
                return {
                    "status": HASIL_GAGAL,
                    "item": None,
                    "pesan": "Crossref membalas HTTP {} setelah {} "
                             "percobaan".format(
                                 respons.status_code, MAKS_COBA),
                }
            _tidur(BACKOFF_DETIK[min(percobaan - 1, len(BACKOFF_DETIK) - 1)])
            continue

        if respons.status_code == 404:
            # Bukan "tidak ada": endpoint-nya sendiri yang salah.
            # Tidak dicoba ulang.
            return {
                "status": HASIL_GAGAL,
                "item": None,
                "pesan": "Crossref membalas HTTP 404 untuk query ini",
            }

        if respons.status_code != 200:
            # 4xx lain (mis. 400 select salah) akan selalu gagal apa
            # pun yang dicoba, jadi langsung laporkan.
            return {
                "status": HASIL_GAGAL,
                "item": None,
                "pesan": "Crossref membalas HTTP {}".format(
                    respons.status_code),
            }

        try:
            payload = respons.json()
        except ValueError as exc:
            return {
                "status": HASIL_GAGAL,
                "item": None,
                "pesan": "Balasan Crossref bukan JSON yang valid: "
                         "{}".format(exc),
            }

        items = ((payload or {}).get("message") or {}).get("items") or []
        if not items:
            return {
                "status": HASIL_TIDAK_DITEMUKAN,
                "item": None,
                "pesan": "Crossref tidak punya hasil untuk judul ini",
            }

        return {"status": HASIL_DITEMUKAN, "item": items[0], "pesan": None}

    # Tidak akan tercapai; jaring pengaman.
    return {
        "status": HASIL_GAGAL,
        "item": None,
        "pesan": "permintaan ke Crossref tidak selesai",
    }


# ============================================================
# PENILAIAN SATU ITEM
# ============================================================

def _nilai_item(item, judul_baku, tahun_awal, batas_skor):
    """Tentukan diterima atau ditolak satu item Crossref.

    Mengembalikan dict yang nanti dipakai untuk UPDATE sekaligus untuk
    ringkasan. Skor selalu dihitung bila judulnya ada - termasuk pada
    kasus yang ditolak - supaya contoh "ditolak tapi skornya tinggi"
    bisa dicetak di ringkasan.
    """
    doi = _potong((item.get("DOI") or "").strip(), PANJANG_DOI) or None
    publisher = _potong(item.get("publisher"), PANJANG_PUBLISHER) or None

    judul_lawan = _normalisasi_judul((item.get("title") or [""])[0])
    if judul_baku and judul_lawan:
        skor = _skor_kecocokan(judul_baku, judul_lawan)
    else:
        skor = None

    tahun_crossref = _tahun_crossref(item)

    # Gerbang 1: tanpa DOI, item itu tidak berguna.
    if not doi:
        return _putuskan(None, doi, publisher, skor, tahun_crossref,
                         ALASAN_TIDAK_ADA_DOI)

    # Gerbang 2: tidak ada yang bisa dibandingkan.
    if not judul_baku or not judul_lawan or skor is None:
        return _putuskan(None, doi, publisher, skor, tahun_crossref,
                         ALASAN_TIDAK_ADA_JUDUL)

    # Gerbang 3: tahun. Hampir gratis, dan menyingkirkan sebagian besar
    # salah match yang lolos secara tematik.
    if (tahun_awal and tahun_crossref
            and abs(tahun_awal - tahun_crossref) > SELISIH_TAHUN_MAKS):
        return _putuskan(None, doi, publisher, skor, tahun_crossref,
                         ALASAN_TAHUN_TIDAK_COCOK)

    # Gerbang 4: skor.
    if skor < batas_skor:
        return _putuskan(None, doi, publisher, skor, tahun_crossref,
                         ALASAN_SKOR_RENDAH)

    return _putuskan(True, doi, publisher, skor, tahun_crossref, None)


def _putuskan(diterima, doi, publisher, skor, tahun_crossref, alasan):
    return {
        "diterima": bool(diterima),
        "doi": doi,
        "publisher": publisher,
        "skor": skor,
        "tahun_crossref": tahun_crossref,
        "alasan": alasan,
    }


# ============================================================
# AKSES DATABASE
# ============================================================

def _sql_pilih(maks=None):
    """Pilih publikasi yang doi-nya masih kosong.

    WHERE doi IS NULL membuat run berulang melewati baris yang sudah
    terisi, jadi skrip ini idempoten.
    """
    stmt = (
        sa.select(
            PUBLICATIONS.c.publications_id,
            PUBLICATIONS.c.title,
            PUBLICATIONS.c.publication_date,
        )
        .where(PUBLICATIONS.c.doi.is_(None))
        .order_by(PUBLICATIONS.c.publications_id)
    )
    if maks is not None and maks > 0:
        stmt = stmt.limit(maks)
    return stmt


def _sql_perbarui(doi, publisher):
    """Bentuk UPDATE untuk satu baris.

    publisher ikut ditulis HANYA kalau Crossref mengembalikannya; kalau
    tidak, kolom itu dibiarkan apa adanya. Menulis NULL di atas nilai
    yang sudah ada adalah kehilangan data, bukan pembaruan.
    """
    nilai = {"doi": doi}
    if publisher:
        nilai["publisher"] = publisher
    return (
        sa.update(PUBLICATIONS)
        .where(PUBLICATIONS.c.publications_id == sa.bindparam("pid"))
        .values(**nilai)
    )


# ============================================================
# CLI
# ============================================================

def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description=(
            "Isi kolom doi (dan publisher) core.publications dari "
            "Crossref untuk baris yang doi-nya masih NULL."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Contoh:\n"
            "  python src/enrichment/backfill_crossref.py --dry-run "
            "--maks 10\n"
            "  python src/enrichment/backfill_crossref.py --maks 20 "
            "--jeda 2.0\n"
            "  python src/enrichment/backfill_crossref.py "
            "--batas-skor 0.95\n"
            "  python src/enrichment/backfill_crossref.py --echo-sql\n"
            "  python -m src.enrichment.backfill_crossref --maks 5\n"
        ),
    )
    p.add_argument("--dry-run", action="store_true",
                   help="Kerjakan semuanya lalu ROLLBACK: tidak ada "
                        "satu pun baris yang berubah di database.")
    p.add_argument("--url", default=None,
                   help="URL koneksi PostgreSQL lengkap, menggantikan "
                        ".env (berguna untuk test).")
    p.add_argument("--echo-sql", action="store_true",
                   help="Cetak SQL yang dijalankan")
    p.add_argument("--jeda", type=float, default=JEDA_DEFAULT,
                   help="Jeda dalam detik antar request Crossref "
                        "(default %.1f, minimal %.1f). Jeda dipakai "
                        "supaya tidak menyalahgunakan rate limit "
                        "Crossref." % (JEDA_DEFAULT, JEDA_MINIMUM))
    p.add_argument("--batas-skor", type=float, default=SKOR_MINIMUM,
                   help="Ambang kemiripan judul 0.0-1.0 untuk diterima "
                        "(default %.2f). Naikkan kalau masih ada DOI "
                        "yang menempel ke paper yang salah." % SKOR_MINIMUM)
    p.add_argument("--maks", type=int, default=None,
                   help="Batasi jumlah baris yang dicek (default: semua "
                        "yang doi-nya NULL). Berguna untuk test di "
                        "sebagian kecil data.")

    args = p.parse_args(argv)

    if args.jeda < JEDA_MINIMUM:
        p.error(
            "--jeda minimal 1 detik ({} detik). Crossref punya rate "
            "limit dan mengirim jeda terlalu cepat bisa membuat run "
            "ikut diblokir.".format(JEDA_MINIMUM)
        )
    if not 0.0 <= args.batas_skor <= 1.0:
        p.error("--batas-skor harus antara 0.0 dan 1.0 (diberi {})."
                .format(args.batas_skor))
    if args.maks is not None and args.maks <= 0:
        p.error("--maks harus angka positif (diberi {}).".format(args.maks))

    return args


# ============================================================
# RINGKASAN
# ============================================================

def _cetak_baris_diterima(catatan):
    print()
    print("  Detail {} row yang diterima (skor, tahun, judul):"
          .format(len(catatan)))
    for c in catatan:
        print("    #{:<6} skor={:.4f} th={} {}".format(
            c["pid"], c["skor"] or 0.0,
            c["tahun_awal"] or "-",
            _potong(c["judul"], 60),
        ))


def _cetak_ringkasan(total, catatan, email, batas_skor, jeda):
    diterima = [c for c in catatan if c["hasil"] == "DITERIMA"]
    ditolak = [c for c in catatan if c["hasil"] == "DITOLAK"]
    tak_ada = [c for c in catatan if c["hasil"] == "TIDAK_DITEMUKAN"]
    gagal = [c for c in catatan if c["hasil"] == "GAGAL_REQUEST"]

    dengan_publisher = [c for c in diterima if c["publisher"]]
    doi_saja = [c for c in diterima if not c["publisher"]]

    hitung_alasan = {}
    for alasan in ALASAN_SEMUA:
        hitung_alasan[alasan] = len(
            [c for c in ditolak if c["alasan"] == alasan])

    print("  email kontak / ambang / jeda            : {} / {} / {} detik"
          .format(email, batas_skor, jeda))
    print("  total publikasi dicek                    : {}".format(total))
    print("  berhasil match (confidence tinggi)      : {}".format(
        len(diterima)))
    print("    - doi + publisher                     : {}".format(
        len(dengan_publisher)))
    print("    - doi saja (publisher tidak diubah)   : {}".format(
        len(doi_saja)))
    print("  dilewati karena confidence rendah       : {}".format(len(ditolak)))
    for alasan in ALASAN_SEMUA:
        print("    - {:<40}: {}".format(alasan, hitung_alasan[alasan]))
    print("  tidak ketemu sama sekali di Crossref    : {}".format(len(tak_ada)))
    print("  gagal request (setelah retry)           : {}".format(len(gagal)))

    if total:
        print("  success rate                            : {:.1f}% ({}/{})"
              .format(100.0 * len(diterima) / total, len(diterima), total))
    else:
        print("  success rate                            : - "
              "(tidak ada baris dicek)")

    if diterima:
        _cetak_baris_diterima(diterima)

        # Kasus diterima dengan skor terendah: kalau angka di sini
        # terlalu dekat dengan batas, ambangnya yang perlu ditinjau.
        terendah = sorted(diterima, key=lambda c: c["skor"] or 0.0)
        print()
        print("  {} match TERIMA dengan skor terendah (naik):"
              .format(min(JUMLAH_CONTOH, len(terendah))))
        for c in terendah[:JUMLAH_CONTOH]:
            print("    #{:<6} skor={:.4f} th={} {}".format(
                c["pid"], c["skor"] or 0.0, c["tahun_awal"] or "-",
                _potong(c["judul"], 60)))

    if ditolak:
        # Kebalikannya: kasus ditolak dengan skor tertinggi. Kalau
        # semuanya jauh di bawah batas, ambangnya memang terlalu
        # tinggi.
        tertinggi = sorted(
            ditolak, key=lambda c: c["skor"] or 0.0, reverse=True)
        print()
        print("  {} kasus DITOLAK dengan skor tertinggi (turun):"
              .format(min(JUMLAH_CONTOH, len(tertinggi))))
        for c in tertinggi[:JUMLAH_CONTOH]:
            print("    #{:<6} skor={:.4f} th={} alasan={} {}".format(
                c["pid"], c["skor"] or 0.0, c["tahun_awal"] or "-",
                c["alasan"], _potong(c["judul"], 50)))


# ============================================================
# UTAMA
# ============================================================

def main(argv=None):
    args = parse_args(argv)

    email, pakai_bawaan = _email_kontak()

    _garis("BACKFILL DOI + PUBLISHER DARI CROSSREF")
    print("  Email kontak     : {}".format(email))
    print("  Ambang skor      : {}".format(args.batas_skor))
    print("  Jeda antar request: {} detik".format(args.jeda))
    print("  Mode             : {}".format(
        "DRY-RUN (akan ROLLBACK)" if args.dry_run else "TULIS (COMMIT)"))

    if pakai_bawaan:
        print()
        print("  Peringatan: {} belum diisi di .env; memakai alamat "
              "bawaan '{}'.".format(ENV_MAILTO, MAILTO_DEFAULT))
        print("  Isi {} di .env kalau ingin alamat kontak Anda sendiri."
              .format(ENV_MAILTO))

    print()
    print("  Peringatan: menjalankan ulang src/pipeline/run_pipeline.py")
    print("  untuk author yang sama akan mengembalikan doi dan publisher")
    print("  ke NULL, karena loader menimpa kolom itu dari EXCLUDED.")
    print("  Jalankan skrip ini SETELAH pipeline, bukan sebelum.")

    # ---------- KONEKSI ----------
    conn = None
    try:
        os.environ.setdefault("PGCONNECT_TIMEOUT", PGCONNECT_TIMEOUT_DETIK)
        conn = connect(url=args.url, echo=args.echo_sql)
    except LoaderError as exc:
        print("\nGAGAL membuka koneksi: {}".format(exc))
        return 1
    except Exception as exc:
        print("\nGAGAL karena kesalahan tak terduga: {}: {}".format(
            type(exc).__name__, exc))
        print("Contoh penyebab: PostgreSQL belum jalan, port salah, "
              "kredensial .env salah, atau tabel belum dibuat (jalankan "
              "sql/ddl/*.sql dulu).")
        return 1

    catatan = []
    try:
        rows = conn.execute(_sql_pilih(args.maks)).fetchall()
        total = len(rows)

        print()
        print("  Baris dengan doi IS NULL : {}".format(total))
        if not total:
            print("  Tidak ada yang perlu diisi. Selesai.")
        else:
            for nomor, (pid, judul, tanggal) in enumerate(rows, start=1):
                tahun_awal = _tahun_publikasi(tanggal)
                judul_baku = _normalisasi_judul(judul)
                catatan_satu = {
                    "pid": pid,
                    "judul": judul or "",
                    "tahun_awal": tahun_awal,
                    "hasil": None,
                    "alasan": None,
                    "skor": None,
                    "doi": None,
                    "publisher": None,
                    "tahun_crossref": None,
                }

                # Judul yang tidak bisa dibakukan (mis. seluruhnya
                # non-Latin) tidak ada yang bisa dicocokkan, jadi
                # tidak ada request yang dikirim ke Crossref.
                if not judul_baku:
                    catatan_satu["hasil"] = "DITOLAK"
                    catatan_satu["alasan"] = ALASAN_TIDAK_ADA_JUDUL
                    catatan.append(catatan_satu)
                    continue

                respons = cari_di_crossref(judul, email)

                # Jeda setelah setiap request, termasuk yang hasilnya
                # "tidak ditemukan" atau "gagal" - semuanya tetap
                # memakai kuota. Tidak perlu tidur setelah yang
                # terakhir.
                if nomor < total:
                    _tidur(args.jeda)

                if respons["status"] == HASIL_TIDAK_DITEMUKAN:
                    catatan_satu["hasil"] = "TIDAK_DITEMUKAN"
                    catatan.append(catatan_satu)
                    continue

                if respons["status"] == HASIL_GAGAL:
                    catatan_satu["hasil"] = "GAGAL_REQUEST"
                    catatan_satu["alasan"] = respons["pesan"]
                    catatan.append(catatan_satu)
                    continue

                nilai = _nilai_item(
                    respons["item"], judul_baku, tahun_awal, args.batas_skor)
                catatan_satu["hasil"] = "DITERIMA" if nilai["diterima"] \
                    else "DITOLAK"
                catatan_satu["alasan"] = nilai["alasan"]
                catatan_satu["skor"] = nilai["skor"]
                catatan_satu["doi"] = nilai["doi"]
                catatan_satu["publisher"] = nilai["publisher"]
                catatan_satu["tahun_crossref"] = nilai["tahun_crossref"]

                if nilai["diterima"]:
                    param = {"pid": pid, "doi": nilai["doi"]}
                    if nilai["publisher"]:
                        param["publisher"] = nilai["publisher"]
                    conn.execute(_sql_perbarui(nilai["doi"],
                                               nilai["publisher"]), param)
                    print("  [{}/{}] #{} doi={} skor={:.4f}".format(
                        nomor, total, pid, nilai["doi"], nilai["skor"]))
                else:
                    print("  [{}/{}] #{} ditolak ({}) skor={}".format(
                        nomor, total, pid, nilai["alasan"],
                        "{:.4f}".format(nilai["skor"] or 0.0)))

                catatan.append(catatan_satu)

    except KeyboardInterrupt:
        print("\nDibatalkan pengguna. Transaksi di-rollback, tidak ada "
              "baris yang berubah.")
        try:
            conn.rollback()
        finally:
            conn.close()
        return 130
    except Exception as exc:
        print("\nGAGAL di tengah jalan: {}: {}".format(
            type(exc).__name__, exc))
        print("Seluruh perubahan run ini dibatalkan (rollback).")
        try:
            conn.rollback()
        finally:
            conn.close()
        return 1

    try:
        # ---------- RINGKASAN ----------
        _garis("RINGKASAN")
        _cetak_ringkasan(total, catatan, email, args.batas_skor, args.jeda)
        print()
        if args.dry_run:
            # Rollback SETELAH ringkasan dicetak: dry-run harus
            # menjalankan seluruh jalur kode yang sama, termasuk
            # penulisan, tanpa meninggalkan jejak apa pun.
            conn.rollback()
            print("  Transaksi: ROLLBACK (--dry-run). Nol baris "
                  "berubah di database.")
        else:
            conn.commit()
            print("  Transaksi: COMMIT. DOI di atas sudah tersimpan.")
    finally:
        conn.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())