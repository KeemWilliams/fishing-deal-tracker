-- 005_listings_offers.up.sql
-- Listings (retailer SKU = one variant at one retailer) and offers (listing x condition x seller).
-- The offer is the core unit of price per the architecture's executive summary.

CREATE TABLE IF NOT EXISTS listings (
  id bigserial PRIMARY KEY,
  retailer_id smallint NOT NULL REFERENCES retailers(id),
  tracked_url_id bigint REFERENCES tracked_urls(id),   -- NULL for data API listings (Phase 2)
  retailer_sku text NOT NULL,                          -- ASIN for Amazon (Phase 2)
  retailer_product_code text,
  url text NOT NULL,
  title_raw text NOT NULL,
  brand_raw text,
  model_raw text,
  variant_label_raw text NOT NULL,
  attributes_raw jsonb NOT NULL,
  attributes_norm jsonb NOT NULL,                      -- normalizer output; compared on every observation
  variant_key_observed text,                           -- NULL if a required attribute is missing
  gtin14 char(14)[] NOT NULL DEFAULT '{}',
  mpn_raw text,
  unit_count int,
  variant_id bigint REFERENCES product_variants(id),
  match_status text NOT NULL DEFAULT 'unmatched' CHECK (match_status IN
    ('unmatched', 'auto_gtin', 'auto_mpn', 'auto_attributes', 'auto_created', 'manual', 'needs_review', 'rejected')),
  match_confidence numeric(3, 2) CHECK (match_confidence IS NULL OR (match_confidence >= 0 AND match_confidence <= 1)),
  matched_at timestamptz,
  is_active boolean NOT NULL DEFAULT true,
  consecutive_absent smallint NOT NULL DEFAULT 0,
  first_seen_at timestamptz NOT NULL DEFAULT now(),
  last_seen_at timestamptz NOT NULL DEFAULT now(),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (retailer_id, retailer_sku),
  -- Bad-data guard: a matched listing must actually carry a match reason and vice versa.
  CHECK ((match_status = 'unmatched') = (variant_id IS NULL) OR match_status IN ('needs_review', 'rejected'))
);
CREATE INDEX IF NOT EXISTS listings_variant_idx ON listings (variant_id);
CREATE INDEX IF NOT EXISTS listings_tracked_url_idx ON listings (tracked_url_id);
CREATE INDEX IF NOT EXISTS listings_gtin_gin ON listings USING gin (gtin14);
CREATE INDEX IF NOT EXISTS listings_needs_review_idx ON listings (match_status) WHERE match_status = 'needs_review';

CREATE TABLE IF NOT EXISTS offers (
  id bigserial PRIMARY KEY,
  listing_id bigint NOT NULL REFERENCES listings(id),
  seller_id bigint NOT NULL REFERENCES sellers(id),
  offer_key text NOT NULL,                             -- source offer id, else f"{seller_key}|{condition}"
  condition text NOT NULL CHECK (condition IN
    ('NEW', 'NEW_OPEN_BOX', 'REFURBISHED', 'USED_LIKE_NEW', 'USED_VERY_GOOD', 'USED_GOOD',
     'USED_ACCEPTABLE', 'USED_UNGRADED')),
  condition_group text NOT NULL CHECK (condition_group IN ('NEW', 'OPEN_BOX', 'REFURB', 'USED')),
  condition_raw text,
  is_active boolean NOT NULL DEFAULT true,
  first_seen_at timestamptz NOT NULL DEFAULT now(),
  last_seen_at timestamptz NOT NULL DEFAULT now(),
  vanished_at timestamptz,                             -- set when absent from a successful fetch of its listing
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (listing_id, offer_key),
  -- Bad-data guard: condition -> condition_group mapping must match the adapter contract's
  -- CONDITION_GROUP table (section 3.1). A row that violates this is a normalizer bug, not data.
  CHECK (
    (condition = 'NEW' AND condition_group = 'NEW') OR
    (condition = 'NEW_OPEN_BOX' AND condition_group = 'OPEN_BOX') OR
    (condition = 'REFURBISHED' AND condition_group = 'REFURB') OR
    (condition IN ('USED_LIKE_NEW', 'USED_VERY_GOOD', 'USED_GOOD', 'USED_ACCEPTABLE', 'USED_UNGRADED')
      AND condition_group = 'USED')
  )
);
CREATE INDEX IF NOT EXISTS offers_listing_group_idx ON offers (listing_id, condition_group) WHERE is_active;
CREATE INDEX IF NOT EXISTS offers_seller_idx ON offers (seller_id);
CREATE INDEX IF NOT EXISTS offers_active_idx ON offers (listing_id) WHERE is_active;

CREATE TABLE IF NOT EXISTS match_candidates (
  listing_id bigint NOT NULL REFERENCES listings(id),
  variant_id bigint NOT NULL REFERENCES product_variants(id),
  score numeric(3, 2) NOT NULL CHECK (score >= 0 AND score <= 1),
  reason text NOT NULL CHECK (reason IN ('gtin_attr_conflict', 'multiple_exact', 'fuzzy_model', 'unit_count_conflict')),
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (listing_id, variant_id)
);
CREATE INDEX IF NOT EXISTS match_candidates_variant_idx ON match_candidates (variant_id);

CREATE OR REPLACE TRIGGER trg_listings_updated_at
  BEFORE UPDATE ON listings FOR EACH ROW EXECUTE FUNCTION fpt_set_updated_at();
CREATE OR REPLACE TRIGGER trg_offers_updated_at
  BEFORE UPDATE ON offers FOR EACH ROW EXECUTE FUNCTION fpt_set_updated_at();
