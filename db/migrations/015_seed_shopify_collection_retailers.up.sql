-- 015_seed_shopify_collection_retailers.up.sql
-- Seed the three retailers added in this task (fishingonline, discounttackle, rodlocker) plus
-- their one discovery mechanism each: a Shopify sale/clearance collection's own products.json
-- endpoint (fpt/adapters/_shopify_collection.py). Idempotent via ON CONFLICT DO NOTHING, same
-- convention as 012_seed_retailers.up.sql / 013_seed_more_retailers.up.sql: ongoing policy/
-- egress changes are expected to be synced from config/retailers.yaml by the application, not by
-- re-running this file.
--
-- Unlike 013 (fishusa/tackledirect/alltackle -- PRODUCT-page-only, no discovery_pages row), all
-- three retailers here DO get a discovery_pages row: their sale/clearance collections were
-- verified live and non-empty via the collection's own products.json endpoint (see
-- knowledge/research/fishing-additional-retailers-2026-09-12.md and fpt/adapters/
-- _shopify_collection.py's module docstring for the endpoint/handle research). The discovery URL
-- for each retailer is that retailer's `/collections/<handle>/products.json` endpoint, NOT the
-- human-visible `/collections/<handle>` page -- matching what fpt/adapters/_shopify_collection.py
-- actually requests for PageType.CLEARANCE_LISTING.
--
-- category_hint is 'other' for all three, matching academy's clearance discovery page (012): each
-- sale/clearance collection mixes rods, reels, line, terminal tackle, etc. in one grid, so no
-- single tracked_urls.category_hint value would be accurate at the discovery-page level.

INSERT INTO retailers (slug, name, base_url, adapter_slug, adapter_kind, enabled, policy, egress)
VALUES
  ('fishingonline', 'Fishing Online', 'https://www.fishingonline.com', 'fishingonline',
   'page_scraper', true,
   '{"min_delay_s":10,"jitter_s":5,"max_requests_per_hour":90,"max_requests_per_day":1200,
     "reserved_share":{"CONFIRM":0.15,"HOT":0.15},"quiet_hours_utc":null,"timeout_s":30,
     "retry_on":[500,502,503,504,"timeout"],"max_retries":1,"retry_backoff_s":120,
     "breaker":{"consecutive_blocks":5,"block_rate_threshold":0.20,"block_rate_min_sample":20,
       "cooldown_hours":12,"disable_after_trips_in_48h":2},
     "user_agent_profile":"identified","tracked_url_cap":2500}'::jsonb,
   '{"mode":"DIRECT","proxy_url_env":null,"api_key_env":null}'::jsonb),
  ('discounttackle', 'Discount Tackle', 'https://discounttackle.com', 'discounttackle',
   'page_scraper', true,
   '{"min_delay_s":10,"jitter_s":5,"max_requests_per_hour":90,"max_requests_per_day":1200,
     "reserved_share":{"CONFIRM":0.15,"HOT":0.15},"quiet_hours_utc":null,"timeout_s":30,
     "retry_on":[500,502,503,504,"timeout"],"max_retries":1,"retry_backoff_s":120,
     "breaker":{"consecutive_blocks":5,"block_rate_threshold":0.20,"block_rate_min_sample":20,
       "cooldown_hours":12,"disable_after_trips_in_48h":2},
     "user_agent_profile":"identified","tracked_url_cap":2500}'::jsonb,
   '{"mode":"DIRECT","proxy_url_env":null,"api_key_env":null}'::jsonb),
  ('rodlocker', 'Rod Locker', 'https://rodlocker.com', 'rodlocker',
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
  ('fishingonline', 'CLEARANCE_LISTING',
   'https://www.fishingonline.com/collections/sale/products.json', 'other', 240, 10),
  ('discounttackle', 'CLEARANCE_LISTING',
   'https://discounttackle.com/collections/clearance/products.json', 'other', 240, 10),
  ('rodlocker', 'CLEARANCE_LISTING',
   'https://rodlocker.com/collections/sale/products.json', 'other', 240, 10)
) AS d(retailer_slug, page_type, url, category_hint, interval_minutes, max_pages)
JOIN retailers r ON r.slug = d.retailer_slug
ON CONFLICT (retailer_id, url) DO NOTHING;
