-- 001_extensions.up.sql
-- Extensions required by the fishing-price-tracker schema.
-- Idempotent: CREATE EXTENSION IF NOT EXISTS is safe to re-run.

CREATE EXTENSION IF NOT EXISTS pgcrypto;   -- gen_random_bytes / digest, used by fpt_generate_ulid() and token hashing
CREATE EXTENSION IF NOT EXISTS citext;     -- case-insensitive email column on subscribers
CREATE EXTENSION IF NOT EXISTS pg_trgm;    -- fuzzy brand/model matching (section 8, rule 4)

-- fpt_generate_ulid(): time-prefixed, random-suffixed opaque id used as the DEFAULT for every
-- public pub_id column. DECISION (see db/README.md): this is NOT a canonical Crockford-Base32
-- ULID (Postgres has no built-in base32 encoder without an extra extension). It is a 22-hex-char
-- string with a millisecond-timestamp prefix (sortable, effectively unique) and a fixed 2-char
-- domain prefix is applied per-table (e.g. 'pr_', 'vr_', 'dl_'). Swap for a real ULID library at
-- the application layer later if the export/API contract requires the exact ULID text format.
CREATE OR REPLACE FUNCTION fpt_generate_ulid() RETURNS text
LANGUAGE sql
AS $$
  SELECT lower(
    encode(int8send(floor(extract(epoch FROM clock_timestamp()) * 1000)::bigint), 'hex')
    || encode(gen_random_bytes(8), 'hex')
  );
$$;
