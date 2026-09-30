# Progress Summary — Telkom Publication Dashboard

Dokumen ini adalah titik masuk untuk siapa pun yang membuka project ini.
Ditulis tanpa asumsi pembaca sudah tahu sejarah chat. Semua keputusan desain
di sini dijelaskan isinya, bukan hanya "sesuai kesepakatan sebelumnya".

**Status ringkas per 29 September 2026:** pipeline scrape → PostgreSQL sudah
terbangun end-to-end secara kode dan lolos 89 pemeriksaan otomatis dengan
koneksi tiruan, **tetapi belum pernah dijalankan ke database PostgreSQL
sungguhan** (lingkungan develop tidak punya server). Validasi Pandera masih
stub, seluruh file test masih stub, dan orkestrasi Prefect belum dimulai.

---

## 1. Arsitektur & Keputusan Desain

### 1.1 Peta file

Struktur project mengikuti pipeline linear: **ingest → transform → load**, dengan
lapisan validasi yang belum jadi dan lapisan pipeline yang merangkai semuanya.

| File | Peran | Status |
|---|---|---|
| `src/ingestion/scrape_scholar_playwright.py` | Scraper Google Scholar via Playwright (async). Menghasilkan run envelope. | Selesai (~750 baris) |
| `src/transformation/publications.py` | Mapper murni: ubah envelope → baris core. Tanpa DB, tanpa jaringan, tanpa file I/O. | Selesai (446 baris) |
| `src/loading/postgres.py` | Loader: tulis raw dulu (commit), lalu upsert core. Validasi kredensial, engine, transaksi. | Selesai (1774 baris) |
| `src/validation/schemas.py` | Skema validasi Pandera. | **STUB** (8 baris, TODO) |
| `src/pipeline/flow.py` | Placeholder orkestrasi Prefect. | **STUB** (8 baris, TODO) |
| `src/pipeline/run_pipeline.py` | CLI pipeline: scrape → load → ringkasan. Titik jalan dari laptop. | Selesai (240 baris, baru) |
| `sql/ddl/01_create_schemas.sql` | Buat schema `core` dan `raw`. | Tidak diubah |
| `sql/ddl/02_create_core_tables.sql` | Lima tabel core (star schema). | Tidak diubah |
| `sql/ddl/03_create_raw_scrape_result.sql` | Tabel raw + natural key untuk upsert. | **BARU** (155 baris) |
| `requirements.txt` | Dependensi. Sudah memuat `psycopg[binary]`, `python-dotenv`, `SQLAlchemy`, `pandera`, `prefect`. | — |
| `.env` / `.env.example` | Kredensial `POSTGRES_*` (host, port, db, user, password). | — |

Tabel core (dari `02`): `core.authors`, `core.publications`,
`core.publication_authors`, `core.author_metrics`, `core.publication_metrics`.

### 1.2 Run envelope — 9 kunci (bentuk dikunci)

`scrape_scholar()` selalu mengembalikan dict dengan tepat 9 kunci, sukses maupun
gagal:

```
run_id, source, started_at, finished_at, status,
records_fetched, records_failed, error_message, data
```

`data` berisi 8 kunci (atau `None` untuk run fatal):

```
scholar_id, name, h_index, rows, failures, domain, batches, elapsed
```

`rows` menyimpan field **mentah** Scholar: `title, authors, venue, extra,
citations, year`. Field `venue` dan `extra` sengaja dibiarkan apa adanya di sini
meskipun tidak dipetakan penuh ke core (lihat §1.5).

Diagnostik `domain`, `batches`, `elapsed` sengaja diletakkan **di dalam `data`**,
bukan di envelope, karena bentuk envelope dikunci 9 kunci.

### 1.3 Pohon keputusan status (urutan prioritas, first-match-wins)

Fungsi `_classify_status()` di scraper menilai dalam urutan ini; yang pertama
cocok menang:

1. **BLOCKED** — fatal, tanpa data, captcha / "unusual traffic".
2. **TIMEOUT** — fatal, tanpa data, error navigasi / timeout jaringan.
3. **PARSING_ERROR** — fatal, tanpa data, parse HTML melempar error.
4. **NO_DATA** — fatal, profil termuat tapi 0 publikasi. (Juga menjadi default
   bila fatal tanpa alasan spesifik.)
5. **PARTIAL_SUCCESS** — ada data, tapi sebagian batch gagal / paging terpotong /
   kena batas `max_batches`.
