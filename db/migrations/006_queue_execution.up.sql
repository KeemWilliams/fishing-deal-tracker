-- 006_queue_execution.up.sql
-- The crawl queue, tick run log, per-fetch log, and hourly health rollup.

CREATE TABLE IF NOT EXISTS crawl_tasks (
  id bigserial PRIMARY KEY,
  retailer_id smallint NOT NULL REFERENCES retailers(id),
  kind text NOT NULL CHECK (kind IN ('CONFIRM', 'HOT', 'DISCOVERY', 'ENROLL', 'BASELINE', 'CANARY')),
  priority smallint NOT NULL,   -- CANARY 5, CONFIRM 10, HOT 20, DISCOVERY 30, ENROLL 40, BASELINE 50
  page_type text NOT NULL CHECK (page_type IN
    ('PRODUCT', 'CLEARANCE_LISTING', 'USED_LISTING', 'CATALOG_LISTING', 'API_BATCH')),
  url text NOT NULL,
  params jsonb NOT NULL DEFAULT '{}'::jsonb,   -- never credentials (section 3.2)
  tracked_url_id bigint REFERENCES tracked_urls(id),
  discovery_page_id bigint REFERENCES discovery_pages(id),
  deal_id bigint,   -- CONFIRM tasks: the candidate being confirmed; FK added in 008_deals.up.sql
  dedupe_key text NOT NULL,   -- e.g. 'BASELINE:tracked_url:123'
  not_before timestamptz NOT NULL DEFAULT now(),
  deadline timestamptz,   -- CONFIRM: candidate expires if not fetched by then
  status text NOT NULL DEFAULT 'QUEUED' CHECK (status IN
    ('QUEUED', 'LEASED', 'DONE', 'FAILED', 'EXPIRED', 'SKIPPED_ROBOTS', 'SKIPPED_BUDGET')),
  outcome text CHECK (outcome IS NULL OR outcome IN
    ('OK', 'BLOCKED', 'EMPTY', 'STRUCTURE_CHANGED', 'NOT_FOUND', 'QUOTA_EXHAUSTED')),
  attempts smallint NOT NULL DEFAULT 0,
  leased_by_tick bigint,
  lease_expires_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  finished_at timestamptz
);
CREATE UNIQUE INDEX IF NOT EXISTS crawl_tasks_dedupe_open ON crawl_tasks (dedupe_key)
  WHERE status IN ('QUEUED', 'LEASED');
CREATE INDEX IF NOT EXISTS crawl_tasks_drain_idx ON crawl_tasks (retailer_id, priority, not_before)
  WHERE status = 'QUEUED';
CREATE INDEX IF NOT EXISTS crawl_tasks_tracked_url_idx ON crawl_tasks (tracked_url_id);
CREATE INDEX IF NOT EXISTS crawl_tasks_discovery_page_idx ON crawl_tasks (discovery_page_id);
CREATE INDEX IF NOT EXISTS crawl_tasks_deal_idx ON crawl_tasks (deal_id) WHERE deal_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS crawl_tasks_leased_idx ON crawl_tasks (leased_by_tick) WHERE status = 'LEASED';

CREATE TABLE IF NOT EXISTS tick_runs (
  id bigserial PRIMARY KEY,
  started_at timestamptz NOT NULL DEFAULT now(),
  finished_at timestamptz,
  step_status jsonb NOT NULL DEFAULT '{}'::jsonb   -- {"plan":"OK","work":"OK","deals":"OK","notify":"OK","export":"GATED"}
);
CREATE INDEX IF NOT EXISTS tick_runs_started_idx ON tick_runs (started_at DESC);

CREATE TABLE IF NOT EXISTS fetch_log (   -- 14-day retention
  id bigserial PRIMARY KEY,
  crawl_task_id bigint NOT NULL REFERENCES crawl_tasks(id),
  retailer_id smallint NOT NULL REFERENCES retailers(id),
  tick_run_id bigint NOT NULL REFERENCES tick_runs(id),
  attempt smallint NOT NULL,
  http_status int,
  transport_error text,
  outcome text NOT NULL CHECK (outcome IN
    ('OK', 'BLOCKED', 'EMPTY', 'STRUCTURE_CHANGED', 'NOT_FOUND', 'QUOTA_EXHAUSTED')),
  block_signature text,
  body_bytes int,
  snapshot_ref text,
  elapsed_ms int,
  adapter_version text NOT NULL,
  egress_mode text NOT NULL CHECK (egress_mode IN ('DIRECT', 'PROXY', 'API')),
  api_units_used int,
  fetched_at timestamptz NOT NULL
);
CREATE INDEX IF NOT EXISTS fetch_log_retailer_time_idx ON fetch_log (retailer_id, fetched_at DESC);
CREATE INDEX IF NOT EXISTS fetch_log_crawl_task_idx ON fetch_log (crawl_task_id);
CREATE INDEX IF NOT EXISTS fetch_log_tick_run_idx ON fetch_log (tick_run_id);

CREATE TABLE IF NOT EXISTS retailer_health_hourly (   -- rollup written each tick; source for status/alerts
  retailer_id smallint NOT NULL REFERENCES retailers(id),
  hour_utc timestamptz NOT NULL,
  fetched int NOT NULL DEFAULT 0,
  ok int NOT NULL DEFAULT 0,
  blocked int NOT NULL DEFAULT 0,
  empty int NOT NULL DEFAULT 0,
  structure_changed int NOT NULL DEFAULT 0,
  not_found int NOT NULL DEFAULT 0,
  http_429 int NOT NULL DEFAULT 0,
  transport_errors int NOT NULL DEFAULT 0,
  obs_ok int NOT NULL DEFAULT 0,
  obs_suspect int NOT NULL DEFAULT 0,
  obs_rejected int NOT NULL DEFAULT 0,
  canaries_ok smallint NOT NULL DEFAULT 0,
  canaries_total smallint NOT NULL DEFAULT 0,
  requests_used int NOT NULL DEFAULT 0,
  api_units_used int,
  PRIMARY KEY (retailer_id, hour_utc)
);
CREATE INDEX IF NOT EXISTS retailer_health_hourly_hour_idx ON retailer_health_hourly (hour_utc DESC);

-- Deferred FK: discovery_hits.crawl_task_id was created as a plain bigint in 004_discovery
-- because crawl_tasks did not exist yet. Add the constraint now that it does.
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint WHERE conname = 'discovery_hits_crawl_task_id_fkey'
  ) THEN
    ALTER TABLE discovery_hits
      ADD CONSTRAINT discovery_hits_crawl_task_id_fkey
      FOREIGN KEY (crawl_task_id) REFERENCES crawl_tasks(id);
  END IF;
END;
$$;
CREATE INDEX IF NOT EXISTS discovery_hits_crawl_task_idx ON discovery_hits (crawl_task_id);
