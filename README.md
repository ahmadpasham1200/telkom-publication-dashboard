# Telkom Publication Dashboard

Proyek ini mengumpulkan data publikasi akademik dosen dari Google Scholar dan menyimpannya ke PostgreSQL untuk mendukung pengelolaan serta analisis publikasi. Ruang lingkup utama saat ini adalah **scraping dan penyimpanan ke lapisan RAW**.

Scraper menggunakan Playwright (Chromium) dan BeautifulSoup. Secara default, runner tidak menjalankan transformasi/loading ke STAGING atau CORE, enrichment, maupun pemeriksaan indeks jurnal. Opsi lanjutan `--load-core` tersedia secara eksplisit, tetapi berada di luar alur utama panduan ini. Dashboard Superset dan orkestrasi Prefect belum diimplementasikan sebagai layanan aktif dalam repository; `flow.py` dan skema validasi masih berupa placeholder.

## Alur utama

1. Runner menerima ID profil, atau membaca daftar ID dari Excel untuk mode batch.
2. Scraper mengambil profil dan daftar publikasi, kemudian mencoba mengambil halaman detail setiap publikasi (kecuali `--tanpa-detail` pada runner satu profil).
3. Hasil dikemas sebagai envelope: identitas run, status, waktu, data, dan informasi kegagalan.
4. Loader menyimpan envelope ke `raw.scholar_scrape_result`. Bila data dan ID hasil tersedia, loader juga menyimpan profil, publikasi, dan penulis publikasi ke `raw.scholar_author`, `raw.scholar_publication`, dan `raw.scholar_publication_author` dalam transaksi RAW yang sama.
5. Runner mencetak ringkasan. Mode batch juga mencatat progress CSV untuk setiap dosen.

Run fatal yang sudah menghasilkan envelope tetap dicoba disimpan ke RAW. Jika scraper gagal sebelum envelope terbentuk, tidak ada envelope yang dapat disimpan. Keberhasilan penyimpanan RAW tidak berarti scraping lengkap atau berhasil.

## Struktur penting

```text
src/
  ingestion/        # Scraper Google Scholar dan parser HTML
  loading/          # Koneksi PostgreSQL, penyimpanan, record penulis
  pipeline/         # CLI satu profil dan batch; flow.py masih placeholder
  transformation/   # Mapper untuk pemrosesan lanjutan
  enrichment/       # Backfill terpisah, bukan bagian scraping default
  journal_index/    # Katalog/verifikasi jurnal dengan CLI terpisah
  validation/       # Placeholder validasi
tests/             # Pengujian parser, loader, CLI, dan modul lainnya
fixtures/scholar/   # HTML acuan pengujian; harus dipertahankan
sql/ddl/           # DDL dan perubahan skema bernomor
sql/maintenance/   # SQL pemeliharaan manual, terpisah dari inisialisasi
docs/              # Dokumentasi dan rencana historis; sebagian masih kosong
ERD/               # Ekspor gambar ERD
docker/            # Saat ini hanya placeholder .gitkeep
data dosen/        # Excel dan progress lokal; diabaikan Git
compose.yml        # Layanan PostgreSQL 16 dan named volume pg_data
requirements.txt   # Dependensi runtime dan pengujian dalam satu file
```

## Prasyarat dan instalasi

Gunakan Python 3.12 sebagai acuan (lingkungan audit menggunakan 3.12.7), pip, akses internet untuk pengambilan halaman, Chromium dari Playwright, serta PostgreSQL 15 atau lebih baru. DDL memakai `UNIQUE NULLS NOT DISTINCT`; Compose menyediakan PostgreSQL 16. Docker dengan Compose bersifat opsional jika PostgreSQL sudah tersedia.

