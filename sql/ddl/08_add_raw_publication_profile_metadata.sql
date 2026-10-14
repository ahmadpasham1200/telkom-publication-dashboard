-- ============================================================
-- 08_add_raw_publication_profile_metadata.sql
-- ------------------------------------------------------------
-- Migrasi ADITIF: tambah kolom bibliografi profil
-- (profile_volume, profile_issue, profile_pages) pada
-- raw.scholar_publication untuk database yang SUDAH ADA.
--
-- Ketiga kolom berisi ekstraksi bibliografi PROFIL (P2) dari teks
-- venue/extra pada halaman daftar, mis. "ACM ... 5 (2), 23-32".
-- Metadata ini TERPISAH dari detail_volume/detail_issue/detail_pages
-- yang datang dari halaman detail: sumbernya berbeda, jadi boleh
-- berbeda nilai, dan keduanya tidak boleh saling menimpa.
-- Parser profil memakai key row "volume"/"issue"/"pages"; awalan
-- profile_ pada nama kolom menandakan asal metadata ini agar jelas
-- dan tidak bercampur dengan detail_*.
--
-- CREATE TABLE IF NOT EXISTS di 04 tidak menambah kolom ke tabel
-- yang sudah ada, jadi migrasi ini diperlukan untuk upgrade.
--
-- TANPA backfill: baris lama dibiarkan NULL. NULL berarti memang
-- tidak ada nilai yang terekstrak (teks venue/extra tidak memuat
-- pola bibliografi), bukan kegagalan menulis.
--
-- IDEMPOTEN: ADD COLUMN IF NOT EXISTS. Aman dijalankan ulang.
-- Tidak menyentuh kolom/constraint/data lain.
-- ============================================================

ALTER TABLE raw.scholar_publication
    ADD COLUMN IF NOT EXISTS profile_volume TEXT,
    ADD COLUMN IF NOT EXISTS profile_issue TEXT,
    ADD COLUMN IF NOT EXISTS profile_pages TEXT;
