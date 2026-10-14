-- ============================================================
-- 09_add_raw_publication_status.sql
-- ------------------------------------------------------------
-- Migrasi ADITIF: tambah kolom status pada
-- raw.scholar_publication untuk database yang SUDAH ADA.
--
-- Kolom status (TEXT) menyimpan penanda kelengkapan metadata
-- bibliografi halaman detail: 'yes' jika keempat field
-- detail_volume, detail_issue, detail_pages, dan
-- detail_description lengkap (non-NULL, non-kosong); 'no' jika
-- setidaknya satu tidak lengkap / key hilang. Nilai ini DIHITUNG
-- oleh loader (fungsi murni _status_kelengkapan_metadata) saat
-- INSERT, bukan diambil dari key baris scraper.
--
-- CREATE TABLE IF NOT EXISTS di 04 tidak menambah kolom ke tabel
-- yang sudah ada, jadi migrasi ini diperlukan untuk upgrade.
--
-- TANPA backfill: baris lama dibiarkan NULL (belum pernah
-- dihitung), bukan dipaksa 'no'.
--
-- IDEMPOTEN: ADD COLUMN IF NOT EXISTS. Aman dijalankan ulang.
-- Tidak menyentuh kolom/constraint/data lain.
-- ============================================================

ALTER TABLE raw.scholar_publication
    ADD COLUMN IF NOT EXISTS status TEXT;
