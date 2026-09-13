-- 018_seed_manufacturer_brand_sites.down.sql
DELETE FROM discovery_pages WHERE retailer_id IN (
  SELECT id FROM retailers WHERE slug IN (
    'abugarcia', 'penn', 'pflueger', 'uglystik', 'berkley',
    'shimano', 'gloomis', 'powerpro', 'jackall'
  )
);
DELETE FROM retailers WHERE slug IN (
  'abugarcia', 'penn', 'pflueger', 'uglystik', 'berkley',
  'shimano', 'gloomis', 'powerpro', 'jackall'
);
