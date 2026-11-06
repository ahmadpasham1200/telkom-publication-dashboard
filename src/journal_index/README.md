# Journal index

Modul ini hanya membaca `raw.scholar_publication`. Tidak menjalankan Scholar.

```sh
python -m src.journal_index.run_journal_index --help
python -m src.journal_index.run_journal_index --dry-run --limit 10
python -m src.journal_index.run_journal_index --source scimago --dry-run --limit 3 --row-limit 20
python -m src.journal_index.run_journal_index --sync-catalog --limit 20
python -m src.journal_index.run_journal_index --check-scopus --limit 2
python -m src.journal_index.run_journal_index --check-scopus --limit 2 --save-verification
python -m src.journal_index.run_journal_index --save --limit 10 --source all
python -m src.journal_index.run_journal_index --save --retry-failed --limit 10
```

Tanpa mode eksplisit, program hanya preview. `--dry-run` tidak dapat digabungkan
dengan mode penulisan. `--sync-catalog` hanya menulis katalog, tanpa pemeriksaan
sumber. `--check-scopus` hanya memeriksa maksimal dua kandidat jurnal unik dan
tidak menulis apa pun kecuali digabungkan dengan `--save-verification`.
`--save` memeriksa sumber yang dipilih dan menyimpan hasil, maksimal `--limit`.
Semua perintah penulisan pada contoh hanya petunjuk penggunaan, tidak dijalankan
otomatis. Preview memerlukan PostgreSQL tetapi tidak
memerlukan kredensial sumber. `POSTGRES_DB` wajib dan dibandingkan dengan URL engine
serta `current_database()` sebelum membaca atau menulis. Koneksi memakai konfigurasi
`src.loading.postgres.build_engine()` yang sudah ada.
Mode penulisan juga mewajibkan `POSTGRES_DB=publication_dashboard`; nama engine
dan nama database aktual harus cocok sebelum DDL/upsert. Mode baca saja dapat
memakai database pengujian. Fungsi database internal mengikuti guard pada runner;
jangan memanggilnya langsung terhadap koneksi yang belum divalidasi.

Khusus `--source scimago` (alias `scimagojr`), preview menjalankan pencocokan
CSV lokal dan menampilkan status hasil sebenarnya, tetap tanpa HTTP, DDL, atau
penulisan. Preview sumber lain/default `all` tetap menampilkan rencana kandidat.
Lihat bagian SCImagoJR Master Export di bawah untuk klasifikasi dan penyimpanan.

Default hanya melihat 1000 publikasi pertama berdasarkan ID, menghasilkan maksimal
20 kandidat untuk tindakan. `--row-limit` maksimum 10000; `--limit` maksimum 200.
Ringkasan dry-run menghitung seluruh jendela ini, sebelum limit tindakan:
`publications_with_venue` mencakup semua publikasi ber-venue,
`candidate_publications` mengecualikan venue nonjurnal, `unique_journals`
mencakup jurnal dan kandidat ambigu, `excluded_journals` menghitung venue unik
yang dikecualikan, dan `ambiguous_journals` menghitung kandidat yang perlu verifikasi.
Ini bukan hitungan seluruh database jika jendela sumber terpotong. Asal publikasi
yang disimpan hanya yang ditemukan dalam jendela ini, bukan klaim daftar lengkap.
Nama asli dan variasinya dipertahankan dalam `publication_origins`.
Klasifikasi bersifat heuristik konservatif. Kandidat nonjurnal dilewati sepenuhnya:
tidak diminta ke sumber dan tidak dibuatkan hasil pemeriksaan, termasuk saat retry.
Nama tanpa penanda eksplisit seperti `IEEE Access` tetap `ambiguous` pada preview,
dengan `verification_required: true` dan aksi `verify_identity`. Tidak ada daftar
nama jurnal yang dijadikan bukti indeks. Pada `--save`, kandidat ambigu diteruskan
ke adapter sumber untuk pencocokan identitas yang sama ketatnya dengan kandidat
`journal`. Klaim indeks hanya berasal dari kecocokan dan bukti sumber tersebut;
label klasifikasi awal tetap disimpan untuk penelusuran. Sumber yang tidak menemukan
identitas atau tidak dapat diakses melaporkan statusnya sendiri tanpa mewarisi hasil
sumber lain. Preview menampilkan `sources: []` dan `excluded_non_journal` untuk
nonjurnal, tanpa HTTP, DDL, atau penulisan. ISSN tidak disimpulkan dari DOI atau URL.

Identitas disimpan pada `raw.journal_catalog` dengan ID stabil dan constraint unik
judul normalisasi. Judul yang hanya mirip tidak digabungkan. ISSN/e-ISSN eksplisit
dibaca apabila field sumber tersedia; tidak disimpulkan dari URL/DOI. Identitas
yang ditemukan dari bukti terverifikasi disimpan pada katalog. Konflik dengan
ISSN katalog ditolak untuk pemeriksaan manual. Variasi judul asli tetap tersedia
dalam `publication_origins`, berupa JSONB yang dipetakan berdasarkan ID publikasi.
Upsert menggabungkan asal publikasi, sehingga dosen/publikasi dari batch sebelumnya
tidak hilang. Katalog memuat jurnal dan kandidat ambigu, tanpa konferensi/institusi.

`raw.journal_index_verification` memiliki foreign key ke katalog dan constraint unik
`(journal_id, source)`. Status tabel adalah `verified`, `unverified`, `not_found`,
`access_unavailable`, atau `error`. Status `indexed` dari adapter hanya dipetakan ke
`verified` jika tersedia bukti sumber dan identitas yang cocok. HTTP 200 atau login
bukan bukti indeks. Kecocokan Scopus harus disertai source ID dan respons API valid.
SCImago memakai master CSV lokal 2025; SINTA mempertahankan jalur bukti manual lama.
Tabel lama `raw.scholar_journal_index`, jika ada, dibiarkan apa adanya; tidak ada
migrasi atau perubahan publikasi RAW secara otomatis.

Cache berlaku per jurnal dan sumber: hasil `verified` selama 30 hari (dapat diatur
`--cache-days 1..365`), `not_found` selama satu hari, dan status lainnya satu jam.
Cache yang masih berlaku digunakan kembali tanpa request sumber dan tanpa
memperpanjang waktu pemeriksaan/masa berlaku. Dengan penyimpanan aktif, asal
publikasi baru tetap digabungkan ke katalog meskipun hasil verifikasi berasal dari
cache. Cache diperiksa sebelum memerlukan akses sumber; credential yang tidak ada
tidak menghalangi penggunaan hasil yang masih berlaku. Tanpa tabel cache, pemeriksaan
tetap berjalan tanpa DDL pada mode baca saja. Hasil tidak bisa digunakan lintas
proses bila tidak pernah disimpan secara eksplisit. Setiap jurnal/sumber mendapat
transaksi sendiri, sehingga satu kegagalan tidak membatalkan batch lain.

Pemeriksaan terbaru mengganti snapshot sumber sebelumnya. Khusus SCImago,
hasil gagal/tidak pasti tidak mengganti snapshot `verified` yang sudah tersimpan.
Jalur master SCImago hanya menyimpan best quartile edisi 2025 di JSONB `sjr`.
Cache SCImago dilewati agar bukti JSON lama atau edisi lain tidak digunakan
untuk pemeriksaan master 2025; CSV dimuat sekali per pemanggilan CLI.
Data periode SINTA tambahan berada di `metadata.accreditations`.
`--retry-failed` hanya mencoba baris lama berstatus error, access_unavailable,
atau unverified; tidak menambahkan pasangan yang belum pernah diperiksa.

## Akses sumber

