-- ============================================================
-- 12_add_core_publication_author_name_type.sql
-- ------------------------------------------------------------
-- Migrasi ADITIF: tambah kolom author_name dan author_type pada
-- core.publication_authors (definisi tabel di
-- 02_create_core_tables.sql:24-29 yang semula hanya
-- publication_id, author_id, author_order + composite PK).
--
-- Kolom baru:
--   author_name  = nama kanonik penulis pada publikasi terkait,
--                  di-backfill dari core.authors lewat FK
--                  author_id (BUKAN tebakan/normalisasi teks).
--   author_type  = peran berbasis urutan, mengikuti vocabulary
--                  author_role di raw.scholar_publication_author
--                  (10_create_raw_publication_author.sql:77-82):
--                  'penulis_pertama' untuk author_order = 0,
--                  'co_author' untuk selain itu — aturan yang
--                  sudah dipakai loader.
--
-- Migrasi KUMULATIF: berlaku untuk database baru (setelah 02)
-- maupun database yang sudah berjalan.
--
-- IDEMPOTEN: aman dijalankan berulang. ADD COLUMN IF NOT EXISTS
-- dan backfill hanya mengisi baris yang masih NULL, sehingga
-- pengulangan tidak pernah menimpa nilai yang sudah ada.
-- TIDAK mengubah dan TIDAK menghapus kolom lama; tidak ada DROP.
-- ============================================================


-- ------------------------------------------------------------
-- 1. Kolom baru (aditif, idempoten)
-- ------------------------------------------------------------
ALTER TABLE core.publication_authors
    ADD COLUMN IF NOT EXISTS author_name TEXT;

ALTER TABLE core.publication_authors
    ADD COLUMN IF NOT EXISTS author_type TEXT;


-- ------------------------------------------------------------
-- 2. Backfill idempoten (hanya baris yang masih NULL)
-- ------------------------------------------------------------
-- 2a. author_name dari nama kanonik core.authors yang terkait FK
--     author_id. Sumber kebenaran: kolom name tabel core.authors
--     (02:5). Baris dengan author_name sudah terisi dilewati.
UPDATE core.publication_authors pa
SET author_name = a.name
FROM core.authors a
WHERE a.author_id = pa.author_id
  AND pa.author_name IS NULL;

-- 2b. author_type dari aturan urutan yang sudah dipakai loader:
--     author_order = 0 -> 'penulis_pertama', selain itu ->
--     'co_author'. Vocabulary sama dengan author_role di
--     raw.scholar_publication_author (10:77-82).
UPDATE core.publication_authors
SET author_type = CASE
        WHEN author_order = 0 THEN 'penulis_pertama'
        ELSE 'co_author'
    END
WHERE author_type IS NULL;


-- ------------------------------------------------------------
-- 3. Catatan
-- ------------------------------------------------------------
-- Kedua kolom NULLABLE: baris tanpa nama kanonik yang cocok
-- (mis. FK penulis lama) tetap NULL — tidak pernah ditebak.
-- author_name di-backfill dari nama kanonik core.authors terkait
-- FK (bukan tebakan), author_type diturunkan dari aturan urutan
-- yang sudah dipakai loader. Tidak ada DROP / TRUNCATE / DELETE;
-- kolom lama (publication_id, author_id, author_order) dan
-- composite PK tidak disentuh.
