-- ============================================================
-- 10_create_raw_publication_author.sql
-- ------------------------------------------------------------
-- Migrasi ADITIF: tabel baru raw.scholar_publication_author —
-- satu baris per penulis per publikasi per run, ditulis dari
-- record author yang disusun src/loading/author_records.py
-- (P57) dan diintegrasikan lewat loader pada P58.
--
-- Dasar desain: keputusan_desain_author_p56.txt §5 (final),
-- berakar pada kesepakatan K1..K12; laporan P57 (helper) dan
-- P58 (kontrak statistik) memverifikasi bahwa kunci record di
-- kode persis sama dengan kolom tabel ini.
--
-- Dua kolom yang sengaja TERPISAH (K5+K7) — JANGAN digabung:
--   author_role          = urutan tampil saja ('penulis_pertama'
--                          untuk order 0, 'co_author' untuk >0);
--   profile_owner_status = hasil verifikasi identitas pemilik
--                          profil ('pemilik_profil' |
--                          'bukan_pemilik_profil' |
--                          'belum_terverifikasi').
-- Seorang penulis bisa berstatus co_author SEKALIGUS
-- pemilik_profil pada publikasi yang sama.
--
-- TANPA backfill, TANPA data contoh: tabel hanya menerima baris
-- dari run baru (append-per-run, mengikuti pola
-- raw.scholar_publication). Baris lama tidak pernah diubah.
--
-- IDEMPOTEN: CREATE TABLE IF NOT EXISTS. Aman dijalankan ulang.
-- Tidak ada DROP / TRUNCATE / DELETE / UPDATE / INSERT, dan
-- tidak menyentuh tabel yang sudah ada.
--
-- CATATAN COMPOSE: folder sql/ddl/ dimuat ke
-- /docker-entrypoint-initdb.d (compose.yml), jadi file ini
-- dijalankan OTOMATIS hanya saat container BARU pertama kali
-- diinisialisasi (volume data kosong) — urutan alfabetis
-- menjamin 04 (tabel induk) berjalan sebelum 10. Database yang
-- SUDAH ADA (publication_dashboard / publication_test) TIDAK
-- disentuh oleh penambahan file ini; penerapan pada database
-- berjalan tetap GERBANG PERSETUJUAN TERPISAH (Tahap 3).
-- ============================================================


-- ------------------------------------------------------------
-- raw.scholar_publication_author
-- ------------------------------------------------------------
-- Relasi ke publikasi memakai raw_publication_id — PK tabel
-- raw.scholar_publication, TERVERIFIKASI di
-- 04_create_raw_structured.sql:78 (SERIAL PRIMARY KEY).
-- result_id BUKAN PK publikasi (04:81/:134), sehingga TIDAK
-- dipakai sebagai FK relasi ini; rantai garis keturunan run
-- tetap terjaga lewat result_id milik publikasi induk.
--
-- Kolom audit: created_at TIMESTAMP DEFAULT now() mengikuti
-- konvensi kolom waktu RAW (scraped_at di 03:50). Tanpa
-- kolom audit tambahan (updated_at dsb.) — baris RAW append-
-- per-run tidak pernah di-update.
CREATE TABLE IF NOT EXISTS raw.scholar_publication_author (
    raw_publication_author_id SERIAL PRIMARY KEY,

    -- Publikasi asal (FK; tipe INTEGER selaras SERIAL PK
    -- induk di 04:78).
    raw_publication_id    INTEGER NOT NULL
                          REFERENCES raw.scholar_publication(
                              raw_publication_id),

    -- Urutan tampil penulis di Google Scholar, 0-based (K4).
    -- Sumber kebenaran urutan; loader tidak pernah mengurutkan
    -- ulang. Bagian dari UNIQUE di bawah.
    author_order          INTEGER NOT NULL,

    -- Nama PENULIS PERSIS apa adanya dari sumber (K2): teks
    -- hasil _pecah_penulis — tanpa pembalikan urutan kata,
    -- tanpa recasing, tanpa pengayaan/penyamaan nama Excel.
    -- TEXT mengikuti gaya nama di raw (04:50).
    author_name           TEXT NOT NULL,

    -- Peran berbasis urutan SAJA (K5, invarian P56 §3 I1):
    -- 'penulis_pertama' (author_order=0) | 'co_author'
    -- (author_order>0). Murni fungsi author_order; tidak
    -- pernah NULL; tidak memuat identitas; tidak menunggu
    -- verifikasi.
    author_role           TEXT NOT NULL,

    -- Status kepemilikan profil hasil verifikasi identitas
    -- (K6, Opsi A P56 §4): 'pemilik_profil' |
    -- 'bukan_pemilik_profil' | 'belum_terverifikasi'.
    -- Tanpa verifikasi yang lolos, SEMUA baris publikasi itu
    -- 'belum_terverifikasi' (K8: tanpa bukti, tanpa klaim).
    profile_owner_status  TEXT NOT NULL,

    -- Scholar ID pemilik profil: HANYA diisi pada baris
    -- 'pemilik_profil' yang terverifikasi (D-3); baris lain
    -- NULL permanen di lingkup ini — termasuk saat verifikasi
    -- gagal (ID tetap utuh di publikasi induk). VARCHAR(50)
    -- selaras dengan scholar_id di 03:45 / 04:47 / 04:88.
    scholar_id            VARCHAR(50),

    -- Asal daftar penulis (D-1): 'detail' = daftar lengkap
    -- halaman detail; 'daftar' = kolom authors halaman profil
    -- (bisa terpotong elipsis). Nilai domain divalidasi di
    -- kode (author_records.py:254-257) dan lewat tes unit;
    -- tanpa CHECK constraint mengikuti gaya tabel raw yang
    -- ada (keputusan P56 §5).
    sumber                TEXT NOT NULL,

    -- TRUE hanya bila sumber='daftar' dan teks daftar asli
    -- memuat elipsis pemotongan Scholar (D-1). Penanda
    -- KETERBATASAN daftar, BUKAN bukti nama hilang (K12).
    daftar_terpotong      BOOLEAN NOT NULL DEFAULT FALSE,

    -- Waktu penulisan baris (konvensi03:50).
    created_at            TIMESTAMP NOT NULL DEFAULT now(),

    -- Satu penulis per posisi per publikasi. Mendukung retry
    -- idempoten ON CONFLICT (raw_publication_id, author_order)
    -- DO NOTHING (P56 §6e) dan meniru pola UNIQUE di 04:60-61
    -- / 04:134-135. TANPA unique berbasis nama / scholar_id:
    -- satu orang BOLEH muncul di lebih dari satu posisi dalam
    -- satu publikasi (D-7), dan identitas orang tidak boleh
    -- disamakan lewat nama (P54 §4, K10).
    CONSTRAINT uq_raw_scholar_publication_author_pub_urutan
        UNIQUE (raw_publication_id, author_order)
);
