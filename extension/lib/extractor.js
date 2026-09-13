/**
 * Pure extraction logic for the fishing-price-tracker capture extension.
 *
 * Deliberately dependency-free and DOM-independent so it can run both
 * inside a browser content script (where `html` is
 * `document.documentElement.outerHTML`) and inside a plain Node process
 * for unit testing (see tests/test_capture_validate.py's sibling test,
 * tests/test_extension_extractor.py, which shells out to `node`).
 *
 * Two extraction layers, tried in order:
 *   1. Schema.org Product JSON-LD (`<script type="application/ld+json">`)
 *      -- the most reliable signal, and what modern e-commerce platforms
 *      (Salesforce Commerce Cloud, Shopify, etc.) already emit for SEO.
 *      This is a plain string/regex scan, NOT a DOM query, so it works
 *      identically in the browser and in this test harness.
 *   2. A per-site "was"/strikethrough-price regex fallback
 *      (see site-configs.js) for retailers whose JSON-LD Product block
 *      doesn't carry a former/list price (schema.org has no standard
 *      field for "was" price on a simple Offer).
 *
 * What this module does NOT do: arbitrary DOM traversal (querySelector
 * etc.) for title/current-price. That lives in content-script.js as a
 * documented, browser-only fallback for pages with no JSON-LD Product
 * block at all -- it cannot be unit-tested here without a real DOM/jsdom,
 * which this project does not depend on. See the ingest task's HANDOFF,
 * "Areas of uncertainty".
 *
 * UMD-ish wrapper: CommonJS export for Node (tests, `require`), and a
 * `self.FPTExtractor` global for the browser content-script <script> tag
 * (classic script, not a module -- MV3 content_scripts don't need ESM for
 * this).
 */
