# Rencana Tahap 1 — Revisi Pipeline 4-Layer & Pengisian Kolom Kosong (Revisi 3)

> **Untuk pekerja agentik:** SUB-SKILL WAJIB: gunakan superpowers:subagent-driven-development (disarankan) atau superpowers:executing-plans untuk mengimplementasikan rencana ini task-per-task. Langkah memakai sintaks checkbox (`- [ ]`) untuk pelacakan.

**Tujuan:** Membangun pipeline berlapis nyata (raw → staging → core) dengan tabel raw terstruktur + layer staging, dan mengisi kolom kosong di core.publications (volume, pages, publisher, description, category) dengan mengambil halaman detail Google Scholar; memperbaiki h_index yang selalu NULL.

**Arsitektur:** Envelope JSONB di raw.scholar_scrape_result dipertahankan sebagai catatan durabilitas run; data terstruktur diturunkan ke raw.scholar_author + raw.scholar_publication (TEXT mentah), dibersihkan di staging.stg_authors + staging.stg_publications, lalu di-upsert ke core dengan natural key (title, publication_year). Scraper menambah fase pengambilan halaman detail per publikasi.

**Tech Stack:** Python 3.12.7, Playwright (async), BeautifulSoup, SQLAlchemy, PostgreSQL 16 (raw/staging/core/mart), pytest.

**Spesifikasi:** Instruksi pengguna (MASALAH 1 & 2, ATURAN KERJA A–F, item 3a–3i) + 5 perubahan persetujuan (kunci natural publication_year, UNIQUE (result_id, urutan) di raw, perbandingan penulis, WHERE backfill_volume_pages, fungsi category terpisah) + keputusan 3j (sumber penulis) + protokol pengujian wajib (publication_test).

## Batasan Global

- A. Tidak ada perubahan kode sebelum rencana disetujui ("lanjut").
- B. Tidak ada perintah destruktif: DROP, TRUNCATE, DELETE tanpa WHERE, `podman compose down -v`.
- C. Jangan mengubah `sql/ddl/01_*.sql`, `02_*.sql`, `03_*.sql`. Perubahan skema = file migrasi BARU (`04_*.sql` dst.), idempotent, harus jalan di DB berisi data, dijalankan manual oleh user via pgAdmin. Query backfill DATA (bukan skema) diletakkan di `sql/maintenance/` - folder `sql/ddl/` dijalankan otomatis saat container baru, jadi file data tidak boleh ada di sana.
- D. Setiap perubahan skema harus disinkronkan ke definisi SQLAlchemy di `src/loading/postgres.py`.
- E. Jangan sentuh: `run_batch.py`, `backfill_crossref.py` (termasuk email default-nya), dan `tests/` kecuali `test_scraper.py` + `test_transformation.py`.
- F. Semua komentar/docstring dalam Bahasa Indonesia; scan karakter CJK/Inggris nyasar setelah edit.
- Bentuk envelope tetap: 9 kunci atas + 8 kunci `data`; field baru masuk ke `rows[]`.
- **Kunci natural publikasi: (title, publication_year), NULLS NOT DISTINCT.** `publication_year` (INTEGER) selalu diisi dari tahun halaman daftar. `publication_date` hanya diisi bila tanggal LENGKAP ada di halaman detail; tanggal parsial (hanya tahun, atau tahun/bulan) → NULL, bukan 1 Januari; NULL dari run baru tidak menimpa tanggal yang sudah ada (COALESCE).
- **Sumber nama penulis (keputusan 3j)**: halaman detail = sumber otoritatif; detail gagal → daftar halaman daftar hanya jika publikasi belum punya relasi penulis; `_bentuk_pendek` tetap cadangan. Lihat 3d/3e.

---

## Laporan Investigasi

### 1a. Struktur halaman detail Scholar (hl=id)

