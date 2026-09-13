-- 012_seed_retailers.up.sql
-- Seed the three MVP retailers (section 3.3) and their discovery pages. Idempotent via
-- ON CONFLICT DO NOTHING: this migration only ever inserts the initial rows; ongoing changes to
-- policy/egress/discovery config are expected to be synced from config/retailers.yaml and
-- config/discovery_pages.yaml by the application, not by re-running this file (DECISION, see
-- db/README.md).

INSERT INTO retailers (slug, name, base_url, adapter_slug, adapter_kind, enabled, policy, egress)
VALUES
  ('tackle_warehouse', 'Tackle Warehouse', 'https://www.tacklewarehouse.com', 'tackle_warehouse',
   'page_scraper', true,
   '{"min_delay_s":10,"jitter_s":5,"max_requests_per_hour":90,"max_requests_per_day":1200,
     "reserved_share":{"CONFIRM":0.15,"HOT":0.15},"quiet_hours_utc":null,"timeout_s":30,
     "retry_on":[500,502,503,504,"timeout"],"max_retries":1,"retry_backoff_s":120,
     "breaker":{"consecutive_blocks":5,"block_rate_threshold":0.20,"block_rate_min_sample":20,
       "cooldown_hours":12,"disable_after_trips_in_48h":2},
     "user_agent_profile":"identified","tracked_url_cap":2500}'::jsonb,
   '{"mode":"DIRECT","proxy_url_env":null,"api_key_env":null}'::jsonb),
  ('academy', 'Academy Sports', 'https://www.academy.com', 'academy',
   'page_scraper', true,
   '{"min_delay_s":8,"jitter_s":5,"max_requests_per_hour":100,"max_requests_per_day":1200,
     "reserved_share":{"CONFIRM":0.15,"HOT":0.15},"quiet_hours_utc":null,"timeout_s":30,
     "retry_on":[500,502,503,504,"timeout"],"max_retries":1,"retry_backoff_s":120,
     "breaker":{"consecutive_blocks":5,"block_rate_threshold":0.20,"block_rate_min_sample":20,
       "cooldown_hours":12,"disable_after_trips_in_48h":2},
     "user_agent_profile":"identified","tracked_url_cap":2500}'::jsonb,
   '{"mode":"DIRECT","proxy_url_env":null,"api_key_env":null}'::jsonb),
  ('jandh', 'J&H Tackle', 'https://www.jandh.com', 'jandh',
   'page_scraper', true,
   '{"min_delay_s":10,"jitter_s":5,"max_requests_per_hour":60,"max_requests_per_day":800,
     "reserved_share":{"CONFIRM":0.15,"HOT":0.15},"quiet_hours_utc":null,"timeout_s":30,
     "retry_on":[500,502,503,504,"timeout"],"max_retries":1,"retry_backoff_s":120,
     "breaker":{"consecutive_blocks":5,"block_rate_threshold":0.20,"block_rate_min_sample":20,
       "cooldown_hours":12,"disable_after_trips_in_48h":2},
     "user_agent_profile":"identified","tracked_url_cap":1500}'::jsonb,
   '{"mode":"DIRECT","proxy_url_env":null,"api_key_env":null}'::jsonb)
ON CONFLICT (slug) DO NOTHING;

INSERT INTO discovery_pages (retailer_id, page_type, url, category_hint, interval_minutes, max_pages)
SELECT r.id, d.page_type, d.url, d.category_hint, d.interval_minutes, d.max_pages
FROM (VALUES
  ('tackle_warehouse', 'CLEARANCE_LISTING', 'https://www.tacklewarehouse.com/catpage-CLEARRODSPP.html', 'rod', 240, 10),
  ('tackle_warehouse', 'CLEARANCE_LISTING', 'https://www.tacklewarehouse.com/catpage-CLEARREELPP.html', 'reel', 240, 10),
  ('tackle_warehouse', 'CLEARANCE_LISTING', 'https://www.tacklewarehouse.com/catpage-CLEARHRDBTS.html', 'hard_bait', 240, 10),
  ('tackle_warehouse', 'USED_LISTING', 'https://www.tacklewarehouse.com/catpage-USED.html', 'other', 120, 10),
  ('academy', 'CLEARANCE_LISTING',
   'https://www.academy.com/c/academy-clearance/outdoors-clearance--1/fishing-clearance-items/rods-reels-clearance--1',
   'other', 360, 10)
) AS d(retailer_slug, page_type, url, category_hint, interval_minutes, max_pages)
JOIN retailers r ON r.slug = d.retailer_slug
ON CONFLICT (retailer_id, url) DO NOTHING;
