-- 014_deal_confirmation_and_currency.up.sql
-- TEST-phase follow-up fixes.
--
-- (1) MEDIUM defect: a deal could reach a "confirmed" status with confirming_observation_id
-- still NULL. Per section 6.4, ACTIVE and HELD_REVIEW are only reachable after a CONFIRM
-- observation passes/partially-passes checks C1-C10, and EXPIRED (section 6.3 lifecycle) is only
-- reachable FROM ACTIVE -- so all three require a non-null confirming_observation_id. CANDIDATE
-- and CONFIRMING precede confirmation by definition. REJECTED is deliberately excluded: a
-- candidate can be REJECTED without ever getting a confirming observation (e.g.
-- `confirm_deadline_missed` when the CONFIRM task itself never got fetched, or
-- `retailer_blocked` per section 5.4) -- requiring confirming_observation_id there would make a
-- real, documented rejection path impossible to record.
--
-- Added NOT VALID then VALIDATE so an existing table with rows is never blocked mid-ALTER by a
-- lock-holding full-table check; VALIDATE takes only a SHARE UPDATE EXCLUSIVE lock and can run
-- against live data. Guarded by a pg_constraint existence check for idempotency (ADD CONSTRAINT
-- has no IF NOT EXISTS clause in Postgres).
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint WHERE conname = 'deals_confirmed_status_requires_observation'
  ) THEN
    ALTER TABLE deals
      ADD CONSTRAINT deals_confirmed_status_requires_observation
      CHECK (status NOT IN ('ACTIVE', 'HELD_REVIEW', 'EXPIRED') OR confirming_observation_id IS NOT NULL)
      NOT VALID;
  END IF;
END;
$$;
ALTER TABLE deals VALIDATE CONSTRAINT deals_confirmed_status_requires_observation;

-- (2) LOW defect: price_observations validates currency in the pipeline (section 6.2's "currency
-- not USD -> REJECTED" guard) but never stored it. Add the column with a safe default so the
-- append-only table's existing rows (all historically USD-only per assumption A5) remain valid
-- without a row-by-row backfill; ADD COLUMN with a constant DEFAULT is a metadata-only change in
-- PG11+ (no table rewrite) and is DDL, not a DML UPDATE, so it does not trip the
-- price_observations_no_mutate() append-only trigger (that trigger only fires on UPDATE/DELETE
-- statements against existing rows, section 007).
ALTER TABLE price_observations ADD COLUMN IF NOT EXISTS currency char(3) NOT NULL DEFAULT 'USD';

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint WHERE conname = 'price_observations_currency_format'
  ) THEN
    ALTER TABLE price_observations
      ADD CONSTRAINT price_observations_currency_format
      CHECK (currency ~ '^[A-Z]{3}$')
      NOT VALID;
  END IF;
END;
$$;
ALTER TABLE price_observations VALIDATE CONSTRAINT price_observations_currency_format;
