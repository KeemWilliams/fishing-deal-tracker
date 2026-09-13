# Fishing Deal Capture (personal-use browser extension)

Chrome/Brave MV3 extension. Records the price on a product page **you are already looking at**
in your own browser, for retailers the tracker deliberately does not auto-scrape (their
robots.txt disallows the product-data endpoints for our identified crawler UA) or that require
JS rendering. A human viewing a page is not our scraper, so this respects each site's robots.txt
where automated fetching would not — see
`knowledge/architecture/fishing-price-tracker-mvp-architecture.md` section 1, driver #1, and
`db/migrations/020_page_captures.up.sql` for why a capture is a **claim**, never a confirmed
observation.

## Install (unpacked, for personal use)

1. `chrome://extensions` (or `brave://extensions`) → enable **Developer mode**.
2. **Load unpacked** → select this `extension/` directory.
3. Open the extension's **Options** page (right-click the toolbar icon → Options, or via
   `chrome://extensions`) and set:
   - **Ingest endpoint URL** — default `http://localhost:8787/v1/captures`, matching
     `fpt/capture/server.py`'s default bind address/port.
   - **Bearer token** — only needed if you set `FPT_CAPTURE_TOKEN` when running the server.
4. Start the ingest server: `python -m fpt.capture.server` (from `apps/fishing-price-tracker/`,
   with `DATABASE_URL` set).
5. Visit a supported product page (Scheels or Academy Sports + Outdoors — see `lib/site-configs.js`) and
   the content script captures it automatically; the toolbar badge turns green (✓) on success or
   red (!) on failure.

## Permission model (least privilege)

| Permission | Why |
|---|---|
| `storage` | Save the endpoint URL/token you set in Options (`chrome.storage.sync`). |
| `host_permissions`: `scheels.com`, `academy.com` | The only sites the content script runs on. Adding a retailer means adding a host permission AND a `content_scripts.matches` entry — both must change together (see `manifest.json`). |
| `host_permissions`: `localhost`/`127.0.0.1` | So the background service worker (not the page) can reach your local ingest server. |

No `activeTab`, no `tabs`, no `<all_urls>`, no `webRequest`, no `cookies`. The content script
cannot run on any page outside the two configured retailers.

## Privacy

- **No analytics, no telemetry, no third-party network calls.** The only network request this
  extension ever makes is the single POST to *your own* configured endpoint.
- **No PII.** The extractor only reads product name/price/condition/URL already published in the
  page's own `schema.org/Product` JSON-LD (the same data the retailer serves to Google), plus one
  small DOM read for a variant label (e.g. "7'6\" Medium, Fast") via a per-site CSS selector. It
  never reads cookies, form fields, account/session data, or anything the retailer hasn't already
  put in the page for search engines to see.
- **No page-context network exposure.** The POST is made by the background service worker, not
  the content script running in the retailer's page — the retailer's own page script can never
  observe, intercept, or forge the request. See `background.js`'s module docstring.

## How it works

```
content-script.js  --(extracted candidate, via chrome.runtime.sendMessage)-->  background.js
                                                                                      |
                                                                                      v
                                                                        POST <endpoint>/v1/captures
                                                                        (fpt/capture/server.py)
```

1. `lib/extractor.js` — pure, DOM-independent extraction: parses the page's own
   `schema.org/Product` JSON-LD for title/price/condition, plus a per-site regex
   (`lib/site-configs.js`) for the "was"/strikethrough price schema.org has no field for.
2. `content-script.js` — runs only on the two configured retailers, adds a best-effort DOM read
   for the variant label, and hands the result to the background worker. It never makes a network
   call itself.
3. `background.js` — the only network-capable part of the extension. Reads your saved
   endpoint/token from `chrome.storage.sync` and POSTs the capture.
4. `options.html`/`options.js` — lets you point the extension at a different host/port or set a
   bearer token, without touching code.

## Adding a new retailer

1. Add an entry to `lib/site-configs.js` (a `wasPriceRegex` and/or `variantSelector` — both
   optional; JSON-LD-only extraction works with neither).
2. Add the retailer's origin to `manifest.json`'s `host_permissions` AND
   `content_scripts[0].matches`.
3. Reload the unpacked extension.

No changes to `lib/extractor.js`, `content-script.js`, or `background.js` are needed for a site
that already emits `schema.org/Product` JSON-LD.

## Testing

`lib/extractor.js`'s pure functions are exercised by
`apps/fishing-price-tracker/tests/test_extension_extractor.py`, which shells out to `node` against
hand-written HTML fixtures in `tests/fixtures/_browser_capture/` (kept separate from the unrelated
`tests/fixtures/_capture/` directory, which holds raw scraper-research response captures for other
adapters). No live requests are made to any retailer as part of testing.
