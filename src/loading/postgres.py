"""
============================================================
Loading data core ke PostgreSQL (SQLAlchemy + psycopg)
============================================================

File ini mengisi tabel-tabel `raw`, `staging`, dan `core` dari run
envelope hasil scraper Google Scholar (lihat
src/ingestion/scrape_scholar_playwright.py):
    - raw.scholar_scrape_result  (sql/ddl/03_create_raw_scrape_result.sql)
    - raw.scholar_author         (sql/ddl/04_create_raw_structured.sql)
    - raw.scholar_publication    (sql/ddl/04_create_raw_structured.sql)
    - staging.stg_authors        (sql/ddl/05_create_staging_tables.sql)
    - staging.stg_publications   (sql/ddl/05_create_staging_tables.sql)
    - core.*                    (sql/ddl/02_create_core_tables.sql)

Batas tanggung jawab file ini
-----------------------------
    1. Menyimpan envelope hasil scrape apa adanya ke
       raw.scholar_scrape_result, PLUS tabel raw terstruktur
       (raw.scholar_author, raw.scholar_publication) bila `data`
       ada, dalam SATU TRANSAKSI RAW SENDIRI yang di-commit lebih
       dulu.
    2. Menyusun perintah INSERT/UPSERT dari nilai yang sudah
       dipetakan oleh mapper (src/transformation/publications.py).
    3. Menyelesaikan nama penulis dari Google Scholar menjadi
       core.authors.author_id.
    4. Menulis staging (staging.stg_authors, staging.stg_publications)
       dan seluruh core dalam SATU transaksi core yang sama.

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
       (title, publication_year) dengan NULLS NOT DISTINCT
       (sql/ddl/06) dan core.authors di-upsert pada
       lower(btrim(name)), keduanya dengan RETURNING id yang sudah
       ada, jadi publications_id / author_id TIDAK berubah dan
       relasi di tabel lain tidak rusak. Kolom non-kunci publikasi
       memakai "nilai non-NULL terakhir menang" via COALESCE
       (kecuali journal yang menang apa adanya), dan
       authors.scholar_id dijaga: NULL baru tidak boleh menimpa
       scholar_id yang sudah diketahui.

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
    - publication_authors memakai kebijakan 3j (PERUBAHAN dari
      "tidak pernah dihapus"): publikasi bersumber 'detail'
      dihapus-tulis-ulang (delete-and-rewrite), sedangkan
      publikasi bersumber 'list' yang sudah punya relasi dilewati
      supaya data detail tidak diturunkan ke daftar terpotong.
      Tautan lain di-INSERT dengan toleransi duplikat (ON CONFLICT
      DO NOTHING).
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
#
# `_rapikan_teks` ikut diimpor di sini (bukan lazy) untuk aturan
# sumber detail-dulu di write_raw_scrape_result(): publications hanya
# mengimpor stdlib, jadi import dari modul ini ke sana tidak pernah
# berputar - persis seperti to_core_rows yang sudah lebih dulu diimpor.
try:
    from src.transformation.publications import to_core_rows, _rapikan_teks
except ImportError:  # pragma: no cover - jalur alternatif
    from transformation.publications import to_core_rows, _rapikan_teks


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
    "RAW_SCHOLAR_AUTHOR",
    "RAW_SCHOLAR_PUBLICATION",
    "RAW_SCHOLAR_PUBLICATION_AUTHOR",
    "STG_AUTHORS",
    "STG_PUBLICATIONS",
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
STATUS_RAW_ONLY = "RAW_ONLY"

# Status scraper yang berarti scraping TIDAK sukses (run fatal).
# Dipakai di mode raw-only untuk membedakan "raw tersimpan" dari
# "scraping sukses": kalau status scraper masuk himpunan ini,
# statistik mengembalikan status scraper ASLI, bukan RAW_ONLY.
STATUS_SCRAPER_FATAL = frozenset({"BLOCKED", "TIMEOUT", "NO_DATA", "PARSING_ERROR"})


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
    sa.Column("publication_year", sa.Integer),
    sa.Column("publication_date", sa.Date),
    sa.Column("journal", sa.String(255)),
    sa.Column("volume", sa.String(50)),
    sa.Column("pages", sa.String(50)),
    sa.Column("issue", sa.String(50)),
    sa.Column("publisher", sa.String(255)),
    sa.Column("description", sa.Text),
    sa.Column("category", sa.String(100)),
    sa.Column("link", sa.Text),
    sa.Column("doi", sa.String(100)),
    # Kunci natural baru (sql/ddl/06): UNIQUE NULLS NOT DISTINCT
    # (title, publication_year). Tanpa fitur NULLS NOT DISTINCT,
    # semua publikasi tanpa tahun akan lolos deteksi duplikat.
    sa.UniqueConstraint(
        "title", "publication_year",
        postgresql_nulls_not_distinct=True,
        name="uq_publications_title_publication_year",
    ),
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
    # Pemilik profil (author dengan scholar_id = profil yang di-scrape)
    # yang menghasilkan baris metrik ini (sql/ddl/06). Nullable: baris
    # lama dan pemanggil tanpa profil tetap sah. Bukan kunci, jadi
    # tabel tetap insert-only.
    sa.Column(
        "profile_author_id", sa.Integer,
        sa.ForeignKey("core.authors.author_id"),
    ),
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


# ------------------------------------------------------------
# TABEL RAW TERSTRUKTUR (sql/ddl/04_create_raw_structured.sql)
# ------------------------------------------------------------
# Turunan kolom dari envelope JSONB. Ditulis dalam transaksi raw
# yang SAMA dengan RAW_SCRAPE_RESULT, jadi retry run_id yang sama
# tidak menggandakan barisnya. Cermin PERSIS migrasi 04, termasuk
# ketiadaan kolom detail_venue_label (field itu tetap hanya di
# envelope JSONB).

RAW_SCHOLAR_AUTHOR = sa.Table(
    "scholar_author",
    _META,
    sa.Column("raw_author_id", sa.Integer, primary_key=True,
              autoincrement=True),
    sa.Column(
        "result_id", sa.Integer,
        sa.ForeignKey("raw.scholar_scrape_result.result_id"),
        nullable=False,
    ),
    sa.Column("urutan", sa.Integer, nullable=False, server_default=sa.text("0")),
    sa.Column("scholar_id", sa.String(50), nullable=False),
    sa.Column("nama", sa.Text, nullable=False),
    sa.Column("h_index", sa.Integer),
    sa.Column("domain", sa.Text),
    sa.Column("batches", sa.Integer),
    sa.Column("elapsed", sa.Numeric),
    # Prodi pemilik profil dari Excel referensi yang lolos gerbang
    # verifikasi K8 (prodi_pemilik); NULL = tanpa referensi / gagal
    # verifikasi / kolom prodi kosong / baris lama sebelum migrasi 11.
    sa.Column("prodi", sa.Text),
    sa.UniqueConstraint(
        "result_id", "urutan",
        name="uq_raw_scholar_author_result_urutan",
    ),
    schema="raw",
)

RAW_SCHOLAR_PUBLICATION = sa.Table(
    "scholar_publication",
    _META,
    sa.Column("raw_publication_id", sa.Integer, primary_key=True,
              autoincrement=True),
    sa.Column(
        "result_id", sa.Integer,
        sa.ForeignKey("raw.scholar_scrape_result.result_id"),
        nullable=False,
    ),
    sa.Column("urutan", sa.Integer, nullable=False),
    sa.Column("scholar_id", sa.String(50), nullable=False),
    # ---- Field halaman daftar (mentah) ----
    sa.Column("title", sa.Text),
    sa.Column("authors", sa.Text),
    sa.Column("venue", sa.Text),
    sa.Column("extra", sa.Text),
    sa.Column("citations", sa.Text),
    sa.Column("year", sa.Text),
    sa.Column("publication_year", sa.Integer),
    # ---- Field halaman detail (mentah) ----
    sa.Column("detail_url", sa.Text),
    sa.Column("detail_title", sa.Text),
    sa.Column("detail_authors", sa.Text),
    sa.Column("detail_date", sa.Text),
    sa.Column("detail_venue", sa.Text),
    sa.Column("detail_volume", sa.Text),
    sa.Column("detail_issue", sa.Text),
    sa.Column("detail_pages", sa.Text),
    sa.Column("detail_publisher", sa.Text),
    sa.Column("detail_description", sa.Text),
    sa.Column("detail_citations", sa.Text),
    sa.Column("detail_link", sa.Text),
    sa.Column("detail_pdf_link", sa.Text),
    sa.Column("detail_doi", sa.Text),
    # ---- Field bibliografi profil (ekstraksi parser profil P2 dari
    # teks venue/extra halaman daftar; TERPISAH dari detail_* yang
    # datang dari halaman detail) ----
    sa.Column("profile_volume", sa.Text),
    sa.Column("profile_issue", sa.Text),
    sa.Column("profile_pages", sa.Text),
    # ---- Penanda kelengkapan metadata detail (turunan, P24) ----
    # 'yes' jika keempat detail_volume/detail_issue/detail_pages/
    # detail_description lengkap; 'no' jika salah satu tidak
    # lengkap. Dihitung oleh loader (_status_kelengkapan_metadata),
    # BUKAN diambil dari key baris scraper.
    sa.Column("status", sa.Text),
    sa.UniqueConstraint(
        "result_id", "urutan",
        name="uq_raw_scholar_publication_result_urutan",
    ),
    schema="raw",
)


# ------------------------------------------------------------
# TABEL RAW AUTHOR PER PUBLIKASI (sql/ddl/10_create_raw_publication_author.sql)
# ------------------------------------------------------------
# Satu baris per penulis per publikasi per run, ditulis dari record
# yang disusun bangun_record_author (src/loading/author_records.py,
# P57) dan diintegrasikan lewat loader pada G2-B. Relasi ke induknya
# memakai raw_publication_id (PK raw.scholar_publication) - persis
# id yang diresolusi lewat RETURNING / SELECT fallback di loop
# write_raw_scrape_result(). author_role (urutan tampil, K5) dan
# profile_owner_status (hasil verifikasi identitas, K6) sengaja
# TERPISAH: keduanya dimensi berbeda (K7), jadi sebuah baris BOLEH
# co_author sekaligus pemilik_profil. scholar_id nullable - hanya
# diisi untuk record pemilik_profil terverifikasi (D-3); baris lain
# NULL permanen, tanpa tebakan. UNIQUE (raw_publication_id,
# author_order) menopang ON CONFLICT DO NOTHING idempoten untuk
# retry (P56 §6e) dan MELARANG unique berbasis nama: satu orang
# boleh muncul di lebih dari satu posisi dalam satu publikasi (D-7).

RAW_SCHOLAR_PUBLICATION_AUTHOR = sa.Table(
    "scholar_publication_author",
    _META,
    sa.Column("raw_publication_author_id", sa.Integer, primary_key=True,
              autoincrement=True),
    sa.Column(
        "raw_publication_id", sa.Integer,
        sa.ForeignKey("raw.scholar_publication.raw_publication_id"),
        nullable=False,
    ),
    # Urutan tampil penulis di Scholar, 0-based (K4); bagian dari
    # UNIQUE di bawah. Sumber kebenaran urutan; tidak pernah diurutkan
    # ulang.
    sa.Column("author_order", sa.Integer, nullable=False),
    # Nama penulis PERSIS apa adanya dari sumber (K2): hasil
    # _pecah_penulis - tanpa pembalikan kata, tanpa recasing, tanpa
    # penyamaan nama Excel.
    sa.Column("author_name", sa.Text, nullable=False),
    # Peran berbasis urutan SAJA (K5): penulis_pertama (order 0) |
    # co_author (order > 0). Murni fungsi urutan; tidak memuat
    # identitas; tidak menunggu verifikasi.
    sa.Column("author_role", sa.Text, nullable=False),
    # Status kepemilikan profil hasil verifikasi identitas (K6):
    # pemilik_profil | bukan_pemilik_profil | belum_terverifikasi.
    sa.Column("profile_owner_status", sa.Text, nullable=False),
    # Scholar ID pemilik profil: HANYA diisi pada record pemilik_profil
    # terverifikasi (D-3); baris lain NULL permanen. VARCHAR(50)
    # selaras dengan kolom scholar_id di tabel raw lain.
    sa.Column("scholar_id", sa.String(50)),
    # Asal daftar penulis (D-1): "detail" = halaman detail (lengkap),
    # "daftar" = kolom authors halaman profil (bisa terpotong).
    sa.Column("sumber", sa.Text, nullable=False),
    # TRUE hanya bila sumber "daftar" dan teks daftar mengandung
    # elipsis pemotongan Scholar (D-1): penanda KETERBATASAN daftar,
    # bukan bukti nama hilang (K12).
    sa.Column("daftar_terpotong", sa.Boolean, nullable=False,
              server_default=sa.text("FALSE")),
    # Waktu penulisan baris, mengikuti konvensi kolom waktu RAW.
    sa.Column("created_at", sa.DateTime, nullable=False,
              server_default=sa.text("now()")),
    sa.UniqueConstraint(
        "raw_publication_id", "author_order",
        name="uq_raw_scholar_publication_author_pub_urutan",
    ),
    schema="raw",
)


# ------------------------------------------------------------
# TABEL STAGING (sql/ddl/05_create_staging_tables.sql)
# ------------------------------------------------------------
# Nilai yang sudah dibersihkan, ditulis dalam transaksi core yang
# SAMA dengan penulisan core.*. Berfungsi sebagai jejak audit.

STG_AUTHORS = sa.Table(
    "stg_authors",
    _META,
    sa.Column("stg_author_id", sa.Integer, primary_key=True,
              autoincrement=True),
    sa.Column("result_id", sa.Integer, nullable=False),
    sa.Column("urutan", sa.Integer, nullable=False, server_default=sa.text("0")),
    sa.Column("scholar_id", sa.String(50), nullable=False),
    sa.Column("nama", sa.Text, nullable=False),
    sa.Column("h_index", sa.Integer),
    sa.Column("domain", sa.Text),
    sa.Column("batches", sa.Integer),
    sa.Column("elapsed", sa.Numeric),
    sa.UniqueConstraint(
        "result_id", "urutan",
        name="uq_stg_authors_result_urutan",
    ),
    schema="staging",
)

STG_PUBLICATIONS = sa.Table(
    "stg_publications",
    _META,
    sa.Column("stg_publication_id", sa.Integer, primary_key=True,
              autoincrement=True),
    sa.Column("result_id", sa.Integer, nullable=False),
    sa.Column("urutan", sa.Integer, nullable=False),
    sa.Column("scholar_id", sa.String(50), nullable=False),
    sa.Column("title", sa.Text, nullable=False),
    sa.Column("publication_year", sa.Integer),
    sa.Column("publication_date", sa.Date),
    sa.Column("journal", sa.String(255)),
    sa.Column("volume", sa.String(50)),
    sa.Column("pages", sa.String(50)),
    sa.Column("issue", sa.String(50)),
    sa.Column("publisher", sa.String(255)),
    sa.Column("description", sa.Text),
    sa.Column("category", sa.String(100)),
    sa.Column("link", sa.Text),
    sa.Column("doi", sa.String(100)),
    sa.Column("authors", sa.Text),
    sa.Column("author_source", sa.String(10)),
    sa.UniqueConstraint(
        "result_id", "urutan",
        name="uq_stg_publications_result_urutan",
    ),
    schema="staging",
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


def _varian_nama_pemilik(nama, lengkap):
    """True kalau `nama` adalah varian nama pemilik profil yang dipotong per kata.

    Halaman DETAIL Google Scholar kadang menulis pemilik profil
    dengan nama yang dipotong di tengah: "Tri Agus D Kuntjoro"
    untuk "Tri Agus Djoko Kuntjoro" (lihat kejadian nyata: tautan
    publikasi 48|67 hilang dan author tiruan 3315 tercipta setelah
    rewrite kebijakan 3j). Bentuk ini TIDAK pernah cocok eksak
    dengan baris pemilik, dan _bentuk_pendek() juga menolaknya
    karena jumlah katanya sama (bukan inisial pendek). Resolver
    lalu membuat author tiruan untuk orang yang sama.

    Fungsi ini melengkapi _bentuk_pendek() KHUSUS untuk jalur
    daftar penulis halaman detail (lihat pemakaian di
    load_core_rows). _bentuk_pendek() tetap dipertahankan apa
    adanya untuk jalur daftar (dan untuk run tanpa detail).

    Syaratnya ketat - SEMUA harus terpenuhi:

      1. Kedua nama tidak None dan tidak kosong.
      2. Jumlah kata `nama` SAMA dengan jumlah kata `lengkap`.
         Tanpa ini, nama pendek berjumlah kata berbeda (mis.
         "TAD Kuntjoro") ikut tertangkap padahal sudah menjadi
         urusan _bentuk_pendek().
      3. Kata terakhir `nama` sama (casefold) dengan kata terakhir
         `lengkap`. Tanpa ini, "Tri Agus D Santoso" ikut menempel
         ke pemilik yang belakangnya "Kuntjoro".
      4. Setiap kata `nama` SEBELUM belakang adalah AWALAN
         (case-insensitive) dari kata `lengkap` pada posisi yang
         SAMA: "D" cocok dengan "Djoko", tapi "Budi" tidak cocok
         dengan "Agus" dan "Z" tidak cocok dengan "Djoko".
      5. `nama` casefold-nya BERBEDA dari `lengkap`. Nama lengkap
         tidak membutuhkan aturan ini; resolve biasa sudah
         menemukannya lewat cache.

    Yang menentukan benar-salahnya HANYA nama pemilik profil run
    ini (argumen `lengkap`), bukan author lain. Itu yang mencegah
    co-author berbeda orang tapi sama belakang ikut tertaut.

    Mengembalikan True/False; tidak pernah melempar untuk input
    yang buruk (None, kosong, whitespace) - semuanya cukup False.
    """
    panjang = _normalisasi_nama(lengkap)
    nama_bersih = _normalisasi_nama(nama)
    if not panjang or not nama_bersih:
        return False

    kata_panjang = panjang.split()
    kata_nama = nama_bersih.split()

    # Syarat 2: jumlah kata harus sama.
    if len(kata_nama) != len(kata_panjang):
        return False
    # Syarat 5: nama yang sama tidak perlu penerjemahan.
    if panjang.casefold() == nama_bersih.casefold():
        return False
    # Syarat 3: belakangnya harus sama.
    if kata_panjang[-1].casefold() != kata_nama[-1].casefold():
        return False

    # Syarat 4: tiap kata sebelum belakang adalah awalan kata
    # yang bersesuaian di nama lengkap.
    for kiri, kanan in zip(kata_nama[:-1], kata_panjang[:-1]):
        if not kanan.casefold().startswith(kiri.casefold()):
            return False
    return True


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
    "title", "publication_year", "publication_date", "journal",
    "volume", "pages", "issue", "publisher", "description",
    "category", "link", "doi",
)
# Kolom data baris mentah raw.scholar_publication. Tidak memuat
# detail_venue_label: field itu hanya hidup di envelope JSONB.
# Kolom terakhir, "status", adalah kolom TURUNAN (P24): nilainya
# dihitung dari kelengkapan field detail oleh
# _status_kelengkapan_metadata, bukan diambil dari key baris.
KOLOM_RAW_PUBLICATION = (
    "title", "authors", "venue", "extra", "citations", "year",
    "publication_year", "detail_url", "detail_title", "detail_authors",
    "detail_date", "detail_venue", "detail_volume", "detail_issue",
    "detail_pages", "detail_publisher", "detail_description",
    "detail_citations", "detail_link", "detail_pdf_link", "detail_doi",
    "profile_volume", "profile_issue", "profile_pages", "status",
)
# Nama kolom RAW yang nilainya diambil dari key row BERBEDA (parser
# profil P2 memakai "volume"/"issue"/"pages"; kolom RAW diberi
# awalan profile_ agar asal metadata jelas dan tidak bercampur
# dengan detail_* dari halaman detail).
SUMBER_KOLOM_RAW_PUBLICATION = {
    "profile_volume": "volume",
    "profile_issue": "issue",
    "profile_pages": "pages",
}
# Kolom data baris staging.stg_publications. Dua kolom terakhir
# (authors, author_source) ditulis eksplisit, bukan dari baris.
KOLOM_STG_PUBLICATION = (
    "title", "publication_year", "publication_date", "journal",
    "volume", "pages", "issue", "publisher", "description",
    "category", "link", "doi", "authors", "author_source",
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


def _stmt_sisip_raw_scholar_author(result_id, urutan, scholar_id, nama,
                                   h_index, domain, batches, elapsed,
                                   prodi=None):
    """INSERT INTO raw.scholar_author ... RETURNING raw_author_id.

    Satu baris data profil per run (urutan = 0). ON CONFLICT
    (result_id, urutan) DO NOTHING: retry run_id yang sama tidak
    menggandakan baris. Ditulis dalam transaksi raw yang sama dengan
    envelope. `prodi` = hasil prodi_pemilik() (Excel referensi yang
    lolos verifikasi K8) atau None -> NULL; tanpa default di
    pemanggilan loader, selalu dihitung di write_raw_scrape_result.
    """
    stmt = pg.insert(RAW_SCHOLAR_AUTHOR).values(
        result_id=result_id,
        urutan=urutan,
        scholar_id=scholar_id,
        nama=nama,
        h_index=h_index,
        domain=domain,
        batches=batches,
        elapsed=elapsed,
        prodi=prodi,
    )
    return stmt.on_conflict_do_nothing(
        index_elements=["result_id", "urutan"],
    ).returning(RAW_SCHOLAR_AUTHOR.c.raw_author_id)


def _status_kelengkapan_metadata(baris):
    """Hitung penanda kelengkapan metadata bibliografi halaman detail.

    'yes' hanya jika KEEMPAT field detail_volume, detail_issue,
    detail_pages, dan detail_description semuanya terisi: non-NULL,
    non-string kosong, dan non-whitespace-only (whitespace hanya
    DIDETEKSI lewat .strip(); nilai asli baris TIDAK diubah).
    Selain itu - termasuk key hilang (baris.get) - hasilnya 'no'.

    Hanya membaca empat key tersebut; tidak menyentuh metadata asli
    dan tidak melihat profile_* (metadata profil P2 dari halaman
    daftar tidak boleh memengaruhi status ini).
    """
    for kunci in ("detail_volume", "detail_issue", "detail_pages",
                  "detail_description"):
        nilai = baris.get(kunci)
        if nilai is None or (isinstance(nilai, str)
                             and not nilai.strip()):
            return "no"
    return "yes"


def _stmt_sisip_raw_scholar_publication(result_id, urutan, scholar_id, baris):
    """INSERT INTO raw.scholar_publication ... RETURNING raw_publication_id.

    Nilai tiap kolom data diambil baris.get(kolom) untuk
    KOLOM_RAW_PUBLICATION - kecuali kolom profile_volume/profile_issue/
    profile_pages, yang diambil dari key profil volume/issue/pages
    (lihat SUMBER_KOLOM_RAW_PUBLICATION), dan kolom status, yang
    DIHITUNG dari kelengkapan field detail lewat
    _status_kelengkapan_metadata (key "status" pada baris, kalau ada,
    di-override agar nilai selalu konsisten dengan metadata).
    Semua disimpan MENTAH apa adanya; kolom yang tidak ada di sumber
    menjadi NULL. ON CONFLICT (result_id, urutan) DO NOTHING.
    """
    nilai = {kolom: baris.get(SUMBER_KOLOM_RAW_PUBLICATION.get(kolom, kolom))
             for kolom in KOLOM_RAW_PUBLICATION}
    # Status dihitung ulang dari baris; jangan pernah memakai key
    # "status" dari scraper.
    nilai["status"] = _status_kelengkapan_metadata(baris)
    stmt = pg.insert(RAW_SCHOLAR_PUBLICATION).values(
        result_id=result_id,
        urutan=urutan,
        scholar_id=scholar_id,
        **nilai,
    )
    return stmt.on_conflict_do_nothing(
        index_elements=["result_id", "urutan"],
    ).returning(RAW_SCHOLAR_PUBLICATION.c.raw_publication_id)


def _stmt_sisip_raw_publication_author(raw_publication_id, author_order,
                                       author_name, author_role, scholar_id,
                                       profile_owner_status, sumber,
                                       daftar_terpotong):
    """INSERT INTO raw.scholar_publication_author ... RETURNING id.

    Satu baris per penulis per publikasi per run, ditulis di dalam
    TRANSAKSI raw yang sama dengan envelope dan publikasi induknya
    (G2-B, D2). Semua nilai diambil APA ADANYA dari record hasil
    bangun_record_author (P57) plus raw_publication_id yang sudah
    diresolusi pemanggil (D1) - tidak ada rekayasa nama, tidak ada
    tebakan identitas, dan scholar_id BOLEH NULL (record yang belum
    terverifikasi tidak pernah diberi id karangan, D-3).

    Idempoten lewat ON CONFLICT (raw_publication_id, author_order)
    DO NOTHING - kunci UNIQUE persis seperti yang didefinisikan di
    sql/ddl/10 (P56 §6e): retry yang mengulang run tidak pernah
    menggandakan baris, dan TIDAK MENGUBAH apa pun (tabel RAW
    append-per-run, tanpa UPDATE). RETURNING
    raw_publication_author_id membedakan "baris tertulis" (id)
    dari "konflik - baris lama sudah ada" (None); keduanya bukan
    error, kecuali pemanggil memutuskan lain.
    """
    stmt = pg.insert(RAW_SCHOLAR_PUBLICATION_AUTHOR).values(
        raw_publication_id=raw_publication_id,
        author_order=author_order,
        author_name=author_name,
        author_role=author_role,
        scholar_id=scholar_id,
        profile_owner_status=profile_owner_status,
        sumber=sumber,
        daftar_terpotong=daftar_terpotong,
    )
    return stmt.on_conflict_do_nothing(
        index_elements=["raw_publication_id", "author_order"],
    ).returning(RAW_SCHOLAR_PUBLICATION_AUTHOR.c.raw_publication_author_id)


def _stmt_sisip_stg_author(result_id, urutan, scholar_id, nama,
                           h_index, domain, batches, elapsed):
    """INSERT INTO staging.stg_authors ... ON CONFLICT DO NOTHING.

    Menyimpan data profil yang sudah dibersihkan sebagai jejak audit,
    dalam transaksi core yang sama. ON CONFLICT (result_id, urutan)
    membuatnya idempoten.
    """
    stmt = pg.insert(STG_AUTHORS).values(
        result_id=result_id,
        urutan=urutan,
        scholar_id=scholar_id,
        nama=nama,
        h_index=h_index,
        domain=domain,
        batches=batches,
        elapsed=elapsed,
    )
    return stmt.on_conflict_do_nothing(index_elements=["result_id", "urutan"])


def _stmt_sisip_stg_publication(result_id, urutan, scholar_id, baris,
                                authors_teks):
    """INSERT INTO staging.stg_publications ... ON CONFLICT DO NOTHING.

    nilai tiap kolom diambil baris.get(kolom) untuk
    KOLOM_STG_PUBLICATION, lalu authors diisi authors_teks (gabungan
    nama penulis yang sudah diresolusi) dan author_source diambil
    dari baris. publication_date dikonversi ke date karena kolomnya
    DATE. ON CONFLICT (result_id, urutan) DO NOTHING.
    """
    nilai = {kolom: baris.get(kolom) for kolom in KOLOM_STG_PUBLICATION}
    nilai["authors"] = authors_teks
    nilai["author_source"] = baris.get("author_source")
    nilai["publication_date"] = _ke_tanggal(nilai["publication_date"])
    stmt = pg.insert(STG_PUBLICATIONS).values(
        result_id=result_id,
        urutan=urutan,
        scholar_id=scholar_id,
        **nilai,
    )
    return stmt.on_conflict_do_nothing(index_elements=["result_id", "urutan"])


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

    Natural key: (title, publication_year), dengan batasan UNIQUE
    NULLS NOT DISTINCT dari sql/ddl/06. NULLS NOT DISTINCT itu yang
    membuat UPSERT ini benar: tanpa itu, semua publikasi tanpa tahun
    (mis. "in press") akan lolos sebagai "berbeda" dan tidak pernah
    terdeteksi duplikat.

    DO UPDATE TIDAK menyentuh publications_id, jadi id yang
    dikembalikan pada kasus konflik adalah id yang sudah
    dipakai publication_authors dan publication_metrics dari
    run sebelumnya. Di sinilah nilai RETURNING itu menentukan:
    kalau pemanggil memakai id baru padahal barisnya sebenarnya
    baris lama, semua tautan di run ini akan menunjuk publikasi
    yang salah - dan tidak akan ada error, hanya relasi keliru.

    PERUBAHAN PERILAKU (rencana 3e): kolom enrichment TIDAK LAGI
    "run terakhir menang" polos, tapi "nilai non-NULL terakhir
    menang". Setiap kolom enrichment memakai
    COALESCE(EXCLUDED.<kolom>, core.publications.<kolom>), sehingga
    nilai NULL dari run terbaru TIDAK menghapus nilai yang sudah
    terisi dari run sebelumnya (mis. publisher/description/issue
    yang hanya ada di halaman detail run lampau, sementara run
    terbaru tanpa detail mengirim NULL). journal tetap "run terakhir
    menang" polos karena venue daftar selalu ada di sumber.
    title dan publication_year adalah natural key, jadi keduanya
    TIDAK PERNAH di-set saat konflik.
    """
    nilai = {kolom: baris.get(kolom) for kolom in KOLOM_PUBLICATIONS}
    nilai["publication_date"] = _ke_tanggal(nilai["publication_date"])

    stmt = pg.insert(PUBLICATIONS).values(**nilai)
    # Kolom yang boleh ditimpa saat konflik. Natural key-nya sendiri
    # (title, publication_year) jelas tidak boleh, karena kolom
    # itulah yang membuat kedua baris dianggap sama. journal menang
    # apa adanya; kolom enrichment memakai COALESCE supaya NULL baru
    # tidak menghapus nilai lama (aturan 3e).
    kolom_coalesce = (
        "publication_date", "volume", "pages", "issue", "publisher",
        "description", "category", "link", "doi",
    )
    set_: dict = {
        kolom: sa.func.coalesce(
            stmt.excluded[kolom], PUBLICATIONS.c[kolom]
        )
        for kolom in kolom_coalesce
    }
    set_["journal"] = stmt.excluded["journal"]
    return stmt.on_conflict_do_update(
        index_elements=["title", "publication_year"],
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


def _stmt_hapus_semua_tautan(publication_id):
    """DELETE FROM core.publication_authors untuk SATU publikasi.

    Dipakai kebijakan 3j untuk sumber 'detail': sebelum menulis ulang
    seluruh tautan penulis publikasi itu, semua tautan lamanya
    (termasuk author_id tiruan yang mungkin menempel) dibuang lebih
    dulu, sehingga hasilnya cermin data otoritatif halaman detail.
    Tidak menyentuh core.authors dan tidak menyentuh publikasi lain.
    """
    return sa.delete(PUBLICATION_AUTHORS).where(
        PUBLICATION_AUTHORS.c.publication_id == publication_id,
    )


def _stmt_ada_tautan(publication_id):
    """SELECT EXISTS tautan penulis untuk satu publikasi.

    Dipakai kebijakan 3j untuk sumber 'list': kalau publikasi sudah
    punya relasi (dari run sebelumnya yang memakai halaman detail),
    tautan daftar yang terpotong TIDAK BOLEH menurunkan mutunya.
    Query SELECT murni, tidak mengubah apa pun.
    """
    return sa.select(
        sa.exists().where(
            PUBLICATION_AUTHORS.c.publication_id == publication_id
        )
    )


def _stmt_author_yatim():
    """SELECT author_id, name FROM core.authors yang tidak terpakai.

    Kandidat author yatim: tidak punya relasi di
    core.publication_authors, tidak punya baris di
    core.author_metrics, dan scholar_id-nya NULL (bukan pemilik
    profil). INI HANYA QUERY SELECT, BUKAN DELETE: pembersihan
    diputuskan user nanti (rencana 3e), bukan dijalankan otomatis
    oleh loader.
    """
    return sa.select(AUTHORS.c.author_id, AUTHORS.c.name).where(
        ~sa.exists().where(
            PUBLICATION_AUTHORS.c.author_id == AUTHORS.c.author_id
        ),
        ~sa.exists().where(
            AUTHOR_METRICS.c.author_id == AUTHORS.c.author_id
        ),
        AUTHORS.c.scholar_id.is_(None),
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


def _stmt_sisip_publication_metrics(baris, publication_id,
                                    profile_author_id=None):
    """INSERT INTO core.publication_metrics.

    publication_id diisi dari hasil load publications, bukan dari
    isi baris mapper (yang publication_id-nya masih None).
    profile_author_id (kolom baru migrasi 06) adalah author_id pemilik
    profil run ini, penanda asal data - boleh None untuk pemanggil
    tanpa profil.

    INSERT polos, TETAP APPEND-ONLY. Lihat blok peringatan di atas;
    JANGAN menambahkan ON CONFLICT ke statement ini.
    """
    return sa.insert(PUBLICATION_METRICS).values(
        publication_id=publication_id,
        source=baris.get("source"),
        citation_count=baris.get("citation_count"),
        index_name=baris.get("index_name"),
        score=baris.get("score"),
        retrieved_at=_ke_waktu(baris.get("retrieved_at")),
        profile_author_id=profile_author_id,
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
#   publication_authors_deleted  jumlah tautan lama yang DIHAPUS karena
#                  publikasi bersumber 'detail' ditulis ulang (kebijakan 3j)
#   publication_authors_skipped_downgrade  jumlah publikasi bersumber 'list'
#                  yang tautannya DILEWATI karena sudah punya relasi, agar
#                  data detail tidak diturunkan ke daftar terpotong
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
#   - publication_authors_deleted menghitung tautan lama yang
#     dihapus borongan untuk publikasi bersumber 'detail' sebelum
#     ditulis ulang (kebijakan 3j). Nilainya rowcount DELETE dari
#     database.
#   - publication_authors_skipped_downgrade menghitung publikasi
#     bersumber 'list' yang sudah punya relasi sehingga seluruh
#     tautannya dilewati. Satu hitungan per publikasi, bukan per
#     tautan.


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
        "publication_authors_deleted": 0,
        "publication_authors_skipped_downgrade": 0,
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
                            scraped_at=None, baris_referensi=None,
                            keluaran_author=None):
    """Tulis satu run envelope ke raw, lalu COMMIT.

    Ini adalah LANGKAH PERTAMA dari pipeline dan selalu dijalankan,
    termasuk untuk run yang gagal. Bentuk envelope apa adanya
    disimpan ke kolom JSONB raw_envelope: field mentah Scholar yang
    tidak dipetakan ke core (venue, extra, failures, domain,
    batches, elapsed) tetap ada di sana, sehingga mapper yang
    berubah atau salah masih punya sumber untuk diulang.

    Selain envelope, run yang punya `data` juga ditulis ke tabel raw
    TERSTRUKTUR raw.scholar_author, raw.scholar_publication, dan
    raw.scholar_publication_author (sql/ddl/04 dan sql/ddl/10).
    Keempatnya ditulis dalam TRANSAKSI YANG SAMA, jadi commit/rollback
    berlaku serentak: tidak ada envelope tanpa tabel terstrukturnya,
    atau sebaliknya. Run fatal (data None) hanya menghasilkan baris
    envelope.

    Sejak G2-B record author per publikasi (helper murni
    bangun_record_author dari src/loading/author_records.py, P57)
    TIDAK LAGI dihitung tanpa disimpan: record ditulis ke
    raw.scholar_publication_author di dalam transaksi yang sama,
    memakai raw_publication_id hasil RETURNING INSERT publikasi
    (dengan SELECT fallback untuk retry yang kena ON CONFLICT).
    Kegagalan apa pun di tahap author ikut me-rollback SELURUH
    transaksi raw - envelope dan tabel terstruktur tidak pernah
    setengah tertulis. Statistiknya keluar lewat `keluaran_author`;
    penyimpanan_author di situ bernilai "tersimpan" (blok penulisan
    terstruktur berjalan) atau "tidak_disimpan" (dilewati: run fatal
    atau driver tidak mengembalikan result_id).

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
    baris_referensi : list[dict] | None, opsional
        Daftar baris referensi Excel berisi kunci `no`, `nama`,
        `scholar_id` - bentuk persis keluaran baca_daftar di
        src/pipeline/run_batch.py - untuk GERBANG verifikasi
        pemilik profil terhadap nama profil envelope. None
        (default) berarti verifikasi gagal total: semua record
        jadi belum_terverifikasi, tanpa satu pun pemilik_profil
        (lihat author_records.verifikasi_pemilik_profil). File
        ini TIDAK membaca Excel sendiri; pemanggil yang
        memuatkannya.
    keluaran_author : dict | None, opsional
        Wadah opsional untuk hasil komputasi + penulisan author. Kalau
        diberikan sebuah dict, diisi dua kunci:
        "records"  = list-of-lists, satu list record dict per baris
                     publikasi (mungkin kosong), urutan sama dengan
                     data["rows"]; dan
        "statistik" = dict Sembilan kunci statistik author:
            author_total               int, seluruh record
            author_pemilik_profil      jumlah status pemilik_profil
            author_bukan_pemilik       jumlah bukan_pemilik_profil
            author_belum_terverifikasi jumlah belum_terverifikasi
            publikasi_daftar_terpotong int, baris yang salah satu
                                       recordnya daftar_terpotong
            publikasi_tanpa_penulis    int, baris tanpa satu pun
                                       record (daftar kosong)
            author_disimpan            int, baris INSERT author yang
                                       RETURNING id-nya terisi (baris
                                       benar-benar tertulis di run ini)
            author_dilewati_konflik    int, baris INSERT author yang
                                       kena ON CONFLICT DO NOTHING
                                       (baris lama sudah ada; konflik
                                       yang DIHARAPKAN untuk retry,
                                       bukan error)
            penyimpanan_author         "tersimpan" - blok penulisan
                                       terstruktur BERJALAN (data
                                       ada dan result_id terbaca) dan
                                       loop INSERT author dieksekusi;
                                       atau "tidak_disimpan" -
                                       penulisan terstruktur DILEWATI
                                       (data None / result_id None):
                                       record dihitung (kalau ada)
                                       tapi tidak disimpan, kedua
                                       hitungan di atas 0.
        Dict statistik yang sama dirujuk sejak sebelum transaksi
        dibuka; nilainya dilengkapi DI DALAM transaksi, jadi kalau
        transaksi gagal -> rollback -> exception naik, dan pemanggil
        TIDAK PERNAH menerima statistik yang mengklaim baris
        ter-rollback sebagai "tersimpan".
        None (default) = hasil dibuang, fungsi berperilaku persis
        seperti sebelum ada komputasi author ini.

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

    # Validasi scholar_id dipindah ke SEBELUM transaksi dibuka (dulu
    # ada di dalam blok try transaksi) demi dua hal: (1) komputasi
    # murni record author di bawah membutuhkan id yang sudah bersih;
    # (2) menolak input yang jelas salah sebelum ada transaksi yang
    # perlu dibuka lalu di-rollback. Untuk scholar_id yang sah,
    # nilai dan perilakunya persis sama dengan sebelumnya.
    scholar_id_bersih = _scholar_id_raw(
        scholar_id, "write_raw_scrape_result()")

    # ---------- Komputasi MURNI record author (P58, G2-B) ----------
    # Dihitung di luar transaksi sebagai dict/list biasa, lalu record
    # per baris DISIMPAN ke raw.scholar_publication_author di dalam
    # blok transaksi di bawah (lihat D1-D4 pada catatan loop): id
    # publikasi dari RETURNING INSERT publikasi (SELECT fallback untuk
    # retry), INSERT author idempoten ON CONFLICT DO NOTHING, dan
    # statistik penyimpanan yang sebenarnya melengkapi dict yang sama.
    # Dict statistik_author di bawah adalah objek YANG SAMA dengan
    # keluaran_author["statistik"] - nilai author_disimpan /
    # author_dilewati_konflik / penyimpanan_author dilengkapi di dalam
    # transaksi, jangan pernah mengganti referensinya.
    #
    # Import lazy di dalam fungsi dengan sengaja:
    # src.loading.author_records mengimpor _kunci_nama dkk. dari
    # modul ini, jadi import di tingkat modul akan berputar
    # (circular import).
    from src.loading.author_records import (
        bangun_record_author, prodi_pemilik, PEMILIK, BUKAN,
        SUMBER_DAFTAR, SUMBER_DETAIL,
    )

    data_author = envelope.get("data") or {}
    records_per_baris = []
    jumlah_total = jumlah_pemilik = jumlah_bukan = jumlah_belum = 0
    jumlah_terpotong = 0
    jumlah_tanpa_penulis = 0
    for baris in data_author.get("rows") or []:
        # Aturan sumber detail-dulu yang SAMA dengan to_core_rows()
        # (src/transformation/publications.py): detail_authors
        # halaman detail otoritatif kalau terisi; selain itu fallback
        # ke daftar kolom authors halaman profil (bisa terpotong).
        if _rapikan_teks(baris.get("detail_authors")):
            sumber = SUMBER_DETAIL
            teks_daftar = baris.get("detail_authors")
        else:
            sumber = SUMBER_DAFTAR
            teks_daftar = baris.get("authors")
        rekaman = bangun_record_author(
            teks_daftar,
            sumber,
            nama_profil=data_author.get("name"),
            baris_referensi=baris_referensi,
            scholar_id_run=scholar_id_bersih,
        )
        records_per_baris.append(rekaman)
        jumlah_total += len(rekaman)
        for r in rekaman:
            status_r = r["profile_owner_status"]
            if status_r == PEMILIK:
                jumlah_pemilik += 1
            elif status_r == BUKAN:
                jumlah_bukan += 1
            else:
                jumlah_belum += 1
        if any(r["daftar_terpotong"] for r in rekaman):
            jumlah_terpotong += 1
        if not rekaman:
            jumlah_tanpa_penulis += 1

    # dict campuran (int + string status): tanpa parameter tipe.
    statistik_author: dict = {
        "author_total": jumlah_total,
        "author_pemilik_profil": jumlah_pemilik,
        "author_bukan_pemilik": jumlah_bukan,
        "author_belum_terverifikasi": jumlah_belum,
        "publikasi_daftar_terpotong": jumlah_terpotong,
        "publikasi_tanpa_penulis": jumlah_tanpa_penulis,
        # Dua hitungan PENYIMPANAN (G2-B, D4): diisi/diubah HANYA di
        # dalam blok transaksi di bawah oleh loop INSERT author -
        # bukan oleh komputasi murni di atas. Nol di sini berarti
        # "belum ada yang dieksekusi", bukan klaim hasil.
        "author_disimpan": 0,
        "author_dilewati_konflik": 0,
        # Penanda status penyimpanan: TIDAK ditebak di sini. Diisi di
        # dalam transaksi - "tersimpan" setelah loop INSERT author
        # selesai dieksekusi, atau "tidak_disimpan" bila blok
        # penulisan terstruktur dilewati (data None / result_id None).
        # Ketiadaan kunci ini pada dict yang bocor lewat
        # keluaran_author berarti transaksi gagal di tengah jalan dan
        # exception-nya sudah naik; pemanggil tidak pernah menerima
        # klaim "tersimpan" untuk baris yang ter-rollback.
    }
    if isinstance(keluaran_author, dict):
        keluaran_author["records"] = records_per_baris
        keluaran_author["statistik"] = statistik_author

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
        # scholar_id_bersih sudah divalidasi dan dibersihkan di atas,
        # SEBELUM transaksi dibuka (lihat catatan di sana); di sini
        # hanya dipakai ulang.
        hasil = raw_conn.execute(_stmt_sisip_raw_scrape_result(
            scholar_id=scholar_id_bersih,
            scraped_at=scraped_at if scraped_at is not None else _utc_naif(),
            run_id=_ke_uuid(envelope.get("run_id")),
            status=envelope.get("status"),
            envelope=envelope,
        ))
        result_id = hasil.scalar_one_or_none()

        # Tabel raw TERSTRUKTUR ditulis dalam transaksi yang SAMA
        # dengan envelope, sebelum commit. Kegagalan di sini otomatis
        # me-rollback seluruh transaksi raw lewat except di bawah,
        # sehingga envelope dan tabel terstruktur tidak pernah
        # setengah tertulis. Dilewati kalau run fatal (data None) atau
        # driver tidak mengembalikan result_id.
        data = envelope.get("data")
        if data is not None and result_id is not None:
            # Prodi pemilik profil: hanya dari Excel referensi yang
            # lolos gerbang verifikasi K8 (id eksak + nama cocok);
            # selain itu None -> NULL, tanpa menebak. Dihitung di
            # sini (bukan di blok komputasi murni) karena bergantung
            # pada data envelope yang baru divalidasi di atas.
            prodi_profil = prodi_pemilik(
                baris_referensi, scholar_id_bersih, data.get("name"))
            raw_conn.execute(_stmt_sisip_raw_scholar_author(
                result_id=result_id,
                urutan=0,
                scholar_id=scholar_id_bersih,
                nama=data.get("name"),
                h_index=data.get("h_index"),
                domain=data.get("domain"),
                batches=data.get("batches"),
                elapsed=data.get("elapsed"),
                prodi=prodi_profil,
            ))
            for i, baris in enumerate(data.get("rows") or []):
                # ---------- D1: resolusi raw_publication_id ----------
                # INSERT publikasi mengembalikan id barisnya lewat
                # RETURNING. Kalau None, ON CONFLICT DO NOTHING kena
                # baris LAMA (retry run yang sama) - publikasi tidak
                # ditulis ulang, tapi author-nya tetap boleh idempoten
                # terhadap baris lama itu, jadi id-nya dicari lewat
                # SELECT dengan kunci yang PERSIS dipakai konflik
                # (result_id + urutan). Tidak ada tebakan id, tidak ada
                # LASTVAL, tidak ada pencarian berdasarkan judul, dan
                # deduplikasi publikasi yang ada TIDAK diubah.
                hasil_pub = raw_conn.execute(
                    _stmt_sisip_raw_scholar_publication(
                        result_id=result_id,
                        urutan=i,
                        scholar_id=scholar_id_bersih,
                        baris=baris,
                    ))
                pid = hasil_pub.scalar_one_or_none()
                if pid is None:
                    pid = raw_conn.execute(
                        sa.select(
                            RAW_SCHOLAR_PUBLICATION.c.raw_publication_id
                        ).where(
                            RAW_SCHOLAR_PUBLICATION.c.result_id == result_id,
                            RAW_SCHOLAR_PUBLICATION.c.urutan == i,
                        )
                    ).scalar_one_or_none()
                if pid is None:
                    # Baris publikasi yang tidak bisa diresolusi id-nya
                    # = data inkonsisten yang tidak boleh ditebak.
                    # LoaderError ini naik ke except di bawah: rollback
                    # total seluruh transaksi raw.
                    raise LoaderError(
                        "write_raw_scrape_result(): id publikasi baris {} "
                        "(result_id={}) tidak bisa diresolusi - RETURNING "
                        "INSERT kosong dan SELECT fallback dengan kunci "
                        "(result_id, urutan) tidak menemukan baris apa pun. "
                        "Tanpa raw_publication_id, record author baris ini "
                        "tidak punya induk yang benar, jadi seluruh "
                        "transaksi raw dibatalkan, bukan ditebak."
                        .format(i, result_id)
                    )

                # ---------- D3: INSERT author idempoten ----------
                # Satu execute per record hasil helper P57; list kosong
                # (publikasi tanpa penulis) dilewati tanpa error. Semua
                # kolom diisi persis dari record + pid di atas;
                # scholar_id boleh None (belum terverifikasi) dan tidak
                # pernah diganti dengan id karangan (D-3).
                for rekaman in records_per_baris[i]:
                    hasil_auth = raw_conn.execute(
                        _stmt_sisip_raw_publication_author(
                            raw_publication_id=pid,
                            author_order=rekaman["author_order"],
                            author_name=rekaman["author_name"],
                            author_role=rekaman["author_role"],
                            scholar_id=rekaman["scholar_id"],
                            profile_owner_status=rekaman[
                                "profile_owner_status"],
                            sumber=rekaman["sumber"],
                            daftar_terpotong=rekaman["daftar_terpotong"],
                        ))
                    # None = ON CONFLICT DO NOTHING kena baris lama:
                    # konflik yang DIHARAPKAN untuk retry, bukan error.
                    if hasil_auth.scalar_one_or_none() is None:
                        statistik_author["author_dilewati_konflik"] += 1
                    else:
                        statistik_author["author_disimpan"] += 1
            # D4: status penyimpanan yang SEBENARNYA, diletakkan di
            # akhir blok (bukan di awal) supaya kegagalan di tengah
            # loop tidak pernah meninggalkan klaim "tersimpan" pada
            # dict yang bocor lewat keluaran_author.
            statistik_author["penyimpanan_author"] = "tersimpan"
        else:
            # Penulisan terstruktur dilewati (run fatal / result_id
            # tidak terbaca): record author (kalau ada) dihitung tapi
            # TIDAK disimpan; kedua hitungan penyimpanan tetap 0.
            statistik_author["penyimpanan_author"] = "tidak_disimpan"
        trans.commit()
    except Exception as exc:
        trans.rollback()
        raise LoaderError(
            "Gagal menyimpan envelope ke raw.scholar_scrape_result (baik di "
            "tahap envelope, tabel terstruktur, maupun tahap penulisan "
            "record author di dalamnya), jadi "
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


def load_core_rows(core, conn=None, run_id=None, profil=None):
    """Muat hasil mapper (5 bucket) ke staging + core dalam satu transaksi.

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
    profil : dict | None, opsional
        Data level profil untuk mengisi STAGING (kunci: result_id,
        scholar_id, nama, h_index, domain, batches, elapsed). Kalau
        None, penulisan staging DILEWATI sama sekali - ini
        kompatibilitas untuk pemanggil yang tidak punya envelope
        (mis. test_database.py memanggil tanpa profil). Penulisan
        core tidak pernah bergantung pada nilai ini.

    Urutan penulisan (WAJIB, karena FK):
        1. author pemilik profil   -> author_id untuk metrics + baris
        2. publications            -> publications_id per indeks
        3. publication_authors     -> publication_id + author_id
        4. author_metrics          -> author_id pemilik
        5. publication_metrics     -> publication_id hasil (2)

    Sebelum langkah (1), kalau `profil` diberikan, data profil dan
    publikasi yang sudah dibersihkan ditulis lebih dulu ke
    staging.stg_authors dan staging.stg_publications dalam transaksi
    yang SAMA. Staging adalah jejak audit "nilai bersih yang dipakai
    untuk upsert core".

    `publications` dan `publication_metrics` dijaga sejajar oleh
    mapper (elemen ke-i keduanya berasal dari raw["rows"][i]), dan
    `publication_authors` menunjuk elemen yang sama lewat
    publication_index. Karena itu publications TIDAK PERNAH dilewati:
    baris tanpa judul menggagalkan SELURUH run, bukan di-skip
    diam-diam, supaya penyambungan indeks tidak bergeser.

    Transaksi
    ---------
    Satu transaksi untuk semua penulisan staging + core. Commit
    sekali di akhir. Exception apa pun (termasuk dari resolver)
    memicu rollback lalu naik lagi apa adanya, jadi tidak pernah
    ada kondisi setengah muat: author tanpa publikasinya, atau
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

    Kebijakan publication_authors (keputusan 3j, PERUBAHAN dari
    "publication_authors tidak pernah dihapus")
    ------------------------------------------------------------
    Asal daftar penulis tiap publikasi ditandai
    publications[i]["author_source"]:
      - "detail" (otoritatif): SEMUA tautan lama publikasi itu
        dihapus lebih dulu (_stmt_hapus_semua_tautan), lalu seluruh
        tautan ditulis ulang tanpa repoint. Hasilnya cermin halaman
        detail, termasuk menghapus author tiruan yang menempel.
      - selain "detail" (daftar, lebih terpotong): kalau publikasi
        itu SUDAH punya tautan (_stmt_ada_tautan), seluruh tautannya
        dilewati dan `publication_authors_skipped_downgrade` naik -
        data lengkap dari run detail tidak boleh diturunkan ke daftar
        terpotong. Kalau belum ada, tautan ditulis dengan logika
        resolve + repoint seperti semula.

    Yang DILEWATI (tidak error)
    ---------------------------
    Dua hal:
      - Satu entri publication_authors yang nama penulisnya kosong
        setelah dirapatkan. Nama begitu tidak bisa jadi
        core.authors.name (NOT NULL) dan menebaknya berarti
        mengarang identitas, jadi tautannya dihitung di `skipped`
        dan load dilanjutkan. Kasus ini tidak mungkin terjadi di
        mapper sekarang (_pecah_penulis sudah membuang elemen
        kosong), jadi angka ini harus 0.
      - Seluruh tautan sebuah publikasi bersumber "list" yang sudah
        punya relasi (lihat kebijakan di atas), dihitung di
        `publication_authors_skipped_downgrade`.

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

        # ---------- 0. staging (hanya kalau profil diberikan) ----------
        # Ditulis dalam transaksi core yang SAMA, sebelum core:
        # jejak audit nilai yang sudah dibersihkan. Dilewati kalau
        # profil None (mis. pemanggil tanpa envelope).
        if profil is not None:
            conn.execute(_stmt_sisip_stg_author(
                result_id=profil["result_id"],
                urutan=0,
                scholar_id=profil["scholar_id"],
                nama=profil.get("nama"),
                h_index=profil.get("h_index"),
                domain=profil.get("domain"),
                batches=profil.get("batches"),
                elapsed=profil.get("elapsed"),
            ))
            # Kelompokkan tautan per publikasi supaya nama penulis
            # tiap publikasi bisa digabung terurut.
            tautan_staging = {}
            for tautan in core["publication_authors"]:
                tautan_staging.setdefault(
                    tautan["publication_index"], []).append(tautan)
            for i, baris in enumerate(core["publications"]):
                penulis = sorted(
                    tautan_staging.get(i, []),
                    key=lambda t: t["author_order"],
                )
                nama_bersih = []
                for t in penulis:
                    nama = _normalisasi_nama(t.get("author_name"))
                    if nama is not None:
                        nama_bersih.append(nama)
                authors_teks = ", ".join(nama_bersih) if nama_bersih else None
                conn.execute(_stmt_sisip_stg_publication(
                    result_id=profil["result_id"],
                    urutan=i,
                    scholar_id=profil["scholar_id"],
                    baris=baris,
                    authors_teks=authors_teks,
                ))

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

        # ---------- 3. publication_authors (kebijakan 3j) ----------
        # Keputusan per publikasi ditentukan lebih dulu, TANPA
        # mengubah urutan proses tautan: iterasi utama tetap
        # mengikuti core["publication_authors"] supaya urutan
        # pembuatan author dan urutan INSERT deterministik.
        indeks_berurutan = []
        for tautan in core["publication_authors"]:
            indeks = tautan["publication_index"]
            if indeks not in indeks_berurutan:
                indeks_berurutan.append(indeks)

        publikasi_detail = set()
        publikasi_skip = set()
        for indeks in indeks_berurutan:
            sumber = core["publications"][indeks].get("author_source")
            if sumber == "detail":
                # Sumber otoritatif: buang SEMUA tautan lama publikasi
                # ini, lalu tulis ulang seluruhnya di bawah (tanpa
                # repoint - semua sudah terhapus).
                hasil_hapus = conn.execute(
                    _stmt_hapus_semua_tautan(publikasi_id[indeks]))
                statistik["publication_authors_deleted"] += (
                    getattr(hasil_hapus, "rowcount", 0) or 0)
                publikasi_detail.add(indeks)
            else:
                # Sumber daftar (lebih terpotong): kalau publikasi ini
                # sudah punya relasi, jangan diturunkan mutunya.
                sudah_ada = bool(conn.execute(
                    _stmt_ada_tautan(publikasi_id[indeks])).scalar_one())
                if sudah_ada:
                    publikasi_skip.add(indeks)
                    statistik["publication_authors_skipped_downgrade"] += 1

        for tautan in core["publication_authors"]:
            indeks = tautan["publication_index"]
            if indeks in publikasi_skip:
                continue
            nama_bersih = _normalisasi_nama(tautan.get("author_name"))
            if nama_bersih is None:
                statistik["skipped"] += 1
                continue
            # Nama yang memuat nama pemilik profil (mis.
            # "Budi Santoso, Budi A. Rahman") akan ketemu pemilik di
            # cache, karena pemilik ditulis lebih dulu.
            #
            # Scholar kadang menulis PEMILIK PROFIL dengan nama
            # pendek ("TAD Kuntjoro") yang tidak pernah cocok eksak
            # dengan nama lengkap baris pemilik. Halaman detail
            # kadang menulis varian yang dipotong per kata
            # ("Tri Agus D Kuntjoro"). Siapa yang SEKARANG memegang
            # nama ini (bisa author tiruan, bisa None) harus dibaca
            # SEBELUM nama diganti.
            lama_id = resolver.cari(nama_bersih)
            nama_pemilik = pemilik.get("name")
            if _bentuk_pendek(nama_bersih, nama_pemilik) or (
                    indeks in publikasi_detail and
                    _varian_nama_pemilik(nama_bersih, nama_pemilik)):
                # _bentuk_pendek berlaku untuk semua jalur (termasuk
                # run tanpa detail); _varian_nama_pemilik hanya untuk
                # jalur daftar penulis halaman detail, di mana varian
                # potong-per-kata seperti kasus publikasi 48 muncul.
                nama_bersih = _normalisasi_nama(nama_pemilik)
            author_id = resolver.resolve(nama_bersih)
            # Repoint: bila nama tadi terlanjur menempel di author lain
            # (umumnya tiruan dari run sebelum fix), buang link lama
            # pada publikasi+urutan ini supaya tidak muncul dua author
            # pada urutan yang sama. Jalur detail sudah menghapus semua
            # tautan, jadi tidak perlu repoint per tautan.
            if indeks not in publikasi_detail and \
                    lama_id is not None and lama_id != author_id:
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
                baris, publikasi_id[indeks],
                profile_author_id=pemilik_id))
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


