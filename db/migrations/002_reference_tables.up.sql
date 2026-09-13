-- 002_reference_tables.up.sql
-- Brands and retailers: the reference data every other table hangs off.

CREATE TABLE IF NOT EXISTS brands (
  id serial PRIMARY KEY,
  name text NOT NULL,
  name_key text UNIQUE NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS brand_aliases (
  alias_key text PRIMARY KEY,
  brand_id int NOT NULL REFERENCES brands(id),
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS brand_aliases_brand_idx ON brand_aliases (brand_id);

CREATE TABLE IF NOT EXISTS retailers (
  id smallserial PRIMARY KEY,
  slug text UNIQUE NOT NULL,
  name text NOT NULL,
  base_url text NOT NULL,
  adapter_slug text NOT NULL,
  adapter_kind text NOT NULL CHECK (adapter_kind IN ('page_scraper', 'data_api')),
  enabled boolean NOT NULL DEFAULT true,
  disabled_reason text,
  policy jsonb NOT NULL,                 -- synced from config/retailers.yaml
  egress jsonb NOT NULL,                 -- synced from config/retailers.yaml
  breaker_open_until timestamptz,
  robots_txt_sha256 text,
  robots_checked_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS sellers (
  id bigserial PRIMARY KEY,
  retailer_id smallint NOT NULL REFERENCES retailers(id),
  seller_key text NOT NULL,
  name text,
  seller_type text NOT NULL CHECK (seller_type IN ('FIRST_PARTY', 'RETAILER_RESALE', 'MARKETPLACE_3P')),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (retailer_id, seller_key)
);

-- updated_at maintenance: one trigger function, reused by every table with an updated_at column.
CREATE OR REPLACE FUNCTION fpt_set_updated_at() RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  NEW.updated_at := now();
  RETURN NEW;
END;
$$;

CREATE OR REPLACE TRIGGER trg_brands_updated_at
  BEFORE UPDATE ON brands FOR EACH ROW EXECUTE FUNCTION fpt_set_updated_at();
CREATE OR REPLACE TRIGGER trg_retailers_updated_at
  BEFORE UPDATE ON retailers FOR EACH ROW EXECUTE FUNCTION fpt_set_updated_at();
CREATE OR REPLACE TRIGGER trg_sellers_updated_at
  BEFORE UPDATE ON sellers FOR EACH ROW EXECUTE FUNCTION fpt_set_updated_at();