6. **SUCCESS** — ada data, semua lancar.

Empat status pertama dianggap "run fatal" (`data=None`). Status 5 dan 6 tetap
punya data dan diproses ke core.

### 1.4 Kebijakan penulisan yang sudah diputuskan

**Raw-first, durable, dan selalu jalan.** Setiap run — sukses maupun fatal —
envelope utuh ditulis ke `raw.scholar_scrape_result` lebih dulu, dalam
transaksi sendiri yang di-commit **sebelum** core disentuh. Tujuannya: kalau
load core gagal, data mentah tetap ada dan bisa diproses ulang. Karena itu
koneksi core dan raw **wajib dua objek koneksi berbeda**; kalau pemanggil
memberikan satu objek untuk keduanya, modul melempar `LoaderError` (bukan
peringatan), sebab rollback core akan ikut menarik baris raw.

**`scholar_id` wajib dari pemanggil, kolom NOT NULL, tanpa placeholder.**
Nilai diambil dari identitas yang dipakai pemanggil menjalankan scraping (argumen
`--id` di CLI, atau `author_id` dari daftar target), **bukan** dari
`envelope["data"]["scholar_id"]`. Alasannya: bentuk envelope dikunci 9 kunci dan
`scholar_id` hanya ada di dalam `data`; run fatal punya `data=None` sehingga uid
profil hilang dari envelope sepenuhnya. Nilai tebakan/placeholder dihapus
sengaja. `scholar_id` kosong atau `None` ditolak sebagai `LoaderError`.

**Mismatch guard = peringatan, bukan exception.** Kalau `scholar_id` pemanggil
berbeda dari `envelope["data"]["scholar_id"]`, modul mengeluarkan
`UserWarning` yang menyebut kedua nilai, lalu **tetap menyimpan** memakai nilai
pemanggil. Run bermasalah justru paling berguna disimpan; menolak menyimpan
berarti record hilang saat paling perlu ditelusuri.

**Replay-safe raw.** `run_id` punya constraint `UNIQUE`. Supaya retry setelah
core gagal tidak buntu, raw insert memakai
`ON CONFLICT (run_id) DO UPDATE SET result_id = ...` — no-op yang mengembalikan
`result_id` lama tanpa menimpa envelope asli.

**Upsert vs insert-only.**
- `core.publications`: **upsert** pada natural key `(title, publication_date)`,
  dengan `UNIQUE NULLS NOT DISTINCT` (butuh PG 15+) agar `publication_date=NULL`
  ikut terdeteksi duplikat. PK `publications_id` tidak pernah ditulis ulang saat
  update.
- `core.authors`: **upsert** pada indeks ekspresi `lower(btrim(name))` — cocok
  persis tapi case-insensitive. Konflik memicu `DO UPDATE` yang hanya mengisi
  `scholar_id` bila yang baru tidak null (`coalesce`), jadi `scholar_id` yang
  sudah dikenal tidak terhapus.
- `core.publication_authors`: **insert dengan `DO NOTHING`** pada composite PK
  `(publication_id, author_id, author_order)` — relasi idempotent, tidak pernah
  di-update.
- `core.author_metrics` dan `core.publication_metrics`: **insert-only, polos** —
  satu baris snapshot per run, **tidak ada** `ON CONFLICT`. Ini untuk menjaga
  histori metrik dari waktu ke waktu.

**Auto-create author & co-author.** Loader menyimpan daftar `name → author_id`
dalam cache terisi ulang dari `core.authors` di awal run. Nama co-author dipecah
dari string `authors` (pemisah koma) sesuai urutan tampilan; setiap nama yang
tidak ada dibuat otomatis sebagai author baru. Author yang dibuat otomatis dari
co-author mendapat `scholar_id = NULL` (tidak ada ID profil untuk mereferensikannya).

**Aturan idempoten pada DDL.** `03_create_raw_scrape_result.sql` membungkus
`ADD CONSTRAINT` di blok `DO ... EXCEPTION WHEN duplicate_object THEN NULL`, jadi
aman dijalankan berkali-kali. `unique_violation` (duplikat data yang sudah
terisi) sengaja **tidak** ditelan agar masalah data tetap muncul.

### 1.5 Batas antara yang dipetakan dan yang dibiarkan kosong

