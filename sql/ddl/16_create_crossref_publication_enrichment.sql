-- Additive article-level Crossref cache. Manual application only on existing DB.
-- No metadata is written to Scholar source snapshots or journal verifications.
BEGIN;
CREATE TABLE IF NOT EXISTS raw.crossref_publication_enrichment (
    enrichment_id BIGSERIAL PRIMARY KEY,
    raw_publication_id INTEGER NOT NULL REFERENCES raw.scholar_publication(raw_publication_id),
    status TEXT NOT NULL,
    lookup_strategy TEXT NOT NULL,
    doi TEXT,
    matched_title JSONB NOT NULL,
    matched_authors JSONB NOT NULL,
    publication_year INTEGER,
    publication_date JSONB,
    publication_date_precision TEXT,
    container_title JSONB NOT NULL,
    issns JSONB NOT NULL,
    publisher TEXT,
    publication_type TEXT,
    author_evidence JSONB NOT NULL,
    year_evidence JSONB NOT NULL,
    container_evidence JSONB NOT NULL,
    title_similarity DOUBLE PRECISION,
    candidate_count INTEGER NOT NULL,
    selected_evidence JSONB NOT NULL,
    selected_conflicts JSONB NOT NULL,
    metadata JSONB NOT NULL,
    retrieved_at TIMESTAMPTZ NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    last_error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_crossref_publication UNIQUE (raw_publication_id),
    CONSTRAINT ck_crossref_status CHECK (status IN ('matched','ambiguous','not_found','error')),
    CONSTRAINT ck_crossref_lookup CHECK (lookup_strategy IN ('doi_lookup','bibliographic_search')),
    CONSTRAINT ck_crossref_expiry CHECK (expires_at > retrieved_at),
    CONSTRAINT ck_crossref_matched CHECK (status <> 'matched' OR (doi IS NOT NULL AND candidate_count > 0))
);
CREATE INDEX IF NOT EXISTS ix_crossref_enrichment_expiry ON raw.crossref_publication_enrichment (expires_at);
CREATE INDEX IF NOT EXISTS ix_crossref_enrichment_doi ON raw.crossref_publication_enrichment (doi) WHERE doi IS NOT NULL;
COMMIT;
