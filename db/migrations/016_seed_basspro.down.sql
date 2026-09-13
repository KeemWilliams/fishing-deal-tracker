-- 016_seed_basspro.down.sql
DELETE FROM discovery_pages WHERE retailer_id IN (
  SELECT id FROM retailers WHERE slug = 'basspro'
);
DELETE FROM retailers WHERE slug = 'basspro';
