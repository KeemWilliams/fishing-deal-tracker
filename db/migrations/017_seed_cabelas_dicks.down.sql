-- 017_seed_cabelas_dicks.down.sql
DELETE FROM discovery_pages WHERE retailer_id IN (
  SELECT id FROM retailers WHERE slug IN ('cabelas', 'dicks')
);
DELETE FROM retailers WHERE slug IN ('cabelas', 'dicks');
