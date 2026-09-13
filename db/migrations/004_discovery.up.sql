-- 004_discovery.up.sql
-- Discovery pages (clearance/used/catalog grids we sweep), tracked_urls (product pages enrolled
-- into regular tracking), and discovery_hits (every grid row seen, per sweep).
--
-- NOTE: discovery_hits.crawl_task_id is a plain bigint here (crawl_tasks does not exist yet --
-- see 006_queue_execution.up.sql). The FK is added by 006 via ALTER TABLE once crawl_tasks exists.

CREATE TABLE IF NOT EXISTS discovery_pages (
  id bigserial PRIMARY KEY,
  retailer_id smallint NOT NULL REFERENCES retailers(id),
  page_type text NOT NULL CHECK (page_type IN ('CLEARANCE_LISTING', 'USED_LISTING', 'CATALOG_LISTING')),
  url text NOT NULL,
  category_hint text NOT NULL,
  interval_minutes int NOT NULL CHECK (interval_minutes >= 60),
  max_pages smallint NOT NULL CHECK (max_pages > 0),
  enabled boolean NOT NULL DEFAULT true,
  next_due_at timestamptz NOT NULL DEFAULT now(),
  last_swept_at timestamptz,
  last_item_count int,                     -- NULL = never swept; distinct from 0 (an empty sweep)
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (retailer_id, url)
);
CREATE INDEX IF NOT EXISTS discovery_pages_due_idx ON discovery_pages (next_due_at) WHERE enabled;

CREATE TABLE IF NOT EXISTS tracked_urls (
  id bigserial PRIMARY KEY,
  retailer_id smallint NOT NULL REFERENCES retailers(id),
  url text NOT NULL,
  category_hint text NOT NULL CHECK (category_hint IN
    ('rod', 'reel', 'line', 'soft_bait', 'hard_bait', 'terminal', 'combo', 'other')),
  source text NOT NULL CHECK (source IN
    ('SEED', 'DISCOVERY_CLEARANCE', 'DISCOVERY_USED', 'DISCOVERY_CATALOG', 'WATCH', 'MATCH_ANCHOR')),
  tier text NOT NULL DEFAULT 'BASELINE' CHECK (tier IN ('BASELINE', 'HOT')),
  interval_minutes int NOT NULL DEFAULT 1440 CHECK (interval_minutes > 0),   -- BASELINE 1440; HOT 120
  next_due_at timestamptz NOT NULL DEFAULT now(),
  expires_at timestamptz,                  -- discovery TTL; NULL for SEED/WATCH
  interest_score int NOT NULL DEFAULT 0,   -- watches, deals, views; used for cap eviction
  is_canary boolean NOT NULL DEFAULT false,
  robots_allowed boolean,
  consecutive_not_found smallint NOT NULL DEFAULT 0,
  enabled boolean NOT NULL DEFAULT true,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (retailer_id, url)
);
CREATE INDEX IF NOT EXISTS tracked_urls_due_idx ON tracked_urls (next_due_at) WHERE enabled;
CREATE INDEX IF NOT EXISTS tracked_urls_expiry_idx ON tracked_urls (expires_at) WHERE expires_at IS NOT NULL;
CREATE INDEX IF NOT EXISTS tracked_urls_retailer_tier_idx ON tracked_urls (retailer_id, tier) WHERE enabled;

CREATE TABLE IF NOT EXISTS discovery_hits (   -- every grid row seen, per sweep; 180-day retention
  id bigserial PRIMARY KEY,
  discovery_page_id bigint NOT NULL REFERENCES discovery_pages(id),
  crawl_task_id bigint NOT NULL,           -- FK to crawl_tasks(id) added in 006_queue_execution.up.sql
  tracked_url_id bigint REFERENCES tracked_urls(id),
  title_raw text NOT NULL,
  price_cents int,
  price_is_range boolean NOT NULL,
  claimed_reference_cents int,
  claimed_reference_kind text CHECK (claimed_reference_kind IN ('WAS', 'MSRP', 'LIST', 'COMPARE_AT')),
  implied_discount_pct numeric(5, 2),      -- from claimed reference only; never published directly
  condition_hint text,
  seen_at timestamptz NOT NULL,
  CHECK (price_cents IS NULL OR price_cents > 0),
  CHECK (claimed_reference_cents IS NULL OR claimed_reference_cents > 0)
);
CREATE INDEX IF NOT EXISTS discovery_hits_page_idx ON discovery_hits (discovery_page_id, seen_at DESC);
CREATE INDEX IF NOT EXISTS discovery_hits_tracked_url_idx ON discovery_hits (tracked_url_id);
CREATE INDEX IF NOT EXISTS discovery_hits_seen_at_brin ON discovery_hits USING brin (seen_at);

CREATE OR REPLACE TRIGGER trg_discovery_pages_updated_at
  BEFORE UPDATE ON discovery_pages FOR EACH ROW EXECUTE FUNCTION fpt_set_updated_at();
CREATE OR REPLACE TRIGGER trg_tracked_urls_updated_at
  BEFORE UPDATE ON tracked_urls FOR EACH ROW EXECUTE FUNCTION fpt_set_updated_at();
