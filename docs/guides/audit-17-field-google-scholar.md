# Audit 17 field Google Scholar → RAW

Audit 1 November 2026 mencakup parser, loader SQLAlchemy, DDL 01–14,
test scraper/database/runner, README, dan panduan scraping. Skema database
yang dikonfigurasi aplikasi juga diperiksa melalui `information_schema.columns`
dalam transaksi **READ ONLY**. Migration tidak diterapkan pada database pengguna.

Saat pekerjaan dilanjutkan setelah interupsi, `git status` menunjukkan empat
file terlacak berubah (parser, loader, dua file test) serta dua file baru
(migration 15 dan test field). Diff tersebut dipertahankan. Implementasi
dan test integrasi sudah tersimpan; dokumentasi audit belum dibuat.
Pengujian ulang awal membuktikan 642 test lulus. Pekerjaan lanjutan mencakup
dokumentasi, pemeriksaan diff, dan enam perbaikan kasus tepi dengan regression
test. Tidak ada reset, penghapusan perubahan, commit, atau push.

## A. Audit awal scraper

| No. | Field | Sebelum perubahan |
|---|---|---|
| 1 | Article Title | Ada, `#gsc_oci_title` |
| 2 | Authors | Ada, tetapi label Inggris `Authors` belum dikenali |
| 3 | Publication Date | Ada; mempertahankan tanggal/tahun sumber |
| 4 | Journal | Ada sebagai venue + label; belum terpisah |
| 5 | Conference | Ada sebagai venue + label; belum terpisah |
| 6 | Book | Label Book sengaja diabaikan implementasi lama |
| 7 | Publisher | Ada |
| 8 | Volume | Ada |
| 9 | Issue | Ada |
| 10 | Pages | Ada |
| 11 | Description | Ada, termasuk fallback label |
| 12 | DOI | Ada dari DOI eksplisit pada URL; belum dari label DOI |
| 13 | Total Citations | Ada, nullable TEXT; pemisah ribuan belum ditangani |
| 14 | Cited By | Belum menjadi struktur relasi |
| 15 | Related Articles | Belum diekstrak |
| 16 | All Versions | Belum diekstrak |
| 17 | Article URL | Ada, terpisah dari URL Scholar dan PDF |

## B. Audit awal database aktual

Semua kolom metadata publikasi di bawah nullable. `detail_url` adalah
halaman Scholar; URL artikel adalah `detail_link`.

| No. | Field | Kolom aktual sebelum perubahan | Tipe | Perlu kolom baru? |
|---|---|---|---|---|
| 1 | Article Title | `detail_title`, fallback `title` | TEXT | Tidak |
| 2 | Authors | `detail_authors`, `authors`; tabel penulis | TEXT | Tidak |
| 3 | Publication Date | `detail_date`; `publication_year` dari profil | TEXT; INTEGER | Tidak |
| 4 | Journal | `detail_venue` tanpa label terstruktur | TEXT | Ya, untuk membedakan tipe |
| 5 | Conference | `detail_venue` tanpa label terstruktur | TEXT | Ya, untuk membedakan tipe |
| 6 | Book | Tidak ada kolom spesifik | — | Ya |
| 7 | Publisher | `detail_publisher` | TEXT | Tidak |
| 8 | Volume | `detail_volume` | TEXT | Tidak |
| 9 | Issue | `detail_issue` | TEXT | Tidak |
| 10 | Pages | `detail_pages` | TEXT | Tidak |
| 11 | Description | `detail_description` | TEXT | Tidak |
| 12 | DOI | `detail_doi` | TEXT | Tidak |
| 13 | Total Citations | `detail_citations`; `citations` dari profil | TEXT | Tidak; hindari duplikasi hitungan |
| 14 | Cited By | Tidak ada kolom spesifik | — | Ya, JSONB |
| 15 | Related Articles | Tidak ada kolom spesifik | — | Ya, JSONB |
| 16 | All Versions | Tidak ada kolom spesifik | — | Ya, JSONB |
| 17 | Article URL | `detail_link`; URL lain `detail_url`, `detail_pdf_link` | TEXT | Tidak |

`raw.scholar_publication_author` sudah memiliki `author_name TEXT NOT NULL`,
`author_order INTEGER NOT NULL`, serta FK `raw_publication_id INTEGER NOT NULL`.
Nama dan urutan sumber dipertahankan; singkatan tidak diperluas di RAW.
Tidak ditambahkan JSONB penulis. `raw.scholar_scrape_result.raw_envelope`
adalah JSONB NOT NULL yang menyimpan envelope asli, termasuk
`detail_venue_label` yang sebelumnya hanya ada dalam envelope.