- URL detail: `https://scholar.google.co.id/citations?view_op=view_citation&hl=id&user={uid}&pagesize=100&citation_for_view={uid}:{token}`
- Token `citation_for_view` tersedia di href link judul pada baris daftar (`a.gsc_a_at`) → URL detail diturunkan dari halaman daftar tanpa pencarian link tambahan.
- Field detail berupa pasangan `div.gs_scl` → `div.gsc_oci_field` (label) + `div.gsc_oci_value` (nilai).
- Label yang ditemukan (hl=id): Pengarang, Tanggal terbit (format `YYYY/MM/DD`), Jurnal / Konferensi (label venue sekaligus penentu tipe), Jilid, Terbitan, Halaman, Penerbit, Deskripsi (`id="gsc_oci_descr"`), Total kutipan, Artikel Scholar.
- Judul: `#gsc_oci_title`; link landing page: `a.gsc_oci_title_link`; link PDF: `div.gsc_oci_title_ggi a`.
- Fixture tersimpan: `fixtures/scholar/detail_1.html` (artikel jurnal), `detail_2.html` (konferensi), `profil_hlid.html`.

### 1b. DOI

- **0 kemunculan "doi" (case-insensitive) dan 0 link doi.org** di kedua file detail. Scholar tidak menyediakan DOI di halaman detail.
- Kesimpulan: kolom `doi` TIDAK akan terisi dari Scholar. Sumber DOI tetap `backfill_crossref.py`. Kolom `link` (URL landing publisher) tersedia dari `a.gsc_oci_title_link`.

### 1c. Kategori

- Tidak ada data kategori eksplisit. Kategori hanya bisa diturunkan dari label venue: "Jurnal" → `journal`, "Konferensi" → `conference`. Ini heuristik, bukan data sumber (lihat 3d).

### 1d. Biaya & risiko scraping

- 1 navigasi tambahan per publikasi (halaman detail) + jitter 2–5 detik.
- 10 publikasi di profil uji → ±10 request tambahan per profil.
- Tidak ada captcha/block saat pengujian, tapi risiko naik sebanding jumlah request. Mitigasi: opsi `--tanpa-detail` (opt-out), deteksi block tetap berhenti di fase detail → data daftar tetap disimpan → status `PARTIAL_SUCCESS`.

### 2. Akar masalah h_index kosong

- Label statistik di profil (hl=id) adalah **"indeks-h"**, bukan "h-index". `_parse_h_index` mencari "h-index" → tidak pernah cocok → h_index selalu NULL.
- Perbaikan: cocokkan "indeks-h" (dan pertahankan "h-index" untuk kompatibilitas hl=en). Ini satu-satunya item tambahan di luar dua masalah utama yang diizinkan.

### 2b. Perbandingan penulis: halaman daftar vs halaman detail (item 3 persetujuan)

| | Halaman daftar (`profil_hlid.html`) | Halaman detail (`detail_*.html`) |
|---|---|---|
| **Pub 1** "A Design of Antipodal Vivaldi Antenna..." | `GW Cahyaningtyas, FS Akbar, RM Untsa, PC Jadmiko, EM Khusna, ...` | `Galuh Widya Cahyaningtyas, Fannush Shofi Akbar, Risdilah Mimma Untsa, Puguh Chondro Jadmiko, Efrilia M Khusna, Tri Agus Djoko Kuntjoro` |
| **Pub 2** "Planning design of NR 5G 26 GHz..." | `NKMA Maria, AAF Purnama, HU Mustakim, RM Untsa, N Rachmaningrum, ...` | `Ni Ketut Melzia Anggi Maria, Arrizky Ayu Faradila Purnama, Hamzah Ulinuha Mustakim, Risdilah Mimma Untsa, Nilla Rachmaningrum, Tri Agus Djoko Kuntjoro` |

