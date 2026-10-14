-- ============================================================
-- sql/maintenance/08_kandidat_author_duplikat.sql
-- ------------------------------------------------------------
-- QUERY MANUAL - BUKAN bagian dari migrasi otomatis.
--
-- Mencari PASANGAN author yang kemungkinan besar orang yang sama
-- (kandidat duplikat) untuk ditinjau manual. INI HANYA SELECT:
-- tidak ada UPDATE/DELETE di file ini. Keputusan menggabungkan
-- author tetap di tangan user.
--
-- Latar belakang: kebijakan 3j menerjemahkan nama bentuk pendek
-- pemilik ("TAD Kuntjoro") dan varian potong-per-kata halaman
-- detail ("Tri Agus D Kuntjoro") ke author pemilik, tapi author
-- tiruan yang TERLANJUR dibuat oleh run sebelum fix (mis. id 3315
-- "Tri Agus D Kuntjoro") tidak dihapus - hanya tidak dipakai lagi.
-- Query ini menemukan pasangan seperti itu supaya bisa ditinjau.
--
-- Kriteria (semua casefold, spasi dirapikan):
--   1. Nama berbeda (bukan baris yang sama).
--   2. Kata TERAKHIR sama persis (nama belakang sama).
--   3. Kata PERTAMA salah satu nama adalah AWALAN dari salah satu
--      kata nama lainnya (mis. "D" awalan dari "Djoko", atau
--      "Tri" sama dengan "Tri").
--   4. Paling tidak satu author bukan pemilik profil (scholar_id
--      NULL). Dua pemilik profil berbeda dengan nama mirip BUKAN
--      duplikat, jadi tidak ikut.
--   5. Simetris: a1.author_id < a2.author_id, jadi tiap pasangan
--      muncul sekali.
--
-- KETERBATASAN: bentuk pendek yang memotong kata ("TAD Kuntjoro")
-- TIDAK tertangkap di sini (kata pertama "TAD" bukan awalan kata
-- mana pun dari "Tri Agus Djoko Kuntjoro") - itu memang bukan
-- duplikat, melainkan singkatan yang sudah ditangani
-- _bentuk_pendek di src/loading/postgres.py.
--
-- UJI DULU di publication_test sebelum dipakai di database utama.
-- ============================================================

SELECT a1.author_id   AS author_id_1,
       a1.name        AS nama_1,
       a1.scholar_id  AS scholar_id_1,
       a2.author_id   AS author_id_2,
       a2.name        AS nama_2,
       a2.scholar_id  AS scholar_id_2
FROM core.authors a1
JOIN core.authors a2 ON a1.author_id < a2.author_id
WHERE lower(btrim(a1.name)) <> lower(btrim(a2.name))
  AND split_part(lower(btrim(a1.name)), ' ', -1)
      = split_part(lower(btrim(a2.name)), ' ', -1)
  AND (a1.scholar_id IS NULL OR a2.scholar_id IS NULL)
  AND (
      -- Kata pertama a1 adalah awalan (casefold) dari salah satu
      -- kata a2. strpos(...) = 1 dipakai, bukan LIKE, supaya
      -- karakter wildcard %/_ di dalam nama tidak mengubah makna.
      EXISTS (
          SELECT 1
          FROM unnest(string_to_array(lower(btrim(a2.name)), ' ')) AS t2(kata)
          WHERE strpos(t2.kata, split_part(lower(btrim(a1.name)), ' ', 1)) = 1
      )
      OR
      -- Kata pertama a2 adalah awalan (casefold) dari salah satu
      -- kata a1.
      EXISTS (
          SELECT 1
          FROM unnest(string_to_array(lower(btrim(a1.name)), ' ')) AS t1(kata)
          WHERE strpos(t1.kata, split_part(lower(btrim(a2.name)), ' ', 1)) = 1
      )
  )
ORDER BY a1.author_id, a2.author_id;