RAW terstruktur menggunakan UNIQUE `(result_id, urutan)` dan menyimpan
snapshot **per run**. Kolom serupa dari profil (`profile_volume/issue/pages`,
`venue`, `citations`) tetap dibedakan dari sumber halaman detail.

## C. Perubahan parser

`src/ingestion/scrape_scholar_playwright.py`:

- `parse_detail_html`: label Authors/DOI/Total citations, pemisahan venue,
  tipe publikasi berdasarkan bukti, pasangan field/value lintas variasi tag,
  teks nested dengan spasi, status ekstraksi, dan relasi.
- `_parse_relations`: membaca tautan DOM saja; tidak menavigasi target.
  URL rusak tidak menggagalkan metadata lain; blok relasi tanpa tautan
  ditandai `extraction_error`. Deskripsi kosong menjadi NULL dan log DOI
  tidak menyebut NULL jika DOI eksplisit berhasil disimpan.
  Konteks blok relasi diprioritaskan dibanding parameter URL, agar judul
  artikel terkait dengan URL cluster tetap tersimpan sebagai artikel terkait.
  Label tanpa value tidak mengambil metadata dari blok lain.
- `_ekstrak_sitasi_dari_blok`: menangani pemisah ribuan, tetap mengabaikan
  histogram tahun. Kontrak tetap string angka atau NULL.
- `DETAIL_KEYS` / `_detail_kosong`: seluruh key baru tersedia pada jalur
  detail berhasil maupun detail tidak diambil/gagal.
- Fase detail meneruskan URL sumber agar tautan relatif memakai domain
  Scholar yang sebenarnya. Alur profil, batch, retry dan `--lanjut` dipertahankan.

Jenis disimpan sebagai `journal`, `conference`, `book`, `book_chapter`,
`thesis`, atau `unknown`. Conference/Proceedings dikenali dari label
Conference/Konferensi/Proceedings/Prosiding; COMNETSAT dengan metadata
Conference menjadi `conference`. Book chapter membutuhkan label atau tipe
eksplisit. Thesis/dissertation membutuhkan label/tipe eksplisit. Tugas akhir
tanpa bukti venue/tipe tetap `unknown`. Bukti jenis yang bertentangan
menghasilkan `unknown`; judul dan publisher tidak dipakai menebak jenis.

## D. Perubahan database dan penyimpanan

Migration: `sql/ddl/15_add_raw_scholar_detail_fields.sql`.

| Kolom baru | Tipe | Alasan |
|---|---|---|
| `detail_journal` | TEXT | Venue berlabel Journal/Jurnal |
| `detail_conference` | TEXT | Venue konferensi/proceedings |
| `detail_book` | TEXT | Metadata buku/book chapter eksplisit |
| `detail_publication_type` | TEXT | Jenis berdasarkan bukti metadata |
| `detail_extraction_status` | TEXT | Bedakan halaman terparse dari HTML tak dikenali |
| `detail_cited_by` | JSONB | Referensi sitasi pada halaman |
| `detail_related_articles` | JSONB | Referensi artikel terkait |
| `detail_all_versions` | JSONB | Referensi versi lain |

Migration hanya memakai `ADD COLUMN IF NOT EXISTS`, dalam transaksi.
Tidak ada perubahan nama/tipe kolom lama, backfill, penghapusan data,
atau perubahan STAGING/CORE/MART. Database baru menjalankan migration 15
setelah DDL sebelumnya; DDL 04 historis tidak diubah.

`src/loading/postgres.py`: deklarasi `RAW_SCHOLAR_PUBLICATION` dan
`KOLOM_RAW_PUBLICATION` memetakan semua kolom baru ke INSERT parameterized.
JSONB memakai `none_as_null=True` agar Python None menjadi SQL NULL.
Envelope, profil, publikasi, dan relasi penulis tetap ditulis dalam satu
transaksi RAW; kegagalan penulis me-rollback seluruh run.

**Kontrak INSERT/UPDATE/UPSERT:** envelope menggunakan
`ON CONFLICT (run_id) DO UPDATE SET result_id = result_id` untuk memperoleh
ID tanpa menimpa envelope lama. Publikasi dan penulis menggunakan
`ON CONFLICT ... DO NOTHING`. Retry run yang sama mempertahankan snapshot
pertama, termasuk saat metadata retry NULL. Run baru menambahkan snapshot
baru; metadata parsial tidak menghapus baris historis. Tidak ditambahkan
UPDATE lintas run atau merge otomatis yang menghilangkan provenance sumber.
Metadata baru pada retry run yang sama juga tidak mengganti snapshot pertama;
gunakan run baru untuk menyimpan hasil pengambilan ulang.

