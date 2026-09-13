-- 011_roles_grants.up.sql
-- Least-privilege DB roles (section 9). DECISION (see db/README.md): roles are created here as
-- NOLOGIN placeholders with no password. Login capability and the actual password/connection
-- string are provisioned separately via Infisical/Coolify env injection (FPT_DATABASE_URL_JOB,
-- FPT_DATABASE_URL_API) -- never via a migration file, since migrations are checked into git.
-- Ops or the devops-engineer runs, once per environment:
--   ALTER ROLE fpt_job  LOGIN PASSWORD '<from Infisical>';
--   ALTER ROLE fpt_api  LOGIN PASSWORD '<from Infisical>';
-- fpt_owner is expected to be whichever role actually runs these migrations (e.g. the Coolify
-- Postgres superuser or a dedicated migration role); it is not created here to avoid colliding
-- with the connecting role's own name.

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fpt_job') THEN
    CREATE ROLE fpt_job NOLOGIN;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fpt_api') THEN
    CREATE ROLE fpt_api NOLOGIN;
  END IF;
END;
$$;

GRANT USAGE ON SCHEMA public TO fpt_job, fpt_api;

-- fpt_job: the crawler/matcher/deal-engine/notifier process. Broad DML, EXCEPT price_observations
-- which is INSERT + SELECT only (append-only is enforced twice: here at the grant level, and by
-- the BEFORE UPDATE/DELETE trigger in 007_price_observations, which fires for any role).
GRANT SELECT, INSERT ON price_observations TO fpt_job;
GRANT SELECT, INSERT, UPDATE, DELETE ON
  brands, brand_aliases, retailers, sellers,
  discovery_pages, tracked_urls, discovery_hits,
  products, product_slug_redirects, product_variants,
  listings, offers, match_candidates,
  crawl_tasks, tick_runs, fetch_log, retailer_health_hourly,
  deals,
  subscribers, watches, alert_deliveries, alert_items, api_tokens_used
  TO fpt_job;
GRANT SELECT ON offer_daily_price, offer_reference_stats, variant_current_new TO fpt_job;
GRANT ALL ON ALL SEQUENCES IN SCHEMA public TO fpt_job;
GRANT EXECUTE ON FUNCTION refresh_fpt_views() TO fpt_job;

-- fpt_api: the public-facing `fpt api` process (section 3.5). It may look up a variant by its
-- opaque pub_id, and manage subscription/watch state -- nothing else. It has NO direct access to
-- tracked_urls; promoting a watched variant to HOT tracking goes through the SECURITY DEFINER
-- function below so fpt_api can never touch any other column or table via that path.
GRANT SELECT (id, pub_id) ON product_variants TO fpt_api;
GRANT SELECT, INSERT, UPDATE ON subscribers TO fpt_api;
GRANT SELECT, INSERT, UPDATE ON watches TO fpt_api;
GRANT SELECT, INSERT ON api_tokens_used TO fpt_api;
GRANT USAGE, SELECT ON subscribers_id_seq, watches_id_seq TO fpt_api;

CREATE OR REPLACE FUNCTION promote_variant_hot(p_variant_id bigint) RETURNS void
SECURITY DEFINER
SET search_path = public
LANGUAGE plpgsql
AS $$
BEGIN
  UPDATE tracked_urls tu
  SET tier = 'HOT',
      interval_minutes = 120,
      next_due_at = LEAST(tu.next_due_at, now()),
      interest_score = tu.interest_score + 1
  FROM listings l
  WHERE l.variant_id = p_variant_id
    AND tu.id = l.tracked_url_id
    AND tu.tier <> 'HOT';
END;
$$;
REVOKE ALL ON FUNCTION promote_variant_hot(bigint) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION promote_variant_hot(bigint) TO fpt_api;
