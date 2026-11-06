-- Additive article-level Scopus master verification. Manual application only on existing DB.
-- Matching uses the local Excel master (data/scopus/ext_list_Sep_2026.xlsx); no HTTP anywhere.
-- Never applied automatically from Python; review and apply this file separately with approval.
BEGIN;
CREATE TABLE IF NOT EXISTS raw.scopus_publication_verification (
    verification_id BIGSERIAL PRIMARY KEY,
    raw_publication_id INTEGER NOT NULL REFERENCES raw.scholar_publication(raw_publication_id) ON DELETE CASCADE,
    scopus_status VARCHAR(32) NOT NULL,
    source_record_id BIGINT,
    source_title TEXT,
    source_type TEXT,
    issn TEXT,
    eissn TEXT,
    isbn TEXT,
    coverage TEXT,
    active_status TEXT,
    discontinued BOOLEAN,
    publisher TEXT,
    asjc TEXT,
    matched_identity JSONB,
    match_confidence VARCHAR(16),
    reason TEXT,
    source_sheet TEXT,
    metadata JSONB,
    checked_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_scopus_pub_verification_raw UNIQUE (raw_publication_id),
    CONSTRAINT ck_scopus_pub_verification_status CHECK (scopus_status IN ('indexed','indicated','needs_verification','not_found'))
);
CREATE INDEX IF NOT EXISTS ix_scopus_pub_verification_status ON raw.scopus_publication_verification (scopus_status);
COMMIT;
