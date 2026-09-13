-- 003_catalog.up.sql
-- Canonical product catalog: products and their variants. Additive/conservative per section 8 --
-- unmatched listings still get an auto_created product+variant.

CREATE TABLE IF NOT EXISTS products (
  id bigserial PRIMARY KEY,
  pub_id text UNIQUE NOT NULL DEFAULT ('pr_' || fpt_generate_ulid()),
  slug text UNIQUE NOT NULL,
  brand_id int NOT NULL REFERENCES brands(id),
  name text NOT NULL,
  model_key text NOT NULL,
  category text NOT NULL CHECK (category IN
    ('rod', 'reel', 'line', 'soft_bait', 'hard_bait', 'terminal', 'combo', 'other')),
  origin text NOT NULL CHECK (origin IN ('auto_created', 'manual')),
  review_status text NOT NULL DEFAULT 'unreviewed' CHECK (review_status IN ('unreviewed', 'reviewed')),
  merged_into_id bigint REFERENCES products(id),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (brand_id, category, model_key)
);
CREATE INDEX IF NOT EXISTS products_merged_into_idx ON products (merged_into_id) WHERE merged_into_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS product_slug_redirects (
  old_slug text PRIMARY KEY,
  product_id bigint NOT NULL REFERENCES products(id),
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS product_slug_redirects_product_idx ON product_slug_redirects (product_id);

CREATE TABLE IF NOT EXISTS product_variants (
  id bigserial PRIMARY KEY,
  pub_id text UNIQUE NOT NULL DEFAULT ('vr_' || fpt_generate_ulid()),
  product_id bigint NOT NULL REFERENCES products(id),
  gtin14 char(14)[] NOT NULL DEFAULT '{}',   -- checksum-valid barcodes only; a variant can carry several
  mpn text,
  attributes jsonb NOT NULL,
  variant_key text NOT NULL,
  label text NOT NULL,
  unit_count int,                             -- pack count for baits/terminal
  merged_into_id bigint REFERENCES product_variants(id),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (product_id, variant_key)
);
CREATE INDEX IF NOT EXISTS product_variants_gtin_gin ON product_variants USING gin (gtin14);
CREATE INDEX IF NOT EXISTS product_variants_product_idx ON product_variants (product_id);
CREATE INDEX IF NOT EXISTS product_variants_merged_into_idx ON product_variants (merged_into_id) WHERE merged_into_id IS NOT NULL;
-- Fuzzy brand/model matching (section 8, rule 4): trigram index on the variant label.
CREATE INDEX IF NOT EXISTS product_variants_label_trgm ON product_variants USING gin (label gin_trgm_ops);

CREATE OR REPLACE TRIGGER trg_products_updated_at
  BEFORE UPDATE ON products FOR EACH ROW EXECUTE FUNCTION fpt_set_updated_at();
CREATE OR REPLACE TRIGGER trg_product_variants_updated_at
  BEFORE UPDATE ON product_variants FOR EACH ROW EXECUTE FUNCTION fpt_set_updated_at();
