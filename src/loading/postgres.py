"""
============================================================
Loading data core ke PostgreSQL (SQLAlchemy + psycopg)
============================================================

File ini mengisi tabel-tabel `raw` dan `core` dari run envelope
hasil scraper Google Scholar (lihat
src/ingestion/scrape_scholar_playwright.py):
    - raw.scholar_scrape_result  (sql/ddl/03_create_raw_scrape_result.sql)
    - core.*                    (sql/ddl/02_create_core_tables.sql)

Batas tanggung jawab file ini
-----------------------------
    1. Menyimpan envelope hasil scrape apa adanya ke
       raw.scholar_scrape_result, dalam TRANSAKSI SENDIRI yang
       di-commit lebih dulu.
    2. Menyusun perintah INSERT/UPSERT dari nilai yang sudah
       dipetakan oleh mapper (src/transformation/publications.py).
    3. Menyelesaikan nama penulis dari Google Scholar menjadi
       core.authors.author_id.
    4. Membungkus seluruh load core dalam SATU transaksi.

Yang BUKAN tanggung jawab file ini:
    - Logika scraping, validasi, dan pemetaan nama kolom. Itu
      milik file masing-masing; loader hanya diberi nilai dan
      tidak menyusun ulang daftar kolom.
    - Star schema / lapisan laporan (mart). Belum ada tabelnya.
    - Pembuatan tabel. DDL milik sql/ddl/ dan dibekukan.

Bentuk pemanggilan
------------------
    from src.loading.postgres import load_scholar_run

    # Semua koneksi dibuat & ditutup oleh file ini
    load_scholar_run(envelope, scholar_id="8kDg_v4AAAAJ")

    # Dua koneksi DIPISAH, masing-masing milik sendiri
    load_scholar_run(envelope, scholar_id="8kDg_v4AAAAJ",
                     conn=koneksi_core, raw_conn=koneksi_raw)

  `scholar_id` ADALAH PARAMETER WAJIB
  ---------------------------------
  Nilai diambil dari identitas yang DIPAKAI PEMANGGIL untuk
  menjalankan scraping (mis. argumen `--id` di CLI, atau author_id
  dari daftar target di pipeline), BUKAN dari
  envelope["data"]["scholar_id"].

  Alasan: bentuk envelope dikunci 9 kunci dan scholar_id hanya ada
  di DALAM `data`. Run fatal punya data=None, jadi uid profil yang
  sedang   di-scrape hilang dari envelope sepenuhnya. Kalau loader
  menebak-nebak dari isi envelope, kolom raw.scholar_id yang NOT
  NULL akan terisi nilai yang tidak tentu benar - atau, lebih
  buruk, terisi placeholder yang tersamar sebagai ID asli.
  Pemanggil sudah tahu uid-nya; memintanya memberi tahu jauh
  lebih jujur daripada menebak. Melewatkan `scholar_id` adalah
  error pemrograman dan ditolak, bukan diisi default.

  PERINGATAN KERAS SOAL DUA KONEKSI
  --------------------------------
  `conn` (core) dan `raw_conn` (raw) HARUS dua objek koneksi
  BERBEDA. Kalau keduanya koneksi yang sama, penulisan raw ikut
  di-rollback bersama core saat core gagal, dan seluruh alasan
  keberadaan tabel raw hilang: data yang "aman" ternyata hilang
  persis di saat paling dibutuhkan. Kalau `load_scholar_run()`
  diberi satu objek untuk keduanya, modul ini melempar
  LoaderError; kalau keduanya None, file ini membuka sendiri dua
  koneksi terpisah.

TIGA KEBIJAKAN PENULISAN (ringkasan; penjelasan lengkap ada di
bagian-bagian yang ditandai di bawah)
---------------------------------------------------------
    1. RAW DULU, DAN BERDAURABOI SENDIRI
       Setiap run - sukses maupun fatal - ditulis ke
       raw.scholar_scrape_result lebih dulu, dalam transaksi
       sendiri yang langsung di-commit. Kegagalan di tahap raw
       MENGHENTIKAN pemuatan core: memuat core tanpa jaring
       pengaman berarti mengisi core dengan data yang tidak bisa
       diproses ulang dari raw. Sebaliknya, kegagalan di tahap
       core TIDAK menyentuh baris raw - itu justru tujuan
       seluruh tabel raw.

    2. UPSERT untuk authors + publications
       core.publications di-upsert pada natural key
       (title, publication_date) dan core.authors di-upsert
       pada lower(btrim(name)), keduanya dengan RETURNING id
       yang sudah ada, jadi publications_id / author_id TIDAK
       berubah dan relasi di tabel lain tidak rusak. Kolom
       non-kunci ditulis ulang apa adanya (run terakhir menang),
       KECUALI authors.scholar_id yang dijaga: NULL baru tidak
       boleh menimpa scholar_id yang sudah diketahui.

    3. METRICS APPEND-ONLY
       core.author_metrics dan core.publication_metrics SELALU
       INSERT baru, tidak pernah di-upsert. Yang membedakan satu
       snapshot dari snapshot berikutnya adalah retrieved_at,
       dan itulah yang membuat kedua tabel itu berguna. Kalau
       keduanya di-upsert, jejaknya hilang.

Kebijakan yang disepakati (lihat bagian RESOLUSI PENULIS)
---------------------------------------------------------
    1. Nama penulis yang tidak ada di core.authors akan
       otomatis dibuat dengan HANYA kolom `name`. Kolom
       sinta_id / scopus_id / program_study / faculty
       dibiarkan NULL dan TIDAK ada kolom baru yang
       ditambahkan ke DDL. Nama mentah aslinya sudah
       tersimpan di lapisan raw.
    2. Pencocokan nama: PERSIS, tidak membedakan huruf
       besar/kecil (case-insensitive). Risiko dua orang berbeda
       dengan nama sama dianggap satu author diabaikan untuk
       sekarang; pencocokan dengan daftar faculty resmi
       ditunda ke tahap berikutnya.
    3. Satu author yang sama dipakai ulang di semua publikasi
       dalam satu run DAN di run-run berikutnya: cache
       nama -> author_id diisi satu kali dari tabel yang ada,
       lalu diperbarui setiap kali ada INSERT baru.

Yang SENGAJA tidak dikerjakan
-----------------------------
    - Tidak ada penguncian tabel dan tidak ada transaksi yang
      melingkupi beberapa run. Dua writer yang benar-benar
      bersamaan masih bisa saling menimpa kolom non-kunci lewat
      UPSERT. Yang dijamin batasan UNIQUE + ON CONFLICT: tidak
      akan ada baris kembar, hanya "run terakhir menang".
    - authors.scholar_id tidak pernah ditimpa dengan NULL
      (lihat kebijakan UPSERT di atas) dan kolom institutional
      lain (sinta_id / scopus_id / program_study / faculty)
      tidak pernah diisi di sini sama sekali.
    - publication_authors tidak pernah di-UPDATE, hanya
      di-INSERT dengan toleransi duplikat (ON CONFLICT DO
      NOTHING). Satu load berarti satu set baris baru.
    - Tidak ada pemrosesan ulang (replay) dari raw ke core.
      Tabel raw ditulis supaya data tidak hilang, belum
      supaya ada perintah untuk memuat ulang.
"""

import json
import os
import uuid
import warnings
from datetime import date, datetime, timezone
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg
from sqlalchemy.engine import URL

# Mapper memakai nama kolom core sebagai kunci dict, jadi modul ini
# butuh fungsi itu. Import dicoba dengan dua gaya supaya file bisa
# dipakai baik dari root proyek (gaya resmi di dokumen mapper) maupun
# saat folder `src` langsung ada di sys.path.
try:
    from src.transformation.publications import to_core_rows
except ImportError:  # pragma: no cover - jalur alternatif
    from transformation.publications import to_core_rows


__all__ = [
    "LoaderError",
    "AuthorResolver",
    "PostgresAuthorStore",
    "AUTHORS",
    "PUBLICATIONS",
    "PUBLICATION_AUTHORS",
    "AUTHOR_METRICS",
    "PUBLICATION_METRICS",
    "RAW_SCRAPE_RESULT",
    "build_engine",
    "connect",
    "write_raw_scrape_result",
    "load_core_rows",
    "load_scholar_run",
]


# ============================================================
# KONSTANTA
# ============================================================

# Nama variabel di .env (lihat .env.example).
ENV_HOST = "POSTGRES_HOST"
ENV_PORT = "POSTGRES_PORT"
ENV_DB = "POSTGRES_DB"
ENV_USER = "POSTGRES_USER"
ENV_PASSWORD = "POSTGRES_PASSWORD"

# Driver psycopg dipakai lewat dialect SQLAlchemy, bukan lewat
# `psycopg2`. Sengaja ditulis eksplisit supaya tidak diam-diam
# memakai driver lain yang kebetulan terpasang.
DRIVER = "postgresql+psycopg"

DEFAULT_HOST = "localhost"
DEFAULT_PORT = 5432

# Role yang seharusnya dipakai loader ini. Sesuai TODO awal file:
# hanya INSERT (dan SELECT), bukan superuser.
ROLE_ETL = "etl_writer"
SUPERUSER = "postgres"

# Nilai status pada dict statistik.
STATUS_LOADED = "LOADED"
STATUS_SKIPPED = "SKIPPED_NO_DATA"


# ============================================================
# DEFINISI TABEL
# ============================================================
#
# Salinan DDL sql/ddl/02_create_core_tables.sql dalam bentuk objek
# SQLAlchemy. File DDL itu dibekukan, jadi tabel di bawah hanya
# cermin, bukan sumber kebenaran: kalau DDL berubah,
# blok ini WAJIB ikut diubah. Baris yang nilainya boleh NULL
# sengaja TIDAK ikut ditulis di INSERT (lihat daftar kolom di
# bagian PEMBANGUN PERINTAH).
#
# Tabel dideklarasikan sebagai objek modul supaya modul ini tetap
# bisa diimpor dan statement-nya bisa dikompilasi tanpa database
# sama sekali.

_META = sa.MetaData()

