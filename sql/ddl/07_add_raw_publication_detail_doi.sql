-- ============================================================
-- 07_add_raw_publication_detail_doi.sql
-- ------------------------------------------------------------
-- Migrasi ADITIF: tambah kolom detail_doi pada
-- raw.scholar_publication untuk database yang SUDAH ADA.
--
-- Kolom detail_doi menyimpan DOI (TEXT) hasil ekstraksi dari
-- detail_link/detail_pdf_link. Turunan, bukan data mentah halaman:
-- NULL berarti DOI tidak berhasil ditentukan secara tidak ambigu
-- (tidak ada / dua DOI berbeda), BUKAN bukti sumber tanpa DOI.
--
-- CREATE TABLE IF NOT EXISTS di 04 tidak menambah kolom ke tabel
-- yang sudah ada, jadi migrasi ini diperlukan untuk upgrade.
--
-- IDEMPOTEN: ADD COLUMN IF NOT EXISTS. Aman dijalankan ulang.
-- Tidak menyentuh kolom/constraint/data lain.
-- ============================================================

ALTER TABLE raw.scholar_publication
    ADD COLUMN IF NOT EXISTS detail_doi TEXT;
