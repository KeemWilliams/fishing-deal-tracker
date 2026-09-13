-- 005_listings_offers.down.sql
DROP TRIGGER IF EXISTS trg_offers_updated_at ON offers;
DROP TRIGGER IF EXISTS trg_listings_updated_at ON listings;
DROP TABLE IF EXISTS match_candidates;
DROP TABLE IF EXISTS offers;
DROP TABLE IF EXISTS listings;
