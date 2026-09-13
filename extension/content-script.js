/**
 * Content script: runs in the context of a product page on a configured
 * retailer (see manifest.json's `matches` and lib/site-configs.js).
 *
 * Privacy/scope note (see README.md "Privacy" section): this script only
 * reads product/price data already visible on the page (JSON-LD the
 * retailer itself published for SEO, plus one small DOM read for a
 * variant label). It never reads cookies, localStorage, form fields, or
 * any personal/account data, and it never runs on any page outside the
 * `matches` patterns below. Nothing is sent anywhere from this file --
 * it hands the extracted candidate to the background service worker via
 * `chrome.runtime.sendMessage`, and the background script is the only
 * place a network request is made (see background.js's docstring for
 * why: avoids page-context CORS/credential exposure entirely).
 */
(function () {
  "use strict";

  function readVariantLabel(siteConfig) {
    if (!siteConfig || !siteConfig.variantSelector) return null;
    try {
      var el = document.querySelector(siteConfig.variantSelector);
      if (!el) return null;
      var text = (el.textContent || "").trim();
      return text || null;
    } catch (err) {
      // A selector that no longer matches the page's current markup must never break capture
      // of the rest of the fields -- this is best-effort enrichment, not a required field.
      return null;
    }
  }

  function buildCapturePayload() {
    var siteConfig = FPTSiteConfigs.getSiteConfig(location.hostname);
    var html = document.documentElement.outerHTML;

    var extracted = FPTExtractor.extractFromHtml(html, {
      pageUrl: location.href,
      siteConfig: siteConfig,
    });
    if (!extracted) return null;

    if (!extracted.variant_label_raw) {
      extracted.variant_label_raw = readVariantLabel(siteConfig);
    }
    if (siteConfig && siteConfig.retailerHint && !extracted.retailer_hint) {
      extracted.retailer_hint = siteConfig.retailerHint;
    }
    extracted.captured_at = new Date().toISOString();
    return extracted;
  }

  function sendCapture(payload) {
    try {
      chrome.runtime.sendMessage({ type: "FPT_CAPTURE", payload: payload });
    } catch (err) {
      // Extension context can be invalidated mid-navigation (e.g. the user closed the tab or
      // the extension was reloaded); never throw out of a content script for this.
    }
  }

  function run() {
    var payload = buildCapturePayload();
    if (payload) {
      sendCapture(payload);
    }
  }

  // Product pages on these platforms render JSON-LD server-side, so it's present by the time
  // `document_idle` content scripts run (see manifest.json's run_at) -- no MutationObserver or
  // polling needed for the MVP.
  run();
})();