## E. Mapping akhir 17 field

Semua parser key dipetakan ke kolom bernama sama pada
`raw.scholar_publication`, kecuali relasi penulis yang juga dinormalisasi.
Kolom baru tersedia pada database aplikasi setelah operator menerapkan migration 15.

| Field Google Scholar | Parser | Kolom database | Tipe | Pengujian |
|---|---|---|---|---|
| Article Title | `detail_title` | `detail_title` | TEXT | Offline + PostgreSQL |
| Authors | `detail_authors` | `detail_authors` + tabel penulis | TEXT + relasi | Offline + PostgreSQL |
| Publication Date | `detail_date` | `detail_date` | TEXT | Offline + PostgreSQL |
| Journal | `detail_journal` | `detail_journal` | TEXT | Offline + PostgreSQL |
| Conference | `detail_conference` | `detail_conference` | TEXT | Offline + PostgreSQL |
| Book | `detail_book` | `detail_book` | TEXT | Offline + PostgreSQL |
| Publisher | `detail_publisher` | `detail_publisher` | TEXT | Offline + PostgreSQL |
| Volume | `detail_volume` | `detail_volume` | TEXT | Offline + PostgreSQL |
| Issue | `detail_issue` | `detail_issue` | TEXT | Offline + PostgreSQL |
| Pages | `detail_pages` | `detail_pages` | TEXT | Offline + PostgreSQL |
| Description | `detail_description` | `detail_description` | TEXT | Offline + PostgreSQL |
| DOI | `detail_doi` | `detail_doi` | TEXT | Offline + PostgreSQL |
| Total Citations | `detail_citations` | `detail_citations` | TEXT nullable | Offline + PostgreSQL |
| Cited By | `detail_cited_by` | `detail_cited_by` | JSONB | Offline + PostgreSQL |
| Related Articles | `detail_related_articles` | `detail_related_articles` | JSONB | Offline + PostgreSQL |
| All Versions | `detail_all_versions` | `detail_all_versions` | JSONB | Offline + PostgreSQL |
| Article URL | `detail_link` | `detail_link` | TEXT | Offline + PostgreSQL |

## F. Pengujian

Baseline: **625 passed**. Pengujian ulang awal setelah melanjutkan sesi:
**642 passed, 0 failed, 0 skipped** dengan opsi integrasi aktif.
Enam regression test tambahan memeriksa kasus tepi yang ditemukan saat
review akhir. Validasi terakhir: **648 passed, 0 failed, 0 skipped** dalam
6,92 detik, termasuk integrasi PostgreSQL pada database test terpisah.
Fixture integrasi mengembalikan mode transaksi setelah migration sebelum
menguji rollback; koneksi autocommit tidak dikembalikan ke pool.
Review terpisah mengidentifikasi kesalahan prioritas konteks relasi; temuan
tersebut diperbaiki dan dikonfirmasi ulang tanpa regresi penting tambahan.
Assertion lama yang sengaja menolak Book dan membatasi kolom ke 25 telah
diperbarui untuk kontrak baru; perilaku lama lainnya tetap diuji.

`tests/test_scholar_fields.py` bersama test scraper/database lama mencakup
18 skenario tugas: jurnal lengkap/tanpa DOI, COMNETSAT, buku/book chapter,
thesis/dissertation, tanpa venue, banyak penulis, tanpa deskripsi/sitasi,
ketiga relasi, metadata parsial, HTML berubah/elemen hilang, retry NULL,
JSONB, mapping 17 field, dan migration pada tabel berisi data.
Test PostgreSQL juga memeriksa SQL NULL, urutan penulis, rollback transaksi,
run baru parsial, tipe kolom aktual, serta migration dua kali.
Seluruh input scraper berasal dari HTML fixture/mock, tanpa Scholar live.

Percobaan lama dengan 38 error disebabkan akses folder temporary Windows
ditolak; pengujian ulang dengan izin yang sesuai berhasil. Pesan interupsi
percakapan tidak mengubah hasil test atau menghapus perubahan tersimpan.

## G. Status migration

Migration dibuat dan **sudah diuji pada database test baru yang terpisah**,
dengan data historis sintetis sebelum penerapan. Tidak diterapkan pada
database aplikasi/pengguna dan tidak mereset database mana pun.
Fixture opt-in membuat database unik `scholar_raw_fields_test_<random>`;
database test dipertahankan untuk pemeriksaan, termasuk database dari
percobaan awal fixture. Tidak ada penghapusan database otomatis.
Kode loader baru memerlukan migration 15 sebelum scraping yang menulis RAW.

## H. Batasan dan struktur JSONB

