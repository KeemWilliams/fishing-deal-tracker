-- 019_seed_big_outdoor_retailers.down.sql
DELETE FROM discovery_pages WHERE retailer_id IN (
  SELECT id FROM retailers WHERE slug IN (
    'sportsmans_warehouse', 'sportsmans_guide'
  )
);
DELETE FROM retailers WHERE slug IN (
  'sportsmans_warehouse', 'sportsmans_guide'
);
