/**
 * Small per-site extraction hints, keyed by hostname. Everything in the
 * extractor that ISN'T covered here (title, current price, condition)
 * comes from generic schema.org Product JSON-LD (lib/extractor.js) --
 * these entries only add what JSON-LD typically can't express: the
 * "was"/list price shown as a strikethrough on the page, and (as a
 * documented, untested-here fallback) a CSS selector for the DOM-based
 * variant label when the content script can't rely on JSON-LD alone.
 *
 * Adding a new retailer is meant to be config, not code: add an entry
 * here, no changes to extractor.js or content-script.js required, per
 * the architecture doc's "retailers plug in as config" philosophy
 * (fishing-price-tracker-mvp-architecture.md section 1, driver #1) --
 * applied here to the personal-capture path, not the scraper adapters.
 *
 * `wasPriceRegex` is matched against the raw page HTML (see
 * extractor.js's extractWasPriceFromHtml) -- capture group 1 must be the
 * price text (e.g. "$129.99" or "129.99"). Keep these narrow and
 * specific to the known markup rather than generic "any struck-through
 * dollar amount" patterns, to avoid picking up an unrelated
 * cross-sell/upsell price elsewhere on the page.
 *
 * `variantSelector` is a CSS selector the content script queries live
 * against `document` (browser-only, not exercised by the Node-based
 * extractor tests) -- see content-script.js's `readVariantLabel()`.
 */
(function (root, factory) {
  if (typeof module === "object" && module.exports) {
    module.exports = factory();
  } else {
    root.FPTSiteConfigs = factory();
  }
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  var SITE_CONFIGS = {
    "www.scheels.com": {
      retailerHint: "Scheels",
      // Scheels (Salesforce Commerce Cloud) product pages render the former price inside a
      // "strike-through"-styled price element next to the sale price, e.g.
      // <span class="strike-through-price">$129.99</span>. See knowledge/research/
      // fishing-gear-price-tracker-scrape-feasibility-2026-09-12.md: Scheels product-data
      // endpoints are robots.txt-disallowed for our own crawler, which is exactly why this
      // capture path exists for it.
      wasPriceRegex: /class="[^"]*strike-through-price[^"]*"[^>]*>\s*\$?\(?([\d,]+\.\d{2})\)?/i,
      variantSelector: '[data-testid="pdp-selected-variant-label"]',
    },
    "www.academy.com": {
      // Already reachable by fpt's own scraper (fpt/adapters/academy.py) -- included as the "one
      // already-working site" example the task asked for. Academy's product-page JSON-LD carries
      // ONLY the current price (see architecture doc's adapter matrix, "Claimed ref: No" for
      // academy), so there is deliberately no wasPriceRegex here -- this site exercises the
      // JSON-LD-only extraction path with no per-site regex fallback needed, using the SAME
      // fixture shape already committed at tests/fixtures/academy/product_single_variant_
      // instoreonly.html (see tests/test_extension_extractor.py).
      retailerHint: "Academy Sports + Outdoors",
    },
  };

  function getSiteConfig(hostname) {
    if (!hostname) return null;
    return SITE_CONFIGS[hostname.toLowerCase()] || null;
  }

  return {
    SITE_CONFIGS: SITE_CONFIGS,
    getSiteConfig: getSiteConfig,
  };
});
