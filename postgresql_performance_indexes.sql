-- PostgreSQL Performance Indexleri
-- Betonarme RAG Sistemi için performans optimizasyonu

-- 1. Documents tablosu için indexler
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_documents_source ON documents(source);
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_documents_country ON documents(country);
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_documents_doc_type ON documents(doc_type);
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_documents_lang ON documents(lang);
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_documents_imported_at ON documents(imported_at);

-- 2. Chunks tablosu için indexler
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_chunks_document_id ON chunks(document_id);
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_chunks_locale ON chunks(locale);
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_chunks_work_types ON chunks USING gin(work_types);
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_chunks_norm_codes ON chunks USING gin(norm_codes);
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_chunks_unit ON chunks(unit);

-- 3. Norms tablosu için indexler
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_norms_source ON norms(source);
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_norms_code ON norms(code);
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_norms_work_item_key ON norms(work_item_key);
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_norms_unit ON norms(unit);
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_norms_locale ON norms(locale);

-- 4. WBS tablosu için indexler
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_wbs_key ON wbs(key);
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_wbs_parent_key ON wbs(parent_key);
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_wbs_locale ON wbs(locale);

-- 5. Revit Quantities tablosu için indexler
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_revit_quantities_model_id ON revit_quantities(model_id);
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_revit_quantities_element_id ON revit_quantities(element_id);
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_revit_quantities_category ON revit_quantities(category);
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_revit_quantities_class_inf ON revit_quantities(class_inf);
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_revit_quantities_wbs_key ON revit_quantities(wbs_key);
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_revit_quantities_unit ON revit_quantities(unit);
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_revit_quantities_captured_at ON revit_quantities(captured_at);

-- 6. Site Observations tablosu için indexler
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_site_observations_work_date ON site_observations(work_date);
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_site_observations_shift ON site_observations(shift);
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_site_observations_crew_id ON site_observations(crew_id);
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_site_observations_wbs_key ON site_observations(wbs_key);
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_site_observations_unit ON site_observations(unit);

-- 7. Crews tablosu için indexler
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_crews_name ON crews(name);
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_crews_locale ON crews(locale);

-- 8. Mappings tablosu için indexler
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_mappings_source_field ON mappings(source_field);
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_mappings_pattern ON mappings(pattern);
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_mappings_mapped_key ON mappings(mapped_key);
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_mappings_confidence ON mappings(confidence);

-- 9. Retrieval Logs tablosu için indexler
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_retrieval_logs_ts ON retrieval_logs(ts);
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_retrieval_logs_query ON retrieval_logs(query);
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_retrieval_logs_accepted ON retrieval_logs(accepted);
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_retrieval_logs_reviewer ON retrieval_logs(reviewer);

-- 10. Composite indexler (çoklu sütun)
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_chunks_locale_work_types ON chunks(locale, work_types);
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_norms_source_code_locale ON norms(source, code, locale);
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_revit_quantities_wbs_unit ON revit_quantities(wbs_key, unit);
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_site_observations_date_wbs ON site_observations(work_date, wbs_key);

-- 11. Text search indexleri (Rusça ve Türkçe için)
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_chunks_text_ru ON chunks USING gin(to_tsvector('russian', text));
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_chunks_text_tr ON chunks USING gin(to_tsvector('turkish', text));
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_documents_content_ru ON documents USING gin(to_tsvector('russian', content));
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_documents_content_tr ON documents USING gin(to_tsvector('turkish', content));

-- 12. Partial indexler (koşullu indexler)
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_chunks_active ON chunks(id) WHERE work_types IS NOT NULL;
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_norms_active ON norms(id) WHERE norm_lh_per_u > 0;
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_revit_quantities_recent ON revit_quantities(id) WHERE captured_at > NOW() - INTERVAL '30 days';

-- 13. Foreign key indexleri (performans için)
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_chunks_fk_document ON chunks(document_id);
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_revit_quantities_fk_wbs ON revit_quantities(wbs_key);
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_site_observations_fk_wbs ON site_observations(wbs_key);

-- 14. Statistics güncelleme
ANALYZE documents;
ANALYZE chunks;
ANALYZE norms;
ANALYZE wbs;
ANALYZE revit_quantities;
ANALYZE site_observations;
ANALYZE crews;
ANALYZE mappings;
ANALYZE retrieval_logs;

-- 15. Index kullanım istatistikleri için view
CREATE OR REPLACE VIEW index_usage_stats AS
SELECT 
    schemaname,
    tablename,
    indexname,
    idx_tup_read,
    idx_tup_fetch
FROM pg_stat_user_indexes
ORDER BY idx_tup_read DESC;

-- 16. Tablo boyutları için view
CREATE OR REPLACE VIEW table_sizes AS
SELECT 
    schemaname,
    tablename,
    pg_size_pretty(pg_total_relation_size(schemaname||'.'||tablename)) as size
FROM pg_tables
WHERE schemaname = 'public'
ORDER BY pg_total_relation_size(schemaname||'.'||tablename) DESC;

