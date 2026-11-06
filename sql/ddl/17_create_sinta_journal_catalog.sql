-- Additive master catalogue for SINTA-affiliated journals (Universitas Telkom, affiliation 1093).
-- Manual application only; never applied automatically by the application.
BEGIN;
CREATE TABLE IF NOT EXISTS raw.sinta_journal_catalog (
    sinta_journal_id BIGINT PRIMARY KEY,
    affiliation_id INTEGER NOT NULL DEFAULT 1093,
    journal_name TEXT NOT NULL,
    cover_url TEXT,
    p_issn TEXT,
    e_issn TEXT,
    subject_area TEXT,
    sinta_rank TEXT,
    accreditation_status TEXT,
    garuda_status TEXT,
    garuda_journal_id BIGINT,
    impact NUMERIC(8,2),
    h5_index INTEGER,
    citations_5yr INTEGER,
    citations INTEGER,
    google_scholar_url TEXT,
    website_url TEXT,
    editor_url TEXT,
    profile_url TEXT NOT NULL,
    source_url TEXT NOT NULL,
    fetched_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ck_sinta_journal_catalog_rank CHECK (sinta_rank IS NULL OR sinta_rank IN ('S1','S2','S3','S4','S5','S6'))
);
CREATE INDEX IF NOT EXISTS ix_sinta_journal_catalog_affiliation ON raw.sinta_journal_catalog (affiliation_id);
COMMIT;
