-- ============================================================
-- 05_create_staging_tables.sql
-- ------------------------------------------------------------
-- Layer STAGING: nilai yang sudah dibersihkan, sebelum masuk core.
--
-- Loader menulis staging + core dalam SATU transaksi:
--   - staging.stg_authors      : data profil dibersihkan
--   - staging.stg_publications : publikasi dibersihkan
--     (trim whitespace, DATE dari YYYY/MM/DD, pecah volume/pages,
--     pecah daftar penulis, category turunan, author_source)
--
-- Staging berfungsi sebagai jejak audit "nilai bersih yang dipakai
-- untuk upsert core" - kalau hasil di core keliru, bisa dilihat
-- apakah kesalahan terjadi saat pembersihan (staging) atau saat
-- penulisan (core).
--
-- IDEMPOTEN: CREATE TABLE IF NOT EXISTS. Aman dijalankan ulang.
-- ============================================================


-- ------------------------------------------------------------
-- 1. staging.stg_authors
-- ------------------------------------------------------------
-- Data level profil yang sudah dibersihkan (nama dirapatkan, dll).
-- Satu baris per run per profil, sama seperti raw.scholar_author.

CREATE TABLE IF NOT EXISTS staging.stg_authors (
    stg_author_id SERIAL PRIMARY KEY,
    result_id     INTEGER NOT NULL,
    urutan        INTEGER NOT NULL DEFAULT 0,
    scholar_id    VARCHAR(50) NOT NULL,
    nama          TEXT NOT NULL,
    h_index       INTEGER,
    domain        TEXT,
    batches       INTEGER,
    elapsed       NUMERIC,

    CONSTRAINT uq_stg_authors_result_urutan
        UNIQUE (result_id, urutan)
);


-- ------------------------------------------------------------
-- 2. staging.stg_publications
-- ------------------------------------------------------------
-- Publikasi yang sudah dibersihkan, siap di-upsert ke core.
-- authors tetap satu TEXT (daftar penulis hasil pembersihan);
-- pemecahan menjadi baris core.publication_authors dilakukan
-- loader saat menulis core.
--
-- author_source mencatat asal daftar penulis:
--   'detail' - dari halaman detail (otoritatif)
--   'list'   - dari halaman daftar (fallback)

CREATE TABLE IF NOT EXISTS staging.stg_publications (
    stg_publication_id SERIAL PRIMARY KEY,
    result_id     INTEGER NOT NULL,
    urutan        INTEGER NOT NULL,
    scholar_id    VARCHAR(50) NOT NULL,

    title            TEXT NOT NULL,
    publication_year INTEGER,
    publication_date DATE,
    journal          VARCHAR(255),
    volume           VARCHAR(50),
    pages            VARCHAR(50),
    issue            VARCHAR(50),
    publisher        VARCHAR(255),
    description      TEXT,
    category         VARCHAR(100),
    link             TEXT,
    doi              VARCHAR(100),

    authors          TEXT,
    author_source    VARCHAR(10),

    CONSTRAINT uq_stg_publications_result_urutan
        UNIQUE (result_id, urutan)
);