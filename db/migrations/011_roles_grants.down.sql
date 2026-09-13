-- 011_roles_grants.down.sql
DROP FUNCTION IF EXISTS promote_variant_hot(bigint);

REVOKE ALL ON subscribers, watches, api_tokens_used FROM fpt_api;
REVOKE ALL ON product_variants FROM fpt_api;
REVOKE USAGE ON SCHEMA public FROM fpt_api;

REVOKE ALL ON ALL TABLES IN SCHEMA public FROM fpt_job;
REVOKE ALL ON offer_daily_price, offer_reference_stats, variant_current_new FROM fpt_job;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM fpt_job;
REVOKE ALL ON FUNCTION refresh_fpt_views() FROM fpt_job;
REVOKE USAGE ON SCHEMA public FROM fpt_job;

-- Roles themselves are left in place (dropping a role that still owns grants elsewhere errors,
-- and this database may not be the only one fpt_job/fpt_api connect to). Drop manually if needed:
--   DROP ROLE IF EXISTS fpt_api;
--   DROP ROLE IF EXISTS fpt_job;
