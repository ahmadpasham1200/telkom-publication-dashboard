-- Additive article-level manual Scopus verification (Scopus Document Search evidence).
-- Separate from the automatic Excel-master results in raw.scopus_publication_verification
-- (migration 19); the two tables are never written by each other's code path.
-- Matching uses manually transcribed document-check evidence; DOI and EID are optional.
-- Never applied automatically from Python; review and apply this file separately with approval.
BEGIN;
CREATE TABLE IF NOT EXISTS raw.scopus_manual_verification (
    verification_id BIGSERIAL PRIMARY KEY,
    raw_publication_id INTEGER NOT NULL REFERENCES raw.scholar_publication(raw_publication_id) ON DELETE CASCADE,
    verification_source VARCHAR(64) NOT NULL,
    title TEXT,
    first_author TEXT,
    source_title TEXT,
    publication_year INTEGER,
    doi TEXT,
    eid TEXT,
    verified BOOLEAN NOT NULL,
    scopus_status VARCHAR(32) NOT NULL,
    matched_identity JSONB,
    reason TEXT,
    metadata JSONB,
    checked_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_scopus_manual_verification_raw UNIQUE (raw_publication_id),
    CONSTRAINT ck_scopus_manual_verification_status CHECK (scopus_status IN ('indexed','indicated','needs_verification','not_found'))
);
CREATE INDEX IF NOT EXISTS ix_scopus_manual_verification_status ON raw.scopus_manual_verification (scopus_status);
COMMIT;