Judul profil yang valid diperlukan untuk memasukkan publikasi ke pipeline;
seluruh metadata detail opsional. Tanggal disimpan TEXT karena Scholar
dapat memberi tahun, tahun/bulan, atau tanggal penuh. Volume/issue/pages
tetap TEXT agar format seperti XII/S1/e123 tidak hilang. Sitasi detail
disimpan TEXT sesuai skema lama; nilai tak ditemukan tetap NULL, bukan nol.
Hitungan profil lama memiliki kontraknya sendiri dan bukan sumber default
untuk mengganti metadata detail yang hilang.

DOI hanya dari label DOI atau URL yang memuat DOI eksplisit; tidak ada
tebakan dari judul/publisher. DOI berlabel eksplisit diprioritaskan.
Journal, Conference dan Book tidak selalu tersedia bersamaan. `detail_venue`
tetap menyimpan venue sumber dan tidak otomatis dianggap jurnal.

Contoh relasi:

```json
{
  "status": "available",
  "items": [
    {
      "title": null,
      "label": "Cited by 8",
      "url": "https://scholar.google.com/scholar?cites=123",
      "identifier": "123"
    }
  ]
}
```

`title` diisi hanya jika judul artikel tertaut tersedia dalam blok relasi;
teks navigasi seperti Cited by/All versions disimpan sebagai `label`.
Identifier diambil dari parameter `cites`, `q=related:...`, atau `cluster`
jika ada. Tautan sudah cukup sebagai referensi; ini bukan klaim daftar
artikel sitasi lengkap. Maksimum 100 item unik per jenis pada satu halaman.
Tidak ada navigasi halaman relasi atau crawling artikel satu per satu.

- `available`: ada URL HTTP(S) atau identifier yang bisa disimpan.
- `unavailable`: halaman dikenali, tetapi relasi tidak ditampilkan.
- `extraction_error`: indikasi relasi ada, tetapi tautan/identifier hilang.
- SQL NULL: detail belum diambil/gagal, HTML tak dikenali, atau data historis.

`detail_extraction_status='parsed'` berarti struktur halaman dikenali;
`unrecognized_html` berarti struktur tak dikenali. Kegagalan pengambilan
atau parse exception tetap tercatat di envelope `data.failures` dan status
run yang sudah ada. Jangan menyimpulkan tidak ada relasi dari SQL NULL.
`detail_url` = URL halaman Google Scholar, `detail_link` = artikel/sumber,
`detail_pdf_link` = PDF. Ketiganya dipertahankan terpisah.

## I. Perintah penggunaan dan pemeriksaan

Setelah operator menerapkan migration 15 pada target yang telah ditinjau:

```powershell
python -m src.pipeline.run_pipeline --id SCHOLAR_ID_ANDA
python -m src.pipeline.run_batch --file "data/dosen/Data dosen.xlsx" --lanjut
python -m pytest -q -p no:cacheprovider
```

Contoh batch hanya untuk penggunaan operator; tidak dijalankan pada audit ini.
Test integrasi membuat database BARU dengan koneksi server dari konfigurasi
aplikasi dan memerlukan hak CREATE DATABASE. Jalankan hanya pada server test:

```powershell
$env:SCHOLAR_RAW_INTEGRATION='1'
python -m pytest -q -p no:cacheprovider --tb=short
Remove-Item Env:SCHOLAR_RAW_INTEGRATION
```

SQL baca-saja setelah migration:

```sql
SELECT current_database(), current_user;
SELECT table_name, column_name, data_type, is_nullable
FROM information_schema.columns
WHERE table_schema = 'raw'
  AND table_name IN ('scholar_publication', 'scholar_publication_author', 'scholar_scrape_result')
ORDER BY table_name, ordinal_position;

SELECT raw_publication_id, result_id, detail_title, detail_authors,
       detail_date, detail_journal, detail_conference, detail_book,
       detail_publisher, detail_volume, detail_issue, detail_pages,
       detail_description, detail_doi, detail_citations,
       detail_cited_by, detail_related_articles, detail_all_versions,
       detail_url, detail_link, detail_pdf_link,
       detail_publication_type, detail_extraction_status
FROM raw.scholar_publication
WHERE scholar_id = 'SCHOLAR_ID_ANDA'
ORDER BY result_id DESC, urutan;

SELECT a.raw_publication_id, a.author_order, a.author_name, a.sumber
FROM raw.scholar_publication_author a
JOIN raw.scholar_publication p USING (raw_publication_id)
WHERE p.scholar_id = 'SCHOLAR_ID_ANDA'
ORDER BY p.result_id DESC, p.urutan, a.author_order;
```
