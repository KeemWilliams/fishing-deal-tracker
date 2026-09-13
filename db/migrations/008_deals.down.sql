-- 008_deals.down.sql
ALTER TABLE IF EXISTS crawl_tasks DROP CONSTRAINT IF EXISTS crawl_tasks_deal_id_fkey;
DROP TRIGGER IF EXISTS trg_deals_updated_at ON deals;
DROP TABLE IF EXISTS deals;