Temuan:
1. **Format berbeda**: daftar = inisial (nama depan disingkat, mis. "GW Cahyaningtyas"); detail = nama lengkap ("Galuh Widya Cahyaningtyas").
2. **Daftar terpotong**: halaman daftar hanya menampilkan 5 penulis pertama + "..." → data penulis saat ini di core.authors TIDAK LENGKAP (penulis ke-6 hilang).
3. **Dampak ke core.authors**: beralih ke detail → nama lengkap + daftar lengkap, tapi natural key nama (case-insensitive) TIDAK mencocokkan "GW Cahyaningtyas" dengan "Galuh Widya Cahyaningtyas" → duplikasi penulis untuk orang yang sama. Keputusan user diperlukan (lihat 3j). **Sumber nama penulis tidak diubah sampai user memutuskan.**

---

## Rencana Implementasi (3a–3i)

### 3a. Desain layer

- **raw**: pertahankan `raw.scholar_scrape_result` (envelope JSONB, alasan durabilitas tetap berlaku). Tambah:
  - `raw.scholar_author` — satu baris per run per profil (scholar_id, nama, h_index, dll).
  - `raw.scholar_publication` — satu baris per publikasi per run, FK ke result_id, semua kolom TEXT mentah (termasuk field detail).
  - **UNIQUE (result_id, urutan) di kedua tabel + `ON CONFLICT (result_id, urutan) DO NOTHING`**, ditulis dalam transaksi raw yang SAMA dengan envelope → retry run yang sama tidak menggandakan baris.
- **staging**: `staging.stg_publications` + `staging.stg_authors` — nilai dibersihkan (trim whitespace, DATE dari `YYYY/MM/DD`, pecah volume/pages, pecah daftar penulis).
- **core**: diisi DARI staging, mempertahankan logika upsert yang sudah terbukti (natural key title + publication_year, nama penulis case-insensitive, metrik insert-only).
- **Transaksi**: raw di-commit duluan via koneksi terpisah (durabilitas), lalu staging + core dalam SATU transaksi di koneksi core.
- **mart**: di luar lingkup.

### 3b. Kolom baru & kolom yang tetap kosong

- Kolom baru di `core.publications`: **`publication_year` (INTEGER, selalu diisi dari tahun halaman daftar)**, `issue`, `link`.
- **Kunci unik/upsert berpindah** dari (title, publication_date) ke **(title, publication_year), NULLS NOT DISTINCT**.
- `publication_date`: hanya diisi bila tanggal LENGKAP ada di detail; tanggal parsial → NULL (bukan 1 Januari); NULL dari run baru tidak menimpa tanggal lama (COALESCE).
- Dampak: mapper, loader (KOLOM_PUBLICATIONS + definisi SQLAlchemy + `index_elements`), DDL baru.
- Kolom yang TETAP kosong setelah revisi: `doi` (Scholar tidak punya; hanya backfill_crossref yang mengisi); `publication_date` NULL untuk publikasi tanpa tanggal lengkap di detail.

### 3c. Perubahan scraper

- Ambil halaman detail per publikasi (default ON), parser detail baru (`parse_detail_html`).
- Kegagalan per-publikasi dicatat ke `failures` (tidak menggagalkan seluruh run).
- Bentuk envelope tetap: 9 kunci atas + 8 kunci `data`; field baru masuk ke `rows[]` (termasuk `publication_year` dari tahun di baris daftar, dan field detail mentah).
- Perbaikan `_parse_h_index` (cocokkan "indeks-h" + "h-index").

### 3d. Mapper

