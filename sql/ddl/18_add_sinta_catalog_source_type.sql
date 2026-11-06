-- Provenance tambahan untuk katalog jurnal SINTA (migrasi manual; additive dan idempoten).
-- source_type membedakan baris dari tarikan HTTP ('http') dan HTML tangkapan manual ('local').
-- source_url menjadi nullable: HTML lokal tanpa komentar "saved from url"/tag canonical
-- dibiarkan kosong, tidak mengarang URL. Aman untuk tabel yang sudah berisi data:
-- kolom baru terisi DEFAULT 'http' untuk semua baris lama, dan tidak ada data dihapus.
BEGIN;
ALTER TABLE raw.sinta_journal_catalog ADD COLUMN IF NOT EXISTS source_type TEXT NOT NULL DEFAULT 'http';
ALTER TABLE raw.sinta_journal_catalog ALTER COLUMN source_url DROP NOT NULL;
COMMIT;
