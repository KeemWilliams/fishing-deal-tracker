-- 015_seed_shopify_collection_retailers.down.sql
DELETE FROM discovery_pages WHERE retailer_id IN (
  SELECT id FROM retailers WHERE slug IN ('fishingonline', 'discounttackle', 'rodlocker')
);
DELETE FROM retailers WHERE slug IN ('fishingonline', 'discounttackle', 'rodlocker');