- Tetap murni (pure function). Field detail dipetakan ke publikasi.
- `publication_year` diambil dari tahun halaman daftar (selalu ada); `publication_date` hanya dari tanggal lengkap detail.
- `pecah_extra_volume_pages` tetap dipakai sebagai fallback saat volume/pages tidak tersedia di detail.
- **category**: fungsi TERPISAH yang mudah dimatikan (mis. `turunan_kategori(label_venue)`), nilai hanya `'journal'` / `'conference'` / NULL dari label halaman detail, didokumentasikan sebagai turunan heuristik, bukan data sumber.
- **Sumber nama penulis (keputusan 3j)**: halaman DETAIL = sumber otoritatif untuk publikasi yang detailnya berhasil diambil. Tidak ada penggabungan/tebakan antara "GW Cahyaningtyas" dan "Galuh Widya Cahyaningtyas". Publikasi yang detailnya GAGAL: pakai daftar halaman daftar HANYA kalau publikasi itu belum punya baris di core.publication_authors; kalau sudah punya, jangan diubah (jangan menurunkan data dari lengkap ke terpotong). `_bentuk_pendek` tetap sebagai cadangan untuk run tanpa detail. Kedua versi daftar penulis (daftar + detail) tetap tersimpan mentah di raw.scholar_publication.

### 3e. Loader

- Ubah `_stmt_sisip_publikasi`: target `ON CONFLICT (title, publication_year)` + `COALESCE(EXCLUDED.kolom, kolom_lama)` untuk kolom enrichment (doi, publisher, description, category, volume, pages, issue, link, **publication_date**) → NULL dari run baru TIDAK menimpa nilai yang sudah terisi (terutama doi dari Crossref dan tanggal lama).
- **Perubahan perilaku yang ditandai**: "run terakhir menang" → "nilai non-NULL terakhir menang".
- **publication_authors (keputusan 3j)**: saat memakai daftar detail untuk publikasi yang SUDAH punya relasi penulis → hapus dulu SEMUA baris core.publication_authors milik publikasi itu, lalu tulis ulang dari daftar detail, dalam transaksi core yang sama. Tanpa ini tiap publikasi punya dua set penulis (inisial lama + nama lengkap baru) karena primary key-nya mencakup author_id. **Tandai sebagai perubahan dari kebijakan lama "publication_authors tidak pernah dihapus".**
- **core.authors yatim**: JANGAN dihapus. Sediakan query SELECT (bukan DELETE) untuk melihat author tanpa relasi, tanpa metrics, dan scholar_id NULL — pembersihan diputuskan user nanti.

### 3f. Backfill

- `backfill_volume_pages.py`: **sesuaikan klausa WHERE ke kunci baru (title, publication_year)**, bukan hanya docstring. Masih diperlukan untuk data historis/run lama tanpa detail.
- `backfill_crossref.py`: TIDAK disentuh; tetap satu-satunya sumber DOI.

### 3g. CLI

- `run_pipeline.py`: tambah opsi `--tanpa-detail` (opsional, default detail ON). Tidak boleh merusak gaya pemanggilan run_batch (`--nama=nilai`).
- Catatan: run_batch akan mewarisi pengambilan detail secara default → risiko block lebih tinggi untuk batch; disebutkan sebagai trade-off.

### 3h. Urutan kerja, migrasi, verifikasi

- Migrasi baru (idempotent, jalan di DB berisi data, dijalankan manual via pgAdmin):
  - `04_create_raw_structured.sql` — raw.scholar_author + raw.scholar_publication dengan **UNIQUE (result_id, urutan)**.
  - `05_create_staging_tables.sql` — staging.stg_publications + staging.stg_authors.
  - `06_add_core_publication_columns.sql` — urutan langkah:
    1. **Guard** `DO $$ ... IF NOT EXISTS (kolom publication_year belum ada) ... END $$`. Di DALAM guard, berurutan: (a) `ADD COLUMN publication_year INTEGER` (+ issue, link); (b) isi `publication_year = EXTRACT(YEAR FROM publication_date)` untuk baris yang publication_date-nya tidak NULL; (c) BARU KEMUDIAN set `publication_date = NULL` untuk baris yang nilainya PERSIS 1 Januari dari tahun yang sama (semua tanggal lama adalah tanggal sintetis dari mapper). Di LUAR guard, migrasi TIDAK menyentuh data → tanggal asli 1 Januari yang masuk belakangan tidak terhapus kalau migrasi dijalankan ulang.
    2. **Cek pelanggaran** kunci baru (title, publication_year) sebelum menambah constraint — jika ada duplikat, hentikan dengan pesan jelas (DO block + raise exception).
    3. **Nasib constraint lama**: `DROP CONSTRAINT IF EXISTS uq_publications_title_publication_date` (didefinisikan di 03_create_raw_scrape_result.sql:121-122) — harus di-drop karena publication_date kini sering NULL dan NULLS NOT DISTINCT akan memblokir baris sah (title sama, tahun beda, tanggal NULL).
    4. `ADD CONSTRAINT uq_publications_title_publication_year UNIQUE NULLS NOT DISTINCT (title, publication_year)`.
  - **Urutan deploy**: migrasi 04–06 dijalankan di DB SEBELUM kode baru dipakai (loader `ON CONFLICT (title, publication_year)` butuh constraint baru sudah ada).