Scopus memakai [Serial Title API resmi Elsevier](https://dev.elsevier.com/guides/Scopus%20API%20Guide_V1_20230907.pdf).
Set `SCOPUS_API_KEY`; `SCOPUS_INST_TOKEN` opsional sesuai entitlement institusi.
API dibatasi 25 hasil per kandidat, tanpa pagination. Kecocokan memakai ISSN jika
tersedia di kedua sisi, selain itu judul persis setelah normalisasi. Source ID
adalah bukti inclusion; bukan jaminan cakupan aktif pada tahun publikasi.
Ketiadaan hasil tidak berarti `not_indexed`. API key hanya dikirim ke
`https://api.elsevier.com`; redirect tidak diikuti. Timeout 5/20 detik, maksimal
tiga percobaan untuk gangguan jaringan/server, exponential backoff, dan jeda satu
detik antarpemeriksaan. 401/403/429 tidak diakali atau diulang.

## SCImagoJR Master Export 2025

Implementasi aktif tetap `scimagojr.py`; tidak ada modul `scimago.py` duplikat.
Audit menemukan adapter JSON manual lama, runner bersama, dan tabel katalog/
verifikasi yang didefinisikan lewat SQLAlchemy. Definisi tabel PostgreSQL aktual
dan constraint-nya sudah diperiksa secara baca-saja. Tidak diperlukan migration
baru untuk tugas SCImago ini. Ingestion dan data publikasi RAW tidak diubah.

File master yang diunduh manual:

```text
data/reference/scimago/scimagojr 2025.csv
```

Audit file asli: **32.193 baris**, **26 kolom**, UTF-8 tanpa BOM, delimiter `;`,
dua header `Publisher`, dan angka SJR dengan koma desimal. File asli hanya dibaca.
Folder `data/` tetap diabaikan Git; tidak ada unduhan, file pengganti, request
HTTP, API, crawling, atau impor seluruh master ke PostgreSQL.

`load_master()` mendeteksi delimiter melalui `csv.Sniffer`, membaca UTF-8/BOM,
UTF-16 dengan BOM, atau fallback Windows-1252. Header dan kolom edisi 2025
divalidasi, termasuk `Total Docs. (2025)`; edisi lain ditolak. Pembacaan memakai
`csv.reader` secara positional, sehingga kedua nilai Publisher dipertahankan
dalam `publishers` sesuai urutan, termasuk bila salah satunya kosong. Nilai
metadata kosong tetap nullable; jurnal tidak dihapus hanya karena SJR, kuartil,
H-index, atau ISSN kosong. Angka tidak valid/nonfinite menjadi null; SJR mentah
juga disimpan. File hilang atau format tidak cocok memberikan error yang jelas.

Urutan pencocokan:

1. ISSN/e-ISSN eksplisit dengan checksum valid, dinormalisasi menjadi `NNNN-NNNX`.
   Kolom CSV `Issn` boleh memuat beberapa identifier. ISSN tidak diambil dari
   DOI, URL, atau judul artikel. Jika ISSN tersedia tetapi konflik dengan judul
   master, hasil `unverified`; tidak berpindah diam-diam ke pencocokan nama.
2. Tanpa ISSN terpercaya, cocokkan nama jurnal persis setelah normalisasi Unicode,
   kapitalisasi, spasi, dan tanda baca melalui helper yang sudah ada. `detail_journal`
   diprioritaskan, lalu `detail_venue`, lalu `venue`. Judul artikel tidak dipakai.
3. `difflib.SequenceMatcher` dengan ambang **0,93** hanya memberi maksimal lima
   kandidat fuzzy. Tidak pernah menghasilkan `indexed`, sekalipun hanya ada
   satu kandidat. Beberapa identitas exact/ISSN atau ISSN publikasi yang
   bertentangan juga menghasilkan `unverified`, tanpa SJR/kuartil terapan.

Hanya bukti eksplisit jurnal yang diperiksa: `detail_publication_type='journal'`,
`detail_journal`, atau label asli `Journal/Jurnal`. Untuk data historis, label
tersebut dibaca dari baris yang sesuai pada `raw.scholar_scrape_result.raw_envelope`
melalui `result_id` dan `urutan`, tanpa backfill. Label konferensi/proceedings,
buku/book chapter, dan thesis/dissertation dikecualikan. `unknown` tanpa bukti
jurnal tetap `unverified`, walaupun nama venue mengandung kata Journal.
Kelompok asal jurnal, unknown, dan nonjurnal dipisah agar kuartil tidak menempel
pada konferensi/buku dengan teks venue sama. Mode `--source all --save` juga
menggunakan kandidat SCImago terpisah, tanpa mengubah kebijakan sumber lain.

### Status dan kompatibilitas database

| Status hasil lokal | Representasi database |
|---|---|
| `indexed` | `status='verified'`, `indexed=true`, bukti identitas dan URL sumber |
| `not_found` | `status='not_found'`, `indexed=null`; pencarian master sudah dilakukan |
| `unverified` | `status='unverified'`, `indexed=null`; identitas belum pasti |
| `excluded` | Hanya laporan; tidak dimasukkan ke katalog jurnal |
| `error` | `status='error'`, `indexed=null`, jika ada kandidat yang dapat disimpan |

Sumber internal tetap **`scimagojr`** agar cocok dengan constraint dan data lama.
Sumber ranking publik tercatat sebagai `metadata.ranking_source='scimago'`.
CLI menerima `--source scimago` maupun `--source scimagojr`. Status `verified`
merupakan kontrak tabel yang sudah ada, setara hasil lokal `indexed`; tidak
ditambahkan status/kolom duplikat. Kandidat tanpa nama jurnal hanya dilaporkan
`unverified`, tanpa membuat entri katalog kosong.

Database hanya menyimpan jurnal/kandidat yang relevan dengan jendela RAW:

- `raw.journal_catalog`: nama asli/normalisasi, seluruh identifier yang cocok,
  serta asal publikasi. CSV tidak membedakan ISSN cetak dan elektronik, sehingga
  kode tidak menebak mana e-ISSN; semua nilai dipertahankan pada `identifiers`.
- `raw.journal_index_verification.matched_identity`: nama master, Sourceid,
  dan identifier hasil pencocokan.
- `sjr`: satu observasi `year=2025`, nilai `sjr`, `quartile`,
  `scope='best_quartile'`, dan `category=null`.
- `metadata`: metode/identitas pencocokan, tahun 2025, path dan SHA-256 master,
  format file, kandidat ambigu jika ada, serta `master_record` terpilih.
  Record berisi Sourceid, Title, seluruh ISSN, kedua Publisher, SJR, best quartile,
  H-index, Coverage, Categories, Areas, Country, dan Region.
- `evidence_url`: URL profil resmi yang dibentuk dari Sourceid; **tidak dibuka**.

`Categories` dan `Areas` dipertahankan sebagai teks sumber. Kode tidak
menghasilkan peringkat per kategori dari best quartile, maupun menyimpulkan
indeks Scopus, keaktifan cakupan tahun artikel, atau akreditasi SINTA.
`Coverage` boleh memuat rentang hingga 2026 pada file asli; ini tidak mengubah
tahun ranking yang tetap **2025**.

UPSERT tetap memakai `(journal_id, source)` dan satu transaksi per kandidat.
Snapshot sumber lain tidak berubah. Hasil negatif/tidak pasti/error SCImago,
termasuk exception pemrosesan, tidak menghapus snapshot SCImago `verified`.
Hasil percobaan terbaru tetap dilaporkan di console; jika snapshot positif
dipertahankan, database tetap memuat waktu/bukti snapshot positif sebelumnya.

### Penggunaan

Dry-run kecil yang tidak menulis apa pun:

```powershell
python -m src.journal_index.run_journal_index --source scimago --dry-run --limit 3 --row-limit 20
```

Path alternatif diberikan eksplisit; tidak ada fallback unduhan:

```powershell
python -m src.journal_index.run_journal_index --source scimago --dry-run --scimago-csv "data/reference/scimago/scimagojr 2025.csv" --limit 3 --row-limit 20
```

`--save` mengaktifkan penyimpanan eksplisit ke target yang lolos guard database
runner. Opsi itu tidak dijalankan pada database utama selama implementasi ini.
`--retry-failed` tetap khusus pemeriksaan tersimpan yang gagal/tidak pasti.

Dry-run terbatas yang dijalankan saat audit menghasilkan:

| Venue RAW | Hasil | Keterangan |
|---|---|---|
| IEEE Access | `indexed` | Sourceid 21100374601; SJR 0,884; best quartile Q1; H-index 338; tahun 2025 |
| Journal of Information Systems Engineering & Business Intelligence | `unverified` | Kandidat nama master dengan `and`, kemiripan 0,9697; tanpa penetapan kuartil |
| 2023 IEEE 12th International Conference on Communication Systems and Network Technologies (CSNT) | `excluded` | Label asli Konferensi; tidak diberi kuartil |

Pemeriksaan SQL setelah operator menjalankan penyimpanan:

```sql
SELECT c.journal_original, v.source, v.status, v.indexed,
       v.metadata->>'manual_status' AS manual_status,
       v.matched_identity->>'source_id' AS scimago_source_id,
       v.sjr, v.metadata->>'reference_year' AS ranking_year,
       v.metadata->'master_record' AS scimago_record,
       v.reason, v.evidence_url
FROM raw.journal_catalog c
JOIN raw.journal_index_verification v USING (journal_id)
WHERE v.source = 'scimagojr'
  AND (v.metadata->>'method' = 'scimago_master_csv'
       OR v.metadata->>'verification_type' = 'manual')
ORDER BY c.journal_normalized;
```

### Pengujian dan jalur lama

Test CSV memakai fixture sintetis kecil pada direktori temporary, bukan file
master asli. Tidak ada CSV dummy yang menggantikan master pengguna. Jalankan:

```powershell
python -m pytest tests/test_scimago_master.py tests/test_journal_index.py tests/test_journal_catalog.py -q -p no:cacheprovider
```

Integrasi PostgreSQL bersifat opt-in, membuat database unik
`scimago_master_test_<random>` dan mempertahankannya untuk inspeksi. Tidak
memigrasikan database utama atau mengimpor seluruh master. Gunakan server test:

```powershell
$env:SCIMAGO_MASTER_INTEGRATION='1'
python -m pytest tests/test_scimago_master.py -q -p no:cacheprovider
Remove-Item Env:SCIMAGO_MASTER_INTEGRATION
```

Validasi akhir implementasi: **687 passed, 0 failed, 0 skipped** dalam 9,44 detik
pada seluruh suite dengan kedua opsi integrasi RAW/SCImago aktif. File test baru
mencakup 39 kasus SCImago termasuk round-trip PostgreSQL, perlindungan snapshot
terverifikasi saat adapter melempar exception, dan isolasi hasil sumber lain.
`git diff --check` lulus. SHA-256 master sebelum/sesudah tetap
`6f20399957173a13e3c1729a5fb3b5821844ba75e8d640c8b3040ab19a6f7180`.

API JSON lama tetap tersedia melalui `check_manual_evidence()` atau pemanggilan
`check(candidate, evidence_path='...json')` secara eksplisit. Variabel
`SCIMAGO_EVIDENCE_FILE` hanya berlaku pada fungsi kompatibilitas tersebut;
runner normal tidak membacanya dan tidak mencampurnya dengan master 2025.

## Bukti manual SINTA dan kompatibilitas JSON lama

SINTA mempertahankan berkas bukti lokal yang ditranskripsi pengguna dari halaman
jurnal resmi; implementasinya tidak diubah pada tugas SCImago ini.
Set `SINTA_EVIDENCE_FILE` ke path JSON lokal. Jalur kompatibilitas manual
SCImago menggunakan `SCIMAGO_EVIDENCE_FILE` hanya jika dipanggil eksplisit.
Berkas JSON dibatasi 2 MB dan tidak boleh berisi kredensial, token atau cookie.

Format berkas: array objek. Field identitas: `title`, opsional `issn`, `eissn`,
dan wajib `evidence_url` HTTPS halaman profil jurnal resmi tanpa fragment.
URL SCImago harus `/journalsearch.php?q=<ID numerik>&tip=sid` (opsional `clean=0/1`);
URL SINTA harus `/journals/profile/<ID numerik>` tanpa query. Parameter lain ditolak.
SCImago menerima domain `www.scimagojr.com` atau `scimagojr.com` dengan field
`sjr`: array objek `quartile` (Q1–Q4), `year` (tahun wajib), `category`
(opsional; gunakan label kategori persis dari bukti, bukan dugaan).
Setiap objek boleh mewakili tahun/kategori berbeda untuk jurnal sama. Simpan
best quartile dengan kategori berlabel `best quartile`, jangan menyatakannya
sebagai quartile setiap kategori.

SINTA menerima domain `sinta.kemdiktisaintek.go.id` atau `sinta.kemdikbud.go.id`.
Field tambahan `sinta_rank` (S1–S6) dan `sinta_period` (teks periode berlaku
seperti terlihat di bukti; boleh null). Peringkat bertentangan ditandai unverified.
Jangan masukkan kata sandi, cookie atau tangkapan respons login ke berkas.

`SINTA_USERNAME` dan `SINTA_PASSWORD` dibaca sebagai konfigurasi akun. Jika tidak
lengkap, SINTA menghasilkan `access_unavailable`. Tidak ada login otomatis:
mekanisme autentikasi otomatis resmi belum dikonfigurasi. Masuk sendiri di portal
resmi SINTA jika diperlukan, selesaikan CAPTCHA/MFA secara normal, cari kandidat
dengan ISSN/judul, buka profil jurnal, cocokkan identitas lalu salin peringkat,
periode dan URL ke berkas bukti. Login sendiri bukan bukti indeks.
Kode tidak mengirim kredensial SINTA ke domain mana pun dan tidak menyimpan sesi.

## Rahasia lokal

Gunakan pengelola rahasia lingkungan, atau `.env` lokal yang sudah diabaikan oleh
aturan `.gitignore` proyek. Jangan melacak berkas tersebut. Jangan mengetik nilai
rahasia sebagai argumen CLI atau mencetaknya. Contoh PowerShell tanpa echo nilai:

```powershell
$env:SINTA_USERNAME = Read-Host 'SINTA username'
$secretInput = Read-Host 'SINTA password' -AsSecureString
$env:SINTA_PASSWORD = [System.Net.NetworkCredential]::new('', $secretInput).Password
$keyInput = Read-Host 'Scopus API key' -AsSecureString
$env:SCOPUS_API_KEY = [System.Net.NetworkCredential]::new('', $keyInput).Password
```

Git Bash (nilai tidak masuk history atau dicetak):

```bash
read -r -p 'SINTA username: ' SINTA_USERNAME; export SINTA_USERNAME
read -r -s -p 'SINTA password: ' SINTA_PASSWORD; export SINTA_PASSWORD
read -r -s -p 'Scopus API key: ' SCOPUS_API_KEY; export SCOPUS_API_KEY
```

Tes sepenuhnya mock, tanpa database atau request eksternal:
`python -m pytest tests/test_journal_index.py tests/test_journal_catalog.py -q`.

## Fondasi Crossref — Tahap 1 dan 2

`crossref.py` menyediakan kontrak hasil, parser metadata, dan client REST yang
belum dihubungkan ke runner. Tidak ada matcher artikel, cache, persistence,
migration, pembaruan RAW/CORE, atau perubahan aturan SCImago pada tahap ini.
Backfill CORE lama tetap terpisah dan tidak diimpor oleh modul baru.

`CrossrefResult` berisi `status`, `lookup_method`, `fetched_at` (datetime aware),
`http_status`, `attempts`, `reason` tersanitasi, dan array `candidates`.
`to_dict()` menghasilkan representasi dengan timestamp ISO-8601 UTC.

| Status kontrak | Penggunaan saat ini |
|---|---|
| `matched` | Dicadangkan untuk matcher tahap berikutnya; tidak dihasilkan client/parser |
| `ambiguous` | Metadata berhasil diambil tetapi belum dinilai kecocokan artikelnya |
| `not_found` | DOI tidak ditemukan pada Crossref atau pencarian valid tanpa kandidat |
| `error` | Input, transport, HTTP, JSON, atau struktur metadata tidak valid |

Bahkan satu hasil DOI atau satu kandidat pencarian tetap `ambiguous`.
Urutan kandidat dan skor relevansi bukan bukti kecocokan. Hasil ini tidak
menyatakan status indeks jurnal dan tidak menggunakan kontrak `result()` lama.

Setiap kandidat berisi:

- `doi`: DOI tanpa prefix resolver yang dikenal; tidak dipotong atau diubah case-nya.
- `titles`, `container_titles`: array string sesuai sumber.
- `authors`: array objek penulis, termasuk urutan dan field sumber yang tersedia.
- `publication_dates`: tanggal `published`, `published-online`, `published-print`,
  dan `issued` yang tersedia, masing-masing berupa `date_parts` dan `precision`
  (`year`, `month`, atau `day`). Contoh: `[2025, 2]`, tanpa tanggal sintetis.
- `issns`, `issn_types`: observasi sumber secara terpisah; urutan ISSN tidak
  menentukan ISSN cetak/elektronik. Jenis ISSN tidak ditebak jika absen.
- `type`, `publisher`, `url`, `score`: nullable. Skor hanya dipertahankan untuk
  hasil pencarian, tanpa interpretasi kecocokan.

Field opsional absen menjadi `[]`, `{}`, atau `None`; bentuk metadata yang
malformed menghasilkan error tersanitasi. Tidak ada key Google Scholar `detail_*`.

`CrossrefClient.lookup_doi()` memanggil `/works/{doi}`. Prefix `doi:`, `doi.org`,
dan `dx.doi.org` dikenali. Encoding resolver URL dibuka sekali; DOI biasa tidak
di-decode. Suffix khusus, percent literal, dan tanda baca tidak dipangkas.
DOI di-encode sebagai komponen path, termasuk karakter `/`, `?`, `#`, dan `&`.
HTTP 404 berarti tidak ditemukan **di Crossref**, bukan DOI tidak valid global.

`search_bibliographic()` memakai `/works`, `query.bibliographic`, dan `rows=5`.
Parser juga membatasi array kandidat ke lima bila server memberikan lebih.
Client tidak mengikuti redirect atau membuka URL DOI/publisher yang dikembalikan.

Konfigurasi client diberikan melalui constructor, tanpa membaca `.env`:

| Parameter | Default / perilaku |
|---|---|
| `session` | `requests.Session`; dapat diganti fake session |
| `timeout` | `(5, 20)` detik connect/read |
| `mailto` | Opsional; tidak ada email personal bawaan |
| `max_attempts` | 3, dapat diturunkan ke 1 untuk tanpa retry |
| `min_interval` | 1 detik antar awal request serial |
| `sleep`, `monotonic`, `now` | Dapat diinjeksi untuk test deterministik |
| `max_retry_wait` | 60 detik per penundaan retry, termasuk backoff dan `Retry-After`; nilai lebih besar menghentikan retry |

Timeout/koneksi gagal, 429, dan 5xx dicoba ulang secara terbatas dengan backoff
2 lalu 4 detik. `Retry-After` mendukung detik maupun HTTP-date dan tidak
dilanggar dengan retry lebih awal. HTTP 4xx lain/redirect, JSON rusak, dan
struktur salah tidak diulang. Search HTTP 404 merupakan error endpoint.
Pesan exception, body error, query, serta kontak tidak ditulis ke log.
Kontak Unicode di-encode untuk User-Agent ASCII, sementara parameter `mailto`
tetap memakai nilai asli. DOI Unicode tidak valid dan tanggal di luar rentang
ditolak sebagai error tersanitasi, tanpa exception mentah.
Client bersifat serial dan tidak mempunyai cache. `close()` menutup session
yang dibuat client sendiri; session yang diinjeksi tetap milik pemanggil.

Fixture lengkap dan parsial berada dalam `tests/test_crossref.py`, menggunakan
fake session/response/clock. Guard test memblokir transport HTTP sungguhan dan
koneksi database, sehingga test tidak memerlukan API, master CSV, atau kredensial.

```powershell
python -m pytest tests/test_crossref.py -q
```

Validasi Tahap 1–2: **75 test Crossref lulus**. Seluruh suite melalui `pytest -q`
menghasilkan **760 passed, 2 skipped**. Dua test integrasi database existing
bersifat opt-in dan tidak diaktifkan karena tahap ini melarang perubahan database;
685 test existing lainnya lulus bersama 75 test Crossref. Tidak ada HTTP sungguhan,
migration, atau penulisan database pada validasi ini.

Pemanggilan API nyata, matcher, integrasi SCImago, serta penyimpanan belum
diaktifkan melalui CLI. Tahap berikutnya menunggu peninjauan fondasi ini.

## Matcher artikel Crossref — Tahap 3

`crossref_matcher.py` menambahkan fungsi pure `match_article()` di atas hasil
parser Tahap 1–2. Client/parser tetap utuh: keberhasilan mengambil metadata
masih menghasilkan `ambiguous`. Hanya matcher yang dapat menghasilkan
`matched`, setelah mengevaluasi bukti dan kandidat pesaing. Matcher belum
diintegrasikan dengan runner, SCImago, cache, atau database.

```python
from src.journal_index.crossref_matcher import match_article

# scholar_row dan retrieval_result sudah tersedia; fungsi ini tidak mengambil data.
decision = match_article(
    scholar_row, retrieval_result,
    raw_authors=ordered_raw_author_records,  # opsional; bukan query database
    title_threshold=0.95,
    competitor_margin=0.02,
)
report = decision.to_dict()
```

`retrieval_result` adalah `CrossrefResult` dengan kandidat metadata terparse.
Seluruh kandidat diperiksa, tanpa pemotongan tambahan atau pemilihan hasil
pertama. Error client diteruskan sebagai error, tanpa upaya request ulang.

Input Scholar diprioritaskan sebagai berikut:

- Judul: `detail_title`, kemudian `title`.
- Penulis: argumen `raw_authors` atau `publication_authors` pada row, diurutkan
  berdasarkan `author_order`; fallback ke `detail_authors`, kemudian `authors`.
  Record memakai `author_name` dan opsional `daftar_terpotong`.
- Tahun: `publication_year`, kemudian `year`; `detail_date` menjadi bukti tambahan
  atau cadangan terakhir bila tahun utama tidak tersedia.
- Container: `detail_journal`, `detail_venue`, kemudian `venue`.
- DOI: `detail_doi`, kemudian `doi` bila disediakan pemanggil.

Field absen/kosong boleh memakai fallback. Field prioritas yang terisi tetapi
malformed tidak disamarkan sebagai absen: hasil `ambiguous` mencatat
`input_conflicts`, misalnya `invalid_detail_doi` atau `invalid_detail_date`.

### Kebijakan normalisasi dan bukti

Judul menggunakan NFC, casefold, normalisasi lebar karakter, whitespace, dan
tanda baca kosmetik. Tidak memakai normalisasi katalog yang membuang simbol.
Angka/desimal, formula alfanumerik, huruf Yunani, operator, prime, factorial,
subscript/superscript, dan huruf matematika dilindungi dengan signature tambahan.
Minus Unicode dan minus ASCII disetarakan tanpa menghapus operatornya.
Signature sebelum casefold juga memveto kemungkinan perubahan formula kimia
seperti `Co` versus `CO`. Ini guard leksikal konservatif, bukan parser kimia atau
pembuktian semantik semua kemungkinan judul ilmiah.

Kemiripan memakai `difflib.SequenceMatcher(autojunk=False)`. Ambang awal 0,95
bisa dikalibrasi; kemiripan tidak cukup sebagai bukti tunggal. Judul pendek,
generik, atau tanpa informasi memadai tetap membutuhkan review. Judul dengan
aksara rapat seperti CJK dapat dinilai tanpa syarat jumlah kata Latin.

Nama penulis dinormalisasi untuk kapitalisasi, Unicode/aksen kanonik, spasi,
tanda baca, dan format `family, given` yang eksplisit dalam record. Tidak ada
pengembangan inisial menjadi nama lengkap. Pasangan nama dicatat satu-ke-satu
beserta posisi asal. Nama belakang saja, mononim saja, atau inisial saja bukan
bukti kuat; inisial yang kompatibel boleh didukung coauthor dengan nama penuh.
Urutan berbeda, elipsis/`et al.`, record rusak, atau daftar Crossref yang hanya
sebagian tetap bukti parsial. Penulis yang hilang tidak otomatis dianggap
konflik identitas, termasuk ketika menilai kandidat pesaing.

Tahun sama mendukung kecocokan meskipun bulan/hari berbeda. Selisih satu tahun
memerlukan tanggal Crossref lain yang mendukung tahun RAW; seluruh sumber dan
presisi tanggal dicatat. Selisih lebih jauh, tahun tanggal detail yang
bertentangan, atau rentang tahun publikasi yang tidak konsisten memblokir
keputusan otomatis. Tidak dibuat tanggal sintetis dari tahun saja.

Container adalah bukti pendukung. Container yang jelas berbeda menjadi konflik;
container kosong, placeholder, atau teks bibliografi profil yang tidak memadai
tidak dipaksa menjadi nama jurnal. DOI sama tidak mengalahkan konflik judul,
penulis, tahun, atau tipe publikasi. DOI berbeda yang terisi menjadi konflik.
Tidak ada pemakaian skor relevance Crossref untuk memilih identitas.

### Keputusan dan diagnostik

`ArticleMatchResult` merupakan turunan backward-compatible dari `CrossrefResult`.
Selain seluruh field lama, hasil memiliki `selected_candidate`,
`candidates_evaluated`, `evidence`, `input_conflicts`, `title_threshold`, dan
`competitor_margin`. `evidence` berisi indeks/DOI serta bukti judul, penulis,
tahun, container, DOI, konflik, dan kelayakan setiap kandidat.
`selected_candidate` hanya diisi untuk hasil `matched`.

- `matched / unique_consistent_candidate`: satu identitas memenuhi syarat judul
  informatif, penulis kuat, tahun konsisten, tanpa konflik atau pesaing yang
  belum terselesaikan. Jalur tambahan terbatas untuk subset penulis penuh
  dijelaskan pada koreksi validasi Tahap 4 di bawah.
- `ambiguous`: beberapa kandidat layak, metadata kurang/bertentangan, input tidak
  memadai, atau kandidat pesaing yang dekat belum dapat dipastikan.
- `not_found / no_plausible_candidate`: tidak ada kandidat yang cukup relevan,
  atau hasil kosong untuk publikasi dengan judul yang layak.
- `error`: error client diteruskan, atau input publikasi bukan struktur yang sesuai.

Pesaing kurang bukti dalam selisih similarity 0,02 dari kandidat layak tetap
memerlukan review; tidak dihapus hanya karena penulis/tahun tidak tersedia.
Kandidat dengan konflik eksplisit tidak mengalahkan kandidat yang konsisten.
Duplikat DOI identik tidak dianggap identitas pesaing, tetapi record duplikat
yang bertentangan memerlukan review. Kandidat malformed juga tidak menjadi
bukti negatif untuk mengesahkan kandidat lain.

Semua metadata input dipertahankan. Timestamp retrieval diteruskan tanpa memakai
waktu sekarang, sehingga input dan parameter yang sama menghasilkan keputusan
dan diagnostik yang sama. `to_dict()` dapat diserialisasi sebagai JSON.

Pengujian matcher memakai fixture metadata melalui `parse_work()`, tanpa HTTP,
database, master CSV, scraper, atau kredensial:

```powershell
python -m pytest tests/test_crossref_matcher.py tests/test_crossref.py -q
```

Validasi Tahap 3: **99 test matcher** dan **75 test fondasi** lulus
(174 passed). Seluruh `pytest -q`: **859 passed, 2 skipped** dalam 9,77 detik.
Dua test integrasi database existing tetap opt-in dan tidak diaktifkan pada
tahap ini. Review false positive dan pemeriksaan hash mengonfirmasi bahwa
client/parser, runner, SCImago, backfill lama, loader, dan migration tidak
berubah. Tidak ada request HTTP atau koneksi database pada pekerjaan ini.

Pada akhir Tahap 3, cache/persistence dan integrasi belum tersedia; implementasi
berikutnya dijelaskan pada bagian Tahap 5–6.

## Pemeriksaan Crossref terbatas — Tahap 4

Mode `--check-crossref` secara eksplisit mengizinkan REST request sungguhan,
namun hanya membaca RAW dan mencetak JSON Lines. Mode ini tidak menyimpan hasil
enrichment atau verifikasi jurnal. Tanpa flag tersebut, jalur Crossref tidak
dijalankan. `--dry-run` tetap offline; kombinasi `--check-crossref` dengan
`--dry-run`, `--save`, `--sync-catalog`, atau `--check-scopus` ditolak argparse.
`--save-verification` dan `--retry-failed` juga tidak dapat dipakai pada mode ini.

| Opsi | Default | Ketentuan |
|---|---|---|
| `--check-crossref` | mati | Aktifkan pemeriksaan artikel individual |
| `--crossref-limit N` | 3 | Maksimum artikel, 1–10 |
| `--crossref-source {doi,search,auto}` | auto | Strategi lookup |
| `--crossref-delay SECONDS` | 1 | Interval minimum antar-request, finite dan >= 0 |
| `--scholar-id ID` | tanpa | Batasi Crossref ke satu profil (`raw.scholar_publication.scholar_id`); tanpa flag perilaku lama. Untuk ID berawalan minus pakai bentuk `--scholar-id=ID` |
| `--fill-raw-crossref` | mati | Dengan `--enrich-crossref`: isi kolom RAW kosong (`detail_doi`, `detail_publisher`, `detail_venue`) hanya dari matched tervalidasi; nilai terisi tidak pernah ditimpa |
| `--row-limit N` | 1000 | Jendela pembacaan RAW, 1–10000 |

`--limit` dan `--source` lama mengatur mode indeks jurnal; tidak memilih strategi
atau batas artikel Crossref. Mode Crossref tidak memuat master SCImago.

### Pemilihan dan keamanan pembacaan

`crossref_check.read_publications()` membaca publikasi individual, **sebelum**
`read_candidates()` melakukan pengelompokan venue. Judul memakai `detail_title`,
fallback `title`; judul kosong dikeluarkan. Publikasi tidak disaring sebagai
jurnal berdasarkan keberadaan venue. Metadata tanggal, tahun, jenis, DOI, serta
container diteruskan ke matcher tanpa menulis kolom `detail_*`. Dengan
`--scholar-id`, query menambah `AND p.scholar_id = :scholar_id` sehingga jendela
RAW hanya berisi profil tersebut; `--crossref-limit` tetap membatasi artikel
unik setelah deduplikasi (mis. `--crossref-limit 8` = maksimal 8 artikel unik
dari profil yang dipilih).

Relasi `raw.scholar_publication_author` diambil dengan `author_order` dan
`daftar_terpotong`. Jika relasi kosong, fallback `detail_authors`, lalu `authors`.
Kolom tambahan `detail_journal` dan `detail_publication_type` dibaca melalui
`to_jsonb(p)` agar kolom yang belum tersedia tetap menghasilkan NULL.
Tabel relasi penulis tetap harus tersedia; jika query gagal, proses berhenti
sebelum membuat client. Tidak ada migration otomatis.

Urutan pemilihan deterministik berdasarkan `raw_publication_id` terkecil dalam
jendela RAW. Snapshot dengan DOI ternormalisasi sama diperiksa sekali. Tanpa
DOI, fingerprint konservatif memakai judul (NFC dan whitespace), penulis
terurut termasuk penanda terpotong, tahun, tanggal detail, container, dan tipe.
Snapshot dengan bukti berbeda dapat tetap diperiksa. DOI yang terisi dan berbeda
tidak disatukan berdasarkan judul. Snapshot tanpa DOI tidak menutupi DOI yang
terisi pada snapshot berikutnya. Ini deduplikasi lokal satu run, bukan resolusi
identitas publikasi global; metadata yang berubah antarsnapshot dapat tetap
menghasilkan pemeriksaan tambahan. Batas artikel berlaku setelah penyaringan
dan deduplikasi, sehingga jumlah aktual bisa lebih kecil daripada limit.

Koneksi menjalankan `SET TRANSACTION READ ONLY` sebelum seluruh `SELECT`,
memverifikasi `current_database()`, dan mematerialisasi hasil sebelum koneksi
ditutup/rollback. PostgreSQL menolak penulisan pada transaksi itu. Tidak ada
INSERT, UPDATE, DELETE, DDL, cache database, atau pemanggilan fungsi penyimpanan
Journal Index. Kegagalan database menghentikan mode dengan exit code 1 tanpa HTTP.

### Lookup dan output

- `doi`: hanya memilih publikasi dengan DOI terisi. DOI malformed dilaporkan
  client sebagai `error`; tidak diubah menjadi search.
- `search`: mencari judul artikel dengan `query.bibliographic`, maksimal lima
  kandidat per respons. Seluruh kandidat diteruskan ke matcher.
- `auto`: DOI lookup jika tersedia, selain itu search. **Tidak ada fallback
  search setelah kegagalan DOI lookup**, termasuk HTTP 404.

Client Tahap 1–2 digunakan kembali: timeout connect/read 5/20 detik, maksimal
tiga percobaan per artikel, retry terbatas/backoff untuk timeout, koneksi,
429, dan 5xx. `Retry-After` yang melebihi batas tunggu 60 detik menghentikan retry.
Redirect tidak diikuti. Konfigurasi kontak opsional `CROSSREF_MAILTO` diteruskan
ke client tanpa nilai default pribadi dan tidak dicetak. Tidak perlu mengubah
`.env`; konfigurasi proses dapat digunakan jika diinginkan.

Satu JSON object per artikel memuat `raw_publication_id`, `scholar_title`,
`scholar_authors`, `scholar_year`, `scholar_date`, `lookup_strategy`,
`candidate_count`, `status`, `selected_doi`, `crossref_titles`,
`crossref_authors`, `container_titles`, `issns`, `type`, `title_similarity`,
`author_evidence`, `year_evidence`, `container_evidence`, `evidence`, `conflicts`,
`reason`, `checked_at`, `http_status`, dan `request_attempts`.
Metadata terpilih dan similarity utama hanya diisi saat `matched`; diagnostik
semua kandidat tetap tersedia dalam `evidence` untuk hasil ambigu atau konflik.
Respons mentah API tidak dicetak. Error teknis satu artikel dilaporkan dengan
alasan tersanitasi dan tidak menghentikan artikel berikutnya. Exit code 1 berarti
ada error teknis; `ambiguous` dan `not_found` bukan kegagalan proses.
Jika jendela tidak memiliki publikasi yang memenuhi filter, satu objek ringkasan
`articles_checked: 0` dicetak dan tidak ada HTTP.

### Command pemeriksaan nyata — belum dijalankan

```powershell
python -m src.journal_index.run_journal_index --check-crossref --crossref-limit 2 --crossref-source search --crossref-delay 1 --row-limit 100
```

Command ini memeriksa **maksimal dua artikel** dari jendela 100 publikasi berjudul,
menggunakan bibliographic search. Maksimal **enam request HTTP** termasuk retry
(2 artikel × 3 percobaan); lima kandidat adalah isi satu respons, bukan lima
request. Tidak ada DOI fallback, redirect, request SCImago, atau penulisan DB.
Gunakan role PostgreSQL read-only sebagai lapisan izin tambahan bila tersedia.
Jangan menambahkan `--save`; kombinasi itu ditolak. Command ini harus mendapat
persetujuan pengguna sebelum dijalankan dalam sesi implementasi.

Pengujian mode memakai fake client/engine, tanpa HTTP atau koneksi DB sungguhan:

```powershell
python -m pytest tests/test_crossref_check.py tests/test_crossref.py tests/test_crossref_matcher.py -q
```

Mode pemeriksaan Tahap 4 tetap tidak menyimpan hasil. Persistence dan integrasi
berada pada mode terpisah Tahap 5–6 di bawah.

Validasi offline Tahap 4: **41 test baru**, **215 test Crossref lulus**
(75 fondasi + 99 matcher + 41 pemeriksaan). Seluruh `pytest -q`:
**900 passed, 2 skipped** dalam 9,38 detik. Dua test integrasi DB existing
tetap opt-in. Pemeriksaan API/DB sungguhan belum dijalankan pada tahap ini.

### Koreksi matcher berdasarkan metadata validasi Tahap 4

Fixture offline berdasarkan kasus yang diberikan pengguna memperbaiki tiga
penyebab false negative tanpa mengulang request API:

- Identitas artikel membandingkan **tahun**, bukan kesamaan bulan/hari.
  Tanggal RAW `2023-04-01` dan Crossref `2023-04-28` menghasilkan `consistent`.
  Semua tanggal Crossref tetap dicatat dalam `crossref_dates` dengan presisi
  pada `crossref_date_precision` dan sumber tahun pendukung pada
  `supporting_date_sources`. Tanggal asli dalam kandidat dan input tidak diubah.
  Tanpa tanggal/tahun, bukti tetap `missing`, bukan konflik.
- Selisih satu tahun tanpa tanggal Crossref lain yang mendukung tahun RAW
  tetap berstatus `one_year_difference` untuk kompatibilitas, tetapi sekarang
  mencatat `conflict: true` dan `year_conflict`. Bukti alternatif yang mendukung
  tahun RAW boleh digunakan jika rentang tahun Crossref tidak melebihi satu
  tahun. Bukti online/print eksplisit tetap memakai `online_print_consistent`.
  Kontradiksi tahun RAW atau rentang Crossref lebih lebar tetap memblokir match.
- Normalisasi **khusus container** menyetarakan `&` dan kata `and`, kapitalisasi,
  whitespace, serta tanda baca. Tidak memakai fuzzy matching atau menghapus
  kata substantif. Nama asli kedua sumber tetap disimpan. Normalisasi judul
  artikel dan aturan katalog/SCImago tidak diubah.

Kontrak penulis membedakan kelengkapan daftar dari identitas artikel.
`order_consistent` mempertahankan pemeriksaan posisi absolut lama;
`relative_order_consistent` menambahkan pemeriksaan urutan relatif.
`ordered_full_subset` hanya true jika sedikitnya dua nama RAW cocok **penuh**,
satu-ke-satu tanpa ambiguitas, seluruhnya ditemukan dalam urutan relatif,
Crossref memiliki penulis tambahan, dan tidak ada record/urutan rusak,
nama Crossref kosong, atau penanda daftar RAW terpotong. Bukti tetap `partial`.

Subset tersebut hanya dapat menghasilkan `matched` jika judul **identik setelah
normalisasi**, container **match**, tahun konsisten, tidak ada konflik lain,
dan semua kandidat pesaing telah dievaluasi. Inisial, mononim, nama belakang
saja, urutan terbalik, penulis hilang, judul hanya mirip, dan metadata kurang
tidak memenuhi jalur ini. Kebijakan umum `strong` tetap berlaku seperti semula.

Hasil fixture:

| Kasus | Status | Penulis | Tahun | Container | Type |
|---|---|---|---|---|---|
| JISEBI `10.20473/jisebi.9.1.84-94` | matched | partial; RAW 0/1/2 → Crossref 1/2/3 | consistent | match | journal-article |
| CSNT `10.1109/csnt57126.2023.10134589` | matched | strong; tujuh nama berurutan | consistent | match | proceedings-article |

Fixture JISEBI memakai nama yang diberikan pengguna dan mempertahankan keempat
penulis Crossref. Nama tujuh penulis CSNT tidak disertakan dalam spesifikasi,
sehingga fixture memakai tujuh nama **sintetis** untuk menguji bentuk dan urutan;
judul, DOI, tahun, container, serta type memakai data yang diberikan.
Klasifikasi konferensi existing tetap `non_journal`; `matched` hanya berarti
identitas artikel, bukan bukti kuartil jurnal atau indeks Scopus/SCImago.

`ArticleMatchResult` menambahkan field kompatibel:
`selected_candidate_index`, `selected_conflicts`, dan `candidate_decisions`.
Setiap keputusan kandidat memiliki indeks, DOI, `selection`, `reason`, dan
`conflicts`. Alasan mencakup konflik metadata, bukti tidak cukup, identitas
pesaing, atau duplikat identitas terpilih. `candidates_evaluated` tetap tersedia.

JSONL tetap mempertahankan array lama `author_evidence`, `year_evidence`,
`container_evidence`, dan seluruh `evidence`. Field tambahan:

- `selected_evidence`: bukti kandidat terpilih; null bila tidak ada pilihan.
- `selected_conflicts`: hanya konflik kandidat terpilih; kosong saat matched.
- `selected_candidate_index`, `candidates_evaluated`, `candidate_decisions`.
- `rejected_candidates` dan `unresolved_candidates`: keputusan kandidat lain,
  yang dapat ditautkan ke `evidence` melalui `candidate_index`.

Untuk `matched`, field tingkat publikasi `conflicts` sekarang hanya mencerminkan
konflik kandidat terpilih, sehingga kandidat salah tidak mencemari hasil.
Untuk hasil tanpa pilihan, `conflicts` mempertahankan agregat diagnostik lama;
`selected_evidence: null` membedakan bahwa agregat itu bukan konflik kandidat
terpilih. `selected_evidence.authors.status` pada JISEBI tetap **partial**;
metadata terpilih tetap berisi seluruh penulis Crossref sesuai urutan sumber.
Tidak ada penyimpanan, request baru, atau integrasi Tahap 5 pada koreksi ini.

Validasi koreksi: **28 test baru** (26 matcher, 2 output pemeriksaan).
Urutan `python -m pytest -q tests/test_crossref_matcher.py`, lalu
`tests/test_crossref.py`, lalu `tests/test_crossref_check.py` menghasilkan
**125**, **75**, dan **43 passed**. Seluruh `python -m pytest -q`:
**928 passed, 2 skipped** dalam 8,75 detik; `git diff --check` lulus.
Dua test integrasi database existing tetap opt-in dan tidak diaktifkan.

## Tahap 5–6: persistence artikel dan integrasi master SCImago

Tiga keputusan tetap terpisah: `crossref_status` menentukan identitas artikel,
identitas serial memerlukan container/ISSN yang konsisten, dan `scimago_status`
berasal dari pemeriksaan master CSV lokal. Crossref tidak membuktikan indeks atau
kuartil. `matched` dengan bukti penulis `partial` tetap sah sesuai matcher.
Client, matcher, scraper, SCImago standalone, SINTA, dan backfill CORE tidak diubah.

### Schema dan provenance

Migration additive baru: `sql/ddl/16_create_crossref_publication_enrichment.sql`.
File ini **belum diterapkan**. Tidak ada DDL otomatis dalam mode baru. Operator
harus meninjau dan menerapkannya secara terpisah setelah persetujuan. Tabel lama
`journal_catalog` dan `journal_index_verification` tetap harus sudah tersedia
untuk penyimpanan SCImago. Jika tabel enrichment belum ada, proses berhenti dengan
petunjuk migration sebelum HTTP; tidak mencoba membuat tabel atau melewati cache.

`raw.crossref_publication_enrichment` menyimpan snapshot terbaru per artikel:

| Kolom | Tipe / fungsi |
|---|---|
| enrichment_id | BIGSERIAL PK |
| raw_publication_id | INTEGER FK ke RAW Scholar; UNIQUE |
| status, lookup_strategy | TEXT dengan CHECK status/metode Crossref |
| doi, publisher, publication_type | TEXT nullable, hanya metadata terpilih |
| matched_title, matched_authors, container_title, issns | JSONB, array asli/urutan sumber |
| publication_year | INTEGER nullable |
| publication_date, publication_date_precision | JSONB date-parts dan TEXT year/month/day; tanpa tanggal sintetis |
| author_evidence, year_evidence, container_evidence | JSONB bukti terpilih |
| title_similarity, candidate_count | DOUBLE PRECISION nullable, INTEGER |
| selected_evidence, selected_conflicts, metadata | JSONB diagnostik dan provenance |
| retrieved_at, expires_at | TIMESTAMPTZ; expires_at > retrieved_at |
| last_error | TEXT kode tersanitasi |
| created_at, updated_at | TIMESTAMPTZ, creation time dipertahankan saat UPSERT |

Index tersedia untuk expiry dan DOI. FK default membatasi penghapusan parent;
migration tidak mengubah parent, menghapus data, atau melakukan backfill.
`metadata.result` menyimpan kontrak matcher lengkap: seluruh kandidat, evidence,
keputusan, timestamp, HTTP status/attempts, ISSN-type eksplisit dan tanggal sumber.
Field terpilih kosong pada ambiguous/not_found/error. Array ISSN tidak diasumsikan
memiliki urutan print/electronic. Label hanya berasal dari `issn_types` jika tersedia.
`metadata.source = crossref`, `matcher_version`, `scholar_fingerprint`, dan
`article_provenance` mencatat asal dan versi interpretasi.

Ini snapshot terakhir, bukan ledger historis semua percobaan. Tidak ada metadata
Crossref ditulis ke `raw.scholar_publication`, CORE, atau STAGING.

### Cache, TTL, dan transaksi

| Status | TTL default |
|---|---|
| matched | 30 hari |
| ambiguous | 7 hari |
| not_found | 1 hari |
| error | 1 jam |

`--crossref-ttl STATUS=SECONDS` bisa diulang. Nilainya harus positif, finite,
maksimal 365 hari. Override berlaku untuk snapshot baru, tidak menulis ulang expiry
historis. Cache hit memerlukan input fingerprint, versi matcher, strategi lookup,
dan TTL yang sesuai. Cache tidak diubah dari ambiguous menjadi matched hanya
dengan membacanya. Timestamp/payload rusak menghasilkan `invalid`, bukan crash.

`cache_status` adalah keadaan saat lookup: `never_checked`, `hit`, `expired`,
`input_changed`, `strategy_changed`, atau `invalid`. Status artikel tetap terpisah:
misalnya cache `hit` dapat berisi hasil `error`. Mode cache-only tidak memakai
metadata expired atau meminta HTTP. Hasil tanpa cache valid adalah `not_checked`,
bukan `not_found`. Dalam mode enrichment/refresh, cache miss boleh meminta HTTP.

UPSERT hanya menulis tabel Crossref, dengan parameter binding. Predicate atomik
mencegah snapshot lebih lama mengganti snapshot lebih baru dan mencegah hasil
negatif/error mengganti `matched` yang masih valid. Ketika proteksi menang karena
proses lain sudah menyimpan hasil, cache dibaca kembali (`protected_existing`).
Jika tidak ada snapshot valid yang dapat digunakan, `protected_unusable` memblokir
penyimpanan SCImago. `request_status` tetap menampilkan hasil request saat ini.
Exception transport tidak disimpan; hanya kode alasan tersanitasi.

Pembacaan memakai transaksi read-only yang ditutup sebelum HTTP. Penyimpanan
Crossref dan katalog/verifikasi memakai transaksi masing-masing; kegagalan
statement menyebabkan rollback transaksi tersebut. Tidak ada transaksi database
yang ditahan selama request/backoff. Kegagalan DB menghentikan run, tanpa retry
write diam-diam. Kedua penyimpanan terpisah: jika penyimpanan SCImago gagal,
enrichment yang sudah berhasil disimpan tetap dapat digunakan saat rerun.

### Gerbang identitas serial dan publication type

`crossref_scimago.match_serial()` hanya memakai kandidat terpilih dari `matched`.
`journal-article` dapat diperiksa; proceedings, buku, book-chapter, dissertation,
posted-content, dataset, report, dan tipe non-jurnal lain tidak mendapat kuartil.
Tipe kosong/tidak dikenal tetap unverified. Konflik tipe Scholar tidak diabaikan.
Nama artikel tidak pernah digunakan sebagai nama jurnal.

ISSN/e-ISSN divalidasi format dan check digit, kemudian dicari persis melalui
adapter master existing. Identifier yang malformed tidak dibuang diam-diam untuk
memaksa title fallback. Beberapa identitas master, duplicate ISSN, atau konflik
ISSN/container menghasilkan unverified tanpa kuartil. Container asli disimpan;
ekuivalensi `&`/`and` dan kosmetik tidak menghapus kata substantif. Container yang
bertentangan dengan master memveto hasil meskipun ISSN ditemukan.

Tanpa ISSN, exact normalized container menjadi fallback dengan alasan
`title_exact`, bukan bukti setara ISSN. Beberapa master identity dengan alias nama
yang ekuivalen tetap unverified. Fuzzy memakai threshold existing 0,93 dan hanya
menghasilkan kandidat unverified. Kuartil berasal dari **SJR Best Quartile 2025**,
bukan kuartil per kategori. Master CSV tidak diimpor ke PostgreSQL.

Setiap artikel diperiksa sendiri. Venue Scholar tidak menjadikan semua anggotanya
jurnal. Identitas katalog memakai nama canonical master, atau memakai kembali
satu katalog existing dengan identifier yang sama. Alias tidak mengubah nama asli
katalog existing. `publication_origins[raw_publication_id]` mempertahankan
`scholar_original` dan provenance Crossref: ini tautan artikel→jurnal deterministik.
Dua serial berbeda tidak digabung berdasarkan kemiripan atau kesamaan venue.

Penyimpanan SCImago hanya menerima local-master `indexed` yang memenuhi kontrak
verified lama. Pemeriksaan katalog mendeteksi identitas ganda dan konflik dengan
source ID terverifikasi. Guard atomik UPSERT khusus jalur ini menolak penambahan
identifier yang bertentangan, termasuk race dengan writer lama. Default perilaku
UPSERT lama tidak berubah. Semua baris verifikasi existing—termasuk manual,
Scopus, SINTA, dan SCImago—dipertahankan; INSERT SCImago memakai DO NOTHING pada
konflik `(journal_id, source)`. Hasil preview dan status tersimpan bisa berbeda;
`scimago_saved = existing_verification_preserved` menyatakan bahwa hasil lama
tidak diganti. Unverified/excluded/error tidak ditulis ke tabel verifikasi.

Dengan `--save-crossref`, hasil integrasi atau konflik disimpan terpisah dalam
`metadata.scimago_2025`, tanpa mengubah identitas artikel atau TTL. Predicate
timestamp/fingerprint mencegah provenance dilekatkan pada snapshot yang berbeda.
Tanpa flag itu, konflik hanya dilaporkan pada JSONL; hasil lama tidak disentuh.

### CLI dan kombinasi yang diizinkan

| Mode | HTTP Crossref | Tulis enrichment | Tulis verifikasi SCImago |
|---|---|---|---|
| default / --dry-run | Tidak | Tidak | Tidak |
| --check-crossref | Ya, eksplisit | Tidak | Tidak |
| --enrich-crossref | Hanya cache miss/expired | Hanya --save-crossref | Tidak |
| --crossref-scimago | Tidak, cache-only | --save-crossref hanya audit integrasi | Hanya --save-scimago |
| --crossref-scimago --refresh-crossref | Hanya cache miss/expired | Hanya --save-crossref | Hanya --save-scimago |

Mode-mode tersebut mutually exclusive dengan `--save`, `--sync-catalog`,
`--check-scopus`, dan `--dry-run`. `--check-crossref` tidak dapat memakai flag
write baru. `--refresh-crossref` hanya untuk `--crossref-scimago`.
Refresh sekaligus `--save-scimago` juga memerlukan `--save-crossref` agar hasil
artikel memiliki provenance persisten. Semua write memvalidasi target
`publication_dashboard` dan tidak menjalankan DDL. `--save-verification` lama
tetap hanya untuk check-scopus. Tidak ada HTTP saat import/default/dry-run.

Batas artikel tetap default 3, maksimum 10 (`--crossref-limit`); `--row-limit`
membatasi jendela RAW. Snapshot dengan DOI sama atau bibliografi identik
dideduplikasi sesuai Tahap 4, sehingga hanya raw_publication_id representatif
yang diperiksa/disimpan pada run tersebut. Cache tidak disalin ke snapshot lain.
Strategi `doi`, `search`, `auto`, delay, timeout, retry dan optional `CROSSREF_MAILTO`
menggunakan client existing; tidak ada fallback search setelah DOI gagal.

Contoh **belum dijalankan**, setelah migration ditinjau dan diterapkan terpisah:

```powershell
# Preview integrasi dari cache valid saja; tidak HTTP dan tidak write.
python -m src.journal_index.run_journal_index --crossref-scimago --crossref-limit 2

# Enrichment opt-in, simpan ke tabel artikel saja (maksimal 6 HTTP attempts).
python -m src.journal_index.run_journal_index --enrich-crossref --save-crossref --crossref-limit 2 --crossref-source auto

# Gunakan cache saja, simpan audit dan verifikasi master-confirmed secara eksplisit.
python -m src.journal_index.run_journal_index --crossref-scimago --save-crossref --save-scimago --crossref-limit 2

# Refresh miss/expired, simpan kedua sumber secara terpisah; tetap dibatasi.
python -m src.journal_index.run_journal_index --crossref-scimago --refresh-crossref --save-crossref --save-scimago --crossref-limit 2 --crossref-ttl matched=2592000
```

JSONL membedakan `crossref_status`, `scimago_status`, `publication_type`,
`matching_method`, `cache_status`, `request_status`, `crossref_saved`,
`scimago_saved`, dan `integration_audit_saved`. Output juga memuat raw ID/judul,
DOI, ISSN dan label eksplisit, selected/all evidence, konflik, alasan, provenance,
ranking year, quartiles, serta metadata master yang relevan. Tidak mencetak email
kontak/config atau connection string. Tidak ada satu status generik yang
mewakili semua tahapan.

### Validasi dan batas operasional

Unit test memakai fixture kecil dan fake session/engine. FK, UNIQUE, JSONB,
parameter binding, UPSERT, TTL, rollback, race guard, dan source isolation
divalidasi dengan metadata/kompilasi PostgreSQL dan mock. Ini **bukan** pengujian
migration atau SQL runtime pada PostgreSQL sungguhan. Migration belum diterapkan
ke database mana pun dalam pekerjaan ini; dua test integrasi DB existing tetap
opt-in. Client/matcher/check dan alur SCImago lama tetap diuji sebagai regresi.

JISEBI mempertahankan matched/partial dan empat penulis Crossref. ISSN tidak
diberikan dalam kasus nyata tersebut, sehingga fixture tidak mengarang ISSN:
title fallback diuji dengan master **sintetis**, tanpa klaim ranking jurnal nyata.
CSNT memakai DOI/title/container/type yang diberikan dan tujuh nama sintetis
karena nama asli tidak disertakan; matched/strong dapat disimpan sebagai artikel,
tetapi proceedings-article selalu dikecualikan dari kuartil jurnal.

Tidak ada API, enrichment massal, database utama, perubahan .env/dependency,
scraper, SINTA, atau backfill lama dijalankan/diubah. Tahap 7 belum dikerjakan.

Validasi akhir Tahap 5–6, berurutan: client **75 passed**, matcher **125 passed**,
persistence **33 passed**, integrasi **35 passed**, SCImago/katalog **83 passed,
1 skipped**, runner/check/journal-index **137 passed**. Ada **90 test baru**.
Seluruh `python -m pytest -q`: **1018 passed, 2 skipped** dalam 9,51 detik.
`git diff --check` lulus. Migration tetap file saja, belum diuji pada server
PostgreSQL atau diterapkan. Tidak ada command contoh di atas yang dijalankan.

### Perbaikan pra-pengujian PostgreSQL: clock, snapshot, target

`crossref_pipeline.run(..., clock=callable)` memperoleh waktu aktual pada setiap
artikel, setelah konflik UPSERT, sebelum integrasi, dan setelah pencocokan master.
Jika TTL habis selama pemrosesan, `expired_during_processing` membuang hasil yang
sudah tidak boleh dipakai dan memblokir penyimpanan SCImago. Tidak ada retry HTTP
tambahan secara tersembunyi. `now=datetime` tetap tersedia sebagai kompatibilitas
test dengan waktu tetap; tidak boleh digabung dengan `clock`. Runner produksi
tidak memakai waktu tetap. Test memakai injected clock, termasuk TTL satu detik.

Setiap snapshot baru memiliki `metadata.snapshot_id`: SHA-256 dari input
fingerprint, versi matcher, dan hasil Crossref terstandar (termasuk waktu retrieval
dan evidence). Tidak ada perubahan kolom atau migration 16.

UPSERT memiliki predicate SQL atomik dengan urutan:

1. Retrieval lebih baru mengalahkan yang lama, dengan proteksi matched valid.
2. Pada timestamp sama, prioritas `matched > ambiguous > not_found > error`.
3. Status sama memakai urutan hash snapshot secara leksikografis dengan collation
   PostgreSQL `C`, sehingga urutan commit tidak menentukan pemenang.
4. Snapshot identik idempotent; hasil negatif tidak mengganti matched yang masih
   valid. Evaluasi proteksi memakai `clock_timestamp()`, bukan waktu transaksi awal.

Fungsi simpan memakai advisory transaction lock **per raw_publication_id**, lalu
row lock. Predicate UPSERT tetap menjadi pengaman SQL, bukan sekadar precheck
Python. Hasil snapshot ditolak/diganti disimpan bersama identitas dan evidence
dalam `metadata.snapshot_conflicts`, dibatasi **20 snapshot terbaru** secara
deterministik. Ini riwayat diagnostik terbatas, bukan ledger historis permanen.
Tie-breaker memilih snapshot secara deterministik; hash tidak membuktikan bahwa
salah satu DOI secara semantik lebih benar. Konflik tetap perlu ditinjau.

Audit integrasi memakai predicate exact `snapshot_id` selain raw ID, timestamp,
status, dan input fingerprint. Audit snapshot lama ditolak (`False`) setelah
snapshot aktif berubah, walaupun timestamp sama. `metadata.integration_audits`
menyimpan observasi dengan audit hash dan snapshot ID; penggabungan JSONB atomik
menghindari hilangnya observasi berbeda untuk snapshot aktif yang sama.
`metadata.scimago_2025` tetap tersedia sebagai observasi representatif yang dipilih
secara deterministik berdasarkan audit hash, bukan jaminan observasi paling baru
secara kronologis. Riwayat konflik snapshot mempertahankan audit yang sudah ada
ketika snapshot diganti; audit lama yang datang sesudah pergantian tidak ditempel
ke snapshot baru. Tidak ada perubahan terhadap verifikasi sumber lain.

#### Target database pengujian eksplisit

Default guard produksi tetap hanya menerima write pada `publication_dashboard`.
Jalur pengujian khusus hanya tersedia pada `--enrich-crossref` atau
`--crossref-scimago` dengan **kedua** opsi:

```text
--test-db-allowlist PATH --confirm-test-db crossref_test_stage56
```

Allowlist JSON adalah file operator yang dipercaya, bukan file rahasia. Contoh
struktur (belum merupakan konfigurasi cluster yang dijalankan):

```json
{
  "version": 1,
  "targets": [{
    "host": "127.0.0.1",
    "port": 55439,
    "database": "crossref_test_stage56",
    "user": "crossref_test_writer",
    "server_address": "127.0.0.1",
    "server_port": 55439,
    "cluster_name": "crossref-test-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
  }]
}
```

Ketentuan fail-closed:

- Nama database dan user harus berawalan `crossref_test_`; database utama tidak
  dapat didaftarkan. Konfirmasi nama harus menunjuk tepat satu entry.
- Host client harus literal loopback `127.0.0.1`/`::1`, port eksplisit 1024–65535
  dan bukan 5432. Untuk port mapping, `server_address`/`server_port` harus berisi
  identitas server yang memang diharapkan, bukan diasumsikan sama dengan port client.
- Keempat `POSTGRES_HOST`, `POSTGRES_PORT`, `POSTGRES_DB`, `POSTGRES_USER` harus
  sudah ada pada environment proses dan cocok persis **sebelum dotenv dibaca**.
  Tidak ada fallback `.env` untuk pemilihan target pengujian.
- URL engine diverifikasi lagi terhadap allowlist. Sebelum dispatch pipeline,
  transaksi read-only memeriksa database, session/current user, address/port server,
  dan `cluster_name`. Role superuser ditolak.
- Cluster terisolasi harus memiliki marker `crossref-test-` diikuti 32 digit hex
  unik yang dipasang operator pada konfigurasi cluster. Nama database saja tidak
  dianggap bukti isolasi. Marker contoh harus diganti dengan marker cluster uji.
- Jalur ini tidak membuka bypass untuk `--save`, mode legacy, atau database utama.
  Konfigurasi produksi tanpa opsi test tidak diubah. Password/connection string
  tidak dicetak, dan file `.env` tidak diubah.

Endpoint uji harus dedicated dan akses role dibatasi ke database uji; allowlist
bukan pengganti pembatasan privilege atau bukti melawan perubahan endpoint oleh
administrator. Tidak ada allowlist aktif atau cluster baru dibuat oleh pekerjaan ini.

Contoh preview **belum dijalankan**, setelah operator menyiapkan cluster/role,
schema, allowlist, dan environment proses yang sesuai:

```powershell
python -m src.journal_index.run_journal_index --crossref-scimago --crossref-limit 2 --test-db-allowlist C:/test-config/crossref-allowlist.json --confirm-test-db crossref_test_stage56
```

#### Batas pembuktian konkurensi

Test baru mencakup clock bergerak, predicate SQL PostgreSQL terkompilasi,
permutasi hasil, rollback mock, serta dua thread/transaksi melalui model lock
yang memanggil fungsi simpan aktual. Model memakai kebijakan urutan yang sama;
ia **tidak mengeksekusi SQL PostgreSQL dan bukan bukti runtime locking**.
Isolasi, blocking, binding JSONB, dan perilaku dua koneksi nyata masih harus diuji
setelah persetujuan. Tidak ada klaim bahwa seluruh race condition deployment
telah terbukti selesai. Penyimpanan enrichment dan verifikasi SCImago tetap
transaksi terpisah; audit terlambat ditolak, bukan ditempel pada snapshot lain.

Parameter key `jsonb_build_object` dan pembanding audit hash diberi CAST TEXT
eksplisit untuk menghindari bind bertipe tidak jelas pada driver PostgreSQL.
Ini diperiksa lewat kompilasi statement, bukan koneksi server.

Validasi perbaikan: **52 test baru**; pipeline **25 passed**, persistence
**33 passed**, konkurensi model/SQL **14 passed**, guard target **35 passed**,
regresi Crossref/SCImago/katalog **433 passed, 1 skipped**. Seluruh suite:
**1070 passed, 2 skipped** dalam 10,00 detik; `git diff --check` lulus.
Migration 16 dan `.env` tetap tidak berubah; tidak ada PostgreSQL, HTTP,
enrichment, migration apply, dependency update, commit, atau push pada pekerjaan ini.

## Katalog jurnal SINTA — sumber HTML lokal

`--sync-sinta-catalog` kini mendukung dua sumber halaman, **satu sumber per run**:

| Opsi | Default | Sumber halaman |
|---|---|---|
| `--source http` | ya | Tarik langsung dari SINTA (perilaku lama) |
| `--source local` | | Baca semua `*.html` dari `--html-dir` (default `data/sinta/1093/`) |

Latar belakang: SINTA menolak request Python dengan HTTP 403 meskipun halamannya
terbuka normal di browser. Kode tidak meniru browser, tidak memakai cookie/proxy,
dan tidak mencoba ulang terhadap 403/429. Saat ditolak, run berhenti seketika
(status `BLOCKED`, exit code 3) dengan diagnosis status, header `Server`, dan 300
karakter awal body, lalu mencetak instruksi jalur manual di bawah.

### Prosedur menangkap HTML secara manual

1. Buka tiap halaman katalog di browser, misalnya
   `https://sinta.kemdiktisaintek.go.id/journals/index/1093` lalu halaman 2, 3, dst.
2. Simpan tiap halaman sebagai **satu file**: `Save as > Webpage, HTML only`
   (format ini menyisipkan komentar `saved from url=...` yang dipakai kode untuk
   menentukan URL sumber dan nomor halaman), atau salin `outerHTML` dari DevTools
   ke file `.html` (bila memakai cara ini, sisipkan sendiri komentar
   `<!-- saved from url=(NN)URL_HALAMAN -->` di awal file atau pastikan tag
   `<link rel="canonical">` tersedia).
3. Taruh semua file di folder default `data/sinta/1093/` (sudah diabaikan Git).
   Nama file bebas — urutan halaman ditentukan dari **isi HTML** (penanda halaman
   aktif atau URL sumber), bukan dari nama file. File tanpa penanda nomor dan
   tanpa URL sumber ditolak; dua file dengan nomor halaman sama ditolak dengan
   menyebut kedua file. Menambah file halaman baru tidak butuh perubahan kode.

### Menjalankan dry-run dan save untuk sumber lokal

```powershell
# Pratinjau baca-saja: pemeriksaan kelengkapan tanpa menulis database.
python -m src.journal_index.run_journal_index --sync-sinta-catalog --source local --dry-run

# Simpan ke database (wajib publication_dashboard; jalankan migration 18 dulu).
python -m src.journal_index.run_journal_index --sync-sinta-catalog --source local --save

# Waktu tangkapan eksplisit (ISO-8601); tanpa ini dipakai waktu modifikasi file
# dan ringkasan menyatakan itu.
python -m src.journal_index.run_journal_index --sync-sinta-catalog --source local --captured-at 2026-11-03T08:00:00+00:00 --dry-run
```

Ringkasan run memuat `pages_found`, `pages_processed`, `items_found`,
`items_unique`, `duplicates_dropped`, `source_type`, `status`
(`LENGKAP`/`PARTIAL`/`BLOCKED`/`GAGAL`), `warnings`, dan `captured_at_source`.
Kode keluar `--save`: 0 LENGKAP, 1 PARTIAL, 2 GAGAL, 3 BLOCKED; `--dry-run`
selalu 0 karena hanya pratinjau. Pemeriksaan kelengkapan menuntut nomor halaman
1..N berurutan tanpa celah, N sama dengan nomor tertinggi di pagination, jumlah
jurnal unik sama dengan total entri bila halaman menyediakannya, ID afiliasi 1093
di semua halaman, dan tidak ada halaman error/kosong tersimpan. Pagination
terpotong (`1 2 3 … 10`) tidak pernah dianggap bukti halaman tersembunyi:
halaman yang tidak tertaut dan tidak ada di folder membuat status PARTIAL.
Untuk sumber HTTP, setelah selesai halaman pertama diambil sekali lagi; bila
total/jurnal pertama berubah, penjelajahan diulang sekali, dan bila masih berubah
status menjadi PARTIAL. Deduplikasi memakai SINTA Profile ID; jurnal yang tidak
muncul di run baru tidak dihapus dari database.

Migration additive `sql/ddl/18_add_sinta_catalog_source_type.sql` menambah kolom
`source_type` (`http`/`local`, default `http`) dan menjadikan `source_url`
nullable: HTML lokal tanpa komentar `saved from url`/canonical dibiarkan kosong,
tidak mengarang URL. Terapkan manual di database utama setelah peninjauan.
