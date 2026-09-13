-- 007_price_observations.up.sql
-- Append-only price history, one row per fetch per offer. This is the highest-volume table and
-- the source of every reference calculation (section 6.1) -- nothing is ever edited in place;
-- a re-classification (e.g. a parser fix) is expressed as a new row plus deal/held-review state,
-- never as an UPDATE.

CREATE TABLE IF NOT EXISTS price_observations (
  id bigserial PRIMARY KEY,
  offer_id bigint NOT NULL REFERENCES offers(id),
  crawl_task_id bigint NOT NULL REFERENCES crawl_tasks(id),
  task_kind text NOT NULL CHECK (task_kind IN ('CONFIRM', 'HOT', 'DISCOVERY', 'ENROLL', 'BASELINE', 'CANARY')),
  observed_at timestamptz NOT NULL,
  observed_date_et date NOT NULL,
  price_cents int,
  shipping_cents int,
  claimed_reference_cents int,
  claimed_reference_kind text CHECK (claimed_reference_kind IS NULL OR claimed_reference_kind IN
    ('WAS', 'MSRP', 'LIST', 'COMPARE_AT')),
  on_clearance boolean NOT NULL,
  availability text NOT NULL CHECK (availability IN
    ('IN_STOCK', 'OUT_OF_STOCK', 'STORE_ONLY', 'BACKORDER', 'UNKNOWN')),
  stock_qty int,
  stock_qty_is_floor boolean NOT NULL DEFAULT false,
  restock_date date,
  unit_count int,
  variant_key_observed text,   -- identity snapshot for mismatch checks (C4 in section 6.4)
  quality text NOT NULL CHECK (quality IN ('OK', 'SUSPECT', 'REJECTED')),
  quality_reasons text[] NOT NULL DEFAULT '{}',
  adapter_version text NOT NULL,
  snapshot_ref text NOT NULL,
  -- Bad-data guard (section 6.2): a non-rejected observation must carry a positive price.
  CHECK (quality = 'REJECTED' OR price_cents > 0),
  CHECK (shipping_cents IS NULL OR shipping_cents >= 0),
  CHECK (claimed_reference_cents IS NULL OR claimed_reference_cents > 0),
  CHECK (stock_qty IS NULL OR stock_qty >= 0)
);
CREATE INDEX IF NOT EXISTS price_obs_offer_time_idx ON price_observations (offer_id, observed_at DESC);
CREATE INDEX IF NOT EXISTS price_obs_time_brin ON price_observations USING brin (observed_at);
CREATE INDEX IF NOT EXISTS price_obs_crawl_task_idx ON price_observations (crawl_task_id);
-- Deal-engine hot path: last OK observation per offer per ET day (feeds offer_daily_price, 009_views).
CREATE INDEX IF NOT EXISTS price_obs_offer_day_ok_idx ON price_observations (offer_id, observed_date_et, observed_at DESC)
  WHERE quality = 'OK';

-- Append-only enforcement: REVOKE UPDATE/DELETE at the role level (011_roles_grants) is the
-- primary control; this trigger is a defense-in-depth guard that fires regardless of role,
-- including for fpt_owner running an ad hoc UPDATE by mistake.
CREATE OR REPLACE FUNCTION price_observations_no_mutate() RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  RAISE EXCEPTION 'price_observations is append-only: % is not permitted (id=%)', TG_OP, OLD.id;
END;
$$;

CREATE OR REPLACE TRIGGER trg_price_observations_no_update
  BEFORE UPDATE ON price_observations FOR EACH ROW EXECUTE FUNCTION price_observations_no_mutate();
CREATE OR REPLACE TRIGGER trg_price_observations_no_delete
  BEFORE DELETE ON price_observations FOR EACH ROW EXECUTE FUNCTION price_observations_no_mutate();
