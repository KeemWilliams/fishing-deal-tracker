# Fishing Price Tracker: Database

Postgres 16 schema for the fishing-gear deep-discount tracker. Implements the data model in
[fishing-price-tracker-mvp-architecture.md](../../../knowledge/architecture/fishing-price-tracker-mvp-architecture.md)
(sections 4, 6, and 9).

## Layout

```
db/
  migrations/
    NNN_name.up.sql     -- forward migration, safe to re-run (idempotent)
    NNN_name.down.sql   -- rollback, drops what .up.sql created
  README.md              -- this file
```

Migrations are plain SQL, applied in filename order (`001_` ... `015_`, `013_` reserved for a
retailer-seed migration landing in parallel from another coder). No migration framework
is assumed yet; apply with `psql` directly:

```bash
for f in db/migrations/*.up.sql; do psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f "$f"; done
```

Rollback in reverse order:

```bash
for f in $(ls db/migrations/*.down.sql | sort -r); do psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f "$f"; done
```

If the backend coders adopt a migration tool (dbmate, golang-migrate, sqlx, node-pg-migrate,
Alembic-style), these files are already split into matching up/down pairs and can be renamed to
fit that tool's convention with no content changes.

## Migration order and contents

| File | Contents |
|---|---|
| 001_extensions | `pgcrypto`, `citext`, `pg_trgm`; `fpt_generate_ulid()` |
| 002_reference_tables | `brands`, `brand_aliases`, `retailers`, `sellers`; `fpt_set_updated_at()` |
| 003_catalog | `products`, `product_slug_redirects`, `product_variants` |
| 004_discovery | `discovery_pages`, `tracked_urls`, `discovery_hits` |
| 005_listings_offers | `listings`, `offers`, `match_candidates` |
| 006_queue_execution | `crawl_tasks`, `tick_runs`, `fetch_log`, `retailer_health_hourly`; backfills the `discovery_hits.crawl_task_id` FK |
| 007_price_observations | `price_observations` (append-only) |
| 008_deals | `deals`; backfills the `crawl_tasks.deal_id` FK |
| 009_views | Materialized views: `offer_daily_price`, `offer_reference_stats`, `variant_current_new`; `refresh_fpt_views()` |
| 010_subscribers_watches | `subscribers`, `watches`, `alert_deliveries`, `alert_items`, `api_tokens_used` |
| 011_roles_grants | `fpt_job`, `fpt_api` roles; least-privilege grants; `promote_variant_hot()` |
| 012_seed_retailers | Seeds Tackle Warehouse, Academy Sports, J&H Tackle + their discovery pages |
| 013_seed_more_retailers | Reserved: landing in parallel from another coder (not authored here) |
| 014_deal_confirmation_and_currency | TEST-phase fix: `deals` CHECK requiring `confirming_observation_id` on confirmed statuses; adds `price_observations.currency` |
| 015_seed_shopify_collection_retailers | Seeds Fishing Online, Discount Tackle, Rod Locker + their discovery pages (each retailer's own sale/clearance collection `products.json` endpoint) |

Two tables have a column whose foreign key is added by a *later* migration rather than declared
inline, because the two tables reference each other's future dependents in a cycle that can't be
satisfied by creation order alone:

- `discovery_hits.crawl_task_id` -> FK added in `006_queue_execution.up.sql` (crawl_tasks doesn't
  exist when discovery_hits is created in `004_discovery`).
- `crawl_tasks.deal_id` -> FK added in `008_deals.up.sql` (deals depends on price_observations,
  which depends on crawl_tasks, which needs to reference deals).

This matches the ordering implied by the architecture doc's own SQL sketch in section 4.1, which
leaves both of these columns as a plain `bigint` with no inline `REFERENCES`.

## Decisions

Column names, types, and constraints follow the architecture doc's section 4.1 SQL verbatim
wherever it was given. Where the doc was silent or intentionally left something for Code phase,
the following calls were made:

1. **`updated_at` added to every table.** The architecture doc's SQL sketch only specifies
   `created_at` on most tables. Per the house standard (every table gets `id`, `created_at`,
   `updated_at`), every table in this schema also has `updated_at timestamptz NOT NULL DEFAULT
   now()`, maintained by a shared `fpt_set_updated_at()` trigger. `price_observations` is the one
   exception: it is append-only by design (section 6, "nothing is ever re-classified in place"),
   so it has no `updated_at` and a trigger actively blocks UPDATE/DELETE.

2. **`pub_id` generation.** The doc specifies "opaque ULID-based `pub_id` for anything public"
   but doesn't give a generation strategy. Postgres has no built-in ULID or Base32 encoder.
   `fpt_generate_ulid()` (in `001_extensions`) produces a 22-hex-character, millisecond-timestamp-
   prefixed random string set as the column `DEFAULT` (e.g. `products.pub_id DEFAULT 'pr_' ||
   fpt_generate_ulid()`). It is sortable and effectively unique, but it is **not** a canonical
   Crockford-Base32 ULID. If the export/API contract in section 3.4/3.5 ends up depending on the
   exact ULID text format shown in the doc's examples (`dl_01J8Z...`), swap this for a real ULID
   library called from the application layer before publishing `pub_id` values externally.

3. **DB roles are created without login/password.** Section 9 specifies `fpt_owner`, `fpt_job`,
   `fpt_api` with specific grants. `011_roles_grants` creates `fpt_job` and `fpt_api` as `NOLOGIN`
   placeholders and applies the grants; it does **not** set passwords or `LOGIN`, and does not
   create `fpt_owner` at all (that's assumed to be whichever role actually runs the migrations,
   e.g. the Coolify Postgres superuser). No secrets belong in a migration file that's checked
   into git. Ops/devops-engineer runs `ALTER ROLE fpt_job LOGIN PASSWORD '<from Infisical>'` (and
   the same for `fpt_api`) once per environment, sourcing the password from
   `FPT_DATABASE_URL_JOB` / `FPT_DATABASE_URL_API` per the Infisical paths in section 9.

4. **Extra CHECK constraints beyond section 6.2's guard table**, added as defense-in-depth for
   "a parse error looks exactly like a 90% discount" (section 1, driver 2):
   - `offers`: a CHECK that `condition` and `condition_group` are a valid pair per the adapter
     contract's `CONDITION_GROUP` mapping (section 3.1) -- catches a normalizer bug writing a
     mismatched pair, not just bad retailer data.
   - `deals`: `price_cents < reference_cents` -- a deal can never claim a discount off a reference
     equal to or below its own price.
   - `listings`: a CHECK tying `match_status = 'unmatched'` to `variant_id IS NULL` (except for
     `needs_review`/`rejected`, which are legitimately unmatched-with-a-reason).
   - Positive-amount CHECKs (`> 0`) added everywhere the doc implies a price/cents column is
     always positive when present, matching the `price_observations` guard already specified
     (`quality = 'REJECTED' OR price_cents > 0`).

5. **`price_observations` append-only is enforced twice**: a `REVOKE`-based grant (only
   `fpt_owner` can UPDATE/DELETE, and `011_roles_grants` never grants that to `fpt_job`) plus a
   `BEFORE UPDATE OR DELETE` trigger that raises unconditionally, regardless of role. Belt and
   suspenders, per the doc's own emphasis that this table is the source of every reference
   calculation.

6. **J&H Tackle's `CATALOG_LISTING` discovery page is not seeded.** Section 3.3's YAML example
   gives its URL as a placeholder template (`.../collections/<rods|reels|soft-plastics>`), not a
   real URL. `012_seed_retailers` seeds the retailer row for J&H (needed as the matching anchor,
   section 8) but leaves its discovery page(s) for whoever resolves the real collection URLs
   (task T6 per the doc) to insert.

7. **Seed migration is insert-only (`ON CONFLICT DO NOTHING`).** Re-running `012_seed_retailers`
   does not update an existing retailer's `policy`/`egress` JSON if `config/retailers.yaml`
   changes later. That sync is expected to be an application-level job reading the YAML, not this
   migration -- this migration only guarantees the three MVP retailers exist on a fresh database.

8. **Materialized views, not regular views**, for `offer_daily_price`, `offer_reference_stats`,
   `variant_current_new`, matching the doc's own wording ("Materialized views (refreshed by deal
   engine...)" in section 4.1 and "refreshed by the deal engine job at most every 30 min" in
   section 5.2). Each has a unique index so `REFRESH MATERIALIZED VIEW CONCURRENTLY` works;
   `refresh_fpt_views()` wraps all three. The doc's "observed within 48h" freshness requirement
   for `variant_current_new` (section 6.1, R2/U1) is deliberately **not** baked into the view's
   `WHERE` clause -- the view always carries the real `observed_at`, and the deal engine checks
   staleness at read time, so a delayed refresh can never silently present a stale offer as fresh.