def load_scholar_run(envelope, scholar_id, conn=None, raw_conn=None,
                     raw_only=False, baris_referensi=None):
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
    Kalau `data` ada, tabel raw TERSTRUKTUR (raw.scholar_author,
    raw.scholar_publication) juga ditulis dalam TRANSAKSI YANG SAMA,
    jadi envelope dan tabel terstruktur selalu kompak.

    Tahap 2 - CORE (hanya kalau `data` ada)
    ---------------------------------------
    to_core_rows() memetakan payload, lalu load_core_rows()
    menulis staging (staging.stg_authors, staging.stg_publications)
    dan core.* dalam satu transaksi sendiri. Profil untuk staging
    dibangun dari bagian `data` envelope dan hanya diteruskan kalau
    raw_result_id tersedia.

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
    raw_only : bool, opsional
        Kalau True, fungsi ini berhenti SETELAH tahap raw selesai
        dan ter-commit. to_core_rows() dan load_core_rows() TIDAK
        dipanggil sama sekali, dan `conn` (koneksi core) tidak
        pernah dipakai - jadi boleh None, dan guard `conn is
        raw_conn` di bawah dilewati (lihat catatan di sana).
        Status yang dikembalikan: STATUS_RAW_ONLY untuk run yang
        scraping-nya sukses (SUCCESS / PARTIAL_SUCCESS / status tak
        dikenal / None), atau status scraper ASLI (BLOCKED / TIMEOUT
        / NO_DATA / PARSING_ERROR) untuk run fatal - supaya pemanggil
        bisa membedakan "raw tersimpan" dari "scraping sukses".
    scholar_id : str, WAJIB
        Identitas profil yang sedang di-scrape, diambil dari
        pemanggil (argumen `--id` di CLI, atau author_id dari
        daftar target di pipeline) - bukan dari isi envelope.
        Diteruskan ke write_raw_scrape_result().
    baris_referensi : list[dict] | None, opsional
        Daftar baris referensi Excel (kunci `no`, `nama`,
        `scholar_id`) untuk GERBANG verifikasi pemilik profil terhadap
        nama profil envelope. Diteruskan apa adanya ke
        write_raw_scrape_result(); modul ini TIDAK membaca Excel
        sendiri. None (default) berarti tidak ada referensi, jadi
        SEMUA record author jadi belum_terverifikasi (nol pemilik).
        Nilai ini memengaruhi isi record author yang DISIMPAN ke
        raw.scholar_publication_author (G2-B) dan statistiknya -
        bukan transaksi core, bukan pula nilai kembalian selain
        kunci statistik author.

    Statistik author (P58 + G2-B)
    -----------------------------
    write_raw_scrape_result() menghitung record author (helper P57),
    MENYIMPANNYA ke raw.scholar_publication_author di dalam
    transaksi raw (DDL sql/ddl/10), dan hasilnya DIGABUNG ADDITIF ke
    dict statistik yang dikembalikan fungsi ini di SEMUA titik keluar:
    jalur raw-only, jalur SKIPPED_NO_DATA, dan jalur loaded/core.
    Sembilan kunci yang ditambahkan (kunci lama TIDAK pernah
    ditimpa): author_total, author_pemilik_profil,
    author_bukan_pemilik, author_belum_terverifikasi,
    publikasi_daftar_terpotong, publikasi_tanpa_penulis,
    author_disimpan, author_dilewati_konflik, dan
    penyimpanan_author. Nilai penyimpanan_author: "tersimpan" bila
    blok penulisan terstruktur berjalan (record benar-benar ditulis,
    menghitung disimpan/dilewati konflik), atau "tidak_disimpan"
    bila dilewati (run fatal / result_id tidak terbaca) - untuk run
    fatal (data=None) ketujuh kunci lain hadir dengan hitungan nol
    dan kedua hitungan penyimpanan 0. Kegagalan tahap raw melempar
    LoaderError, jadi statistik yang kembali SELALU berasal dari
    transaksi yang ter-commit - tidak pernah dari baris ter-rollback.
    Untuk writer tiruan yang tidak mengisi wadah keluaran_author
    (tes), fallback nol memakai penyimpanan_author "belum_aktif" -
    nilai itu hanya penanda jalur tiruan, bukan status penyimpanan
    sungguhan.

    Perilaku
    --------
    - `raw_only=True`: tahap raw tetap dijalankan dan di-commit,
      lalu fungsi ini LANGSUNG mengembalikan statistik dengan
      `raw_result_id`-nya. to_core_rows() dan load_core_rows() tidak
      dipanggil, staging/core tidak disentuh, dan `conn` tidak
      dibuka/dipakai. Status yang dikembalikan: STATUS_RAW_ONLY untuk
      run yang scraping-nya sukses, atau status scraper ASLI (BLOCKED
      / TIMEOUT / NO_DATA / PARSING_ERROR) untuk run fatal - raw
      tetap ditulis untuk semua run, tetapi raw tersimpan bukan
      berarti scraping sukses.
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
      INSERT polos setiap run; staging memakai INSERT ... ON
      CONFLICT DO NOTHING. Ringkasnya: raw-first-durable (envelope +
      tabel terstruktur), upsert untuk authors+publications,
      append-only untuk metrics. Penjelasan lengkapnya ada di tiap
      statement.

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

    # Guard hanya berlaku untuk mode normal. Mode raw-only tidak
    # memakai `conn` sama sekali (core tidak disentuh), jadi koneksi
    # yang sama untuk core dan raw tidak lagi berbahaya.
    if (not raw_only and conn is not None and raw_conn is not None
            and conn is raw_conn):
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

    # Wadah hasil record author dari tahap raw (P58 + G2-B). Dibuat
    # SEBELUM pemanggilan supaya write_raw_scrape_result() bisa
    # mengisinya lewat referensi: "records" (list per baris) dan
    # "statistik" (sembilan kunci, termasuk hasil penyimpanan nyata
    # ke raw.scholar_publication_author di dalam transaksi raw).
    keluaran_author = {}
    raw_result_id = write_raw_scrape_result(
        envelope, raw_conn=raw_conn, scholar_id=scholar_id,
        baris_referensi=baris_referensi,
        keluaran_author=keluaran_author,
    )

    # Statistik author untuk digabung ADDITIF ke setiap titik keluar.
    # Kalau write_raw_scrape_result() tidak mengisi wadahnya (mis.
    # karena diganti tiruan di test, atau gagal sebelum komputasi),
    # dipakai nol supaya Sembilan kunci tetap hadir di semua jalur.
    # "belum_aktif" di dict fallback ini HANYA untuk jalur writer
    # tiruan yang tidak mengisi wadah - BUKAN status penyimpanan
    # sungguhan; writer asli selalu mengisi "tersimpan" /
    # "tidak_disimpan" sendiri (G2-B, D4).
    statistik_author = keluaran_author.get("statistik") or {
        "author_total": 0,
        "author_pemilik_profil": 0,
        "author_bukan_pemilik": 0,
        "author_belum_terverifikasi": 0,
        "publikasi_daftar_terpotong": 0,
        "publikasi_tanpa_penulis": 0,
        "author_disimpan": 0,
        "author_dilewati_konflik": 0,
        "penyimpanan_author": "belum_aktif",
    }

    # ---------- MODE RAW-ONLY: berhenti setelah raw ----------
    # Data mentah sudah ter-commit di atas. Mode ini TIDAK menjalankan
    # to_core_rows() maupun load_core_rows(): transformasi dan loading
    # ke staging/core berada di luar cakupan. Run fatal (data None)
    # juga berhenti di sini - yang penting adalah raw tersimpan, bukan
    # ada-tidaknya data. TAPI: raw tersimpan bukan berarti scraping
    # sukses. Kalau status scraper asli masuk STATUS_SCRAPER_FATAL,
    # statistik mengembalikan status scraper itu (mis. "BLOCKED"),
    # bukan RAW_ONLY, supaya pemanggil bisa membedakan keduanya.
    if raw_only:
        status_scraper = (envelope or {}).get("status")
        statistik = _stats_kosong()
        statistik["run_id"] = run_id
        statistik["raw_result_id"] = raw_result_id
        if status_scraper in STATUS_SCRAPER_FATAL:
            statistik["status"] = status_scraper
            statistik["reason"] = (
                "Mode raw-only: envelope tersimpan di "
                "raw.scholar_scrape_result (result_id={}), sudah di-commit, "
                "tetapi scraping TIDAK sukses (status={}). Transformasi "
                "dan loading ke staging/core TIDAK dijalankan."
                .format(raw_result_id if raw_result_id is not None
                        else "(tidak ada)",
                        status_scraper)
            )
        else:
            statistik["status"] = STATUS_RAW_ONLY
            statistik["reason"] = (
                "Mode raw-only: data mentah tersimpan di "
                "raw.scholar_scrape_result (result_id={}), sudah di-commit. "
                "Transformasi dan loading ke staging/core TIDAK dijalankan."
                .format(raw_result_id if raw_result_id is not None
                        else "(tidak ada)")
            )
        # Gabung ADDITIF ketujuh kunci statistik author (P58).
        # update() di sini murni penambahan: ketujuh kunci itu tidak
        # pernah ada di _stats_kosong(), jadi kunci lama (raw_result_id,
        # run_id, status, ...) tidak mungkin tertimpa.
        statistik.update(statistik_author)
        return statistik

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
        # Run fatal = nol record author; ketujuh kunci tetap hadir
        # (additif, tanpa menimpa kunci lama) supaya pemanggil tidak
        # pernah menemui jalur tanpa kunci statistik author.
        statistik.update(statistik_author)
        return statistik

    core = to_core_rows(data, retrieved_at=envelope.get("finished_at"))
    # Profil untuk staging: hanya kalau tahap raw mengembalikan
    # result_id (kunci UNIQUE staging). Kalau tidak, staging dilewati
    # dan core tetap ditulis seperti biasa.
    profil = None
    if raw_result_id is not None and data is not None:
        profil = {
            "result_id": raw_result_id,
            "scholar_id": data.get("scholar_id"),
            "nama": data.get("name"),
            "h_index": data.get("h_index"),
            "domain": data.get("domain"),
            "batches": data.get("batches"),
            "elapsed": data.get("elapsed"),
        }
    statistik = load_core_rows(core, conn=conn, run_id=run_id, profil=profil)
    statistik["raw_result_id"] = raw_result_id
    # Gabung ADDITIF ketujuh kunci statistik author (P58). Kunci ini
    # tidak pernah tumpang tindih dengan kunci yang dikembalikan
    # load_core_rows (maupun raw_result_id di atas), jadi kunci lama
    # tetap utuh.
    statistik.update(statistik_author)
    return statistik
