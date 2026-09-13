-- 001_extensions.down.sql
DROP FUNCTION IF EXISTS fpt_generate_ulid();
-- Extensions are left in place on down-migration: other databases on the same cluster may use
-- them and dropping is not reversible-safe without checking dependents. Drop manually if needed:
--   DROP EXTENSION IF EXISTS pg_trgm;
--   DROP EXTENSION IF EXISTS citext;
--   DROP EXTENSION IF EXISTS pgcrypto;