9. **014 (TEST-phase follow-up): the confirmed-status guard covers ACTIVE, HELD_REVIEW, and
   EXPIRED, but deliberately not REJECTED.** Per section 6.4, ACTIVE and HELD_REVIEW are only
   reachable after a CONFIRM observation runs checks C1-C10, and per section 6.3's lifecycle
   EXPIRED is only reachable from ACTIVE, so all three require a non-null
   `confirming_observation_id`. REJECTED is excluded on purpose: `confirm_deadline_missed` (the
   CONFIRM task never got fetched, section 5.2) and `retailer_blocked` (section 5.4) are both
   real, documented ways a candidate gets REJECTED with no confirming observation ever taken.
   Constraining REJECTED the same way would make those two paths impossible to record. Added as
   `NOT VALID` then `VALIDATE CONSTRAINT` (a `SHARE UPDATE EXCLUSIVE` lock, not a full table
   rewrite lock) rather than a plain inline CHECK, since the table already has rows by the time
   this migration runs in any real environment.

10. **014's `price_observations.currency` column is metadata-only DDL, not a DML UPDATE.** The
    table is append-only and a trigger blocks `UPDATE`/`DELETE` (007). `ALTER TABLE ... ADD
    COLUMN ... DEFAULT 'USD'` is DDL that Postgres 11+ applies without rewriting existing rows for
    a constant default, and it does not invoke the row-level `BEFORE UPDATE` trigger at all (that
    trigger only fires for `UPDATE`/`DELETE` statements against existing rows) -- so existing
    observations backfill to `'USD'` (correct per assumption A5, USD-only for MVP) without
    touching append-only semantics.

