# Fishing Price Tracker — Site

Public MVP frontend for the deep-discount fishing gear tracker. Astro static site, deployed to
Cloudflare Pages. Reads the deals feed the scraper/db side publishes; never talks to the tracker's
database directly.

Design reference: `knowledge/architecture/fishing-price-tracker-mvp-architecture.md` (section 3.4
"Export contract" and section 6 "Deal Detection").

## Stack

- Astro (static output)
- Tailwind CSS v4
- No client-side framework — filtering/sorting on the home page and the stale-feed check are plain
  JS in `public/scripts/`, since the grid is fully rendered at build time and only needs to
  show/hide/reorder existing DOM nodes.

## Running locally

```bash
npm install
npm run dev      # http://localhost:4321
npm run build    # astro check && astro build -> dist/
npm run preview
```

No environment variables are required for local dev. With `PUBLIC_FEED_BASE_URL` unset, the site
reads the bundled sample feed in `sample-data/` instead of a live feed (see below).

## Feed contract

Set `PUBLIC_FEED_BASE_URL` (see `.env.example`) to the base URL the export job publishes to, e.g.
`https://data.tackledeals.example/latest`. The site expects, under that base URL:

- `meta.json` — export metadata, retailer health, counts, thresholds
- `deals.json` — the published deal list (only 50%+ discounts; nothing else is ever included)
- `products/<slug>.json` — per-product variant/offer/history detail, used by `/p/<slug>/`

`src/lib/types.ts` mirrors `export.schema.json` and `src/lib/feed.ts` checks `schema_version` at
build time — a schema bump on the scraper side should fail this site's build loudly rather than
render silently-wrong data. Update both files together when the schema changes.

If `PUBLIC_FEED_BASE_URL` is set but unreachable, `npm run build` fails on purpose. A misconfigured
production feed URL should not silently ship stale sample data.

### Sample data (local dev only)

`sample-data/` contains a small, clearly-fictional feed (`latest/meta.json`, `latest/deals.json`,
`products/*.json`) covering all three deal lanes (verified / retailer-claimed / used) and several
categories, so the UI can be built and reviewed without a live backend. `FeedMeta.is_sample_data` is
set only when this fallback is used, and the home page renders an amber "sample data" banner
whenever it's true. **This flag and banner must never appear in a production build** — that only
happens if `PUBLIC_FEED_BASE_URL` is left unset in the deploy environment, which would be a
deployment misconfiguration, not a site bug.

## Decisions made here (not fully specified in the architecture doc)

- **Repo path**: the architecture doc's repo layout (section 10.2) shows a separate
  `apps/fishing-price-tracker-site/` app. The actual repo consolidated into
  `apps/fishing-price-tracker/` with `db/`, `scraper/`, and this `site/` as subfolders. This app
  follows that convention; adjust deploy config accordingly if the two are ever reconciled.
- **Home page layout**: the doc describes the home page as three fixed sections in order (verified
  50%+, then retailer-claimed 50%+, then used 50%+ vs new). This build instead renders one unified,
  filterable/sortable grid (category, retailer, new/used, sort by discount/price/newest), matching
  the coding task's explicit requirement for those controls. The **default** sort still orders
  verified deals before claimed before used (ties broken by discount desc), so the lane priority
  from the doc is preserved until a visitor picks an explicit sort.
- **`schema_version`**: pinned to `2` (the version shown in the doc's example payloads). Bump
  `SUPPORTED_SCHEMA_VERSION` in `src/lib/feed.ts` when the export format changes.
- **Stale-feed threshold**: 6 hours, matching the doc's export cadence ("Export runs at most every
  30 minutes and only if a deal changed state or 6h passed"). Configurable in `src/lib/feed.ts`.
- **Price alerts / email signup**: per the coding task, no functional signup form ships yet — the
  sender postal address needed for CAN-SPAM compliance hasn't been decided. `WatchAlertPlaceholder`
  renders the eventual form's layout with disabled inputs, and `/watch/*` + `/unsubscribed/` render
  as inert placeholder pages (the backend endpoints they'd otherwise land from don't exist yet
  either). Wire these up for real once the API in section 3.5 of the architecture doc ships and a
  sender address is confirmed.
- **Product name**: undecided. `SITE_NAME` in `src/lib/site-config.ts` is the single place the
  working name "Tackle Deals" lives; rename there when a real name is chosen.
- **Outbound retailer links**: plain `<a>` tags, `target="_blank" rel="noopener noreferrer"`, no
  query-string additions of any kind (no affiliate tags, no UTM params), per the coding task.

## Accessibility notes

- All interactive controls (filters, outbound links, the disabled watch-signup button) are native
  `<button>`/`<a>`/`<select>`/`<input>` elements — keyboard reachable by default.
- The filter form's result count is an `aria-live="polite"` region so screen reader users get
  feedback when a filter change changes what's shown.
- Outbound links carry a visually-hidden "(opens in a new tab)" suffix.
- Condition badges use both color and text (never color alone) to distinguish new vs. used.

## Known gaps / follow-ups for QA

- Product page history table is a plain HTML table (not a chart). Fine for MVP; a chart is an
  enhancement.
- No pagination on the home grid. At MVP deal volumes (tens, not thousands) this is fine; revisit if
  the feed grows.
- The client-side stale-feed poll (`public/scripts/feed-status.js`) only runs when
  `PUBLIC_FEED_BASE_URL` is set (i.e., never in local/sample mode) — this is intentional, not a bug.
