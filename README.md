# Fishing Gear Deal Tracker — scraper package

Python package implementing the scraper/adapter core described in
[`knowledge/architecture/fishing-price-tracker-mvp-architecture.md`](../../knowledge/architecture/fishing-price-tracker-mvp-architecture.md).

## What's here

- `fpt/adapters/base.py` — the adapter contract (dataclasses/enums/Protocol).
  Committed early so other retailer adapters can build against it.
- `fpt/adapters/tackle_warehouse.py` — the Tackle Warehouse adapter
  (clearance + used discovery, product page confirmation).
- `fpt/fetch/` — HTTP fetch layer (plain `scrapling.fetchers.Fetcher` by
  default; a `use_stealth` flag exists but is never enabled without an
  explicit owner decision), robots.txt admission checks, block/silent-
  failure detection, gzip'd raw snapshots.
- `fpt/scheduler/limiter.py` — per-retailer rate limiting, hourly/daily
  budgets with reserved shares for CONFIRM/HOT, and the breaker.
- `fpt/pipeline/validate.py` — observation quality guards.
- `fpt/deals/` — reference-price resolution, the three deal rules, and the
  two-fetch confirmation checks (C1-C10).
- `fpt/cli.py` — `python run.py tick` runs one self-contained tick over
  `config/discovery_pages.yaml`.

## Running

```bash
pip install -r requirements-dev.txt
pytest
python run.py tick --max-discovery-pages 1
```

## Environment variables

| Var | Purpose |
|-----|---------|
| `DATABASE_URL` | Postgres connection string. If unset (or the expected tables don't exist yet), `tick` runs in dry-run/print mode instead of failing. |
| `FPT_SNAPSHOT_DIR` | Where raw response snapshots are written (default `/var/lib/fpt/snapshots`). |

No other environment variables are read by this package. Secrets (proxy
URLs, future data-API keys) are wired through `EgressConfig` env-var
*names*, never values, per the architecture doc's security section.

## Scope note

This package does not include the Academy or J&H Tackle adapters (owned
by a peer coder building against `fpt/adapters/base.py`), the DB schema/
migrations (owned by the database engineer), or the Astro site.
