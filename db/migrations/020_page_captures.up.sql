-- 020_page_captures.up.sql
-- Personal-use browser-capture path (see knowledge/architecture/fishing-price-tracker-mvp-
-- architecture.md and knowledge/architecture/fishing-deal-quality-ai-scoring.md): a human
-- viewing a page in their own browser is not our scraper and is not subject to the same
-- robots.txt admission rule as fpt's own fetchers (section 7.1 of the architecture doc governs
-- OUR crawling; it says nothing about what a person may transcribe from a page they are looking
-- at). This table lets Wakeem's browser extension record what he sees on sites we deliberately do
-- NOT auto-scrape (e.g. Scheels, whose robots.txt disallows its product-data endpoints for our
-- identified UA) or any JS-heavy site the scraper can't reach.
--
-- CRITICAL DISTINCTION (do not weaken this later without a new ADR): a page_captures row is a
-- CLAIM, never a price_observation. price_observations (007_price_observations) is populated
-- ONLY by fpt's own fetchers going through the adapter/validator pipeline with an identity check
-- and a snapshot_ref; it is append-only and confirmed-observation-grade. A capture has none of
-- that: no second independent fetch, no adapter contract, no snapshot, and it self-reports its
-- own retailer. It can surface a deal CANDIDATE for human review but must never be treated as our
-- own confirmed observation and must never itself back a VERIFIED-lane deal (see the deals table
-- CHECK on reference_kind in 008_deals.up.sql -- no PAGE_CAPTURE value exists there by design).
--
-- This table is intentionally separate from listings/offers/price_observations rather than
-- shoehorned into them: those tables assume an adapter_version, a crawl_task, and a validated
-- variant_key_observed, none of which a capture has. `listing_id`/`variant_id` are nullable
-- forward-looking link columns for the pipeline-integration seam described in the ingest task
-- (see fpt/store/captures.py module docstring for the TODO on how a capture graduates into a
-- reviewed candidate); nothing in this migration or in fpt/ writes them yet.

CREATE TABLE page_captures (
  id bigserial PRIMARY KEY,
  pub_id text UNIQUE NOT NULL DEFAULT ('cap_' || fpt_generate_ulid()),
  source text NOT NULL DEFAULT 'PAGE_CAPTURE' CHECK (source = 'PAGE_CAPTURE'),

  -- Where this came from. `host` is ALWAYS derived server-side from `product_url` (see
  -- fpt/capture/validate.py) -- the extension's self-reported `retailer_hint` is kept for
  -- display/audit only and must never be trusted for identity or linking decisions.
  host text NOT NULL,
  retailer_hint text,
  product_url text NOT NULL,

  title_raw text NOT NULL,
  variant_label_raw text,
  condition text NOT NULL DEFAULT 'NEW' CHECK (condition IN (
    'NEW','NEW_OPEN_BOX','REFURBISHED','USED_LIKE_NEW','USED_VERY_GOOD','USED_GOOD',
    'USED_ACCEPTABLE','USED_UNGRADED'
  )),

  currency text NOT NULL DEFAULT 'USD',
  current_price_cents int NOT NULL CHECK (current_price_cents > 0),
  -- The page's own "was"/strikethrough price, if any -- this is a CLAIM about a claim, one level
  -- further from ground truth than offers.claimed_reference_cents (which at least comes from our
  -- own fetcher). Never required; when present it must be a genuine markdown.
  was_price_cents int CHECK (was_price_cents IS NULL OR was_price_cents > current_price_cents),

  captured_at timestamptz NOT NULL,      -- when the human viewed the page (extension-reported, clamped server-side)
  received_at timestamptz NOT NULL DEFAULT now(),

  -- Structurally impossible to double-insert the same capture: the ingest endpoint derives this
  -- deterministically (see fpt/capture/validate.py) when the extension doesn't supply one, so a
  -- page reload or a retried POST from the extension's background worker is a no-op, not a
  -- duplicate row.
  idempotency_key text NOT NULL,

  raw jsonb NOT NULL DEFAULT '{}'::jsonb,   -- sanitized extractor output, for audit/debugging only

  status text NOT NULL DEFAULT 'PENDING_REVIEW' CHECK (status IN ('PENDING_REVIEW','LINKED','IGNORED')),
  -- TODO(pipeline seam, not implemented by this task): once a capture is manually or
  -- automatically matched to a canonical variant, set listing_id/variant_id and status='LINKED'.
  -- Nothing in fpt/ writes these columns yet -- see fpt/store/captures.py.
  listing_id bigint REFERENCES listings(id),
  variant_id bigint REFERENCES product_variants(id),

  created_at timestamptz NOT NULL DEFAULT now(),

  UNIQUE (idempotency_key)
);

CREATE INDEX page_captures_host_idx ON page_captures (host, captured_at DESC);
CREATE INDEX page_captures_status_idx ON page_captures (status) WHERE status = 'PENDING_REVIEW';
CREATE INDEX page_captures_product_url_idx ON page_captures (product_url);

-- Least-privilege: the capture ingest process (fpt/capture/server.py) only ever needs to insert
-- and read back what it just inserted -- same role used by the (not-yet-built) public fpt api
-- process per 011_roles_grants.up.sql, since both are internet/LAN-facing intake surfaces with
-- the same trust level (untrusted caller, validated input, no update/delete authority).
GRANT SELECT, INSERT ON page_captures TO fpt_api, fpt_job;
GRANT USAGE, SELECT ON page_captures_id_seq TO fpt_api, fpt_job;
