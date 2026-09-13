-- 003_catalog.down.sql
DROP TRIGGER IF EXISTS trg_product_variants_updated_at ON product_variants;
DROP TRIGGER IF EXISTS trg_products_updated_at ON products;
DROP TABLE IF EXISTS product_variants;
DROP TABLE IF EXISTS product_slug_redirects;
DROP TABLE IF EXISTS products;
