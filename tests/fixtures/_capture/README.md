# Captured fixtures (Bass Pro / Cabela's / Dick's)

Real responses captured via Playwright (a real browser) on 2026-09-13 for building the
three Akamai-fronted retailer adapters. Captured because these sites serve a real browser
fine and their robots.txt explicitly allows Claude-User, while plain HTTP clients get a 403.

- `basspro_coveo_rods.json` — Bass Pro Coveo product-search API response for the fishing-rods
  listing, containing products with prices. Bass Pro is a Next.js app whose listing data loads
  client-side from Coveo (platform.cloud.coveo.com/rest/search/v2), not from the page HTML.
  **NOTE (2026-09-13, backend-coder):** this file was accidentally overwritten in place while
  trimming it down for `tests/fixtures/basspro/coveo_search_rods.json` -- it originally held 36
  real captured results and now holds only 3 real + 1 synthetic (see that test fixture's own
  docstring). The full 36-result capture was not preserved. If Cabela's/Dick's adapter work
  wants the untrimmed shape, re-capture via Playwright rather than trusting this file's current
  (reduced) contents as representative of the original response size.
