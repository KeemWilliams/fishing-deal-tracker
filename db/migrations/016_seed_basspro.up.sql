-- 016_seed_basspro.up.sql
-- Seed the Bass Pro Shops retailer added in this task, plus one discovery
-- page for its fishing-rods category. Idempotent via ON CONFLICT DO
-- NOTHING, same convention as 012/013/015: ongoing policy/egress changes
-- are expected to be synced from config/retailers.yaml by the application,
-- not by re-running this file.
--
-- Bass Pro's listing data is NOT served by the human-visible category page
-- HTML -- it loads client-side from a Coveo search API response (see
-- fpt/adapters/basspro.py's module docstring and tests/fixtures/_capture/
-- README.md). The `discovery_pages.url` below is therefore the
-- human-visible fishing-rods category page (a `/c/` category path, not the
-- disallowed `/shop/` path and not the raw Coveo API endpoint itself,
-- which requires a POST body/org context this table has no column for) --
-- the fetch layer's real-browser fetcher for this retailer is expected to
-- resolve that category page's URL to the underlying Coveo request itself,
-- matching the split already documented in fpt/adapters/basspro.py between
-- "what fpt tick tracks" and "what actually gets fetched".
--
-- category_hint is 'rods', matching the specific category this discovery
-- page targets -- unlike academy's/the Shopify collection retailers' 'other'
-- (which mix multiple categories in one sale/clearance grid), this page is
-- rods-only.

INSERT INTO retailers (slug, name, base_url, adapter_slug, adapter_kind, enabled, policy, egress)
VALUES
  ('basspro', 'Bass Pro Shops', 'https://www.basspro.com', 'basspro',
   'page_scraper', true,
   '{"min_delay_s":10,"jitter_s":5,"max_requests_per_hour":90,"max_requests_per_day":1200,
     "reserved_share":{"CONFIRM":0.15,"HOT":0.15},"quiet_hours_utc":null,"timeout_s":30,
     "retry_on":[500,502,503,504,"timeout"],"max_retries":1,"retry_backoff_s":120,
     "breaker":{"consecutive_blocks":5,"block_rate_threshold":0.20,"block_rate_min_sample":20,
       "cooldown_hours":12,"disable_after_trips_in_48h":2},
     "user_agent_profile":"identified","tracked_url_cap":2500}'::jsonb,
   '{"mode":"DIRECT","proxy_url_env":null,"api_key_env":null}'::jsonb)
ON CONFLICT (slug) DO NOTHING;

INSERT INTO discovery_pages (retailer_id, page_type, url, category_hint, interval_minutes, max_pages)
SELECT r.id, d.page_type, d.url, d.category_hint, d.interval_minutes, d.max_pages
FROM (VALUES
  ('basspro', 'CATALOG_LISTING',
   'https://www.basspro.com/c/fishing-rods', 'rods', 240, 10)
) AS d(retailer_slug, page_type, url, category_hint, interval_minutes, max_pages)
JOIN retailers r ON r.slug = d.retailer_slug
ON CONFLICT (retailer_id, url) DO NOTHING;
