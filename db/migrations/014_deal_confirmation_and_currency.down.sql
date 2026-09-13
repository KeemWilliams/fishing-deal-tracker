-- 014_deal_confirmation_and_currency.down.sql
ALTER TABLE price_observations DROP CONSTRAINT IF EXISTS price_observations_currency_format;
ALTER TABLE price_observations DROP COLUMN IF EXISTS currency;
ALTER TABLE deals DROP CONSTRAINT IF EXISTS deals_confirmed_status_requires_observation;
