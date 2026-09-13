-- 002_reference_tables.down.sql
DROP TRIGGER IF EXISTS trg_sellers_updated_at ON sellers;
DROP TRIGGER IF EXISTS trg_retailers_updated_at ON retailers;
DROP TRIGGER IF EXISTS trg_brands_updated_at ON brands;
DROP FUNCTION IF EXISTS fpt_set_updated_at();
DROP TABLE IF EXISTS sellers;
DROP TABLE IF EXISTS retailers;
DROP TABLE IF EXISTS brand_aliases;
DROP TABLE IF EXISTS brands;
