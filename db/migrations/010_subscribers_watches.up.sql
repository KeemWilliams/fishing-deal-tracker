-- 010_subscribers_watches.up.sql
-- Subscribers and watches (section 3.5, 5.3, 6.5). Stubbed for the MVP per the brief: schema
-- ships now, the `fpt api` process and notifier are separate later work.

CREATE TABLE IF NOT EXISTS subscribers (
  id bigserial PRIMARY KEY,
  pub_id text UNIQUE NOT NULL DEFAULT ('sb_' || fpt_generate_ulid()),
  email citext UNIQUE,
  email_sha256 char(64) UNIQUE NOT NULL,
  status text NOT NULL CHECK (status IN ('PENDING', 'ACTIVE', 'UNSUBSCRIBED', 'BOUNCED', 'COMPLAINED')),
  unsubscribe_token_hash char(64) UNIQUE NOT NULL,
  consent_source text NOT NULL,
  consent_text_version text NOT NULL,
  confirmed_at timestamptz,
  unsubscribed_at timestamptz,
  account_id bigint,   -- future accounts / paid tier; no accounts table in MVP
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  -- Bad-data guard: email is NULLed 30 days after unsubscribe (section 9 PII rule) but
  -- email_sha256 is retained for suppression, so email_sha256 must always be a real hash.
  CHECK (email_sha256 ~ '^[0-9a-f]{64}$')
);
CREATE INDEX IF NOT EXISTS subscribers_status_idx ON subscribers (status);

CREATE TABLE IF NOT EXISTS watches (
  id bigserial PRIMARY KEY,
  pub_id text UNIQUE NOT NULL DEFAULT ('wt_' || fpt_generate_ulid()),
  subscriber_id bigint NOT NULL REFERENCES subscribers(id),
  variant_id bigint NOT NULL REFERENCES product_variants(id),
  target_price_cents int NOT NULL CHECK (target_price_cents > 0),
  condition_filter text NOT NULL CHECK (condition_filter IN ('NEW_ONLY', 'NEW_OR_USED', 'USED_ONLY')),
  min_used_condition text CHECK (min_used_condition IS NULL OR min_used_condition IN
    ('NEW', 'NEW_OPEN_BOX', 'REFURBISHED', 'USED_LIKE_NEW', 'USED_VERY_GOOD', 'USED_GOOD',
     'USED_ACCEPTABLE', 'USED_UNGRADED')),
  notify_back_in_stock boolean NOT NULL DEFAULT false,
  status text NOT NULL CHECK (status IN ('PENDING', 'ACTIVE', 'CANCELLED', 'EXPIRED')),
  confirm_token_hash char(64) UNIQUE,
  confirm_expires_at timestamptz,
  cancel_token_hash char(64) UNIQUE NOT NULL,
  armed boolean NOT NULL DEFAULT true,
  last_alert_price_cents int CHECK (last_alert_price_cents IS NULL OR last_alert_price_cents > 0),
  last_alert_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  confirmed_at timestamptz,
  updated_at timestamptz NOT NULL DEFAULT now(),
  CHECK ((condition_filter = 'NEW_ONLY') = (min_used_condition IS NULL)),
  CHECK (confirm_token_hash IS NULL OR confirm_token_hash ~ '^[0-9a-f]{64}$'),
  CHECK (cancel_token_hash ~ '^[0-9a-f]{64}$')
);
CREATE INDEX IF NOT EXISTS watches_active_variant_idx ON watches (variant_id) WHERE status = 'ACTIVE';
CREATE INDEX IF NOT EXISTS watches_subscriber_idx ON watches (subscriber_id);
CREATE INDEX IF NOT EXISTS watches_pending_expiry_idx ON watches (confirm_expires_at) WHERE status = 'PENDING';

CREATE TABLE IF NOT EXISTS alert_deliveries (
  id bigserial PRIMARY KEY,
  subscriber_id bigint NOT NULL REFERENCES subscribers(id),
  mode text NOT NULL CHECK (mode IN ('INSTANT', 'DIGEST')),
  send_date_et date NOT NULL,
  outcome text NOT NULL DEFAULT 'CLAIMED' CHECK (outcome IN ('CLAIMED', 'SENT', 'FAILED', 'SUPPRESSED')),
  provider_message_id text,
  error text,
  claimed_at timestamptz NOT NULL DEFAULT now(),
  sent_at timestamptz
);
CREATE UNIQUE INDEX IF NOT EXISTS alert_deliveries_one_digest ON alert_deliveries (subscriber_id, send_date_et)
  WHERE mode = 'DIGEST';
CREATE INDEX IF NOT EXISTS alert_deliveries_subscriber_idx ON alert_deliveries (subscriber_id, send_date_et DESC);
CREATE INDEX IF NOT EXISTS alert_deliveries_instant_cap_idx ON alert_deliveries (subscriber_id, claimed_at)
  WHERE mode = 'INSTANT';

CREATE TABLE IF NOT EXISTS alert_items (
  delivery_id bigint NOT NULL REFERENCES alert_deliveries(id),
  watch_id bigint NOT NULL REFERENCES watches(id),
  offer_id bigint NOT NULL REFERENCES offers(id),
  observation_id bigint NOT NULL REFERENCES price_observations(id),
  reason text NOT NULL CHECK (reason IN ('UNDER_TARGET', 'FURTHER_DROP', 'BACK_IN_STOCK')),
  price_cents int NOT NULL CHECK (price_cents > 0),
  condition text NOT NULL,
  PRIMARY KEY (watch_id, observation_id)
);
CREATE INDEX IF NOT EXISTS alert_items_delivery_idx ON alert_items (delivery_id);
CREATE INDEX IF NOT EXISTS alert_items_offer_idx ON alert_items (offer_id);

CREATE TABLE IF NOT EXISTS api_tokens_used (
  token_hash char(64) PRIMARY KEY CHECK (token_hash ~ '^[0-9a-f]{64}$'),
  used_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS api_tokens_used_used_at_idx ON api_tokens_used (used_at);

CREATE OR REPLACE TRIGGER trg_subscribers_updated_at
  BEFORE UPDATE ON subscribers FOR EACH ROW EXECUTE FUNCTION fpt_set_updated_at();
CREATE OR REPLACE TRIGGER trg_watches_updated_at
  BEFORE UPDATE ON watches FOR EACH ROW EXECUTE FUNCTION fpt_set_updated_at();