Kolom berikut ada di DDL tapi **tidak** bisa diisi dari sumber Scholar, dan
mapper tidak mengarang nilai (lihat blok "CELAH YANG SENGAJA TIDAK
DIPETAHKAN" di `publications.py`):

- `authors.sinta_id`, `scopus_id`, `program_study`, `faculty` — atribut
  institutional, bukan indeks sitasi; harus berasal dari sistem internal.
- `publications.publisher`, `description`, `category`, `doi` — tidak ada di
  Scholar; butuh sumber lain (Crossref/SINTA).
- `publications.volume`, `pages` — berasal dari `rows[].extra` yang berupa satu
  string bebas ("3(2), 45-60"); sengaja **tidak** dipecah otomatis karena
  parser yang salah menulis volume/halaman salah ke core.
- `publications.journal` diisi 1:1 dari `rows[].venue` (teks bebas Scholar),
  jadi isinya **fidelity rendah** — bukan nama jurnal bersih.
- `publications.publication_date` hanya terisi kalau `year` berupa angka 4 digit
  bersih ("2019"); tahun seperti "in press"/"2019-2020" jadi `NULL` (deterministik,
  tanpa tebakan bulan/hari).

### 1.6 Star schema core

`core.authors` (1) ← `core.author_metrics`; `core.publications` (1) ←
`core.publication_metrics`; `core.publications` (N) ↔ `core.authors` (N) lewat
`core.publication_authors` sebagai tabel junction. Kolom SERIAL/FK
(`author_id`, `publications_id`, `metric_id`, dll.) diisi database atau pemanggil
setelah INSERT.

---

## 2. Status Hasil Test

### 2.1 Apakah `run_pipeline.py` sudah berhasil end-to-end ke Postgres asli?

**BELUM.** Pipeline `src/pipeline/run_pipeline.py` sudah lengkap secara kode dan
sudah diuji sampai memalsukan koneksi, tapi **tidak pernah menyentuh
database PostgreSQL sungguhan**. Alasan: lingkungan develop tidak punya server
Postgres (Docker mati, port 5432 tertutup, driver `psycopg` tidak terpasang).

Yang sudah terbukti (89 pemeriksaan otomatis, koneksi tiruan):
- Raw di-commit **sebelum** core disentuh; core rollback tidak menghapus raw.
- Kegagalan raw menghentikan core sepenuhnya.
- Empat status fatal (BLOCKED/TIMEOUT/NO_DATA/PARSING_ERROR) tetap tersimpan di
  raw, core tidak disentuh, dan loader mengembalikan status `SKIPPED_NO_DATA`.
- Upsert publikasi/author, insert-only metrics, dan `DO NOTHING` relasi
  menghasilkan SQL yang benar secara teks.
- Retry setelah core gagal berhasil (replay-safe).
- `scholar_id` wajib + mismatch guard berperilaku benar.
- CLI jalan sebagai skrip, sebagai modul (`-m`), dan dari direktori mana pun.

### 2.2 Hasil run ke database asli

**Tidak ada** — belum pernah dijalankan. Saat pipeline dijalankan di laptop
pengguna dengan Postgres aktif via Podman, ini yang pertama akan terjadi:
buka koneksi → tulis raw → commit → upsert core → commit → cetak ringkasan
(status run, `records_fetched`/`records_failed`, dan apakah load berhasil).

### 2.3 Status tiap klaim "belum diverifikasi ke server nyata"

Semua klaim berikut **masih belum terverifikasi** terhadap PostgreSQL nyata
(diuji hanya lewat kompilasi SQL + koneksi tiruan):

- `UNIQUE NULLS NOT DISTINCT` benar-benar menolak duplikat dengan `NULL`.
- Inferensi conflict `ON CONFLICT (lower(btrim(name)))` cocok dengan indeks
  ekspresi di DDL.
- `RETURNING` + interaksi SERIAL/FK.
- Binding psycopg untuk UUID / JSONB / TIMESTAMP.
- Durabilitas transaksinya (commit raw benar-benar terpisah di server).
- Subclass JSONB kustom (`_EnvelopeJSONB`) yang memakai API privat SQLAlchemy
  (`_str_impl`, `_make_bind_processor`) — sudah terbukti jalan di SQLAlchemy
  2.0.45 yang terpasang, **tapi** harus dicek ulang sebelum upgrade versi besar.
- Perilaku dua penulis (writer) bersamaan.

---

## 3. Keterbatasan yang Diketahui

- **Kolom yang sengaja kosong** — daftar lengkap di §1.5. Ringkasnya: atribut
  institutional author, `publisher`/`description`/`category`/`doi` publikasi,
  serta `volume`/`pages` (kasus khusus `extra`).
- **`journal` fidelity rendah** — diisi apa adanya dari `venue` Scholar yang
  berupa teks bebas; belum diurai menjadi nama jurnal/volume/halaman bersih.
- **Traceability co-author terbatas.** Hubungan "publasi X punya penulis Y"
  yang berasal dari parsing string co-author **tidak** punya FK atau jejak
  khusus ke run sumbernya. Satu-satunya cara menelusuri ke run scraping
  tertentu adalah lewat `raw.scholar_scrape_result` (yang menyimpan `run_id` dan
  envelope mentah) — tidak ada kolom `result_id` di tabel core yang mengaitkan
  baris core ke baris raw-nya.
- **File test masih stub.** Keempat file di `tests/` (`test_database.py`,
  `test_scraper.py`, `test_transformation.py`, `test_validation.py`) masing-masing
  **1 baris, nol fungsi `test_`, nol `assert`** — belum ada test otomatis yang
  tersimpan di repo. Rangkaian 89 pemeriksaan di atas hidup di file verifikasi
  sementara di luar project, **belum** dipindahkan ke `tests/`.
- **`lower()` vs `casefold()`.** PostgreSQL `lower()` dan Python `.casefold()`
  tidak identik untuk beberapa huruf non-ASCII (mis. "ß": Python → "ss",
  PostgreSQL → tetap "ß"). Untuk nama Indonesia/ASCII tidak masalah; nama
  beraksen/ligatur bisa terpisah menjadi dua author. Batas ini **dengan
  sengaja diterima**.
- **Spasi di dalam nama** tidak diratakan oleh indeks ekspresi; loader sudah
  merapikan spasi berlebih sebelum menulis, jadi `btrim()` hanya jaring
  pengaman.
- **Tanggal kosong di DB.** `publication_date` NULL sering terjadi; dengan
  `NULLS NOT DISTINCT` semua publikasi tanpa tanggal akan dianggap duplikat
  bila judulnya sama.

---

## 4. Belum Dikerjakan (Next Steps)

**Prioritas tinggi**

1. **Jalankan pipeline ke Postgres asli** (pengguna, di laptop dengan Podman).
   Cek ringkasan, lalu query `raw.scholar_scrape_result` dan tabel core untuk
   memastikan isi dan idempotensi sesuai.
2. **Pindahkan 89 pemeriksaan ke `tests/`** sebagai test pytest sungguhan, lalu
   isi keempat file test yang masih stub.
3. **Validasi Pandera** (`src/validation/schemas.py` masih stub): definisikan
   schema untuk raw author & publication dan untuk hasil staging, dengan
   baris gagal masuk quarantine/error log (bukan langsung dibuang).
4. **Scraping seluruh daftar dosen LPPM.** Saat ini pipeline hanya menerima
   satu `--id`; butuh mode batch (daftar target) yang mengiterasi banyak uid
   dan mengumpulkan statistik per-target.

**Prioritas menengah**

5. **Orkestrasi Prefect** (`src/pipeline/flow.py`): atur jadwal scrape → load →
   refresh terjadwal, plus retry dan observability. `run_pipeline.py` saat ini
   adalah CLI sekali jalan, bukan orkestrator.
6. **Parsing `extra` → volume/pages** dan enrichment publikasi dari
   Crossref/SINTA untuk mengisi `publisher`/`doi`/`category`.
7. **Atribut institutional author** dari sistem internal (LPPM) untuk mengisi
   `sinta_id`/`scopus_id`/`program_study`/`faculty`.
8. **Lapisan laporan (mart)** — belum ada tabelnya sama sekali.

**Catatan arsitektur untuk session berikutnya**

- `_EnvelopeJSONB` memakai API privat SQLAlchemy. Sebelum meng-upgrade versi
  SQLAlchemy, periksa ulang `_str_impl` dan `_make_bind_processor`.
- Editor/lokal sering menyisipkan karakter non-Latin ke komentar/komentar
  string saat menulis cepat. Semua file `.py` sudah discan bebas karakter
  CJK, tapi lakukan scan yang sama setelah edit besar berikutnya.
- Alembic/pem migrasi belum ada; perubahan schema saat ini berdiri sebagai file
  SQL idempotent yang dijalankan manual.
