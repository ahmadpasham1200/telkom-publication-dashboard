-- ============================================================
-- 13_rename_core_publications_publication_id.sql
-- ------------------------------------------------------------
-- Migrasi RENAME kolom PK: core.publications.publications_id
-- -> publication_id (naming target schema terbaru).
--
-- Di PostgreSQL rename kolom bersifat METADATA-ONLY: indeks PK,
-- NOT NULL, DEFAULT, dan constraint tidak berubah isi/data.
-- FK di core.publication_authors (02:25) dan
-- core.publication_metrics (02:43) OTOMATIS MENGIKUTI karena
-- Postgres menyimpan referensi kolom luar negeri berdasarkan
-- attnum (nomor kolom), BUKAN nama kolom — tidak ada FK yang
-- rusak, tidak perlu perbaikan manual.
--
-- IDEMPOTEN: hanya rename jika kolom lama publications_id MASIH
-- ADA (guard information_schema); kalau sudah pernah di-rename,
-- blok dilewati dan hasilnya tetap publication_id. Aman
-- dijalankan berulang.
--
-- TIDAK ADA DROP: tidak ada constraint, indeks, maupun data yang
-- dihapus; hanya nama kolom yang berganti.
--
-- CATATAN COMPOSE: folder sql/ddl/ dimuat ke
-- /docker-entrypoint-initdb.d (compose.yml), jadi file ini
-- dijalankan OTOMATIS hanya saat container BARU pertama kali
-- diinisialisasi, berurutan alfabetis setelah 02. Database yang
-- SUDAH ADA menjalankan file ini MANUAL (pgAdmin) — gerbang
-- persetujuan terpisah.
-- ============================================================


-- ------------------------------------------------------------
-- Guard idempoten: rename hanya saat kolom lama masih ada
-- ------------------------------------------------------------
DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = 'core'
          AND table_name  = 'publications'
          AND column_name = 'publications_id'
    ) THEN
        ALTER TABLE core.publications
            RENAME COLUMN publications_id TO publication_id;
    END IF;
END
$$;


-- ------------------------------------------------------------
-- Catatan hasil
-- ------------------------------------------------------------
-- Di DB baru: 02_create_core_tables.sql membuat publications_id
-- (02:12) lalu file ini me-rename-nya — hasil akhir KONSISTEN
-- publication_id, sama dengan nama yang dipakai kolom FK
-- pemanggil (core.publication_authors.publication_id,
-- core.publication_metrics.publication_id). Kolom lain pada
-- core.publications (title, publication_year, issue, link,
-- journal, volume, pages, publisher, description, category, doi)
-- TIDAK disentuh. Kalau sudah pernah dijalankan, guard di atas
-- membuatnya no-op.
