-- 017_seed_cabelas_dicks.up.sql
-- Seed Cabela's and Dick's Sporting Goods, added in this task, plus one
-- discovery page each. Idempotent via ON CONFLICT DO NOTHING, same
-- convention as 012/013/015/016: ongoing policy/egress changes are expected
-- to be synced from config/retailers.yaml by the application, not by
-- re-running this file.
--
-- Cabela's runs the identical Coveo search backend/org as Bass Pro Shops
-- (see fpt/adapters/cabelas.py's module docstring) -- like basspro's own
-- seed (016), the discovery_pages.url below is the human-visible category
-- page, not the raw Coveo API endpoint (which requires a POST body/org
-- context this table has no column for); the fetch layer's real-browser
-- fetcher is expected to resolve that category page to the underlying
-- Coveo request itself. category_hint is 'rods', matching this specific
-- category page.
--
-- Dick's Sporting Goods reads a `prod-catalog-product-api` v2/search JSON
-- endpoint (see fpt/adapters/dicks.py's module docstring). The
-- discovery_pages.url below is the human-visible `/f/sale` listing page for
-- the same reason: the fetch layer resolves it to the underlying v2/search
-- request. category_hint is 'other' -- unlike Cabela's rods-only category
-- page, Dick's sale grid mixes multiple product categories (footwear,
-- apparel, equipment), matching academy.py's/the Shopify collection
-- retailers' 'other' convention for mixed-category grids.

INSERT INTO retailers (slug, name, base_url, adapter_slug, adapter_kind, enabled, policy, egress)
VALUES
  ('cabelas', 'Cabela''s', 'https://www.cabelas.com', 'cabelas',
   'page_scraper', true,
   '{"min_delay_s":10,"jitter_s":5,"max_requests_per_hour":90,"max_requests_per_day":1200,
     "reserved_share":{"CONFIRM":0.15,"HOT":0.15},"quiet_hours_utc":null,"timeout_s":30,
     "retry_on":[500,502,503,504,"timeout"],"max_retries":1,"retry_backoff_s":120,
     "breaker":{"consecutive_blocks":5,"block_rate_threshold":0.20,"block_rate_min_sample":20,
       "cooldown_hours":12,"disable_after_trips_in_48h":2},
     "user_agent_profile":"identified","tracked_url_cap":2500}'::jsonb,
   '{"mode":"DIRECT","proxy_url_env":null,"api_key_env":null}'::jsonb),
  ('dicks', 'Dick''s Sporting Goods', 'https://www.dickssportinggoods.com', 'dicks',
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
  ('cabelas', 'CATALOG_LISTING',
   'https://www.cabelas.com/c/fishing-rods', 'rods', 240, 10),
  ('dicks', 'CATALOG_LISTING',
   'https://www.dickssportinggoods.com/f/sale', 'other', 240, 10)
) AS d(retailer_slug, page_type, url, category_hint, interval_minutes, max_pages)
JOIN retailers r ON r.slug = d.retailer_slug
ON CONFLICT (retailer_id, url) DO NOTHING;