- Urutan implementasi: DDL → SQLAlchemy (postgres.py) → scraper + parser → mapper → loader → CLI → tes.
- Verifikasi offline: fixture HTML sebagai input parser (test_scraper.py, test_transformation.py boleh diisi). Verifikasi DB: butuh DB nyata (lihat 3i).

### 3i. Yang tidak bisa dibuktikan tanpa DB nyata

- Perilaku upsert/COALESCE, migrasi di DB berisi data (backfill + cek pelanggaran + swap constraint), dan integrasi end-to-end hanya bisa diverifikasi dengan DB nyata (user menjalankan migrasi via pgAdmin).
- Kolom yang tetap kosong: `doi` (sampai backfill_crossref dijalankan).

### 3j. Keputusan user: sumber & format nama penulis (DIPUTUSKAN)

- Temuan 2b: daftar = inisial + terpotong (5 dari 6 penulis); detail = nama lengkap + lengkap.
- **Keputusan**: halaman DETAIL = sumber otoritatif untuk publikasi yang detailnya berhasil diambil; tidak ada penggabungan/tebakan antar format. Detail gagal → daftar halaman daftar HANYA jika publikasi belum punya baris di core.publication_authors (kalau sudah punya, jangan diubah). `_bentuk_pendek` tetap cadangan untuk run tanpa detail. Kedua versi daftar penulis tersimpan mentah di raw.scholar_publication. Penerapan: lihat 3d (mapper) dan 3e (loader).

## Protokol Pengujian (wajib)

- **Jangan pernah menyentuh database utama `publication_dashboard`.** Salinan uji: `publication_test` (2522 baris core.publications hasil batch nyata; backup di `../backup_sebelum_revisi.dump`).
- Untuk semua pengujian, set environment proses: `$env:POSTGRES_DB="publication_test"` (PowerShell). Loader memakai `load_dotenv` yang TIDAK menimpa env var yang sudah ada → cara ini cukup. Jangan ubah `.env`; jangan minta password. Postgres di Podman, `localhost:5432`.
- **Verifikasi dua jalur migrasi**:
  1. DB KOSONG baru: jalankan 01..06 berurutan.
  2. `publication_test` (salinan data nyata): jalankan 04..06 saja.
  - Laporkan hasil nyata: jumlah baris core.publications, core.authors, core.publication_authors, core.publication_metrics SEBELUM dan SESUDAH. Cek pelanggaran di migrasi 06 WAJIB dijalankan pada data nyata ini.
- **Uji idempotensi run**: setelah migrasi, scrape ulang SATU profil kecil ke publication_test DUA KALI berturut-turut, lalu buktikan:
  - jumlah baris core.publications tidak bertambah di run kedua (dan tidak melonjak mendekati dua kali lipat);
  - publication_authors tidak ganda;
  - raw.scholar_publication tidak ganda saat run_id yang sama diulang.
- **Laporkan apa pun yang tetap belum teruji.**