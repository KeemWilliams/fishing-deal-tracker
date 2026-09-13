-- 010_subscribers_watches.down.sql
DROP TRIGGER IF EXISTS trg_watches_updated_at ON watches;
DROP TRIGGER IF EXISTS trg_subscribers_updated_at ON subscribers;
DROP TABLE IF EXISTS api_tokens_used;
DROP TABLE IF EXISTS alert_items;
DROP TABLE IF EXISTS alert_deliveries;
DROP TABLE IF EXISTS watches;
DROP TABLE IF EXISTS subscribers;
