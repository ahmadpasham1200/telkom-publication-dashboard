-- ============================================================
-- 03_create_raw_scrape_result.sql
-- ------------------------------------------------------------
-- Dua hal sekaligus:
--
--   1. Tabel raw untuk menyimpan envelope hasil scraping Google
--      Scholar APA ADANYA (JSONB), ditulis sebelum mapping ke core.
--      Tujuannya: kalau mapper atau loader berubah nanti dan
--      salah, data mentahnya sudah aman dan bisa diproses ulang.
--
--   2. Batasan UNIQUE untuk membuat UPSERT di core.publications
--      dan core.authors. Tanpa ini, scraper yang dijalankan dua
--      kali akan menduplikasi seluruh isi tabel.
--
-- CATATAN PENTING SOAL PENERAPAN
-- ------------------------------
-- File ini memakai fitur PostgreSQL 15+:
--     - UNIQUE NULLS NOT DISTINCT  (butuh PG 15)
--     - ON CONFLICT dengan indeks ekspresi (dipakai di loader)
-- Image postgres:16 di compose.yml sudah memenuhi.
-- Jalankan dengan server PostgreSQL 15 atau lebih baru.
--
-- File 01 dan 02 TIDAK diubah, karena keduanya sudah pernah
-- diterapkan di database yang ada. File ini sengaja terpisah
-- sebagai langkah migrasi.
-- ============================================================


-- ============================================================
-- 1. RAW: hasil scraping apa adanya
-- ============================================================
-- Satu baris = satu panggilan scrape_scholar(), sukses maupun
-- gagal. Run yang gagal (BLOCKED / TIMEOUT / NO_DATA /
-- PARSING_ERROR) TETAP disimpan di sini, karena justru record
-- itu yang paling berguna saat mau menelusuri kenapa scraping
-- bermasalah. Jangan filter berdasarkan status.