(function (root, factory) {
  if (typeof module === "object" && module.exports) {
    module.exports = factory();
  } else {
    root.FPTExtractor = factory();
  }
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  var JSON_LD_SCRIPT_RE = /<script[^>]*type=["']application\/ld\+json["'][^>]*>([\s\S]*?)<\/script>/gi;

  var CONDITION_ITEM_AVAILABILITY_MAP = {
    newcondition: "NEW",
    usedcondition: "USED_UNGRADED",
    refurbishedcondition: "REFURBISHED",
    damagedcondition: "USED_ACCEPTABLE",
  };

  /**
   * "$1,299.00" / "1299.00" / "$64.97" / "64" -> integer cents.
   * Returns null (never 0, never NaN) when the text has no discernible
   * numeric price -- callers must treat null as "unknown", not "free".
   */
  function normalizePriceToCents(value) {
    if (value === null || value === undefined) return null;
    if (typeof value === "number") {
      if (!isFinite(value) || value <= 0) return null;
      return Math.round(value * 100);
    }
    if (typeof value !== "string") return null;
    var match = value.replace(/,/g, "").match(/(\d+(?:\.\d{1,2})?)/);
    if (!match) return null;
    var num = parseFloat(match[1]);
    if (!isFinite(num) || num <= 0) return null;
    return Math.round(num * 100);
  }

  function mapSchemaItemCondition(itemCondition) {
    if (typeof itemCondition !== "string") return null;
    var key = itemCondition
      .toLowerCase()
      .replace("https://schema.org/", "")
      .replace("http://schema.org/", "");
    return CONDITION_ITEM_AVAILABILITY_MAP[key] || null;
  }

  /**
   * Parses every JSON-LD <script> block in `html` and returns the ones
   * that look like a schema.org Product. Malformed JSON in one block
   * never aborts the others -- each block is parsed independently and
   * failures are silently skipped (a page with a broken analytics JSON-LD
   * block elsewhere must not break product extraction).
   */
  function extractJsonLdProducts(html) {
    if (typeof html !== "string" || !html) return [];
    var products = [];
    var match;
    JSON_LD_SCRIPT_RE.lastIndex = 0;
    while ((match = JSON_LD_SCRIPT_RE.exec(html)) !== null) {
      var raw = match[1];
      var parsed;
      try {
        parsed = JSON.parse(raw);
      } catch (err) {
        continue;
      }
      var candidates = Array.isArray(parsed) ? parsed : [parsed];
      for (var i = 0; i < candidates.length; i++) {
        var node = candidates[i];
        if (node && node["@type"] === "Product") {
          products.push(node);
        }
      }
    }
    return products;
  }

  function firstOffer(product) {
    if (!product || !product.offers) return null;
    if (Array.isArray(product.offers)) return product.offers[0] || null;
    return product.offers;
  }

  /**
   * Turns a schema.org Product node into the same shape the ingest
   * endpoint expects (see fpt/capture/validate.py). Returns null when the
   * node has no usable price -- a Product block with no price is not a
   * capture candidate, not a $0 one.
   */
  function productToCapture(product, pageUrl) {
    var offer = firstOffer(product);
    if (!offer) return null;

    var priceCents = normalizePriceToCents(offer.price);
    if (priceCents === null) return null;

    var condition = mapSchemaItemCondition(offer.itemCondition);
    var productUrl = (offer.url || product["@id"] || product.url || pageUrl || "").toString();

    return {
      product_url: productUrl,
      title_raw: typeof product.name === "string" ? product.name : "",
      current_price_cents: priceCents,
      currency: typeof offer.priceCurrency === "string" ? offer.priceCurrency : "USD",
      condition: condition || undefined,
      raw: { source: "jsonld", sku: product.sku || null, brand: (product.brand && product.brand.name) || null },
    };
  }

  /**
   * Best-effort "was"/list price from raw HTML using a per-site regex
   * (see site-configs.js). This is intentionally narrow: one capture
   * group, first match wins, no price math -- a false negative (missing
   * was-price) is always safe, a false positive is guarded by the
   * ingest endpoint's own `was_price > current_price` check
   * (fpt/capture/validate.py silently drops a nonsensical was-price
   * rather than trusting it).
   */
  function extractWasPriceFromHtml(html, wasPriceRegex) {
    if (typeof html !== "string" || !html || !wasPriceRegex) return null;
    var match = html.match(wasPriceRegex);
    if (!match || !match[1]) return null;
    return normalizePriceToCents(match[1]);
  }

  /**
   * Top-level entry point. `html` is the full page HTML (or
   * document.documentElement.outerHTML in the browser). `options.pageUrl`
   * is the current tab URL (JSON-LD often omits it). `options.siteConfig`
   * is one entry from site-configs.js (may be undefined for an
   * unconfigured host -- JSON-LD-only extraction still works).
   *
   * Returns a capture payload object ready to POST to
   * `POST /v1/captures`, or null if nothing extractable was found.
   */
  function extractFromHtml(html, options) {
    options = options || {};
    var products = extractJsonLdProducts(html);
    if (products.length === 0) return null;

    var capture = null;
    for (var i = 0; i < products.length; i++) {
      capture = productToCapture(products[i], options.pageUrl);
      if (capture) break;
    }
    if (!capture) return null;

    if (options.siteConfig && options.siteConfig.wasPriceRegex) {
      var wasPriceCents = extractWasPriceFromHtml(html, options.siteConfig.wasPriceRegex);
      if (wasPriceCents !== null) {
        capture.was_price_cents = wasPriceCents;
      }
    }

    if (!capture.product_url && options.pageUrl) {
      capture.product_url = options.pageUrl;
    }

    return capture;
  }

  return {
    normalizePriceToCents: normalizePriceToCents,
    mapSchemaItemCondition: mapSchemaItemCondition,
    extractJsonLdProducts: extractJsonLdProducts,
    productToCapture: productToCapture,
    extractWasPriceFromHtml: extractWasPriceFromHtml,
    extractFromHtml: extractFromHtml,
  };
});
