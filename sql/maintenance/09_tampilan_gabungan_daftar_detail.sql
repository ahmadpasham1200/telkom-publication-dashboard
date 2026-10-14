-- ============================================================
-- sql/maintenance/09_tampilan_gabungan_daftar_detail.sql
-- ------------------------------------------------------------
-- QUERY MANUAL - BUKAN bagian dari migrasi otomatis.
-- INI HANYA SELECT (CTE + LATERAL): tidak ada CREATE/INSERT/
-- UPDATE/DELETE/DDL apa pun di file ini, tidak mengubah tabel
-- RAW maupun data historis, dan TIDAK dijalankan otomatis oleh
-- pipeline. Tidak ada objek database (view/index/tabel) yang
-- dibuat.
--
-- PARAMETER: :scholar_id (VARCHAR) HARUS di-bind oleh klien yang
-- benar-benar mendukung named bind parameter - jalur yang didukung
-- dan dipakai repo ini adalah SQLAlchemy text() dengan bindparams
-- (pola yang sama dengan sa.text(...).bindparams(...) di
-- src/pipeline/run_batch.py). Klien TANPA named bind (mis. psql
-- polos) menjalankan :scholar_id sebagai interpolasi variabel
-- klien, BUKAN bind - jangan jalankan skrip ini lewat jalur itu
-- tanpa mekanisme parameterisasi yang setara. JANGAN
-- string-interpolate nilai scholar_id ke dalam teks query.
--
-- Tujuan: memperoleh data publikasi TERKINI satu profil dengan
-- menggabungkan metadata halaman daftar dari run TERBARU dan
-- metadata halaman detail yang DIWARISI dari run terdahulu
-- (skenario run --tanpa-detail / run yang berhenti karena HTTP
-- 429; lihat audit P32-P33).
--
-- ATURAN PILIH RUN TERBARU:
--   1. Hanya run yang MEMILIKI baris di raw.scholar_publication
--      (EXISTS). Run fatal (BLOCKED/TIMEOUT/NO_DATA - envelope
--      data NULL, tanpa baris terstruktur) otomatis dilewati
--      sehingga tidak pernah mengosongkan tampilan.
--   2. Deterministik: ORDER BY scraped_at DESC, result_id DESC
--      (result_id SERIAL = urutan insert, sebagai tiebreak).
--
-- ATURAN PILIH DONOR DETAIL:
--   1. Pencocokan HANYA kecocokan persis (scholar_id +
--      citation_for_view yang diekstrak dari detail_url).
--      TIDAK PERNAH mencocokkan berdasarkan judul, apalagi judul
--      mirip. Tanpa citkey (detail_url NULL/format berubah) ->
--      tanpa warisan (fail-safe), bukan merge buta.
--   2. Penanda keberhasilan parse donor: detail_title IS NOT
--      NULL (parse_detail_html selalu mengisi detail_title
--      bila halaman detail berhasil diparse).
--   3. Satu donor per baris daftar: LATERAL + LIMIT 1, urut
--      scraped_at DESC, result_id DESC -> maksimal SATU baris
--      keluaran per publikasi terbaru, tanpa penghitungan ganda.
--
-- Kolom diagnosis (percobaan pada RUN TERBARU) TERPISAH dari
-- penanda ada_detail_warisan (ketersediaan detail lintas run):
--   tanpa_url      - baris tanpa detail_url
--   belum_dicoba   - run terbaru tidak mencoba fase detail
--                    (khas --tanpa-detail, atau baris sesudah
--                    titik henti HTTP 429 pasca-P29)
--   gagal:<reason> - run terbaru mencoba dan gagal (entri
--                    failures di envelope)
--   berhasil       - run terbaru sendiri mengambil detail
--
-- KETERBATASAN (konsisten dengan P32-P33):
--   - Kode HTTP per baris (429 vs lainnya) TIDAK disimpan di
--     mana pun; diagnosis gagal hanya menampilkan reason entri
--     failures, bukan status HTTP.
--   - Publikasi yang tidak lagi muncul di run terbaru TIDAK
--     tampil di sini (tampilan = snapshot terkini); baris
--     lamanya tetap ada di RAW.
--   - Kueri ini belum dieksekusi terhadap PostgreSQL sungguhan;
--     validasi sintaks/logika menyusul (P33 butir D).
--
-- UJI DULU di publication_test sebelum dipakai di database utama.
-- ============================================================