CREATE TABLE IF NOT EXISTS raw.scholar_scrape_result (
    -- Nama kolom PK memakai awalan entitas mengikuti konvensi
    -- project ini (author_id, publications_id, metric_id),
    -- bukan "id" polos.
    result_id     SERIAL PRIMARY KEY,

    -- Scholar ID yang di-scrape.
    scholar_id    VARCHAR(50) NOT NULL,

    -- Waktu penulisan baris ini. Berbeda dari started_at /
    -- finished_at di dalam envelope: ini kapan envelope itu
    -- DITARUH ke database, bukan kapan scraping berjalan.
    scraped_at    TIMESTAMP NOT NULL DEFAULT now(),

    -- run_id dari envelope (uuid4, satu per run). UNIQUE supaya
    -- run yang sama tidak bisa tercatat dua kali, sekaligus
    -- memudahkan menelusuri asal-usul data core.
    run_id        UUID UNIQUE,

    -- Salinan status dari envelope, supaya bisa difilter dengan
    -- SQL tanpa harus membuka JSONB dulu.
    status        VARCHAR(20),

    -- Seluruh envelope hasil scrape_scholar() apa adanya:
    -- run_id, source, started_at, finished_at, status,
    -- records_fetched, records_failed, error_message, data.
    -- data berisi scholar_id, name, h_index, rows, failures,
    -- domain, batches, elapsed. rows menyimpan field mentah
    -- Scholar (title, authors, venue, extra, citations, year)
    -- termasuk venue/extra yang sengaja tidak dipetakan ke core.
    raw_envelope  JSONB NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_raw_scholar_scrape_result_scholar_id
    ON raw.scholar_scrape_result (scholar_id);

-- Pencarian gagal / what's new per waktu.
CREATE INDEX IF NOT EXISTS ix_raw_scholar_scrape_result_scraped_at
    ON raw.scholar_scrape_result (scraped_at);


-- ============================================================
-- 2. NATURAL KEY UNTUK UPSERT
-- ============================================================
-- Dipakai loader sebagai berikut:
--     INSERT ... ON CONFLICT (...) DO UPDATE ... RETURNING ...
-- Kalau baris sudah ada -> UPDATE dan author_id /
-- publications_id lama dikembalikan. Kalau belum -> INSERT.
-- publications_id / author_id TIDAK berubah saat UPDATE,
-- jadi relasi di tabel lain tidak rusak.
--
-- CATATAN PENTING: Adding UNIQUE pada tabel yang SUAH berisi
-- duplikat akan GAGAL. Kalau diterapkan pada database yang sudah
-- terisi, bersihkan duplikatnya lebih dulu - error itu sengaja
-- TIDAK ditelan oleh blok DO di bawah, supaya masalah data tidak
-- ikut tersembunyi.


-- ------------------------------------------------------------
-- 2a. core.publications
-- ------------------------------------------------------------
-- Natural key: (title, publication_date).
--
-- NULLS NOT DISTINCT itu wajib, bukan opsional. Publication_date
-- sering NULL: mapper hanya bisa mengubah tahun yang bersih
-- ("2019") menjadi DATE, sedangkan tahun seperti "in press" atau
-- "2019-2020" jadi NULL. Tanpa NULLS NOT DISTINCT, PostgreSQL
-- menganggap setiap NULL berbeda satu sama lain, sehingga semua
-- publikasi tanpa tanggal akan lolos tanpa terdeteksi duplikat
-- sama sekali. Fitur ini butuh PostgreSQL 15+.
--
-- IDEMPOTEN: dibungkus DO ... EXCEPTION supaya file ini aman
-- dijalankan berkali-kali. PostgreSQL tidak punya
-- "ADD CONSTRAINT IF NOT EXISTS"; kalau perintah itu diulang,
-- yang muncul adalah duplicate_object (42710) - nama constraint
-- sudah dipakai. Baris WHEN itu hanya menelan error TERSEBUT
-- dan tidak yang lain, jadi:
--   - constraint sudah ada  -> dilewati diam-diam (idempoten)
--   - masih ada duplikat     -> unique_violation tetap naik,
--                              persis seperti pada ALTER TABLE polos.
DO $$
BEGIN
    ALTER TABLE core.publications
        ADD CONSTRAINT uq_publications_title_publication_date
        UNIQUE NULLS NOT DISTINCT (title, publication_date);
EXCEPTION
    WHEN duplicate_object THEN NULL;
END
$$;


-- ------------------------------------------------------------
-- 2b. core.authors
-- ------------------------------------------------------------
-- Natural key: nama yang sudah dinormalisasi, tidak membedakan
-- huruf besar-kecil. Ini sengaja memakai indeks EKSPRESI,
-- bukan UNIQUE(name) polos, karena:
--
--   - Kebijakan pencocokan yang disepakati adalah pencocokan
--     PERSIS tapi case-insensitive. UNIQUE(name) polos tidak
--     bisa menegakkan itu: "Budi Santoso" dan "budi santoso"
--     akan lolos sebagai dua baris berbeda.
--   - Nama yang masuk lewat loader sudah dinormalisasi lebih
--     dulu (spasi berlebih dirapatkan), jadi btrim() di sini
--     hanya jaring pengaman untuk tulisan yang masuk lewat
--     jalur lain.
--
-- Batas yang perlu diketahui:
--   - lower() di PostgreSQL tidak identik dengan .casefold() di
--     Python untuk beberapa huruf non-ASCII (mis. "ß": Python
--     casefold -> "ss", PostgreSQL lower -> tetap "ß"). Untuk
--     nama berbahasa Indonesia/ASCII tidak ada masalah, tapi
--     nama dengan umlaut atau ligatur bisa terpisah.
--   - Spasi di DALAM nama tidak diratakan oleh indeks ini.
--     Loader sudah menormalisasikannya sebelum menulis.

CREATE UNIQUE INDEX IF NOT EXISTS uq_authors_name_normalized
    ON core.authors (lower(btrim(name)));
