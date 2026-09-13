-- 007_price_observations.down.sql
DROP TRIGGER IF EXISTS trg_price_observations_no_delete ON price_observations;
DROP TRIGGER IF EXISTS trg_price_observations_no_update ON price_observations;
DROP FUNCTION IF EXISTS price_observations_no_mutate();
DROP TABLE IF EXISTS price_observations;
