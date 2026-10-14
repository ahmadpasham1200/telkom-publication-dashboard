-- ============================================================
-- 11_add_raw_scholar_author_prodi.sql
-- ------------------------------------------------------------
-- Migrasi ADITIF: tambah kolom prodi pada raw.scholar_author
-- untuk database yang SUDAH ADA.
--
-- Kolom prodi (TEXT) menyimpan program studi dosen PEMILIK PROFIL
-- Google Scholar, diambil dari kolom PRODI Excel referensi
-- (--referensi-dosen) dan hanya disimpan ketika identitas lolos
-- gerbang verifikasi K8 (scholar_id eksak + nama cocok; tanpa
-- fuzzy matching). NULL = tanpa referensi, gagal verifikasi, atau
-- sel prodi kosong di Excel.
--
-- CREATE TABLE IF NOT EXISTS di 04 tidak menambah kolom ke tabel
-- yang sudah ada, jadi migrasi ini diperlukan untuk upgrade.
--
-- TANPA backfill: baris lama dibiarkan NULL (tidak pernah ada
-- nilai prodi untuk run lama).
--
-- IDEMPOTEN: ADD COLUMN IF NOT EXISTS. Aman dijalankan ulang.
-- Tidak menyentuh kolom/constraint/data lain.
-- ============================================================

ALTER TABLE raw.scholar_author
    ADD COLUMN IF NOT EXISTS prodi TEXT;