AUTHORS = sa.Table(
    "authors",
    _META,
    sa.Column("author_id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("name", sa.String(255), nullable=False),
    sa.Column("sinta_id", sa.String(50)),
    sa.Column("scholar_id", sa.String(50)),
    sa.Column("scopus_id", sa.String(50)),
    sa.Column("program_study", sa.String(150)),
    sa.Column("faculty", sa.String(150)),
    schema="core",
)

PUBLICATIONS = sa.Table(
    "publications",
    _META,
    sa.Column("publications_id", sa.Integer, primary_key=True,
              autoincrement=True),
    sa.Column("title", sa.Text, nullable=False),
    sa.Column("publication_date", sa.Date),
    sa.Column("journal", sa.String(255)),
    sa.Column("volume", sa.String(50)),
    sa.Column("pages", sa.String(50)),
    sa.Column("publisher", sa.String(255)),
    sa.Column("description", sa.Text),
    sa.Column("category", sa.String(100)),
    sa.Column("doi", sa.String(100)),
    schema="core",
)

PUBLICATION_AUTHORS = sa.Table(
    "publication_authors",
    _META,
    sa.Column("publication_id", sa.Integer, primary_key=True, nullable=False),
    sa.Column("author_id", sa.Integer, primary_key=True, nullable=False),
    sa.Column("author_order", sa.Integer, primary_key=True, nullable=False),
    schema="core",
)

AUTHOR_METRICS = sa.Table(
    "author_metrics",
    _META,
    sa.Column("metric_id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("author_id", sa.Integer, nullable=False),
    sa.Column("source", sa.String(50)),
    sa.Column("h_index", sa.Integer),
    sa.Column("score", sa.Numeric),
    sa.Column("index_name", sa.String(100)),
    sa.Column("retrieved_at", sa.DateTime),
    schema="core",
)

PUBLICATION_METRICS = sa.Table(
    "publication_metrics",
    _META,
    sa.Column("metric_id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("publication_id", sa.Integer, nullable=False),
    sa.Column("source", sa.String(50)),
    sa.Column("citation_count", sa.Integer),
    sa.Column("index_name", sa.String(100)),
    sa.Column("score", sa.Numeric),
    sa.Column("retrieved_at", sa.DateTime),
    schema="core",
)

# ------------------------------------------------------------
# TABEL RAW (sql/ddl/03_create_raw_scrape_result.sql)
# ------------------------------------------------------------
# Cermin DDL yang sama seperti blok di atas, untuk schema `raw`.
# Berbeda dengan core, TIDAK ada kolom yang boleh kosong lalu
# dilewati: file ini menulis semua kolom tabel raw.
#
# raw_envelope memakai JSONB dengan serializer yang eksplisit
# supaya yang tersimpan benar-benar envelope apa adanya, dan
# perilakunya tidak ikut berubah karena ada yang meng-set
# json_serializer default di tempat lain. ensure_ascii=False
# supaya nama dan teks non-ASCII tetap terbaca apa adanya di
# database; default=str adalah jaring pengaman kalau ada nilai
# yang tidak serializable, supaya satu nilai aneh tidak
# membuat seluruh data mentah hilang.
#
# run_id memakai sa.Uuid(as_uuid=True), bukan String, supaya
# driver mengirim objek uuid.UUID sungguhan ke kolom UUID
# PostgreSQL.


def _serialisasi_envelope(obj):
    """Serialisasi objek Python menjadi teks JSON untuk kolom JSONB.

    Dipasang sebagai serializer pada tipe kolom raw_envelope
    (lihat kelas _EnvelopeJSONB). ensure_ascii=False supaya huruf
    non-ASCII tidak jadi escape \\uXXXX; untuk JSONB sendiri itu
    tidak mengubah isi yang tersimpan, tapi tetap membuat dump
    mentah enak dibaca saat menelusuri data scholar. default=str
    adalah jaring pengaman: kalau ada nilai yang tidak
    serializable, nilai itu ditulis dengan str() daripada
    menggagalkan seluruh penulisan raw. Untuk data yang hilang
    karena satu nilai aneh jauh lebih buruk daripada satu nilai
    yang tertulis sebagai teks repr-nya.
    """
    return json.dumps(obj, ensure_ascii=False, default=str)


class _EnvelopeJSONB(pg.JSONB):
    """JSONB yang serializer-nya dikunci ke _serialisasi_envelope.

    Kenapa subclass dan bukan pg.JSONB biasa: sejak SQLAlchemy 2.0,
    serializer JSONB TIDAK bisa dipasang per-tipe lewat
    json_serializer=...; ia diambil dari dialect
    (create_engine(json_serializer=...)). Kalau hanya mengandalkan
    default dialect, penyerialisasannya bisa berubah diam-diam
    oleh whoever yang membuat engine-nya, dan penulisan raw
    ikut berubah bersama - padahal di sinilah data yang tidak
    boleh hilang disimpan. Override ini mengunci perilaku di
    dalam file ini, termasuk saat pemanggil menyuntikkan
    koneksinya sendiri.

    CATATAN VERSI: _str_impl dan _make_bind_processor adalah API
    privat SQLAlchemy. Method ini menyalin persis
    sqlalchemy.types.JSON.bind_processor versi 2.0.x, hanya
    menyreplace json_serializer-nya. Kalau proyek naik ke
    SQLAlchemy major berikutnya, periksa ulang dua nama ini
    SEBELUM menukar file ini ke server sungguhan.
    """

    def bind_processor(self, dialect):
        string_process = self._str_impl.bind_processor(dialect)
        return self._make_bind_processor(string_process, _serialisasi_envelope)


RAW_SCRAPE_RESULT = sa.Table(
    "scholar_scrape_result",
    _META,
    sa.Column("result_id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("scholar_id", sa.String(50), nullable=False),
    sa.Column("scraped_at", sa.DateTime, nullable=False),
    sa.Column("run_id", sa.Uuid(as_uuid=True), nullable=True),
    sa.Column("status", sa.String(20)),
    sa.Column("raw_envelope", _EnvelopeJSONB(), nullable=False),
    schema="raw",
)


# ============================================================
# EXCEPTION
# ============================================================

class LoaderError(RuntimeError):
    """Kegagalan yang harus terlihat jelas oleh operator.

    Dipakai untuk dua hal:
      - integrity guard pada kolom NOT NULL (nama pemilik profil,
        judul publikasi);
      - konfigurasi yang tidak lengkap (variabel .env hilang,
        driver psycopg belum ter-install, koneksi sedang punya
        transaksi terbuka).

    Error dari database itu sendiri (mis. pelanggaran FK) sengaja
    TIDAK dibungkus: pesan aslinya lebih berguna kalau dibiarkan
    naik apa adanya. Yang dijamin file ini: transaksi tetap di
    rollback sebelum error apa pun keluar.
    """


# ============================================================
# HELPER: NORMALISASI NAMA
# ============================================================

def _normalisasi_nama(nilai):
    """Rapatkan nama author: rapikan spasi berlebih, potong tepi.

    Hasil scrape Scholar kadang berisi newline, tab, atau spasi
    ganda ("Budi  Santoso\\n"). Semua itu dirapatkan supaya dua
    penulis yang sebenarnya sama tidak terbaca sebagai dua nama
    berbeda. String kosong (atau yang isinya cuma spasi)
    dinormalkan jadi None.
    """
    if nilai is None:
        return None
    teks = " ".join(str(nilai).split())
    return teks or None


def _kunci_nama(nama):
    """Kunci lookup nama di cache: huruf kecil semua (casefold).

    casefold (bukan lower) dipilih karena nama bisa memuat huruf
    non-ASCII, dan casefold menormalkan keduanya dengan lebih
    baik. Ini TIDAK membuat pencocokan jadi lebih longgar
    daripada kebijakan: tetap persis per karakter, hanya
    perbedaan huruf besar/kecil yang diabaikan.
    """
    bersih = _normalisasi_nama(nama)
    if bersih is None:
        return None
    return bersih.casefold()


def _bentuk_pendek(pendek, lengkap):
    """True kalau `pendek` adalah bentuk inisial dari nama `lengkap`.

    Google Scholar menulis pemilik profil dengan NAMA PENDEK di
    kolom authors tiap baris publikasi ("TAD Kuntjoro"), sementara
    baris pemilik sendiri dibuat dari NAMA LENGKAP ("Tri Agus Djoko
    Kuntjoro"). Pencocokan eksak resolver tidak pernah memertemukan
    keduanya - hasilnya author tiruan tercipta dan baris pemilik
    kehilangan seluruh relasinya. Fungsi ini dipakai PEMANGGIL
    (loop publication_authors di load_core_rows) untuk menerjemahkan
    bentuk pendek ke nama lengkap SEBELUM resolve(); resolver,
    cache-nya, dan mapper tidak diubah sama sekali (kontraknya ada
    di transformation/publications.py: kebijakan pencocokan author
    milik pemanggil).

    Syaratnya ketat - SEMUA harus terpenuhi:

      1. Kedua nama tidak None dan tidak kosong.
      2. Kata terakhir `pendek` sama (casefold) dengan kata terakhir
         `lengkap` (= "belakang"). Tanpa ini, inisial apa pun bisa
         menempel ke orang lain: "TAD Kuntjoro" vs "Abduh Sayid
         Albana" ditolak di sini.
      3. Sisa kata `pendek` sebelum belakang, digabung tanpa spasi
         dan di-casefold, sama dengan inisial (huruf pertama tiap
         kata) kata `lengkap` sebelum belakang - ATAU sama dengan
         inisial SELURUH kata `lengkap`. Bentuk kedua menangkap data
         nyata seperti "MIR Riansyah" untuk "Moch. Iskandar
         Riansyah" (inisial M+I+R). Kata depan tanpa inisial sama
         sekali ditolak: "Kuntjoro" vs "Tri Agus Djoko Kuntjoro".
      4. `pendek` casefold-nya BERBEDA dari `lengkap`. Nama lengkap
         tidak membutuhkan aturan ini; resolve biasa sudah
         menemukannya lewat cache.
      5. `lengkap` terdiri dari minimal dua kata, jadi ada minimal
         satu inisial. Satu kata tidak bisa dibedakan antara nama
         pendek dan nama lengkap.

    Yang menentukan benar-salahnya HANYA nama pemilik profil run
    ini (argumen `lengkap`), bukan author lain. Itu yang mencegah
    co-author berbeda orang tapi sama belakang ikut tertaut.

    Mengembalikan True/False; tidak pernah melempar untuk input
    yang buruk (None, kosong, whitespace) - semuanya cukup False.
    """
    panjang = _normalisasi_nama(lengkap)
    pendek_bersih = _normalisasi_nama(pendek)
    if not panjang or not pendek_bersih:
        return False

    kata_panjang = panjang.split()
    kata_pendek = pendek_bersih.split()

    # Syarat 5: minimal satu inisial di sebelum belakang.
    if len(kata_panjang) < 2:
        return False
    # Syarat 4: nama yang sama tidak perlu penerjemahan.
    if panjang.casefold() == pendek_bersih.casefold():
        return False
    # Syarat 2: belakangnya harus sama.
    if kata_panjang[-1].casefold() != kata_pendek[-1].casefold():
        return False

    # Syarat 3: awalan pendek == inisial nama lengkap.
    awalan = "".join(kata_pendek[:-1]).casefold()
    if not awalan:
        return False
    inisial = "".join(kata[0] for kata in kata_panjang[:-1]).casefold()
    inisial_penuh = inisial + kata_panjang[-1][0].casefold()
    return awalan in (inisial, inisial_penuh)


# ============================================================
# HELPER: ADAPTASI NILAI UNTUK DRIVER
# ============================================================
#
# Mapper mengembalikan "YYYY-01-01" dan timestamp ISO-8601 berupa
# string. Driver psycopg butuh objek date/datetime sungguhan untuk
# kolom DATE / TIMESTAMP; mengirim string mentah berisiko ditolak
# PostgreSQL dengan pesan "column is of type date but expression is
# of type text". Jadi konversi terjadi di lapisan SQL, bukan di
# mapper (mapper tetap murni).

def _ke_tanggal(nilai):
    """Ubah "YYYY-01-01" jadi datetime.date (atau None)."""
    if nilai is None:
        return None
    if isinstance(nilai, datetime):
        return nilai.date()
    if isinstance(nilai, date):
        return nilai
    teks = str(nilai).strip()
    if not teks:
        return None
    try:
        return date.fromisoformat(teks)
    except ValueError:
        raise LoaderError(
            "publication_date '{}' tidak bisa dibaca sebagai tanggal "
            "ISO (YYYY-MM-DD). Ini melanggar kontrak mapper, bukan "
            "data yang hilang - lebih baik gagal sekarang daripada "
            "menulis tanggal-ngawur.".format(teks)
        )


def _ke_waktu(nilai):
    """Ubah timestamp ISO-8601 jadi datetime tanpa timezone.

    Kolom retrieved_at bertipe TIMESTAMP (tanpa zona waktu).
    Scraper selalu menulis UTC (akhiran "Z"), jadi kalau nilai
    punya zona waktu, waktu UTC-nya yang dipakai lalu zonanya
    dibuang. Hasilnya jam dinding UTC, tanpa ambigu.
    """
    if nilai is None:
        return None
    if isinstance(nilai, datetime):
        waktu = nilai
    else:
        teks = str(nilai).strip()
        if not teks:
            return None
        if teks[-1:] in ("Z", "z"):
            teks = "{}+00:00".format(teks[:-1])
        try:
            waktu = datetime.fromisoformat(teks)
        except ValueError:
            raise LoaderError(
                "retrieved_at '{}' tidak bisa dibaca sebagai timestamp "
                "ISO-8601. Ini melanggar kontrak mapper (seharusnya "
                "envelope['finished_at']), bukan data yang hilang."
                .format(teks)
            )
    if waktu.tzinfo is not None:
        waktu = waktu.astimezone(timezone.utc).replace(tzinfo=None)
    return waktu


# ------------------------------------------------------------
# Helper untuk lapisan raw
# ------------------------------------------------------------

def _ke_uuid(nilai):
    """Ubah run_id dari envelope jadi uuid.UUID, atau None kalau rusak.

    Sama sekali TIDAK melempar error. Kolom raw.run_id bertipe
    UUID dan keunikannya dipakai untuk mengenali run, tapi
    envelope adalah data dari luar: kalau isinya bukan UUID yang
    sah, akibatnya tidak boleh lebih besar dari pada kehilangan
    run_id. Menolak seluruh penulisan raw karena satu run_id
    jelek berarti data mentah yang justru bisa dicari dan
    diperbaiki manual ikut hilang - padahal inilah seluruh alasan
    tabel raw ada.

    Objek uuid.UUID yang sudah jadi dikembalikan apa adanya, dan
    nilai kosong/None jadi None. Teks yang tidak bisa diurai juga
    None: kolomnya nullable, dan uuid.UUID akan menolaknya lebih
    keras daripada json yang disimpan di raw_envelope (di sana
    teks aslinya tetap utuh, jadi tidak ada informasi yang benar-
    benar hilang).
    """
    if nilai is None:
        return None
    if isinstance(nilai, uuid.UUID):
        return nilai
    teks = str(nilai).strip()
    if not teks:
        return None
    try:
        return uuid.UUID(teks)
    except (ValueError, AttributeError, TypeError):
        return None


def _utc_naif():
    """Waktu sekarang sebagai jam dinding UTC, TANPA zona waktu.

    Dipakai untuk raw.scraped_at. Kolomnya TIMESTAMP (tanpa zona
    waktu), dan default now() di sisi server memakai zona waktu
    server - kalau server-nya tidak UTC, kolom TIMESTAMP yang
    sama akan berisi dua meaning yang berbeda dalam satu tabel
    (jam server untuk DEFAULT, jam UTC untuk loader). Karena itu
    jamnya dikirim eksplisit dari Python, sebagai UTC naive,
    satu-satunya bentuk yang dipakai file ini di mana pun
    (lihat _ke_waktu untuk retrieved_at).

    scraped_at sengaja BUKAN envelope.finished_at: dua waktu itu
    berbeda artinya. finished_at = scraping selesai; scraped_at =
    envelope itu ditaruh ke database. Untuk run normal selisihnya
    milidetik, untuk run yang menggantung bisa berminggu-minggu.
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _scholar_id_raw(scholar_id, fungsi):
    """Validasi scholar_id yang diberikan pemanggil untuk baris raw.

    Tidak ada lagi tebakan dari envelope dan tidak ada placeholder.
    Nilai WAJIB datang dari pemanggil: identitas yang dipakainya
    untuk menjalankan scraping (argumen `--id` di CLI, author_id dari
    daftar target di pipeline).

    Kenapa tidak dikembalikan dari envelope: bentuk envelope dikunci
    9 kunci dan scholar_id hanya hidup DI DALAM `data`. Run fatal
    punya data=None, jadi uid profil yang sedang di-scrape hilang
    dari envelope sepenuhnya - scraper memorinya lalu membuang begitu
    _make_envelope() selesai. Satu-satunya sumber yang benar adalah
    pemanggil.

    Run fatal TETAP ditulis (justru itu record paling berguna), tapi
    identitasnya harus benar. Lebih baik menolak pemanggilan yang
    tidak bisa mengidentifikasi dirinya daripada menyimpan baris raw
    dengan scholar_id yang menipu: kolom ini NOT NULL dan dipakai
    sebagai identitas profil di seluruh query hilir.
    """
    if scholar_id is None:
        raise LoaderError(
            "{}() wajib diberi scholar_id: identitas profil yang "
            "di-scrape, diambil dari argumen yang dipakai pemanggil "
            "untuk menjalankan scraping (mis. --id di CLI atau "
            "author_id dari daftar target). Nilai ini tidak bisa "
            "disimpulkan dari envelope, karena run fatal punya "
            "data=None sehingga tidak ada scholar_id di dalamnya. "
            "Menebaknya akan mengisi kolom NOT NULL dengan nilai "
            "yang tidak tentu benar.".format(fungsi)
        )
    teks = str(scholar_id).strip()
    if not teks:
        raise LoaderError(
            "{}() menerima scholar_id kosong setelah dibersihkan. "
            "Isi dengan identitas profil yang sedang di-scrape, "
            "jangan dengan string kosong.".format(fungsi)
        )
    return teks


def _peringatan_kemiripan_scholar_id(envelope, scholar_id, fungsi):
    """Beri peringatan kalau scholar_id pemanggil beda dari envelope.

    SENGaja hanya peringatan, bukan exception. Data tetap disimpan
    apa adanya. Alasannya: run yang bermasalah justru paling
    berguna disimpan - dan uid yang muncul di halaman Scholar bisa
    memang berbeda dari yang diminta (profil di-redirect ke ID
    lain, atau pemanggil keliru mengetik angka). Menolak menyimpan
    berarti record itu hilang persis saat paling perlu ditelusuri.

    Yang dicatat ke raw tetap nilai dari pemanggil, karena itu
    identitas yang dipakainya untuk memanggil scraping. Kolom
    scholar_id tidak diubah hanya karena envelope berbeda.

    Diamnya danger ini: tanpa peringatan, tabel raw bisa terlihat
    berisi "profil X" padahal isinya profil Y. Karena itu
    ketidakcocokan harus terlihat, bukan sekadar tercatat.
    """
    data = envelope.get("data")
    if not isinstance(data, dict):
        # Run fatal: tidak ada scholar_id di envelope sama sekali,
        # jadi tidak ada yang bisa dibandingkan. Bukan mismatch.
        return
    dari_envelope = data.get("scholar_id")
    if dari_envelope is None:
        return
    pemanggil = str(scholar_id).strip()
    dari_envelope = str(dari_envelope).strip()
    if not pemanggil or not dari_envelope or pemanggil == dari_envelope:
        return
    warnings.warn(
        "{}(): scholar_id dari pemanggil ({!r}) TIDAK SAMA dengan "
        "scholar_id di dalam envelope.data ({!r}). Baris raw akan "
        "disimpan dengan nilai pemanggil ({!r}) supaya tetap bisa "
        "ditelusuri ke run yang memang ini, tapi periksa dulu "
        "apakah --id yang dipakai benar.".format(
            fungsi, pemanggil, dari_envelope, pemanggil
        ),
        UserWarning,
        stacklevel=3,
    )


# ============================================================
# KONEKSI
# ============================================================

def _lokasi_env_default():
    """Lokasi .env default: dua level di atas src/loading/."""
    return Path(__file__).resolve().parents[2] / ".env"


def _periksa_role(nama_user):
    """Pastikan kredensial sesuai niat file: role etl_writer.

    Tidak ada pelanggaran yang lolos diam-diam: kalau kredensialnya superuser
    `postgres`, keluar peringatan, karena file ini hanya butuh
    INSERT.
    """
    if not nama_user:
        raise LoaderError(
            "{} belum diisi di .env. Isi dengan role ETL (mis. '{}'), "
            "bukan superuser.".format(ENV_USER, ROLE_ETL)
        )
    if nama_user.lower() == SUPERUSER:
        warnings.warn(
            "Koneksi memakai superuser '{}'. Loader ini hanya butuh "
            "INSERT; pakai role '{}' sesuai least privilege."
            .format(SUPERUSER, ROLE_ETL),
            UserWarning,
            stacklevel=3,
        )


def _ambil_int_env(nama, bawaan):
    """Baca variabel .env sebagai int; nilai rusak -> bawaan."""
    mentah = (os.getenv(nama) or "").strip()
    if not mentah:
        return bawaan
    try:
        return int(mentah)
    except ValueError:
        raise LoaderError(
            "{}='{}' bukan angka (bulat) yang bisa dipakai sebagai "
            "port.".format(nama, mentah)
        )


def build_engine(dotenv_path=None, url=None, echo=False):
    """Buat SQLAlchemy Engine dari kredensial .env.

    Parameter
    ---------
    dotenv_path : str | Path | None, opsional
        Lokasi file .env. Default: <root proyek>/.env. File yang
        tidak ada tidak apa-apa; variabel dari environment proses
        tetap dipakai.
    url : str | None, opsional
        URL lengkap untuk menggantikan semua kredensial .env.
    echo : bool, opsional
        True = SQL yang dijalankan dicetak ke log.

    Kredensial dibaca dari POSTGRES_HOST / POSTGRES_PORT /
    POSTGRES_DB / POSTGRES_USER / POSTGRES_PASSWORD. Variabel yang
    sudah ada di environment proses TIDAK ditimpa oleh isi .env.

    Catatan operasional: jangan pernah mencetak
    `engine.url` apa adanya karena memuat password. Kalau perlu
    melihat target koneksi tanpa rahasia:
        make_url(engine.url).render_as_string(hide_password=True)
    """
    # Import dotenv di dalam fungsi supaya modul ini tetap bisa
    # diimpor di lingkungan yang belum memasang driver sama sekali.
    from dotenv import load_dotenv

    load_dotenv(Path(dotenv_path) if dotenv_path else _lokasi_env_default())

    if url:
        return sa.create_engine(url, echo=echo, future=True)

    host = (os.getenv(ENV_HOST) or "").strip() or DEFAULT_HOST
    port = _ambil_int_env(ENV_PORT, DEFAULT_PORT)
    nama_db = (os.getenv(ENV_DB) or "").strip()
    user = (os.getenv(ENV_USER) or "").strip()
    password = os.getenv(ENV_PASSWORD)

    if not nama_db:
        raise LoaderError(
            "{} belum diisi di .env. Tidak ada nama database yang bisa "
            "dibuatkan URL-nya.".format(ENV_DB)
        )
    _periksa_role(user)

    alamat = URL.create(
        drivername=DRIVER,
        username=user,
        password=password,
        host=host,
        port=port,
        database=nama_db,
    )
    return sa.create_engine(alamat, echo=echo, future=True)


def _pastikan_driver_ada():
    """Pastikan psycopg terpasang, dengan pesan yang jelas.

    Sengaja dipanggil hanya dari connect(): modul ini harus tetap
    bisa diimpor (dan diuji) di mesin tanpa psycopg.
    """
    try:
        import psycopg  # noqa: F401
    except ImportError as exc:  # pragma: no cover - tanpa driver
        raise LoaderError(
            'Driver "psycopg" belum ter-install. Jalankan: '
            'pip install "psycopg[binary]" (atau: pip install -r '
            "requirements.txt)"
        ) from exc


def connect(dotenv_path=None, url=None, echo=False):
    """Buka satu koneksi SQLAlchemy ke PostgreSQL.

    Thin wrapper di atas build_engine(). Dipanggil HANYA saat
    pemanggil tidak menyuntikkan koneksinya sendiri; modul ini
    tidak pernah menyambung ke database saat diimpor.

    Kalau alamat target ingin dicatat di log, pakai engine:
        engine = build_engine()
        print(make_url(engine.url).render_as_string(hide_password=True))
    """
    _pastikan_driver_ada()
    return build_engine(dotenv_path=dotenv_path, url=url, echo=echo).connect()


# ============================================================
# RESOLUSI PENULIS (AuthorResolver)
# ============================================================
#
# Bagian ini adalah Policy yang bisa diuji TANPA database. Semua
# keputusan soal "nama ini sudah ada atau belum" ada di sini, dan
# satu-satunya jalan ke database adalah objek `store` yang harus
# menyediakan dua method:
#
#     daftar_author()                    -> iterable (author_id, name)
#     buat_author(nama, scholar_id=None) -> author_id
#
# PostgresAuthorStore adalah implementasi sungguhan; test memakai
# store palsu. Resolver sendiri tidak tahu-menahu soal SQL, dan
# sengaja tidak ikut tahu apa pun soal ON CONFLICT: UPSERT ada
# di lapisan statement (_stmt_buat_author), yang jauh di bawah
# cache ini.

class AuthorResolver:
    """Petakan nama penulis ke core.authors.author_id, buat bila perlu.

    Aturan yang dijalankan:
      1. Cache diisi satu kali dari tabel yang sudah ada (isi_cache),
         dengan kunci nama yang sudah dirapatkan dan di-casefold.
      2. resolve(nama) melihat cache dulu. Ada -> pakai ulang
         author_id itu (counter `dipakai_ulang` naik).
      3. Tidak ada -> INSERT satu baris (counter `dibuat` naik) lalu
         id-nya langsung masuk cache, sehingga pemanggilan
         berikutnya dengan nama yang sama tidak membuat baris kedua.

    Cache ini SENGJAJA TIDAK DISENTUHKAN oleh UPSERT, dan itu
    keputusan yang benar. Cache adalah penghematan, bukan penjaga
    kebenaran:
      - Fungsinya hanya menghemat perjalanan ke database. Satu run
        dengan 40 baris dan 6 penulis unik akan melakukan 1 SELECT
        + 6 INSERT, bukan 46 SELECT.
      - Kebenarannya dijaga database: indeks unik
        lower(btrim(name)) + ON CONFLICT (lihat
        _stmt_buat_author). Kalau dua run bersamaan memproses
        nama yang sama, keduanya bisa saja sama-sama tidak
        menemukannya di cache, tapi hanya satu yang boleh membuat
        baris - yang kedua menerima author_id yang sudah ada lewat
        RETURNING. Tidak ada baris kembar, dan tidak ada insert
        yang gagal.
      - Kalau cache dihapus demi "kesederhanaan", setiap resolusi
        nama jadi satu perjalanan bolak-balik, dan perilaku
        deduplikasi nama - bagian paling halus dari loader ini -
        jadi sulit diuji tanpa database.

    Yang perlu diketahui: `dibuat` menghitung nama yang TIDAK ada
    di cache, jadi dalam dua writer yang benar-benar bersamaan
    satu angka bisa melebihi jumlah baris baru. Lihat catatan di
    bagian STATISTIK.
    """

    def __init__(self, store):
        if store is None:
            raise LoaderError("AuthorResolver butuh store author.")
        self._store = store
        # kunci nama (casefold, spasi sudah dirapatkan) -> author_id
        self._cache = {}
        self.dibuat = 0
        self.dipakai_ulang = 0

    def isi_cache(self, baris_existing):
        """Isi cache dari hasil SELECT core.authors.

        `baris_existing` boleh apa saja yang bisa di-iterate dan
        tiap elemennya punya dua nilai: author_id lalu name.

        Kalau tabel sudah punya dua baris dengan nama yang sama,
        yang DITEMUKAN PERTAMA yang dipakai. Duplikat seperti itu
        bukan yang harus dibersihkan di sini.
        """
        jumlah = 0
        for author_id, nama in baris_existing:
            kunci = _kunci_nama(nama)
            if kunci is None or kunci in self._cache:
                continue
            self._cache[kunci] = author_id
            jumlah += 1
        return jumlah

    @property
    def ukuran_cache(self):
        """Berapa nama author yang sudah ada di cache."""
        return len(self._cache)

    def cari(self, nama):
        """Cari author_id di cache saja, tanpa membuat apa pun."""
        kunci = _kunci_nama(nama)
        if kunci is None:
            return None
        return self._cache.get(kunci)

    def resolve(self, nama, scholar_id=None):
        """Kembalikan author_id untuk `nama`, membuatnya bila belum ada.

        `scholar_id` hanya diisi untuk baris author pemilik profil.
        Nama co-author yang dibuat otomatis mendapat scholar_id NULL,
        sesuai kebijakan yang disepakati.

        Nama None/kosong adalah pelanggaran kontrak (bukan kondisi
        data yang wajar), jadi lempar LoaderError, bukan diam-diam
        membuat author tanpa nama.
        """
        bersih = _normalisasi_nama(nama)
        if bersih is None:
            raise LoaderError(
                "AuthorResolver.resolve() dipanggil dengan nama kosong. "
                "core.authors.name NOT NULL, jadi tidak ada nama yang "
                "aman untuk disimpan; cek pemanggilnya."
            )

        kunci = bersih.casefold()
        author_id = self._cache.get(kunci)
        if author_id is not None:
            self.dipakai_ulang += 1
            return author_id

        author_id = self._store.buat_author(bersih, scholar_id=scholar_id)
        if author_id is None:
            raise LoaderError(
                "INSERT INTO core.authors untuk nama '{}' tidak "
                "mengembalikan author_id. Baris mungkin tidak "
                "tersimpan.".format(bersih)
            )
        # WAJIB: id baru langsung masuk cache, supaya kemunculan
        # nama yang sama di publikasi berikutnya memakai baris ini
        # dan bukan membuat baris kedua.
        self._cache[kunci] = author_id
        self.dibuat += 1
        return author_id


class PostgresAuthorStore:
    """Implementasi store author di atas satu koneksi SQLAlchemy.

    Dua method ini adalah SELURUH permukaan yang dipakai
    AuthorResolver, jadi yang perlu ditiru saat menulis test adalah
    dua method ini saja.
    """

    def __init__(self, conn):
        self._conn = conn

    def daftar_author(self):
        """SELECT author_id, name FROM core.authors (sekali, saat mulai)."""
        return self._conn.execute(_stmt_daftar_author()).all()

    def buat_author(self, nama, scholar_id=None):
        """UPSERT satu baris core.authors, kembalikan author_id.

        Yang ditulis hanya `name` dan `scholar_id`. scholar_id
        NULL berarti kolom itu tetap kosong, sama saja dengan tidak
        disebut sama sekali - dan tidak akan menimpa scholar_id
        yang sudah terisi (lihat _stmt_buat_author). Kolom
        sinta_id / scopus_id / program_study / faculty tidak
        pernah diisi di sini: itu atribut institutional, bukan
        hasil scraping.

        Kembalikan author_id apa adanya, baik untuk baris yang
        baru dibuat maupun untuk baris yang sudah ada dan
        sekarang di-UPDATE. Pemanggil tidak perlu - dan tidak
        bisa - membedakan keduanya, dan memang tidak perlu:
        yang dia butuhkan hanya "author_id untuk nama ini".
        """
        hasil = self._conn.execute(_stmt_buat_author(nama, scholar_id))
        return hasil.scalar_one()


# ============================================================
# PEMBANGUN PERINTAH (thin SQL layer)
# ============================================================
#
# Semua fungsi di sini PURE: mengubah nilai jadi objek statement
# SQLAlchemy, tanpa menyentuh koneksi. Karena itu statement-nya
# bisa dikompilasi terhadap dialect postgresql tanpa database -
# dipakai untuk memeriksa bentuk SQL-nya.

# Kolom yang benar-benar ditulis per tabel. Kolom SERIAL/PK dan
# kolom yang tidak ada di sumber Scholar sengaja tidak disebut,
# supaya PostgreSQL yang mengisi dengan NULL.
KOLOM_PUBLICATIONS = (
    "title", "publication_date", "journal", "volume", "pages",
    "publisher", "description", "category", "doi",
)
KOLOM_AUTHOR_METRICS = (
    "source", "h_index", "score", "index_name", "retrieved_at",
)
KOLOM_PUBLICATION_METRICS = (
    "source", "citation_count", "index_name", "score", "retrieved_at",
)


# ------------------------------------------------------------
# LAPISAN RAW (DI-COMMIT LEBIH DAHULU)
# ------------------------------------------------------------

def _stmt_sisip_raw_scrape_result(scholar_id, scraped_at, run_id, status,
                                  envelope):
    """INSERT INTO raw.scholar_scrape_result ... RETURNING result_id.

    Lima kolom, semuanya diisi eksplisit. scraped_at TIDAK boleh
    diserahkan ke DEFAULT now() di server: kolomnya TIMESTAMP
    tanpa zona waktu, jadi now() akan menghasilkan jam lokal
    server dan bercampur dengan jam UTC yang dipakai envelope
    (lihat _utc_naif).

    raw_envelope dikirim sebagai objek Python utuh, bukan string
    JSON. Tipe kolom (_EnvelopeJSONB) yang bertanggung jawab atas
    penyerialisasinya; kalau string JSON dikirim ke sini, ia akan
    diserialisasi lagi dan tersimpan sebagai JSON string, bukan
    JSON object - dan seluruh query hilir yang memakai
    raw_envelope -> 'data' akan gagal.

    ON CONFLICT (run_id) DO UPDATE ... SET result_id = result_id adalah
    no-op yang sengaja: kalau run yang sama dicoba ulang (mis. core
    gagal lalu operator retry), baris raw lama dikembalikan apa adanya
    alih-alih melempar unique violation. Tanpa klausa ini retry MUSTAHIL:
    raw sudah ter-commit, jadi insert ulang error, dan karena error raw
    menghentikan core demi safety, core tidak akan pernah termuat tanpa
    penghapusan manual baris raw - persis kondisi yang harus dihindari.
    """
    return (
        pg.insert(RAW_SCRAPE_RESULT)
        .values(
            scholar_id=scholar_id,
            scraped_at=scraped_at,
            run_id=run_id,
            status=status,
            raw_envelope=envelope,
        )
        .on_conflict_do_update(
            index_elements=[RAW_SCRAPE_RESULT.c.run_id],
            set_={"result_id": RAW_SCRAPE_RESULT.c.result_id},
        )
        .returning(RAW_SCRAPE_RESULT.c.result_id)
    )


# ------------------------------------------------------------
# STATEMENT CORE
# ------------------------------------------------------------

def _stmt_daftar_author():
    """SELECT author_id, name FROM core.authors."""
    return sa.select(AUTHORS.c.author_id, AUTHORS.c.name)


def _stmt_buat_author(nama, scholar_id=None):
    """INSERT ... ON CONFLICT INTO core.authors, RETURNING author_id.

    Upsert, bukan INSERT biasa: scraper yang dijalankan dua kali
    tidak boleh menduplikasi seluruh isi core.authors.

    TARGET KONFLIK WAJIB PERSIS dengan indeks di DDL 03:
        CREATE UNIQUE INDEX uq_authors_name_normalized
            ON core.authors (lower(btrim(name)));
    Jadi yang ditulis di ON CONFLICT adalah EKSPRESI
    lower(btrim(authors.name)), bukan kolom name polos. Kalau
    targetnya diganti jadi (name), PostgreSQL akan menolak
    statement ini karena tidak ada batasan yang cocok - dan bukan
    diam-diam menulis ulang indeks.

    Kenapa btrim() dan lower() di sini, sementara cache Python
    memakai _normalisasi_nama() + casefold()? Karena keduanya
    pursue satu tujuan yang sama: menyamakan tulisan yang
    berbeda penulisan-spasi atau huruf-besar-nya. Yang benar-benar
    memutuskan tetap database; cache Python hanya menghemat
    perjalanan bolak-balik. Perbedaannya: .casefold() di Python
    lebih agresif untuk beberapa huruf non-ASCII (mis. "ss"
    dari "ß", sedangkan lower() PostgreSQL meninggalkannya
    "ß"). Untuk nama Indonesia/ASCII tidak ada bedanya; untuk
    nama dengan huruf beraksen mungkin. Lihat catatan yang sama
    di sql/ddl/03.

    SET scholar_id = COALESCE(EXCLUDED.scholar_id, core.authors.scholar_id)
    -------------------------------------------------------------------
    Sengaja TIDAK simetris dengan publikasi, dan itu disengaja.
    Publikasi memakai "run terakhir menang" untuk semua kolom
    non-kunci; author tidak boleh menimpa scholar_id yang SUDAH
    DIKETAHUI dengan NULL. Alasannya bukan sekadar hemat hati:
    (a) author dicocokkan berdasarkan nama case-insensitive, dan
    pencocokan nama tidak membuktikan dua orang itu orang yang
    sama - dua orang berbeda dengan nama sama bisa saling
    menimpa kolom ini kalau COALESCE tidak dipakai;
    (b) scholar_id hanya terisi kalau baris author itu berasal
    dari profil Scholar. Baris yang sama bisa muncul di run lain
    hanya sebagai co-author publikasi orang lain, yang
    scholar_id-nya NULL. Menimpa kolom yang sudah terisi dengan
    NULL berarti menghapus satu-satunya bukti bahwa author ini
    punya profil Scholar;
    (c) atribut institutional lain (sinta_id, scopus_id,
    program_study, faculty) tidak pernah terisi lewat jalur ini,
    jadi scholar_id adalah satu-satunya identitas eksternal
    yang bisa didapat di sini.

    POLA yang dipakai di file ini: kolom yang isinya memang
    boleh NULL di sumber (journal, doi, publisher, ...)
    ditulis ulang apa adanya, karena NULL menimpa NULL tidak
    menghapus informasi apa pun. Kolom identitas (scholar_id)
    dijaga, karena NULL menimpa nilai berarti menghapus bukti.
    """
    stmt = pg.insert(AUTHORS).values(name=nama, scholar_id=scholar_id)
    return stmt.on_conflict_do_update(
        # Kunci case-insensitive: harus sama persis dengan
        # ekspresi indeks uq_authors_name_normalized.
        index_elements=[sa.func.lower(sa.func.btrim(AUTHORS.c.name))],
        set_={
            "scholar_id": sa.func.coalesce(
                stmt.excluded.scholar_id, AUTHORS.c.scholar_id
            ),
        },
    ).returning(AUTHORS.c.author_id)


def _stmt_sisip_publikasi(baris):
    """INSERT ... ON CONFLICT INTO core.publications, RETURNING publications_id.

    publication_date dikonversi ke date karena kolomnya DATE.

    Natural key: (title, publication_date), dengan batasan UNIQUE
    NULLS NOT DISTINCT dari sql/ddl/03. NULLS NOT DISTINCT itu yang
    membuat UPSERT ini benar: tanpa itu, semua publikasi tanpa
    tanggal (tahun "in press", "2019-2020") akan lolos sebagai
    "berbeda" dan tidak pernah terdeteksi duplikat.

    DO UPDATE TIDAK menyentuh publications_id, jadi id yang
    dikembalikan pada kasus konflik adalah id yang sudah
    dipakai publication_authors dan publication_metrics dari
    run sebelumnya. Di sinilah nilai RETURNING itu menentukan:
    kalau pemanggil memakai id baru padahal barisnya sebenarnya
    baris lama, semua tautan di run ini akan menunjuk publikasi
    yang salah - dan tidak akan ada error, hanya relasi keliru.

    Semua kolom non-kunci ditulis ulang apa adanya dari EXCLUDED
    ("run terakhir menang"). Untuk publications ini konsekuensinya
    ringan: kolom yang NULL biasanya memang NULL di sumber
    (publisher, description, category, doi tidak ada di Scholar
    sama sekali), jadi menimpa NULL dengan NULL tidak menghapus
    informasi apa pun. Berbeda dengan authors.scholar_id, yang
    penjagaannya berlawanan arah (lihat _stmt_buat_author).
    """
    nilai = {kolom: baris.get(kolom) for kolom in KOLOM_PUBLICATIONS}
    nilai["publication_date"] = _ke_tanggal(nilai["publication_date"])

    stmt = pg.insert(PUBLICATIONS).values(**nilai)
    # Kolom yang boleh ditimpa saat konflik: natural key-nya
    # sendiri (title, publication_date) jelas tidak boleh, karena
    # kolom itulah yang membuat kedua baris dianggap sama.
    set_ = {
        kolom: stmt.excluded[kolom]
        for kolom in KOLOM_PUBLICATIONS
        if kolom not in ("title", "publication_date")
    }
    return stmt.on_conflict_do_update(
        index_elements=["title", "publication_date"],
        set_=set_,
    ).returning(PUBLICATIONS.c.publications_id)


def _stmt_sisip_publication_authors(publication_id, author_id, author_order):
    """INSERT INTO core.publication_authors ... ON CONFLICT DO NOTHING.

    Ketiganya composite primary key. Tanpa ON CONFLICT, satu baris
    kembar akan menggagalkan SELURUH run (violasi PK tidak bisa
    di-rollback sebagian), padahal kasus itu nyata dan berasal dari
    pipeline sendiri:

      - Scraper meniadakan duplikat baris publikasi berdasarkan
        judul MENTAH (lihat _collect_rows).
      - Mapper merapikan spasi internal saat menulis title
        (_rapikan_teks di publications.py).
      - Jadi dua judul mentah berbeda yang hanya beda spasi ganda,
        newline, atau spasi di akhir bisa menjadi string title yang
        PERSIS SAMA, lalu keduanya menabrak natural key yang sama
        pada UPSERT di atas dan menyatu jadi satu publications_id.
      - publication_authors untuk keduanya lalu punya pasangan
        (publication_id, author_id, author_order) yang sama persis
        untuk penulis pertama - PK-nya bentrok.

    DO NOTHING membuat load ini idempoten, bukan crash. Yang
    hilang bukan data, cuma pengulangan atas relasi yang sudah
    tercatat.

    author_order sendiri tetap aman: satu nama yang muncul dua
    kali dalam daftar penulis punya order berbeda, jadi dua
    barisnya memang berbeda.
    """
    return pg.insert(PUBLICATION_AUTHORS).values(
        publication_id=publication_id,
        author_id=author_id,
        author_order=author_order,
    ).on_conflict_do_nothing(
        # composite PK: publication_id, author_id, author_order
        index_elements=["publication_id", "author_id", "author_order"],
    )


def _stmt_hapus_tautan(publication_id, author_id, author_order):
    """DELETE FROM core.publication_authors untuk tautan spesifik.

    Hanya menghapus baris (publication_id, author_id, author_order)
    yang SEDANG diproses saat ini. Dipakai saat nama bentuk pendek
    pemilik terlanjur menempel di author tiruan dari run sebelum fix:
    tautan lama dibuang, lalu tautan baru disisipkan ke author_id
    pemilik. Tidak ada DELETE terhadap core.authors, dan tidak ada
    perubahan ke publikasi lain.
    """
    return sa.delete(PUBLICATION_AUTHORS).where(
        PUBLICATION_AUTHORS.c.publication_id == publication_id,
        PUBLICATION_AUTHORS.c.author_id == author_id,
        PUBLICATION_AUTHORS.c.author_order == author_order,
    )


# ------------------------------------------------------------
# METRICS: APPEND-ONLY, SENGAJA TIDAK DI-UPSERT
# ------------------------------------------------------------
# PERINGATAN untuk pembaca di kemudian hari: JANGAN menambahkan
# ON CONFLICT ke statement di bawah.
#
# author_metrics dan publication_metrics adalah SNAPSHOT
# MOMENTAL, bukan state saat ini. Baris "h_index = 12 pada
# retrieved_at tertentu" adalah fakta yang berbeda dengan "h_index
# = 12 pada retrieved_at lain", dan perbedaan itulah yang membuat
# tabel ini berguna: tanpa itu, loader ini tidak punya jejak
# sama sekali tentang bagaimana angka sitasi bergerak dari waktu
# ke waktu.
#
# Karena itu setiap run MENAMBAH baris baru. Kalau dua baris punya
# retrieved_at yang sama, itu memang dua pengamatan (atau satu
# run yang ditulis dua kali) dan keduanya sah disimpan. Upsert di
# sini akan menghapus justru catatan perubahan dari run ke run,
# dan tidak ada batasan UNIQUE pun yang mendukung upsert itu -
# jadi tidak ada yang dikorbankan dengan tidak memakai
# ON CONFLICT.


def _stmt_sisip_author_metrics(baris, author_id):
    """INSERT INTO core.author_metrics ... dengan author_id pemilik profil.

    INSERT polos. Lihat blok peringatan di atas.
    """
    return sa.insert(AUTHOR_METRICS).values(
        author_id=author_id,
        source=baris.get("source"),
        h_index=baris.get("h_index"),
        score=baris.get("score"),
        index_name=baris.get("index_name"),
        retrieved_at=_ke_waktu(baris.get("retrieved_at")),
    )


def _stmt_sisip_publication_metrics(baris, publication_id):
    """INSERT INTO core.publication_metrics.

    publication_id diisi dari hasil load publications, bukan dari
    isi baris mapper (yang publication_id-nya masih None).

    INSERT polos. Lihat blok peringatan di atas.
    """
    return sa.insert(PUBLICATION_METRICS).values(
        publication_id=publication_id,
        source=baris.get("source"),
        citation_count=baris.get("citation_count"),
        index_name=baris.get("index_name"),
        score=baris.get("score"),
        retrieved_at=_ke_waktu(baris.get("retrieved_at")),
    )


# ============================================================
# INTEGRITY GUARD
# ============================================================

def _pastikan_nama_pemilik(nama, run_id):
    """Pastikan nama pemilik profil ada sebelum menyentuh core.authors.

    core.authors.name NOT NULL, dan mapper SENGAJA mengembalikan None
    kalau nama profil tidak terbaca, supaya pemanggil yang memutuskan
    (lihat publications.py). Loader memutuskan: itu error, bukan
    alasan untuk menebak nama dari daftar penulis publikasi.
    """
    if _normalisasi_nama(nama) is not None:
        return
    raise LoaderError(
        "Nama pemilik profil kosong, jadi core.authors tidak bisa diisi "
        "(kolom name NOT NULL). Run id={}. Penyebabnya hampir pasti "
        "selector #gsc_prf_in tidak terbaca, atau profilnya memang "
        "tidak punya nama. Data tidak dimuat apa pun; tidak ada "
        "author karangan yang dibuat dari daftar penulis."
        .format(run_id if run_id else "(tidak ada run_id)")
    )


def _pastikan_judul(judul, indeks, run_id):
    """Pastikan satu baris publikasi punya judul sebelum di-INSERT."""
    if _normalisasi_nama(judul) is not None:
        return
    raise LoaderError(
        "Baris publikasi ke-{} tidak punya judul, padahal "
        "core.publications.title NOT NULL. Scraper seharusnya sudah "
        "menyaringnya (lihat _collect_rows), jadi kalau muncul di "
        "sini berarti ada bug di parser. Run id={} dibatalkan "
        "penuh, tidak ada data yang dimuat sebagian."
        .format(indeks, run_id if run_id else "(tidak ada run_id)")
    )


# ============================================================
# STATISTIK
# ============================================================
#
# Dict yang dikembalikan load_core_rows()/load_scholar_run().
# KunciNYA SELALU sama, apa pun hasilnya, supaya pemanggil tidak
# perlu memakai .get() atau .get(..., 0).
#
#   status        "LOADED" | "SKIPPED_NO_DATA"
#   reason        alasan kalau status SKIPPED_NO_DATA, None kalau sukses
#   run_id        run_id dari envelope (None kalau pemanggil tidak memberi)
#   raw_result_id result_id baris raw.scholar_scrape_result milik run ini
#                 (None kalau load dipanggil tanpa menulis raw, yaitu
#                 load_core_rows() yang dipanggil langsung)
#   author_id     author_id pemilik profil setelah dimuat
#   authors_created   jumlah baris core.authors yang di-INSERT oleh run ini
#   authors_reused    jumlah resolusi nama yang memakai author yang sudah ada
#   publications_inserted        jumlah baris core.publications yang di-INSERT
#   publication_authors_inserted  jumlah baris core.publication_authors
#                                 yang di-INSERT
#   metrics_inserted  jumlah baris metrics yang di-INSERT = 1 (author_metrics)
#                     + satu publication_metrics per publikasi
#   skipped        jumlah tautan penulis yang DILEWATI karena namanya
#                  tidak bisa dipakai (lihat load_core_rows). Judul
#                  tanpa isi TIDAK dihitung di sini karena itu error.
#
# CATATAN SOAK ANGKA-ANGKA INI SETELAH UPSERT
# ------------------------------------------
# Nama kuncinya tetap "inserted" dan nilainya tetap menghitung
# setiap baris yang PERNAH diproses, bukan hanya baris baru yang
# benar-benar tercipta. Alasannya supaya angka run-ke-run tetap
# bisa dibandingkan: kalau dibalik menjadi "jumlah baris benar-
# benar baru", run kedua akan melaporkan 0 publikasi padahal
# datanya tetap lengkap dan diproses semua.
#
# Konsekuensi yang perlu diketahui saat membaca angka:
#   - publications_inserted = jumlah baris yang diproses. Pada run
#     kedua untuk profil yang sama, sebagian besar bisa jadi
#     UPDATE (ON CONFLICT DO UPDATE), bukan INSERT baru.
#   - authors_created = resolver.dibuat, yaitu jumlah nama yang
#     TIDAK ada di cache. Cache diisi ulang dari core.authors di
#     awal run, jadi dalam run yang normal ini sama dengan
#     "jumlah baris author yang benar-benar baru". Kalau dua
#     writer jalan bersamaan, satu bisa saja melakukan INSERT
#     yang ternyata berubah jadi DO UPDATE karena writer lain
#     menduduki nama itu duluan; angka authors_created lalu bisa
#     1 lebih besar dari jumlah baris baru. Itu konsekuensi wajar
#     dari dua writer yang boleh hidup berdampingan, bukan bug.
#   - publication_authors_inserted dihitung SEBAGAI kalau INSERT
#     dikembalikan, meskipun ON CONFLICT DO NOTHING bisa
#     diam-diam tidak menulis apa pun (lihat
#     _stmt_sisip_publication_authors).
#   - publication_authors_repointed menghitung tautan yang
#     dialihkan dari author tiruan ke author pemilik (DELETE lama +
#     INSERT baru untuk publikasi+urutan yang sama). Hanya terjadi
#     saat nama bentuk pendek pemilik terlanjur menempel di author
#     lain dari run sebelum fix.


def _stats_kosong():
    """Dict statistik dengan semua kunci terisi nol."""
    return {
        "status": None,
        "reason": None,
        "run_id": None,
        "raw_result_id": None,
        "author_id": None,
        "authors_created": 0,
        "authors_reused": 0,
        "publications_inserted": 0,
        "publication_authors_inserted": 0,
        "publication_authors_repointed": 0,
        "metrics_inserted": 0,
        "skipped": 0,
    }


# ============================================================
# TRANSAKSI
# ============================================================

def _mulai_transaksi(conn, untuk="load_scholar_run()"):
    """Buka SATU transaksi; tolak koneksi yang masih punya transaksi.

    Dipakai dua kali, untuk dua koneksi yang berbeda: satu
    untuk core, satu untuk raw. Argumen `untuk` hanya dipakai
    untuk menyusun pesan error supaya operator tahu koneksi mana
    yang bermasalah.
    """
    if conn.in_transaction():
        raise LoaderError(
            "Koneksi yang disuntikkan ke {} masih punya transaksi "
            "terbuka. Semua penulisan harus jalan dalam satu transaksi "
            "milik sendiri: commit atau rollback dulu koneksi itu "
            "sebelum memanggilnya.".format(untuk)
        )
    return conn.begin()


# ============================================================
# API PUBLIK
# ============================================================

def write_raw_scrape_result(envelope, scholar_id, raw_conn=None,
                            scraped_at=None):
    """Tulis satu run envelope ke raw.scholar_scrape_result, lalu COMMIT.

    Ini adalah LANGKAH PERTAMA dari pipeline dan selalu dijalankan,
    termasuk untuk run yang gagal. Bentuk envelope apa adanya
    disimpan ke kolom JSONB raw_envelope: field mentah Scholar yang
    tidak dipetakan ke core (venue, extra, failures, domain,
    batches, elapsed) tetap ada di sana, sehingga mapper yang
    berubah atau salah masih punya sumber untuk diulang.

    Parameter
    ---------
    envelope : dict
        Run envelope hasil scrape_scholar(). Tidak boleh None;
        dict kosong pun tetap ditulis (dengan run_id NULL),
        karena "run yang jalan tapi tidak menghasilkan apa-apa"
        adalah informasi, bukan kelalaian. Yang wajib diisi
        terpisah adalah scholar_id, bukan isinya.
    scholar_id : str, WAJIB
        Identitas profil yang sedang di-scrape, diambil dari
        pemanggil - mis. argumen `--id` di CLI atau author_id
        dari daftar target di pipeline. WAJIB karena kolom
        raw.scholar_scrape_result.scholar_id NOT NULL dan
        envelope tidak selalu membawanya: run fatal punya
        data=None, jadi scholar_id tidak ada di dalam envelope
        sama sekali. Tidak ada nilai tebakan maupun placeholder;
        nilai kosong atau None ditolak sebagai LoaderError.
    raw_conn : Connection | None, opsional
        Koneksi SQLAlchemy. Kalau None, file ini yang membuka
        koneksi dari .env, commit, lalu menutupnya. Kalau
        diberikan, file ini tetap commit; menutup koneksi itu
        tetap urusan pemanggil.
    scraped_at : datetime | None, opsional
        Waktu penulisan, sebagai jam dinding UTC tanpa zona
        waktu. Default: sekarang (lihat _utc_naif). Argumen ini
        ada supaya test bisa menentukan nilainya sendiri.

    Transaksi
    ---------
    TRANSAKSI MILIK SENDIRI, terpisah dari (dan mendahului)
    transaksi core. Inilah inti dari fungsi ini: kalau penulisan
    raw ikut di-rollback bersama core, tabel raw tidak akan
    pernah berisi apa pun pada saat justru dibutuhkan, yaitu saat
    load core gagal. Commit di sini terjadi sebelum pemanggil
    menyentuh core sama sekali.

    Karena itu pemanggil TIDAK BOLEH memakai koneksi yang sama
    untuk core dan untuk raw. Kalau iya, "komit dulu" di sini
    tidak berarti apa-apa: rollback core enam baris kemudian akan
    menarik baris raw ini juga.

    Kegagalan
    ----------
    Apa pun yang menggagalkan INSERT (tabel belum ada, hak akses
    salah, run_id duplikat) dilempar sebagai LoaderError, dengan
    penyebab aslinya tetap terpasang. Pemanggil harus menghentikan
    pemuatan core: kalau data mentah tidak bisa disimpan, memuat
    core berarti mengisi core dengan data yang tidak bisa diproses
    ulang dari raw. Kegagalan lapisan raw yang dipilih untuk
    menggagalkan seluruh run, bukan diperbaiki diam-diam; kalau
    kebijakan ini tidak sesuai dengan operasi Anda, ubah di
    pemanggil, bukan di sini.

    Mengembalikan
    -------------
    result_id integer dari raw.scholar_scrape_result (dikembalikan
    lewat RETURNING). None kalau driver tidak mengembalikan apa
    pun - pemanggil tetap boleh lanjut, karena yang dijamin di
    sini adalah "tidak ada error", bukan "pasti ada barisnya".
    """
    envelope = envelope if envelope is not None else {}

    # Cek cepat SEBELUM transaksi dibuka: kalau envelope tidak bisa
    # diserialisasi JSON, lebih baik gagal sekarang dengan pesan
    # yang jelas daripada membuka transaksi lalu menggagalkan di
    # tengahnya. Serializer-nya sama persis dengan yang dipakai
    # tipe kolom, jadi hasil cek ini bukan tebakan.
    try:
        _serialisasi_envelope(envelope)
    except (TypeError, ValueError) as exc:
        raise LoaderError(
            "Envelope tidak bisa diserialisasi ke JSON, jadi tidak bisa "
            "disimpan ke raw.scholar_scrape_result. Data mentah hilang, "
            "jadi core TIDAK dimuat. Run id={}."
            .format(envelope.get("run_id") or "(tidak ada)")
        ) from exc

    conn_milik_sendiri = raw_conn is None
    if conn_milik_sendiri:
        raw_conn = connect()

    # BEGIN di luar try berikutnya dengan sengaja: kalau dia gagal,
    # tidak ada transaksi yang perlu di-rollback, dan pesan
    # LoaderError-nya harus naik apa adanya (menyatakannya jadi
    # "gagal menyimpan envelope" akan salah, karena masalahnya ada
    # di koneksi, bukan di penulisan). Tapi koneksi yang kita buka
    # sendiri tetap harus ditutup di jalur itu juga.
    try:
        trans = _mulai_transaksi(raw_conn, untuk="write_raw_scrape_result()")
    except Exception:
        if conn_milik_sendiri:
            raw_conn.close()
        raise

    try:
        hasil = raw_conn.execute(_stmt_sisip_raw_scrape_result(
            scholar_id=_scholar_id_raw(scholar_id, "write_raw_scrape_result()"),
            scraped_at=scraped_at if scraped_at is not None else _utc_naif(),
            run_id=_ke_uuid(envelope.get("run_id")),
            status=envelope.get("status"),
            envelope=envelope,
        ))
        result_id = hasil.scalar_one_or_none()
        trans.commit()
    except Exception as exc:
        trans.rollback()
        raise LoaderError(
            "Gagal menyimpan envelope ke raw.scholar_scrape_result, jadi "
            "data mentah run ini tidak aman. core TIDAK dimuat: memuat "
            "core tanpa jaring pengaman berarti mengisi core dengan "
            "data yang tidak bisa diproses ulang dari raw. Run id={}, "
            "status={}. Penyebab aslinya: {}"
            .format(
                envelope.get("run_id") or "(tidak ada)",
                envelope.get("status") or "(tidak ada)",
                exc,
            )
        ) from exc
    finally:
        if conn_milik_sendiri:
            raw_conn.close()

    return result_id


def load_core_rows(core, conn=None, run_id=None):
    """Muat hasil mapper (5 bucket) ke core.* dalam satu transaksi.

    Parameter
    ---------
    core : dict
        Output to_core_rows(), yaitu dict dengan kunci
        `authors`, `publications`, `publication_authors`,
        `author_metrics`, `publication_metrics`.
    conn : Connection | None, opsional
        Koneksi SQLAlchemy ke core. Kalau None, file ini yang
        membuka koneksi dari .env lalu menutupnya lagi setelah
        selesai. Fungsi ini TIDAK menyentuh raw: pemanggil yang
        butuh raw memanggil write_raw_scrape_result() lebih dulu.
    run_id : str | None, opsional
        Dipakai untuk pesan error, supaya kegagalan bisa
        dikaitkan ke run tertentu.

    Urutan penulisan (WAJIB, karena FK):
        1. author pemilik profil   -> author_id untuk metrics + baris
        2. publications            -> publications_id per indeks
        3. publication_authors     -> publication_id + author_id
        4. author_metrics          -> author_id pemilik
        5. publication_metrics     -> publication_id hasil (2)

    `publications` dan `publication_metrics` dijaga sejajar oleh
    mapper (elemen ke-i keduanya berasal dari raw["rows"][i]), dan
    `publication_authors` menunjuk elemen yang sama lewat
    publication_index. Karena itu publications TIDAK PERNAH dilewati:
    baris tanpa judul menggagalkan SELURUH run, bukan di-skip
    diam-diam, supaya penyambungan indeks tidak bergeser.

    Transaksi
    ---------
    Satu transaksi untuk semua penulisan core. Commit sekali di
    akhir. Exception apa pun (termasuk dari resolver) memicu
    rollback lalu naik lagi apa adanya, jadi tidak pernah ada
    kondisi setengah muat: author tanpa publikasinya, atau
    publikasi tanpa publication_authors-nya.

    Perilaku UPSERT
    ---------------
    Baris (1) dan (2) memakai ON CONFLICT, jadi menjalankan ulang
    tidak menggandakan isi core. publications_id / author_id yang
    dipakai di langkah (3)-(5) adalah id yang dikembalikan
    UPSERT, yaitu id yang sudah ada kalau barisnya memang sudah
    ada - sehingga tautan dari run sebelumnya tetap utuh.
    author_id yang tidak ada di cache akan dibuat lewat
    _stmt_buat_author(); AuthorResolver tidak berubah, cache
    Python tetap hanya penghematan perjalanan ke database, dan
    batasan UNIQUE + ON CONFLICT yang menjadi penjaga
    kebenaran. Lihat _stmt_buat_author.

    Yang DILEWATI (tidak error)
    ---------------------------
    Hanya satu: satu entri publication_authors yang nama penulisnya
    kosong setelah dirapatkan. Nama begitu tidak bisa jadi
    core.authors.name (NOT NULL) dan menebaknya berarti mengarang
    identitas, jadi tautannya dihitung di `skipped` dan load
    dilanjutkan. Kasus ini tidak mungkin terjadi di mapper sekarang
    (_pecah_penulis sudah membuang elemen kosong), jadi angka ini
    harus 0; kalau tidak, periksa mapper-nya.

    Mengembalikan
    -------------
    dict statistik; kunci lengkapnya dijelaskan di bagian STATISTIK.
    """
    statistik = _stats_kosong()
    statistik["run_id"] = run_id

    conn_milik_sendiri = conn is None
    if conn_milik_sendiri:
        conn = connect()

    trans = _mulai_transaksi(conn)
    try:
        store = PostgresAuthorStore(conn)
        resolver = AuthorResolver(store)
        # Satu SELECT untuk seluruh author yang sudah ada. Setelah
        # ini tidak ada SELECT nama lagi: semua resolution dilayani
        # cache, jadi satu run tidak pernah menduplikasi dirinya
        # sendiri.
        resolver.isi_cache(store.daftar_author())

        # ---------- 1. author pemilik profil ----------
        pemilik = core["authors"]
        _pastikan_nama_pemilik(pemilik.get("name"), run_id)
        pemilik_id = resolver.resolve(
            _normalisasi_nama(pemilik["name"]),
            scholar_id=pemilik.get("scholar_id"),
        )
        statistik["author_id"] = pemilik_id

        # ---------- 2. publications ----------
        publikasi_id = []
        for indeks, baris in enumerate(core["publications"]):
            _pastikan_judul(baris.get("title"), indeks, run_id)
            hasil = conn.execute(_stmt_sisip_publikasi(baris))
            publikasi_id.append(hasil.scalar_one())
        statistik["publications_inserted"] = len(publikasi_id)

        # ---------- 3. publication_authors ----------
        for tautan in core["publication_authors"]:
            nama_bersih = _normalisasi_nama(tautan.get("author_name"))
            if nama_bersih is None:
                statistik["skipped"] += 1
                continue
            indeks = tautan["publication_index"]
            # Nama yang memuat nama pemilik profil (mis.
            # "Budi Santoso, Budi A. Rahman") akan ketemu pemilik di
            # cache, karena pemilik ditulis lebih dulu.
            #
            # Scholar kadang menulis PEMILIK PROFIL dengan nama
            # pendek ("TAD Kuntjoro") yang tidak pernah cocok eksak
            # dengan nama lengkap baris pemilik. Siapa yang SEKARANG
            # memegang nama ini (bisa author tiruan, bisa None) harus
            # dibaca SEBELUM nama diganti.
            lama_id = resolver.cari(nama_bersih)
            if _bentuk_pendek(nama_bersih, pemilik.get("name")):
                nama_bersih = _normalisasi_nama(pemilik["name"])
            author_id = resolver.resolve(nama_bersih)
            # Repoint: bila nama tadi terlanjur menempel di author lain
            # (umumnya tiruan dari run sebelum fix), buang link lama
            # pada publikasi+urutan ini supaya tidak muncul dua author
            # pada urutan yang sama.
            if lama_id is not None and lama_id != author_id:
                conn.execute(_stmt_hapus_tautan(
                    publikasi_id[indeks], lama_id, tautan["author_order"]))
                statistik["publication_authors_repointed"] += 1
            conn.execute(_stmt_sisip_publication_authors(
                publikasi_id[indeks],
                author_id,
                tautan["author_order"],
            ))
            statistik["publication_authors_inserted"] += 1

        # ---------- 4. author_metrics ----------
        conn.execute(
            _stmt_sisip_author_metrics(core["author_metrics"], pemilik_id))

        # ---------- 5. publication_metrics ----------
        for indeks, baris in enumerate(core["publication_metrics"]):
            conn.execute(_stmt_sisip_publication_metrics(
                baris, publikasi_id[indeks]))
        statistik["metrics_inserted"] = 1 + len(core["publication_metrics"])

        statistik["authors_created"] = resolver.dibuat
        statistik["authors_reused"] = resolver.dipakai_ulang
        statistik["status"] = STATUS_LOADED

        trans.commit()
    except Exception:
        trans.rollback()
        raise
    finally:
        if conn_milik_sendiri:
            conn.close()

    return statistik


def load_scholar_run(envelope, scholar_id, conn=None, raw_conn=None):
    """Muat satu run envelope: raw dulu (commit), lalu core (satu transaksi).

    Ini orchestrator dua tahap. Urutannya TIDAK boleh ditukar,
    dan itu bukan sekadar soal urutan: tujuan raw adalah
    bertahan hidup ketika core gagal. Kalau keduanya satu
    transaksi, rollback core akan menghapus baris raw-nya juga,
    dan tabel raw tidak akan pernah berisi apa pun tepat pada
    saat paling dibutuhkan.

    Tahap 1 - RAW (selalu, termasuk run gagal)
    -----------------------------------------
    write_raw_scrape_result() membuka koneksi sendiri (atau
    memakai `raw_conn`), menulis envelope apa adanya ke
    raw.scholar_scrape_result, lalu COMMIT. Tidak ada kondisi
    pada tahap ini: `data` None, status BLOCKED, atau apa pun
    yang lain, satu-satunya syarat berhenti adalah kegagalan
    menulis. Run yang gagal justru yang paling perlu tercatat.

    Tahap 2 - CORE (hanya kalau `data` ada)
    ---------------------------------------
    to_core_rows() memetakan payload, lalu load_core_rows()
    menulis ke core.* dalam satu transaksi sendiri.

    Parameter
    ---------
    envelope : dict
        Run envelope 9 kunci dari scrape_scholar() (lihat
        src/ingestion/scrape_scholar_playwright.py): run_id,
        source, started_at, finished_at, status, records_fetched,
        records_failed, error_message, data. Yang dipakai file ini
        cuma run_id, finished_at, status, error_message, dan data.
    conn : Connection | None, opsional
        Koneksi untuk core. Kalau None, file ini yang membuka
        koneksi dari .env lalu menutupnya setelah selesai.
    raw_conn : Connection | None, opsional
        Koneksi untuk raw, terpisah dari `conn`. Kalau None, file
        ini yang membukanya dari .env, commit, lalu menutupnya.

        WAJIB BERBEDA dari `conn`. Satu objek untuk keduanya
        membatalkan seluruh jaminan tahap ini: commit di tahap 1
        tidak berarti apa-apa kalau rollback tahap 2 bisa menarik
        baris itu kembali. Kesalahan ini tidak akan muncul
        sebagai error, hanya sebagai data hilang tanpa jejak,
        jadi modul ini menolaknya dengan LoaderError alih-alih
        issuing warning yang mudah terlewat.
    scholar_id : str, WAJIB
        Identitas profil yang sedang di-scrape, diambil dari
        pemanggil (argumen `--id` di CLI, atau author_id dari
        daftar target di pipeline) - bukan dari isi envelope.
        Diteruskan ke write_raw_scrape_result().

    Perilaku
    --------
    - Tahap raw GAGAL: lempar LoaderError, core tidak disentuh
      sama sekali. Memuat core tanpa jaring pengaman berarti
      mengisi core dengan data yang tidak bisa diproses ulang dari
      raw. Ini keputusan yang disengaja dan bisa dibatalkan di
      pemanggil, bukan di file ini.
    - `data` None (run fatal: BLOCKED / TIMEOUT / PARSING_ERROR /
      NO_DATA) BUKAN error untuk core. Tidak ada yang bisa
      dimuat, jadi load_scholar_run mengembalikan statistik dengan
      status SKIPPED_NO_DATA dan `reason` yang menjelaskan
      kenapa - tapi baris raw-nya sudah tersimpan dan sudah
      ter-commit. Koneksi core tidak dibuka.
    - `data` ada: `finished_at` diteruskan sebagai retrieved_at ke
      kedua tabel metrics, lalu seluruhnya masuk core dalam satu
      transaksi (lihat load_core_rows).
    - Nama pemilik profil kosong, atau ada satu baris publikasi
      tanpa judul: lempar LoaderError dan rollback seluruh run
      core. Baris raw TIDAK terpengaruh - itu sudah ter-commit
      dan justru itu gunanya.
    - Authors dan publications memakai UPSERT; metrics memakai
      INSERT polos setiap run. Ringkasnya: raw-first-durable,
      upsert untuk authors+publications, append-only untuk
      metrics. Penjelasan lengkapnya ada di tiap statement.

    Mengembalikan
    -------------
    dict statistik; kunci lengkapnya dijelaskan di bagian STATISTIK.
    `raw_result_id` selalu terisi kalau tahap raw berhasil,
    termasuk untuk run yang mengembalikan SKIPPED_NO_DATA - jadi
    pemanggil bisa menautkan statistik mana pun ke baris raw-nya.

    Contoh
    -------
        envelope = scrape_scholar("8kDg_v4AAAAJ")
        statistik = load_scholar_run(envelope, scholar_id="8kDg_v4AAAAJ")
        print(statistik["status"], statistik["raw_result_id"],
              statistik["authors_created"])
    """
    envelope = envelope or {}
    data = envelope.get("data")
    run_id = envelope.get("run_id")

    # conn dan raw_conn tidak boleh objek yang sama; lihat catatan
    # panjang di docstring fungsi ini dan di docstring modul.
    if conn is not None and raw_conn is not None and conn is raw_conn:
        raise LoaderError(
            "load_scholar_run() diberi objek koneksi yang sama untuk core "
            "dan raw. Penulisan raw akan ikut ter-rollback bersama core "
            "saat core gagal, jadi durability data mentah - alasan utama "
            "tabel raw ada - HILANG. Ini ditolak, bukan sekadar "
            "diperingatkan, karena peringatan mudah terlewat dan "
            "kerugiannya diam-diam. Beri dua koneksi berbeda."
        )

    # ---------- TAHAP 1: RAW, commit sendiri, SELALU ----------
    # Tidak ada if di sini. Run yang gagal ditulis juga, karena
    # record BLOCKED / TIMEOUT / NO_DATA / PARSING_ERROR justru
    # yang paling berguna saat menelusuri kenapa scraping bermasalah.
    # Kalau tahap ini gagal, exception naik ke sini dan core tidak
    # pernah dibuka - itu perilaku yang dimaksud, bukan bug.

    # Peringatan saja, tidak menghentikan apa pun. Nilai yang
    # disimpan tetap milik pemanggil.
    _peringatan_kemiripan_scholar_id(envelope, scholar_id,
                                    "load_scholar_run()")

    raw_result_id = write_raw_scrape_result(
        envelope, raw_conn=raw_conn, scholar_id=scholar_id
    )

    # ---------- TAHAP 2: CORE, hanya kalau ada data ----------
    if data is None:
        statistik = _stats_kosong()
        statistik["run_id"] = run_id
        statistik["raw_result_id"] = raw_result_id
        statistik["status"] = STATUS_SKIPPED
        statistik["reason"] = (
            "Run fatal tanpa data (status={}, pesan={}), jadi tidak ada "
            "yang bisa dimuat ke core. Envelope sudah tersimpan di "
            "raw.scholar_scrape_result (result_id={}), jadi masih bisa "
            "ditelusuri. Koneksi core tidak dibuka."
            .format(
                envelope.get("status") or "(tidak ada)",
                envelope.get("error_message") or "(tidak ada)",
                raw_result_id if raw_result_id is not None
                else "(tidak ada)",
            )
        )
        return statistik

    core = to_core_rows(data, retrieved_at=envelope.get("finished_at"))
    statistik = load_core_rows(core, conn=conn, run_id=run_id)
    statistik["raw_result_id"] = raw_result_id
    return statistik
