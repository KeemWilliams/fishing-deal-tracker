-- 012_seed_retailers.down.sql
DELETE FROM discovery_pages WHERE retailer_id IN (
  SELECT id FROM retailers WHERE slug IN ('tackle_warehouse', 'academy', 'jandh')
);
DELETE FROM retailers WHERE slug IN ('tackle_warehouse', 'academy', 'jandh');
