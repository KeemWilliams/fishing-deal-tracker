-- 018_seed_manufacturer_brand_sites.up.sql
-- Seed the nine manufacturer/brand clearance retailers added in this task, plus one
-- CLEARANCE_LISTING discovery page each: a Shopify sale collection's own products.json
-- endpoint (fpt/adapters/_shopify_collection.py), same mechanism as 015_seed_shopify_
-- collection_retailers.up.sql. Idempotent via ON CONFLICT DO NOTHING, same convention as
-- 012/013/015/016/017: ongoing policy/egress changes are expected to be synced from
-- config/retailers.yaml by the application, not by re-running this file.
--
-- Two Shopify-platform families -- see knowledge/research/fishing-manufacturer-sites-
-- 2026-09-13.md:
--
-- Family 1 (Pure Fishing, Inc.): abugarcia, penn, pflueger, uglystik, berkley -- five
-- separate brand domains, one retailer row each, sale collection handle `sale` on all
-- five.
--
-- Family 2 (Shimano North America Fishing): shimano, gloomis, powerpro, jackall all
-- share ONE storefront domain (fishshop.shimano.com) -- modeled as four separate
-- retailer rows (one per brand/seller_key, see fpt/adapters/shimano.py's module
-- docstring for the rationale), each with its own `last-cast-savings-<brand>`
-- collection handle. `retailers.base_url` is not unique-constrained (see
-- db/migrations/002_reference_tables.up.sql), so all four safely share the same
-- base_url.
--
-- category_hint is 'other' for all nine, matching academy's/the prior Shopify
-- collection retailers' convention: each sale/clearance collection mixes rods, reels,
-- line, lures, apparel, etc. in one grid, so no single tracked_urls.category_hint value
-- would be accurate at the discovery-page level.

