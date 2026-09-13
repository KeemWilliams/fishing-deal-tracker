-- 013_seed_more_retailers.up.sql
-- Seed the three retailers added in this task (fishusa, tackledirect, alltackle) plus their
-- one supported discovery/tracking mechanism. Idempotent via ON CONFLICT DO NOTHING, same
-- convention as 012_seed_retailers.up.sql: ongoing policy/egress changes are expected to be
-- synced from config/retailers.yaml by the application, not by re-running this file.
--
-- Note: tackle_warehouse, academy and jandh were already seeded (and already enabled=true) by
-- 012_seed_retailers.up.sql. This migration does not touch those rows -- the reason Academy and
-- J&H never actually ran via `fpt tick` was that fpt/adapters/registry.py never registered their
-- adapter classes and config/retailers.yaml (a separate, application-level config file `fpt tick`
-- reads directly, independent of this table) never carried entries for them. Both are fixed in
-- this task's code/config changes, not via a database migration.
--
-- None of the three new retailers get a discovery_pages row: this task verified live that none
-- of their clearance/sale grids render via plain HTTP (see fpt/adapters/fishusa.py,
-- tackledirect.py, alltackle.py module docstrings for what was actually confirmed and why). Their
-- adapters are PRODUCT-page-only (capabilities.page_types = {PRODUCT}); price refresh for them is
-- expected to come from an existing tracked-product catalog, not category-page discovery.

INSERT INTO retailers (slug, name, base_url, adapter_slug, adapter_kind, enabled, policy, egress)
VALUES
  ('fishusa', 'FishUSA', 'https://www.fishusa.com', 'fishusa',
   'page_scraper', true,
   '{"min_delay_s":10,"jitter_s":5,"max_requests_per_hour":90,"max_requests_per_day":1200,
     "reserved_share":{"CONFIRM":0.15,"HOT":0.15},"quiet_hours_utc":null,"timeout_s":30,
     "retry_on":[500,502,503,504,"timeout"],"max_retries":1,"retry_backoff_s":120,
     "breaker":{"consecutive_blocks":5,"block_rate_threshold":0.20,"block_rate_min_sample":20,
       "cooldown_hours":12,"disable_after_trips_in_48h":2},
     "user_agent_profile":"identified","tracked_url_cap":2500}'::jsonb,
   '{"mode":"DIRECT","proxy_url_env":null,"api_key_env":null}'::jsonb),
  ('tackledirect', 'TackleDirect', 'https://www.tackledirect.com', 'tackledirect',
   'page_scraper', true,
   '{"min_delay_s":10,"jitter_s":5,"max_requests_per_hour":90,"max_requests_per_day":1200,
     "reserved_share":{"CONFIRM":0.15,"HOT":0.15},"quiet_hours_utc":null,"timeout_s":30,
     "retry_on":[500,502,503,504,"timeout"],"max_retries":1,"retry_backoff_s":120,
     "breaker":{"consecutive_blocks":5,"block_rate_threshold":0.20,"block_rate_min_sample":20,
       "cooldown_hours":12,"disable_after_trips_in_48h":2},
     "user_agent_profile":"identified","tracked_url_cap":2500}'::jsonb,
   '{"mode":"DIRECT","proxy_url_env":null,"api_key_env":null}'::jsonb),
  ('alltackle', 'alltackle.com', 'https://alltackle.com', 'alltackle',
   'page_scraper', true,
   '{"min_delay_s":10,"jitter_s":5,"max_requests_per_hour":90,"max_requests_per_day":1200,
     "reserved_share":{"CONFIRM":0.15,"HOT":0.15},"quiet_hours_utc":null,"timeout_s":30,
     "retry_on":[500,502,503,504,"timeout"],"max_retries":1,"retry_backoff_s":120,
     "breaker":{"consecutive_blocks":5,"block_rate_threshold":0.20,"block_rate_min_sample":20,
       "cooldown_hours":12,"disable_after_trips_in_48h":2},
     "user_agent_profile":"identified","tracked_url_cap":2500}'::jsonb,
   '{"mode":"DIRECT","proxy_url_env":null,"api_key_env":null}'::jsonb)
ON CONFLICT (slug) DO NOTHING;