## Smoke test (ran live during this task)

A throwaway `postgres:16` Docker container was used -- no production or shared database was
touched.

1. Applied all 12 `.up.sql` migrations in order against a fresh database: clean, zero errors.
2. Re-ran all 12 `.up.sql` migrations a second time: every `CREATE TABLE`/`CREATE INDEX`/etc.
   emitted `NOTICE: ... already exists, skipping` and the seed migration reported `INSERT 0 0` on
   both statements. **Confirmed idempotent.**
3. Basic CRUD: inserted a brand -> product -> variant -> retailer -> seller -> tracked_url ->
   listing -> offer -> crawl_task -> two price_observations (one OK, one REJECTED with a NULL
   price). All inserts succeeded as expected.
4. Bad-data guards: an `OK`-quality row with `price_cents = -100` was rejected by the CHECK
   constraint, as intended.
5. Append-only guard: both `UPDATE` and `DELETE` against an existing `price_observations` row
   raised the expected `price_observations is append-only` exception from the trigger.
6. Role least-privilege: `SET ROLE fpt_job` then `UPDATE price_observations` was denied
   (`permission denied for table price_observations`); `SET ROLE fpt_api` then a direct `SELECT`
   from `tracked_urls` was denied; `SET ROLE fpt_api` then `SELECT promote_variant_hot(...)`
   succeeded (the one sanctioned path to touch `tracked_urls.tier`).
7. Materialized views: `refresh_fpt_views()` ran without error and `offer_reference_stats`
   returned the expected median for the single seeded observation.
8. Rollback: ran all 12 `.down.sql` files in reverse order against the same database -- zero
   errors, `\dt` afterward showed no relations. Re-applied all `.up.sql` files once more
   afterward to confirm the schema comes back up cleanly post-rollback.

The container (`fpt-smoketest`) was removed after the test; nothing was left running.

### 014 follow-up smoke test (TEST-phase defect fixes, second throwaway container `fpt-smoketest2`)

1. Applied `001` through `012` then `014` (`013` skipped, reserved for the parallel adapter
   coder's retailer seed) against a fresh database: zero errors.
2. Re-ran the same sequence a second time: `CREATE ...` statements emitted the expected
   `already exists, skipping` notices, `012`'s seed inserts reported `INSERT 0 0`, and `014`'s
   guarded `DO` blocks + `ADD COLUMN IF NOT EXISTS` + re-run `VALIDATE CONSTRAINT` all no-op'd
   cleanly. **Confirmed idempotent.**
3. Inserted a full brand -> ... -> price_observation chain; `SELECT currency FROM
   price_observations` showed the `'USD'` default applied with no value supplied.
4. Guard test: inserting a `deals` row with `status = 'ACTIVE'` and `confirming_observation_id =
   NULL` failed with `violates check constraint "deals_confirmed_status_requires_observation"`,
   exactly as intended.
5. Control test: the identical insert with `confirming_observation_id` set to a real observation
   id succeeded.
6. Guard test: inserting a `price_observations` row with `currency = 'usd'` (lowercase) failed
   with `violates check constraint "price_observations_currency_format"`.
7. Rollback: ran `014`'s down migration followed by `012` through `001` in reverse order -- zero
   errors, `\dt` afterward showed no relations.
8. Container `fpt-smoketest2` was removed after the test; nothing was left running.

## For backend coders

- `listings` is "one retailer SKU = one variant at that retailer." `offers` is the actual unit of
  price: listing x condition x seller. Don't compute discounts off `listings` directly.
- `price_observations` is INSERT + SELECT only from application code (`fpt_job` role). Never
  UPDATE or DELETE a row in it -- the trigger will reject it, and semantically a "correction" is
  always a new row plus a `deals` state change, never an edit.
- `product_variants.gtin14` and `listings.gtin14` are `char(14)[]` (checksum-valid barcodes only,
  a listing/variant can carry more than one). Use the GIN index (`... USING gin (gtin14)`) for
  "does any listing carry barcode X" lookups, not a sequential scan.
- The three materialized views are the reference-resolution inputs (section 6.1): call
  `SELECT refresh_fpt_views();` from the deal engine job, not ad hoc `REFRESH MATERIALIZED VIEW`
  calls scattered through the codebase, so refresh cadence stays centralized.
- `fpt api` (the public HTTP surface) should connect as `fpt_api`, never as `fpt_job` or a
  superuser -- it structurally cannot see `tracked_urls`, `crawl_tasks`, `price_observations`, or
  anything else outside `subscribers`/`watches`/`api_tokens_used` and a two-column slice of
  `product_variants`.
