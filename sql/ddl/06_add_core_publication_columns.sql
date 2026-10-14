-- ============================================================
-- 06_add_core_publication_columns.sql
-- ------------------------------------------------------------
-- Migrasi kunci natural publikasi dari (title, publication_date)
-- ke (title, publication_year), plus kolom baru issue & link,
-- plus kolom profile_author_id di core.publication_metrics.
--
-- URUTAN LANGKAH (wajib berurutan dalam satu file):
--   1. Guard: kalau kolom publication_year BELUM ada, tambahkan
--      kolom baru, backfill dari publication_date, drop constraint
--      lama, dan BARU SETELAH itu nolkan tanggal sintetis 1 Januari.
--      Di LUAR guard, migrasi TIDAK menyentuh data apa pun -
--      jadi tanggal 1 Januari ASLI yang masuk belakangan tidak
--      akan terhapus kalau file ini dijalankan ulang.
--   2. Cek pelanggaran kunci baru (title, publication_year)
--      SEBELUM menambah constraint. Kalau ada duplikat, berhenti
--      dengan pesan jelas (exception sengaja tidak ditelan).
--   3. Drop constraint lama uq_publications_title_publication_date
--      (didefinisikan di 03_create_raw_scrape_result.sql) - di sini
--      hanya jaring pengaman; drop utama terjadi di dalam guard
--      (langkah 1c) karena constraint lama HARUS hilang sebelum
--      tanggal sintetis di-NULL-kan (lihat komentar di 1c).
--      Wajib di-drop: publication_date kini sering NULL, dan
--      NULLS NOT DISTINCT pada (title, publication_date) akan
--      memblokir baris sah (title sama, tahun beda, tanggal NULL).
--   4. Tambah constraint baru
--      uq_publications_title_publication_year
--      UNIQUE NULLS NOT DISTINCT (title, publication_year).
--
-- DEPLOY ORDER: file ini (dan 04, 05) dijalankan di database
-- SEBELUM kode loader baru dipakai - loader baru memakai
-- ON CONFLICT (title, publication_year) yang butuh constraint
-- ini sudah ada.
--
-- IDEMPOTEN: aman dijalankan berkali-kali.
-- ============================================================


-- ------------------------------------------------------------
-- 1. Kolom baru + backfill + pembersihan tanggal sintetis
-- ------------------------------------------------------------
-- Semua langkah di bawah hanya berjalan SEKALI, saat kolom
-- publication_year pertama kali ditambahkan. Kalau kolom sudah
-- ada, blok ini dilewati total (tidak ada data yang disentuh).

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = 'core'
          AND table_name = 'publications'
          AND column_name = 'publication_year'
    ) THEN
        -- 1a. Kolom baru.
        ALTER TABLE core.publications
            ADD COLUMN publication_year INTEGER;
        ALTER TABLE core.publications
            ADD COLUMN issue VARCHAR(50);
        ALTER TABLE core.publications
            ADD COLUMN link TEXT;

        -- 1b. Backfill publication_year dari publication_date.
        --     Baris tanpa tanggal tetap NULL (tahunnya tidak
        --     diketahui dari data lama).
        UPDATE core.publications
        SET publication_year = EXTRACT(YEAR FROM publication_date)::INTEGER
        WHERE publication_date IS NOT NULL;

        -- 1c. Drop constraint lama SEBELUM menolkan tanggal sintetis.
        --     Constraint lama UNIQUE NULLS NOT DISTINCT (title,
        --     publication_date) menolak baris yang baru di-NULL-kan
        --     kalau ada publikasi lain dengan title sama dan
        --     publication_date NULL (terbukti gagal di data nyata:
        --     "duplicate key value violates unique constraint
        --     uq_publications_title_publication_date"). Constraint
        --     ini diganti di langkah 4, jadi di-drop lebih awal
        --     tidak mengubah hasil akhir.
        ALTER TABLE core.publications
            DROP CONSTRAINT IF EXISTS uq_publications_title_publication_date;

        -- 1d. Nolkan tanggal sintetis: semua tanggal lama dihasilkan
        --     mapper sebagai 1 Januari tahun terbit. Tanggal yang
        --     PERSIS 1 Januari dari tahun yang sama dianggap
        --     sintetis dan dikosongkan - publikasi tanpa tanggal
        --     lengkap kini NULL, bukan 1 Januari.
        UPDATE core.publications
        SET publication_date = NULL
        WHERE publication_date = make_date(publication_year, 1, 1);
    END IF;
END
$$;


-- ------------------------------------------------------------
-- 2. Cek pelanggaran kunci baru (title, publication_year)
-- ------------------------------------------------------------
-- GROUP BY memperlakukan NULL sebagai satu kelompok, jadi duplikat
-- dengan publication_year NULL (yang akan ditolak NULLS NOT
-- DISTINCT) ikut terdeteksi. Kalau ada duplikat, exception naik
-- dan file berhenti - constraint tidak ditambahkan.

DO $$
DECLARE
    v_jumlah_duplikat INTEGER;
BEGIN
    SELECT count(*)
    INTO v_jumlah_duplikat
    FROM (
        SELECT title, publication_year
        FROM core.publications
        GROUP BY title, publication_year
        HAVING count(*) > 1
    ) duplikat;

    IF v_jumlah_duplikat > 0 THEN
        RAISE EXCEPTION
            'Terdapat % kelompok duplikat (title, publication_year) di core.publications. '
            'Bersihkan duplikatnya lebih dulu sebelum menambah constraint unik baru.',
            v_jumlah_duplikat;
    END IF;
END
$$;


-- ------------------------------------------------------------
-- 3. Drop constraint lama
-- ------------------------------------------------------------
ALTER TABLE core.publications
    DROP CONSTRAINT IF EXISTS uq_publications_title_publication_date;


-- ------------------------------------------------------------
-- 4. Constraint baru (kunci natural baru)
-- ------------------------------------------------------------
-- NULLS NOT DISTINCT wajib: publication_year bisa NULL untuk
-- publikasi lama tanpa tanggal. Tanpa fitur ini, semua baris
-- dengan tahun NULL lolos dari deteksi duplikat.
--
-- IDEMPOTEN: dibungkus DO ... EXCEPTION. Catatan: ADD CONSTRAINT
-- UNIQUE membuat indeks dengan nama yang sama; kalau diulang,
-- PostgreSQL menaikkan duplicate_table (42P07), BUKAN
-- duplicate_object (42710) - keduanya ditelan di sini.

DO $$
BEGIN
    ALTER TABLE core.publications
        ADD CONSTRAINT uq_publications_title_publication_year
        UNIQUE NULLS NOT DISTINCT (title, publication_year);
EXCEPTION
    WHEN duplicate_object OR duplicate_table THEN NULL;
END
$$;


-- ------------------------------------------------------------
-- 5. profile_author_id di core.publication_metrics
-- ------------------------------------------------------------
-- Pemilik profil (author yang scholar_id-nya = profil yang
-- di-scrape) pada run yang menghasilkan baris metrik ini.
-- Tabel publication_metrics tetap insert-only; kolom ini hanya
-- penanda asal data, bukan kunci.
--
-- Backfill baris LAMA tidak dilakukan di sini - itu query manual
-- terpisah (sql/maintenance/07_backfill_publication_metrics_profile_author.sql)
-- yang diuji di publication_test dulu.

ALTER TABLE core.publication_metrics
    ADD COLUMN IF NOT EXISTS profile_author_id INTEGER
    REFERENCES core.authors(author_id);