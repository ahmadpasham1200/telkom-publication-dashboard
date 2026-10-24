-- ============================================================
-- 14_update_core_publication_authors_pk.sql
-- ------------------------------------------------------------
-- Menggunakan publication_author_id sebagai PK.
-- author_id boleh NULL untuk coauthor tanpa profil teridentifikasi.
-- author_name wajib diisi jika author_id NULL.
-- Urutan penulis harus unik dalam setiap publikasi.
--
-- Tipe kolom: INTEGER (bukan BIGINT) — selaras dengan mirror kode
-- di src/loading/postgres.py (sa.Integer, autoincrement) dan
-- konvensi tabel project yang memakai SERIAL/INTEGER.
--
-- IDEMPOTEN: seluruh urutan ALTER dibungkus satu blok DO $$
-- conditional yang dijalankan HANYA jika kolom publication_author_id
-- BELUM ada di core.publication_authors. PostgreSQL tidak punya
-- ADD CONSTRAINT IF NOT EXISTS, jadi guard-nya ada di level blok:
-- kalau kolom sudah ada (migrasi pernah jalan), DROP/ADD constraint
-- dan DROP NOT NULL dilewati semua — aman dijalankan berulang.
-- Pola guard mengikuti 13_rename_core_publications_publication_id.sql
-- (DO $$ + information_schema.columns). BEGIN/COMMIT dipertahankan:
-- seluruh ALTER berjalan sebagai satu transaksi atomik.
-- ============================================================

BEGIN;

-- ------------------------------------------------------------
-- Guard idempoten: ubah hanya saat kolom PK surrogate belum ada
-- ------------------------------------------------------------
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = 'core'
          AND table_name  = 'publication_authors'
          AND column_name = 'publication_author_id'
    ) THEN
        ALTER TABLE core.publication_authors
            DROP CONSTRAINT publication_authors_pkey;

        ALTER TABLE core.publication_authors
            ADD COLUMN publication_author_id INTEGER
            GENERATED ALWAYS AS IDENTITY;

        ALTER TABLE core.publication_authors
            ADD CONSTRAINT publication_authors_pkey
            PRIMARY KEY (publication_author_id);

        ALTER TABLE core.publication_authors
            ALTER COLUMN author_id DROP NOT NULL;

        ALTER TABLE core.publication_authors
            ADD CONSTRAINT publication_authors_publication_order_key
            UNIQUE (publication_id, author_order);

        ALTER TABLE core.publication_authors
            ADD CONSTRAINT publication_authors_name_required_without_author_id
            CHECK (
                author_id IS NOT NULL
                OR NULLIF(BTRIM(author_name), '') IS NOT NULL
            );
    END IF;
END
$$;

COMMIT;
