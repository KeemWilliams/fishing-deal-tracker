/**
 * Node-side driver for testing extension/lib/extractor.js as a pure
 * function, without a browser/DOM. Invoked by
 * tests/test_extension_extractor.py via `node run_extractor.js <html-file> <page-url>`.
 * Prints the extracted capture object (or `null`) as JSON on stdout.
 */
"use strict";

var fs = require("fs");
var path = require("path");

var extractorPath = path.join(__dirname, "..", "..", "..", "extension", "lib", "extractor.js");
var FPTExtractor = require(extractorPath);

var htmlFile = process.argv[2];
var pageUrl = process.argv[3] || "";
var wasPriceRegexSource = process.argv[4]; // optional, passed as a regex source string

if (!htmlFile) {
  console.error("usage: node run_extractor.js <html-file> [page-url] [was-price-regex-source]");
  process.exit(2);
}

var html = fs.readFileSync(htmlFile, "utf-8");

var options = { pageUrl: pageUrl };
if (wasPriceRegexSource) {
  options.siteConfig = { wasPriceRegex: new RegExp(wasPriceRegexSource, "i") };
}

var result = FPTExtractor.extractFromHtml(html, options);
process.stdout.write(JSON.stringify(result));
