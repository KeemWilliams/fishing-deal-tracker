-- 006_queue_execution.down.sql
ALTER TABLE IF EXISTS discovery_hits DROP CONSTRAINT IF EXISTS discovery_hits_crawl_task_id_fkey;
DROP TABLE IF EXISTS retailer_health_hourly;
DROP TABLE IF EXISTS fetch_log;
DROP TABLE IF EXISTS tick_runs;
DROP TABLE IF EXISTS crawl_tasks;
