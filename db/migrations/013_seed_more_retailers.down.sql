-- 013_seed_more_retailers.down.sql
DELETE FROM discovery_pages WHERE retailer_id IN (
  SELECT id FROM retailers WHERE slug IN ('fishusa', 'tackledirect', 'alltackle')
);
DELETE FROM retailers WHERE slug IN ('fishusa', 'tackledirect', 'alltackle');
