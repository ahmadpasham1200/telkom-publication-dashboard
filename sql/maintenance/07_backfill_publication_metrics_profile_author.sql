-- ============================================================
-- sql/maintenance/07_backfill_publication_metrics_profile_author.sql
-- ------------------------------------------------------------
-- QUERY MANUAL - BUKAN bagian dari migrasi otomatis.
--
-- Sengaja TIDAK diletakkan di sql/ddl/: folder itu dijalankan
-- otomatis saat container baru dibuat, sedangkan file ini adalah
-- backfill DATA (bukan skema) yang hanya boleh dijalankan manual
-- setelah migrasi 06, dan hanya di database yang memang butuh.
--
-- Mengisi profile_author_id untuk baris core.publication_metrics
-- LAMA (yang dibuat sebelum kolom ini ada). Baris baru sudah
-- diisi loader secara langsung.
--
-- Cara kerja (best-effort):
--   - core.publications tidak menyimpan run_id, jadi run asal
--     dicari lewat raw.scholar_publication (title harus cocok
--     persis dengan title di core - keduanya lewat pembersihan
--     mapper yang sama).
--   - scholar_id dari run itu dicocokkan ke core.authors.scholar_id
--     (author pemilik profil).
--   - DISTINCT ON + scraped_at DESC: kalau satu publikasi muncul
--     di beberapa run/profil, dipakai run TERBARU.
--
-- KETERBATASAN: title yang berbeda antar run (mis. karena
-- pembersihan berbeda) tidak akan cocok dan tetap NULL. Baris
-- yang tidak terisi bisa dicek dengan query SELECT di bawah.
--
-- UJI DULU di publication_test sebelum dipakai di database utama.
-- ============================================================


-- ------------------------------------------------------------
-- 1. Backfill
-- ------------------------------------------------------------
UPDATE core.publication_metrics pm
SET profile_author_id = sub.author_id
FROM (
    SELECT DISTINCT ON (p.publication_id)
           p.publication_id,
           a.author_id
    FROM core.publications p
    JOIN raw.scholar_publication rp
      ON rp.title = p.title
    JOIN raw.scholar_scrape_result rs
      ON rs.result_id = rp.result_id
    JOIN core.authors a
      ON a.scholar_id = rs.scholar_id
    ORDER BY p.publication_id, rs.scraped_at DESC, a.author_id
) sub
WHERE pm.publication_id = sub.publication_id
  AND pm.profile_author_id IS NULL;


-- ------------------------------------------------------------
-- 2. Cek sisa yang belum terisi (harusnya sedikit / kosong)
-- ------------------------------------------------------------
SELECT pm.metric_id, pm.publication_id, pm.profile_author_id
FROM core.publication_metrics pm
WHERE pm.profile_author_id IS NULL
ORDER BY pm.metric_id;