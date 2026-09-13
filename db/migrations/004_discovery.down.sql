-- 004_discovery.down.sql
DROP TRIGGER IF EXISTS trg_tracked_urls_updated_at ON tracked_urls;
DROP TRIGGER IF EXISTS trg_discovery_pages_updated_at ON discovery_pages;
DROP TABLE IF EXISTS discovery_hits;
DROP TABLE IF EXISTS tracked_urls;
DROP TABLE IF EXISTS discovery_pages;