Jalankan perintah dari root repository. Contoh penyiapan di PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m playwright install chromium
Copy-Item .env.example .env
```

Salin template hanya jika `.env` belum ada. Jangan menimpa konfigurasi lokal yang sudah digunakan. Pada Linux/macOS, aktivasi environment menggunakan `source .venv/bin/activate` dan penyalinan menggunakan `cp .env.example .env`.

Instalasi browser merupakan langkah terpisah dari instalasi paket; lihat [dokumentasi library Playwright](https://playwright.dev/python/docs/library). Repository tidak membutuhkan plugin `pytest-playwright` untuk pengujian yang ada.

`requirements.txt` mengelompokkan runtime dan pengujian melalui komentar, tanpa file dependensi tambahan. `requests` dipakai modul enrichment/indeks jurnal; `pytest` dipakai pengujian. Versi langsung mengacu pada metadata paket lingkungan audit, bukan klaim versi minimum atau matriks kompatibilitas. Dependensi transitif belum dikunci, dan instalasi environment bersih belum diverifikasi. Loader menggunakan API internal JSONB SQLAlchemy, sehingga upgrade perlu pengujian khusus.

## Konfigurasi dan kesiapan PostgreSQL

Edit `.env` secara lokal menggunakan nama variabel yang dibaca `src/loading/postgres.py`:

| Variabel | Fungsi |
|---|---|
| `POSTGRES_HOST` | Host server; default kode `localhost` |
| `POSTGRES_PORT` | Port server; default kode `5432` |
| `POSTGRES_DB` | Nama database target; wajib diisi |
| `POSTGRES_USER` | Role database; gunakan role ETL dengan hak yang sesuai |
| `POSTGRES_PASSWORD` | Password role; isi secara lokal |

Environment proses yang sudah terpasang mengalahkan nilai `.env`. Sebelum menjalankan perintah yang menulis data, pastikan host, port, role, dan database benar; untuk database utama gunakan `POSTGRES_DB=publication_dashboard`. Verifikasi target pada klien database dengan query baca-saja:

```sql
SELECT current_database(), current_user;
```

Runner scraping tidak memiliki pemeriksaan otomatis bahwa nama database harus `publication_dashboard`. Pemeriksaan target tetap tanggung jawab operator; jangan menganggap guard modul indeks jurnal juga berlaku pada scraper.

Jangan membagikan `.env`, mencetak connection string berkredensial, atau memasukkan rahasia ke argumen CLI. `.env` dan folder data lokal sudah diabaikan Git. Variabel Superset dan `SCHOLAR_PROXY_ENABLED` pada template tidak digunakan oleh alur scraping saat ini; tidak ada dukungan proxy aktif yang dapat disimpulkan dari template tersebut.

Siapkan database dan DDL sebelum scraping. Tinjau [sql/ddl/](sql/ddl/) sesuai urutan nomor dengan pengelola database, termasuk perubahan kolom dan tabel RAW terstruktur. Loader tidak membuat tabel otomatis. Role ETL memerlukan hak tabel dan sequence yang sesuai; pembuatan role/hak akses tidak disediakan sebagai prosedur setup lengkap di repository.

Untuk database development baru, layanan pada [compose.yml](compose.yml) dapat dijalankan setelah konfigurasi ditinjau:

```sh
docker compose up -d postgres
```

Perintah tersebut membuat/menjalankan layanan dan dapat menjalankan DDL pada inisialisasi volume kosong. Compose memasang `sql/ddl/` ke direktori inisialisasi PostgreSQL; database dengan volume yang sudah berisi data tidak otomatis menerapkan file SQL baru. Jangan menghapus volume untuk memperbarui skema. Compose menggunakan `POSTGRES_USER` sebagai pengguna bootstrap; jangan menganggapnya otomatis role ETL dengan hak terbatas. Port host Compose tetap `5432`, sedangkan `POSTGRES_PORT` mengatur koneksi aplikasi.

## Scraping satu profil

Ganti `SCHOLAR_ID_ANDA` dengan nilai parameter `user` pada URL profil, bukan seluruh URL:

```sh
python -m src.pipeline.run_pipeline --id SCHOLAR_ID_ANDA
```

Bentuk skrip juga tersedia: `python src/pipeline/run_pipeline.py --id SCHOLAR_ID_ANDA`. Untuk ID yang diawali tanda minus, gunakan `--id=SCHOLAR_ID_ANDA`. Selalu isi ID secara eksplisit karena kode memiliki ID bawaan.

```sh
python -m src.pipeline.run_pipeline --id SCHOLAR_ID_ANDA --headed --batch 5 --tampil 10
python -m src.pipeline.run_pipeline --id SCHOLAR_ID_ANDA --tanpa-detail
```

| Argumen satu profil | Perilaku |
|---|---|
| `--id` | ID profil Google Scholar |
| `--headed` | Menampilkan browser; default headless |
| `--tanpa-detail` | Melewati pengambilan detail; kelengkapan metadata/nama dapat berkurang |
| `--batch` | Batas klik "Tampilkan lainnya", default 60; bukan jumlah profil |
| `--tampil` | Batas baris tabel di terminal, default 20; gunakan 0 untuk tidak mencetak |
| `--diam` | Mengurangi progress scraper, bukan seluruh ringkasan |
| `--referensi-dosen` | Excel referensi identitas; default `data dosen/Data dosen.xlsx` |
| `--raw-only` | Menegaskan mode default, berhenti setelah RAW |
| `--load-core` | Opsi lanjutan untuk transformasi/loading STAGING/CORE; bukan alur utama |
| `--url` | Tersedia pada parser, tetapi bukan pengganti konfigurasi koneksi RAW; lihat batasan di bawah |
| `--echo-sql` | Opsi log SQL pada koneksi yang menerima flag; tidak diteruskan ke writer RAW dan dapat mengekspos parameter pada log |
| `-h`, `--help` | Menampilkan bantuan tanpa memulai scraping |

Referensi Excel yang tidak ada/tidak terbaca menonaktifkan verifikasi identitas tanpa menghentikan run. Nama dan ID yang belum dapat diverifikasi tidak boleh dianggap pasti milik pemilik profil.

**Batasan koneksi:** `run_pipeline` tidak meneruskan `--url` ke writer RAW yang membuka koneksinya sendiri melalui `connect()`. Pada mode default, target penulisan RAW tetap dari `.env`/environment; pada `--load-core`, URL dapat memengaruhi koneksi CORE sehingga target RAW dan CORE berpotensi berbeda. Panduan ini menggunakan konfigurasi environment dan tidak memakai `--url`.

## Scraping batch dari Excel

Sediakan workbook lokal, misalnya `data dosen/Data dosen.xlsx`. Kolom `scholar_id` wajib ada; gunakan juga header `no`, `nama`, dan opsional `prodi`. Header dibaca tanpa membedakan huruf besar/kecil. Kode memilih `Sheet1` bila tersedia, selain itu sheet pertama. Baris yang seluruhnya kosong dilewati; baris data tanpa ID dicatat `DILEWATI`. Kolom `nama` dan ID menjadi referensi verifikasi identitas, sedangkan `prodi` hanya pelengkap.

```sh
python src/pipeline/run_batch.py --file "data dosen/Data dosen.xlsx" --lanjut
```

Bentuk modul `python -m src.pipeline.run_batch` juga tersedia. Batch memakai file `--file` sebagai referensi identitas bagi runner satu profil.

| Argumen batch | Perilaku |
|---|---|
| `--file` | Wajib: path workbook Excel |
| `--jeda` | Jeda antarprofil dalam detik, default 8; tidak menjamin terhindar dari pembatasan akses |
| `--lanjut` | Melewati ID dengan status terminal pada progress |
| `--ulang` | Mengabaikan filter `--lanjut`; mengulang kandidat setelah filter `--mulai` |
| `--mulai` | Mulai dari baris data ke-N, 1-based, default 1; bukan nomor baris header Excel |
| `--progress` | Path CSV, default `data dosen/progress_batch.csv` |
| `--headed`, `--batch` | Diteruskan ke runner satu profil; `--batch` tetap batas klik per profil |
| `--tampil` | Baris tabel per profil; default batch 0 |
| `--diam` | Diterima untuk kompatibilitas; batch selalu meneruskannya |
| `--raw-only`, `--load-core` | Default RAW; pemrosesan lanjutan harus eksplisit |
| `--url`, `--echo-sql` | Diteruskan, dengan batasan koneksi RAW yang sama seperti satu profil |
| `-h`, `--help` | Bantuan tanpa memulai batch |

Batch tidak menyediakan `--tanpa-detail` atau `--referensi-dosen` sebagai argumen tersendiri.

## Memeriksa hasil dan melanjutkan proses

Periksa `run_id`, `raw_result_id`, status run/load, `records_fetched`, `records_failed`, serta ringkasan penulis di terminal. Pada envelope RAW, periksa juga `error_message` dan `data.failures`. Contoh query baca-saja pada database target:

```sql
SELECT result_id, scholar_id, run_id, status, scraped_at,
       raw_envelope ->> 'error_message' AS error_message,
       raw_envelope -> 'data' -> 'failures' AS failures
