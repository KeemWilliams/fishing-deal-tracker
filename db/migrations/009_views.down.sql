-- 009_views.down.sql
DROP FUNCTION IF EXISTS refresh_fpt_views();
DROP MATERIALIZED VIEW IF EXISTS variant_current_new;
DROP MATERIALIZED VIEW IF EXISTS offer_reference_stats;
DROP MATERIALIZED VIEW IF EXISTS offer_daily_price;
