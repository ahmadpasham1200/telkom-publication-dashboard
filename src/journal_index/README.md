# Journal index

Modul ini hanya membaca `raw.scholar_publication`. Tidak menjalankan Scholar.

```sh
python -m src.journal_index.run_journal_index --help
python -m src.journal_index.run_journal_index --dry-run --limit 10
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
SCImago/SINTA memakai jalur bukti manual yang sudah tersedia dan diberi label demikian.
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

Pemeriksaan terbaru mengganti snapshot sumber sebelumnya.
Satu snapshot SCImago bisa memuat banyak tahun/kategori di JSONB `sjr`.
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

SCImago dan SINTA menggunakan berkas bukti lokal yang ditranskripsi pengguna dari
halaman jurnal resmi. Implementasi ini tidak mengklaim pencarian web langsung atau
verifikasi daring atas berkas tersebut. Jangan mengimpor katalog penuh: hanya
masukkan jurnal dari kandidat RAW. Tanpa berkas, status `access_unavailable`.
Set `SCIMAGO_EVIDENCE_FILE` / `SINTA_EVIDENCE_FILE` ke path JSON lokal.
Berkas dibatasi 2 MB dan tidak boleh berisi kredensial, token atau cookie.

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