FROM raw.scholar_scrape_result
ORDER BY result_id DESC
LIMIT 20;
```

Lihat pula `raw.scholar_publication` melalui `result_id` dan `raw.scholar_publication_author` melalui `raw_publication_id` bila perlu menelusuri penulis. Jangan menyamakan isi tabel RAW dengan data yang sudah bersih atau bebas duplikasi.

Status scraper mencakup `SUCCESS`, `PARTIAL_SUCCESS`, `BLOCKED`, `TIMEOUT`, `NO_DATA`, dan `PARSING_ERROR`. Detail atau nama penulis tidak selalu lengkap. Batas `--batch` dapat menghasilkan `PARTIAL_SUCCESS` meskipun `records_failed=0` dan daftar kegagalan kosong.

Pada mode RAW default, exit code satu profil adalah 0 untuk penyimpanan run nonfatal (termasuk parsial), 1 untuk kegagalan load, 2 untuk kegagalan scraping, dan 130 untuk interupsi. Kesalahan argumen juga dapat menghasilkan exit 2. Status fatal dapat tetap tercatat di RAW; exit 0 bukan jaminan kelengkapan.

CSV progress memuat `scholar_id`, `nama`, `status`, `exit_code`, dan `waktu`:

| Status batch | Makna |
|---|---|
| `OK_RAW` | Exit 0 pada mode RAW; dapat mencakup hasil parsial |
| `OK` | Exit 0 pada mode lanjutan; periksa ringkasan load untuk hasil sebenarnya |
| `GAGAL_LOAD` | Exit 1 dari runner |
| `GAGAL_SCRAPE` | Exit 2 dari runner |
| `DILEWATI` | Baris tanpa ID profil |
| `GAGAL` | Kegagalan lain |

`--lanjut` melewati `OK`, `OK_RAW`, `GAGAL_LOAD`, `GAGAL_SCRAPE`, dan `DILEWATI`; jadi bukan otomatis mencoba ulang semua kegagalan. `GAGAL` tidak terminal. Untuk melengkapi profil parsial atau mencoba ulang profil gagal, jalankan satu profil secara eksplisit. `--ulang` dapat mengulang banyak profil dan menghasilkan run baru; tinjau daftar terlebih dahulu. Jangan mengedit/menyimpan ulang CSV dengan Excel saat proses berjalan.

Exit batch 0 berarti tidak ada kegagalan di antara kandidat yang diproses saat itu; status lama yang dilewati tidak dihitung ulang. Exit 1 berarti ada kegagalan, 2 berarti daftar tidak terbaca, dan 130 berarti interupsi. Ringkasan batch juga mencoba membaca RAW untuk kandidat sukses; pembacaan tambahan ini bukan bukti kelengkapan.

## Batasan dan troubleshooting

- **Chromium tidak ditemukan:** jalankan `python -m playwright install chromium` pada environment yang sama dengan runner.
- **Modul tidak ditemukan:** aktifkan environment, jalankan instalasi dari `requirements.txt`, dan gunakan root repository sebagai direktori kerja.
- **Koneksi/tabel gagal:** periksa layanan, host/port, target database, role, hak tabel/sequence, dan DDL yang sudah diterapkan. Jangan menyalin rahasia ke laporan masalah.
- **Referensi Excel gagal:** pastikan workbook valid, header `scholar_id` tersedia, dan path benar. Kegagalan referensi satu profil menurunkan verifikasi identitas; kegagalan daftar batch menghentikan batch.
- **Timeout, CAPTCHA, halaman blokir, atau HTTP 429:** hasil dapat fatal atau parsial. HTTP 429 pada fase detail menghentikan pengambilan detail berikutnya; data daftar yang sudah diperoleh dipertahankan. Hentikan percobaan berulang, beri jeda, lalu tinjau status dan kegagalan. `--headed` membantu pengamatan, bukan jaminan berhasil.
- **Nama penulis/metadata tidak lengkap:** halaman daftar dapat menampilkan nama terpotong dan halaman detail dapat gagal atau tidak memuat field tertentu. Periksa sumber serta penandanya; jangan mengarang nilai yang hilang.
- **Fitur lanjutan:** enrichment/backfill, `--load-core`, dan modul indeks jurnal memiliki dampak tersendiri. Tidak dijalankan otomatis oleh alur RAW. Skema Pandera dan flow Prefect belum menjadi validasi/orkestrasi aktif.

## Pengujian dan referensi

Contoh pemeriksaan CLI yang tidak memulai scraping atau membuka koneksi database:

```sh
python -B -m src.pipeline.run_pipeline --help
python -B src/pipeline/run_batch.py --help
```

Pengujian terarah berikut menggunakan parser, data sintetis, dan pengganti koneksi/scraper; tidak memerlukan scraping atau database hidup. Direktori sementara pytest tetap digunakan:

```sh
python -B -m pytest -p no:cacheprovider tests/test_run_pipeline.py tests/test_run_batch.py tests/test_transformation.py tests/test_author_records.py -q
```

Pengujian tiruan tidak membuktikan keberhasilan end-to-end terhadap Google Scholar atau PostgreSQL sebenarnya.

- [DDL dan perubahan skema](sql/ddl/), khususnya [envelope RAW](sql/ddl/03_create_raw_scrape_result.sql), [RAW terstruktur](sql/ddl/04_create_raw_structured.sql), [penulis publikasi](sql/ddl/10_create_raw_publication_author.sql), dan [prodi referensi](sql/ddl/11_add_raw_scholar_author_prodi.sql).
- [SQL pemeliharaan manual](sql/maintenance/) - jangan ditempatkan pada inisialisasi otomatis.
- [Dokumentasi modul indeks jurnal](src/journal_index/README.md) - terpisah dari scraping default, termasuk pemeriksaan target database sebelum penulisan.
- [Pembahasan ERD](docs/PEMBAHASAN%20ERD%20DATABASE%20PUBLICATION.pdf) dan [gambar ERD](ERD/drawSQL-image-export-2026-10-25.webp).
- [Snapshot skema PostgreSQL](publication_dashboard_schema.sql) - artefak skema, bukan pengganti urutan migrasi.
- [Rencana historis revisi pipeline](docs/superpowers/plans/2026-10-09-revisi-pipeline-4-layer.md) - konteks desain; perilaku aktual tetap mengikuti kode.
