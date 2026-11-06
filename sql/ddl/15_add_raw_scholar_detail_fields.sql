-- Additive RAW-only migration. No historical backfill or type conversion.
-- Existing detail_venue/date/citations/link columns retain source values.
BEGIN;
ALTER TABLE raw.scholar_publication
    ADD COLUMN IF NOT EXISTS detail_journal TEXT,
    ADD COLUMN IF NOT EXISTS detail_conference TEXT,
    ADD COLUMN IF NOT EXISTS detail_book TEXT,
    ADD COLUMN IF NOT EXISTS detail_publication_type TEXT,
    ADD COLUMN IF NOT EXISTS detail_extraction_status TEXT,
    ADD COLUMN IF NOT EXISTS detail_cited_by JSONB,
    ADD COLUMN IF NOT EXISTS detail_related_articles JSONB,
    ADD COLUMN IF NOT EXISTS detail_all_versions JSONB;
COMMIT;