WITH param AS (
    SELECT CAST(:scholar_id AS varchar) AS scholar_id
),
run_terbaru AS (
    -- Run terbaru yang memiliki baris publikasi (lihat aturan 1
    -- pada kepala file). Run fatal tanpa baris dilewati tanpa
    -- memfilter string status.
    SELECT r.result_id
    FROM raw.scholar_scrape_result r
    JOIN param p USING (scholar_id)
    WHERE EXISTS (
        SELECT 1
        FROM raw.scholar_publication pr
        WHERE pr.result_id = r.result_id
    )
    ORDER BY r.scraped_at DESC, r.result_id DESC
    LIMIT 1
),
daftar_terkini AS (
    -- Baris daftar dari run terbaru + citkey hasil ekstraksi.
    -- regexp_match strict: detail_url NULL -> citkey NULL.
    SELECT pr.*,
           (regexp_match(pr.detail_url,
                         '[?&]citation_for_view=([^&]+)'))[1] AS citkey
    FROM raw.scholar_publication pr
    WHERE pr.result_id = (SELECT result_id FROM run_terbaru)
),
penyedia_detail AS (
    -- Kandidat pemberi detail lintas run: baris yang parse
    -- detail-nya berhasil (detail_title IS NOT NULL).
    SELECT pr.scholar_id,
           (regexp_match(pr.detail_url,
                         '[?&]citation_for_view=([^&]+)'))[1] AS citkey,
           pr.detail_title, pr.detail_authors, pr.detail_date,
           pr.detail_venue, pr.detail_volume, pr.detail_issue,
           pr.detail_pages, pr.detail_publisher,
           pr.detail_description, pr.detail_citations,
           pr.detail_link, pr.detail_pdf_link, pr.detail_doi,
           r2.result_id, r2.scraped_at
    FROM raw.scholar_publication pr
    JOIN raw.scholar_scrape_result r2 USING (result_id)
    JOIN param p ON p.scholar_id = pr.scholar_id
    WHERE pr.detail_title IS NOT NULL
),
envelope_terbaru AS (
    -- Envelope run terbaru (untuk membaca failures). Bisa berupa
    -- data JSON null pada run fatal, tapi run fatal tidak pernah
    -- menjadi run terbaru; jsonb_array_elements tetap aman
    -- (strict -> 0 baris) bila data/failures NULL.
    SELECT r.raw_envelope
    FROM raw.scholar_scrape_result r
    WHERE r.result_id = (SELECT result_id FROM run_terbaru)
)
SELECT
    t.urutan,
    -- ===== metadata halaman daftar: dari RUN TERBARU =====
    t.title, t.authors, t.venue, t.extra, t.citations, t.year,
    t.publication_year, t.detail_url,
    t.profile_volume, t.profile_issue, t.profile_pages,
    t.status AS status_kelengkapan_run_terbaru,
    -- ===== metadata halaman detail: DIWARISI (NULL jika
    -- tidak ada donor yang cocok persis) =====
    d.detail_title, d.detail_authors, d.detail_date, d.detail_venue,
    d.detail_volume, d.detail_issue, d.detail_pages, d.detail_publisher,
    d.detail_description, d.detail_citations, d.detail_link,
    d.detail_pdf_link, d.detail_doi,
    -- penanda ketersediaan detail warisan (bukan status percobaan)
    (d.detail_title IS NOT NULL) AS ada_detail_warisan,
    d.result_id  AS detail_dari_result_id,
    d.scraped_at AS detail_dari_scraped_at,
    -- ===== diagnosis percobaan pada RUN TERBARU (terpisah dari
    -- penanda warisan di atas) =====
    CASE
        WHEN t.detail_url  IS NULL        THEN 'tanpa_url'
        WHEN g.reason      IS NOT NULL    THEN 'gagal:' || g.reason
        WHEN t.detail_title IS NOT NULL   THEN 'berhasil'
        ELSE 'belum_dicoba'
    END AS diagnosis_run_terbaru
FROM daftar_terkini t
-- Donor detail: equality persis pada scholar_id + citkey;
-- tanpa citkey -> tanpa warisan. LIMIT 1 + tiebreak -> deterministik
-- dan maksimal satu donor per baris daftar.
LEFT JOIN LATERAL (
    SELECT pd.*
    FROM penyedia_detail pd
    WHERE pd.scholar_id = t.scholar_id
      AND t.citkey IS NOT NULL
      AND pd.citkey = t.citkey
    ORDER BY pd.scraped_at DESC, pd.result_id DESC
    LIMIT 1
) d ON TRUE
-- Kegagalan fase detail pada run terbaru, dari failures envelope.
-- Guard sebelum cast: hanya index berupa digits yang di-cast ke
-- integer (pertahanan terhadap envelope luar/rusak). Guard tipe
-- jsonb: jsonb_array_elements HANYA menerima array; failures yang
-- hilang / JSON null / bukan array -> '[]'::jsonb (0 baris), tanpa
-- error "cannot extract elements from a scalar".
LEFT JOIN LATERAL (
    SELECT f.value->>'reason' AS reason
    FROM envelope_terbaru e
    CROSS JOIN LATERAL jsonb_array_elements(
        CASE WHEN jsonb_typeof(e.raw_envelope->'data'->'failures') = 'array'
             THEN e.raw_envelope->'data'->'failures'
             ELSE '[]'::jsonb END) AS f(value)
    WHERE f.value->>'reason'
          IN ('detail_fetch_error', 'detail_parse_error', 'detail_blocked')
      AND f.value->>'index' ~ '^[0-9]+$'
      AND (f.value->>'index')::int = t.urutan
    LIMIT 1
) g ON TRUE
ORDER BY t.urutan;
