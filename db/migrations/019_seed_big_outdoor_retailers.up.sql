-- 019_seed_big_outdoor_retailers.up.sql
-- Seed the two big outdoor retailers added in this task, plus one CATALOG_LISTING
-- discovery page each. Idempotent via ON CONFLICT DO NOTHING, same convention as
-- 012/013/015/016/017/018: ongoing policy/egress changes are expected to be synced from
-- config/retailers.yaml by the application, not by re-running this file.
--
-- sportsmans_warehouse (SAP Commerce Cloud/Hybris): dedicated fishing-clearance category,
-- `?pageSize=100` requested up front to cut request count on the ~2,960-item/65-page
-- category (see fpt/adapters/sportsmans_warehouse.py module docstring) -- pagination
-- itself is still followed via the page's own `rel="next"` anchor, which the adapter
-- resolves at parse time rather than this seed constructing `page=N` URLs by hand.
--
-- sportsmans_guide (legacy ATG-style storefront): `fishdeals` collection mixes true
-- clearance/on-sale rows with generic Members-Only-badge rows -- see
-- fpt/adapters/sportsmans_guide.py's module docstring for the badge-based filtering this
-- adapter applies at parse time (not something this seed can express).
--
-- category_hint is 'other' for both, matching every prior CATALOG_LISTING/CLEARANCE_
-- LISTING retailer's convention in this table: each listing mixes rods, reels, line,
-- lures, etc. in one grid, so no single tracked_urls.category_hint value would be
-- accurate at the discovery-page level.

INSERT INTO retailers (slug, name, base_url, adapter_slug, adapter_kind, enabled, policy, egress)
VALUES
  ('sportsmans_warehouse', 'Sportsman''s Warehouse', 'https://www.sportsmans.com',
   'sportsmans_warehouse', 'page_scraper', true,
   '{"min_delay_s":10,"jitter_s":5,"max_requests_per_hour":90,"max_requests_per_day":1200,
     "reserved_share":{"CONFIRM":0.15,"HOT":0.15},"quiet_hours_utc":null,"timeout_s":30,
     "retry_on":[500,502,503,504,"timeout"],"max_retries":1,"retry_backoff_s":120,
     "breaker":{"consecutive_blocks":5,"block_rate_threshold":0.20,"block_rate_min_sample":20,
       "cooldown_hours":12,"disable_after_trips_in_48h":2},
     "user_agent_profile":"identified","tracked_url_cap":2500}'::jsonb,
   '{"mode":"DIRECT","proxy_url_env":null,"api_key_env":null}'::jsonb),
  ('sportsmans_guide', 'Sportsman''s Guide', 'https://www.sportsmansguide.com',
   'sportsmans_guide', 'page_scraper', true,
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
  ('sportsmans_warehouse', 'CATALOG_LISTING',
   'https://www.sportsmans.com/deals-clearance/fishing-clearance/c/cat101209?pageSize=100',
   'other', 240, 65),
  ('sportsmans_guide', 'CATALOG_LISTING',
   'https://www.sportsmansguide.com/productlist?collection=fishdeals', 'other', 240, 10)
) AS d(retailer_slug, page_type, url, category_hint, interval_minutes, max_pages)
JOIN retailers r ON r.slug = d.retailer_slug
ON CONFLICT (retailer_id, url) DO NOTHING;