INSERT INTO retailers (slug, name, base_url, adapter_slug, adapter_kind, enabled, policy, egress)
VALUES
  ('abugarcia', 'Abu Garcia', 'https://www.abugarcia.com', 'abugarcia',
   'page_scraper', true,
   '{"min_delay_s":10,"jitter_s":5,"max_requests_per_hour":90,"max_requests_per_day":1200,
     "reserved_share":{"CONFIRM":0.15,"HOT":0.15},"quiet_hours_utc":null,"timeout_s":30,
     "retry_on":[500,502,503,504,"timeout"],"max_retries":1,"retry_backoff_s":120,
     "breaker":{"consecutive_blocks":5,"block_rate_threshold":0.20,"block_rate_min_sample":20,
       "cooldown_hours":12,"disable_after_trips_in_48h":2},
     "user_agent_profile":"identified","tracked_url_cap":2500}'::jsonb,
   '{"mode":"DIRECT","proxy_url_env":null,"api_key_env":null}'::jsonb),
  ('penn', 'Penn', 'https://www.pennfishing.com', 'penn',
   'page_scraper', true,
   '{"min_delay_s":10,"jitter_s":5,"max_requests_per_hour":90,"max_requests_per_day":1200,
     "reserved_share":{"CONFIRM":0.15,"HOT":0.15},"quiet_hours_utc":null,"timeout_s":30,
     "retry_on":[500,502,503,504,"timeout"],"max_retries":1,"retry_backoff_s":120,
     "breaker":{"consecutive_blocks":5,"block_rate_threshold":0.20,"block_rate_min_sample":20,
       "cooldown_hours":12,"disable_after_trips_in_48h":2},
     "user_agent_profile":"identified","tracked_url_cap":2500}'::jsonb,
   '{"mode":"DIRECT","proxy_url_env":null,"api_key_env":null}'::jsonb),
  ('pflueger', 'Pflueger', 'https://pfluegerfishing.com', 'pflueger',
   'page_scraper', true,
   '{"min_delay_s":10,"jitter_s":5,"max_requests_per_hour":90,"max_requests_per_day":1200,
     "reserved_share":{"CONFIRM":0.15,"HOT":0.15},"quiet_hours_utc":null,"timeout_s":30,
     "retry_on":[500,502,503,504,"timeout"],"max_retries":1,"retry_backoff_s":120,
     "breaker":{"consecutive_blocks":5,"block_rate_threshold":0.20,"block_rate_min_sample":20,
       "cooldown_hours":12,"disable_after_trips_in_48h":2},
     "user_agent_profile":"identified","tracked_url_cap":2500}'::jsonb,
   '{"mode":"DIRECT","proxy_url_env":null,"api_key_env":null}'::jsonb),
  ('uglystik', 'Ugly Stik', 'https://www.uglystik.com', 'uglystik',
   'page_scraper', true,
   '{"min_delay_s":10,"jitter_s":5,"max_requests_per_hour":90,"max_requests_per_day":1200,
     "reserved_share":{"CONFIRM":0.15,"HOT":0.15},"quiet_hours_utc":null,"timeout_s":30,
     "retry_on":[500,502,503,504,"timeout"],"max_retries":1,"retry_backoff_s":120,
     "breaker":{"consecutive_blocks":5,"block_rate_threshold":0.20,"block_rate_min_sample":20,
       "cooldown_hours":12,"disable_after_trips_in_48h":2},
     "user_agent_profile":"identified","tracked_url_cap":2500}'::jsonb,
   '{"mode":"DIRECT","proxy_url_env":null,"api_key_env":null}'::jsonb),
  ('berkley', 'Berkley', 'https://www.berkley-fishing.com', 'berkley',
   'page_scraper', true,
   '{"min_delay_s":10,"jitter_s":5,"max_requests_per_hour":90,"max_requests_per_day":1200,
     "reserved_share":{"CONFIRM":0.15,"HOT":0.15},"quiet_hours_utc":null,"timeout_s":30,
     "retry_on":[500,502,503,504,"timeout"],"max_retries":1,"retry_backoff_s":120,
     "breaker":{"consecutive_blocks":5,"block_rate_threshold":0.20,"block_rate_min_sample":20,
       "cooldown_hours":12,"disable_after_trips_in_48h":2},
     "user_agent_profile":"identified","tracked_url_cap":2500}'::jsonb,
   '{"mode":"DIRECT","proxy_url_env":null,"api_key_env":null}'::jsonb),
  ('shimano', 'Shimano', 'https://fishshop.shimano.com', 'shimano',
   'page_scraper', true,
   '{"min_delay_s":10,"jitter_s":5,"max_requests_per_hour":90,"max_requests_per_day":1200,
     "reserved_share":{"CONFIRM":0.15,"HOT":0.15},"quiet_hours_utc":null,"timeout_s":30,
     "retry_on":[500,502,503,504,"timeout"],"max_retries":1,"retry_backoff_s":120,
     "breaker":{"consecutive_blocks":5,"block_rate_threshold":0.20,"block_rate_min_sample":20,
       "cooldown_hours":12,"disable_after_trips_in_48h":2},
     "user_agent_profile":"identified","tracked_url_cap":2500}'::jsonb,
   '{"mode":"DIRECT","proxy_url_env":null,"api_key_env":null}'::jsonb),
  ('gloomis', 'G. Loomis', 'https://fishshop.shimano.com', 'gloomis',
   'page_scraper', true,
   '{"min_delay_s":10,"jitter_s":5,"max_requests_per_hour":90,"max_requests_per_day":1200,
     "reserved_share":{"CONFIRM":0.15,"HOT":0.15},"quiet_hours_utc":null,"timeout_s":30,
     "retry_on":[500,502,503,504,"timeout"],"max_retries":1,"retry_backoff_s":120,
     "breaker":{"consecutive_blocks":5,"block_rate_threshold":0.20,"block_rate_min_sample":20,
       "cooldown_hours":12,"disable_after_trips_in_48h":2},
     "user_agent_profile":"identified","tracked_url_cap":2500}'::jsonb,
   '{"mode":"DIRECT","proxy_url_env":null,"api_key_env":null}'::jsonb),
  ('powerpro', 'PowerPro', 'https://fishshop.shimano.com', 'powerpro',
   'page_scraper', true,
   '{"min_delay_s":10,"jitter_s":5,"max_requests_per_hour":90,"max_requests_per_day":1200,
     "reserved_share":{"CONFIRM":0.15,"HOT":0.15},"quiet_hours_utc":null,"timeout_s":30,
     "retry_on":[500,502,503,504,"timeout"],"max_retries":1,"retry_backoff_s":120,
     "breaker":{"consecutive_blocks":5,"block_rate_threshold":0.20,"block_rate_min_sample":20,
       "cooldown_hours":12,"disable_after_trips_in_48h":2},
     "user_agent_profile":"identified","tracked_url_cap":2500}'::jsonb,
   '{"mode":"DIRECT","proxy_url_env":null,"api_key_env":null}'::jsonb),
  ('jackall', 'Jackall Lures', 'https://fishshop.shimano.com', 'jackall',
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
  ('abugarcia', 'CLEARANCE_LISTING',
   'https://www.abugarcia.com/collections/sale/products.json', 'other', 240, 10),
  ('penn', 'CLEARANCE_LISTING',
   'https://www.pennfishing.com/collections/sale/products.json', 'other', 240, 10),
  ('pflueger', 'CLEARANCE_LISTING',
   'https://pfluegerfishing.com/collections/sale/products.json', 'other', 240, 10),
  ('uglystik', 'CLEARANCE_LISTING',
   'https://www.uglystik.com/collections/sale/products.json', 'other', 240, 10),
  ('berkley', 'CLEARANCE_LISTING',
   'https://www.berkley-fishing.com/collections/sale/products.json', 'other', 240, 10),
  ('shimano', 'CLEARANCE_LISTING',
   'https://fishshop.shimano.com/collections/last-cast-savings-shimano/products.json', 'other', 240, 10),
  ('gloomis', 'CLEARANCE_LISTING',
   'https://fishshop.shimano.com/collections/last-cast-savings-g-loomis/products.json', 'other', 240, 10),
  ('powerpro', 'CLEARANCE_LISTING',
   'https://fishshop.shimano.com/collections/last-cast-savings-powerpro/products.json', 'other', 240, 10),
  ('jackall', 'CLEARANCE_LISTING',
   'https://fishshop.shimano.com/collections/last-cast-savings-jackall/products.json', 'other', 240, 10)
) AS d(retailer_slug, page_type, url, category_hint, interval_minutes, max_pages)
JOIN retailers r ON r.slug = d.retailer_slug
ON CONFLICT (retailer_id, url) DO NOTHING;
