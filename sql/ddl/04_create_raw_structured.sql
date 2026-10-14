-- ============================================================
-- 04_create_raw_structured.sql
-- ------------------------------------------------------------
-- Tabel raw TERSTRUKTUR, diturunkan dari envelope JSONB
-- raw.scholar_scrape_result.
--
-- Envelope JSONB tetap dipertahankan sebagai catatan durabilitas
-- run (apa adanya, termasuk field yang tidak dipetakan ke core).
-- Tabel di file ini menyimpan data yang sama dalam bentuk kolom
-- biasa supaya bisa difilter / di-join dengan SQL tanpa membuka
-- JSONB, dan menjadi sumber data untuk layer staging.
--
-- Dua tabel:
--   1. raw.scholar_author      - satu baris per run per profil
--                                (data level profil: nama, h_index,
--                                domain, batches, elapsed).
--   2. raw.scholar_publication - satu baris per publikasi per run,
--                                semua kolom TEXT mentah apa adanya
--                                (termasuk field halaman detail).
--
-- IDEMPOTEN: CREATE TABLE IF NOT EXISTS. Aman dijalankan ulang.
-- ============================================================


-- ------------------------------------------------------------
-- 1. raw.scholar_author
-- ------------------------------------------------------------
-- Satu baris = satu run scraping untuk satu profil. Ditulis dalam
-- transaksi raw yang SAMA dengan envelope (raw.scholar_scrape_result),
-- jadi retry run_id yang sama tidak menggandakan baris.
--
-- UNIQUE (result_id, urutan): urutan selalu 0 di tabel ini (hanya
-- satu baris per run), tapi kolomnya dipertahankan supaya pola
-- ON CONFLICT (result_id, urutan) DO NOTHING seragam dengan
-- raw.scholar_publication.

CREATE TABLE IF NOT EXISTS raw.scholar_author (
    raw_author_id SERIAL PRIMARY KEY,

    -- Run asal (FK ke envelope).
    result_id     INTEGER NOT NULL REFERENCES raw.scholar_scrape_result(result_id),

    -- Urutan dalam run; selalu 0 untuk data profil.
    urutan        INTEGER NOT NULL DEFAULT 0,

    -- Scholar ID profil yang di-scrape.
    scholar_id    VARCHAR(50) NOT NULL,

    -- Nama profil apa adanya dari halaman daftar.
    nama          TEXT NOT NULL,

    -- h_index dari halaman daftar (sudah berupa angka di envelope).
    h_index       INTEGER,

    -- Domain / batches / elapsed dari envelope (metadata run).
    domain        TEXT,
    batches       INTEGER,
    elapsed       NUMERIC,

    -- Prodi pemilik profil dari Excel referensi yang lolos verifikasi
    -- identitas (gerbang K8); NULL = tanpa referensi / gagal
    -- verifikasi / kolom prodi kosong / baris lama sebelum migrasi 11.
    prodi         TEXT,

    CONSTRAINT uq_raw_scholar_author_result_urutan
        UNIQUE (result_id, urutan)
);


-- ------------------------------------------------------------
-- 2. raw.scholar_publication
-- ------------------------------------------------------------
-- Satu baris = satu publikasi dalam satu run. Semua kolom TEXT
-- mentah APA ADANYA dari halaman daftar maupun halaman detail,
-- tanpa pembersihan apa pun - pembersihan terjadi di staging.
--
-- Field halaman daftar: title, authors, venue, extra, citations,
-- year, publication_year (tahun hasil parse baris daftar).
-- Field halaman detail: detail_* (diisi hanya kalau halaman detail
-- berhasil diambil; NULL kalau gagal / --tanpa-detail).

CREATE TABLE IF NOT EXISTS raw.scholar_publication (
    raw_publication_id SERIAL PRIMARY KEY,

    -- Run asal (FK ke envelope).
    result_id     INTEGER NOT NULL REFERENCES raw.scholar_scrape_result(result_id),

    -- Urutan publikasi di dalam rows[] envelope (0, 1, 2, ...).
    urutan        INTEGER NOT NULL,

    -- Denormalisasi scholar_id profil, supaya bisa difilter per
    -- profil tanpa join ke envelope.
    scholar_id    VARCHAR(50) NOT NULL,

    -- ---- Field halaman daftar (mentah) ----
    title            TEXT,
    authors          TEXT,
    venue            TEXT,
    extra            TEXT,
    citations        TEXT,
    year             TEXT,
    publication_year INTEGER,

    -- ---- Field halaman detail (mentah) ----
    -- URL halaman detail (diturunkan dari href link judul).
    detail_url         TEXT,
    detail_title       TEXT,
    detail_authors     TEXT,
    detail_date        TEXT,
    detail_venue       TEXT,
    detail_volume      TEXT,
    detail_issue       TEXT,
    detail_pages       TEXT,
    detail_publisher   TEXT,
    detail_description TEXT,
    detail_citations   TEXT,
    detail_link        TEXT,
    detail_pdf_link    TEXT,
    -- DOI hasil ekstraksi dari detail_link/detail_pdf_link (TURUNAN,
    -- bukan data mentah halaman). NULL = tidak ditemukan / ambigu,
    -- BUKAN bukti sumber tanpa DOI. Lihat migrasi 07.
    detail_doi         TEXT,

    -- ---- Field bibliografi profil (ekstraksi P2 dari teks venue/extra
    -- halaman daftar; TERPISAH dari detail_* halaman detail, sumber
    -- dan nilai boleh berbeda). NULL = tidak terekstrak. Lihat migrasi 08. ----
    profile_volume     TEXT,
    profile_issue      TEXT,
    profile_pages      TEXT,

    -- ---- Penanda kelengkapan metadata detail (turunan, P24) ----
    -- 'yes' jika keempat detail_volume/detail_issue/detail_pages/
    -- detail_description lengkap; 'no' jika salah satu tidak
    -- lengkap. DIHITUNG oleh loader, bukan data mentah scraper.
    -- NULL = baris lama sebelum migrasi 09 / belum dihitung.
    -- Lihat migrasi 09.
    status             TEXT,

    CONSTRAINT uq_raw_scholar_publication_result_urutan
        UNIQUE (result_id, urutan)
);