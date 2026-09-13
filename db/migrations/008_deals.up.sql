-- 008_deals.up.sql
-- Deal candidate lifecycle (section 6): CANDIDATE -> CONFIRMING -> ACTIVE | HELD_REVIEW | REJECTED | EXPIRED.

CREATE TABLE IF NOT EXISTS deals (
  id bigserial PRIMARY KEY,
  pub_id text UNIQUE NOT NULL DEFAULT ('dl_' || fpt_generate_ulid()),
  offer_id bigint NOT NULL REFERENCES offers(id),
  rule text NOT NULL CHECK (rule IN ('DEEP_DISCOUNT_NEW', 'DEEP_DISCOUNT_CLAIMED', 'USED_VS_CURRENT_NEW')),
  lane text NOT NULL CHECK (lane IN ('VERIFIED', 'CLAIMED', 'USED')),
  status text NOT NULL CHECK (status IN
    ('CANDIDATE', 'CONFIRMING', 'ACTIVE', 'HELD_REVIEW', 'REJECTED', 'EXPIRED')),
  detected_observation_id bigint NOT NULL REFERENCES price_observations(id),
  confirming_observation_id bigint REFERENCES price_observations(id),
  last_confirmed_observation_id bigint REFERENCES price_observations(id),
  detected_via text NOT NULL CHECK (detected_via IN ('DISCOVERY_GRID', 'PRODUCT_PAGE', 'DATA_API')),
  price_cents int NOT NULL CHECK (price_cents > 0),
  landed_price_cents int CHECK (landed_price_cents IS NULL OR landed_price_cents > 0),
  reference_kind text NOT NULL CHECK (reference_kind IN
    ('OWN_HISTORY_MEDIAN_90D', 'CROSS_RETAILER_NEW', 'DATA_API_HISTORY', 'CURRENT_NEW', 'RETAILER_CLAIMED')),
  reference_cents int NOT NULL CHECK (reference_cents > 0),
  reference_detail jsonb NOT NULL,   -- {"retailer":"jandh","offer_id":..,"days":74,"match":"auto_gtin"}
  claimed_reference_cents int CHECK (claimed_reference_cents IS NULL OR claimed_reference_cents > 0),
  claimed_reference_kind text CHECK (claimed_reference_kind IS NULL OR claimed_reference_kind IN
    ('WAS', 'MSRP', 'LIST', 'COMPARE_AT')),
  claimed_inflated boolean,   -- claimed > 1.25 x verified reference; NULL if no verified reference
  discount_pct numeric(5, 2) NOT NULL CHECK (discount_pct >= 0 AND discount_pct <= 100),
  reject_reason text,
  hold_reason text,
  detected_at timestamptz NOT NULL DEFAULT now(),
  confirmed_at timestamptz,
  expired_at timestamptz,
  expire_reason text,
  reviewed_by text,
  reviewed_at timestamptz,
  updated_at timestamptz NOT NULL DEFAULT now(),
  -- Bad-data guard: a NEW-priced deal can never be cheaper than $10 below its reference falling
  -- below the 50% headline rule (section 1, 6.3) -- price must be materially below reference.
  CHECK (price_cents < reference_cents)
);
CREATE UNIQUE INDEX IF NOT EXISTS deals_one_open_per_offer_rule ON deals (offer_id, rule)
  WHERE status IN ('CANDIDATE', 'CONFIRMING', 'ACTIVE', 'HELD_REVIEW');
CREATE INDEX IF NOT EXISTS deals_offer_idx ON deals (offer_id);
CREATE INDEX IF NOT EXISTS deals_status_idx ON deals (status);
CREATE INDEX IF NOT EXISTS deals_active_lane_idx ON deals (lane, discount_pct DESC) WHERE status = 'ACTIVE';
CREATE INDEX IF NOT EXISTS deals_held_review_idx ON deals (detected_at) WHERE status = 'HELD_REVIEW';

CREATE OR REPLACE TRIGGER trg_deals_updated_at
  BEFORE UPDATE ON deals FOR EACH ROW EXECUTE FUNCTION fpt_set_updated_at();

-- Deferred FK: crawl_tasks.deal_id was created as a plain bigint in 006_queue_execution because
-- deals did not exist yet (deals depends on price_observations, which depends on crawl_tasks).
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint WHERE conname = 'crawl_tasks_deal_id_fkey'
  ) THEN
    ALTER TABLE crawl_tasks
      ADD CONSTRAINT crawl_tasks_deal_id_fkey
      FOREIGN KEY (deal_id) REFERENCES deals(id);
  END IF;
END;
$$;
