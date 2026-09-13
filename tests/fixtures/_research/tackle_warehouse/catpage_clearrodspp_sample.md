# Tackle Warehouse - catpage-CLEARRODSPP.html (research sample, 2026-09-12)

Plain HTTP GET, markdown extraction. No pagination controls observed on this page --
"Shop All Clearance Rods" appears to be a single page combining a "Clearance Casting Rods"
subsection and a "Clearance Spinning Rods" subsection, roughly 25 items total visible in this
capture (confirmed count, not inferred -- counted directly in the markdown body).

Sample rows (name | now | was | %off):

- Savage Gear Battletek Series Swimbait Casting Rods | $129.97 | $269.99 | -51%
- Savage Gear Squad Musky Swimbait Casting Rods | $99.97 | $209.99 | -52%
- Daiwa ISLA AGS Inshore Casting Rods | $149.97 | $299.99 | -50%
- Lew's Team Pro Ti Speed Stick Spinning Rods | $149.97 | $299.99 | -50%
- Ark Invoker Pro Series Casting Rods | $104.97 | $149.95 | -30%
- Savage Gear Squad Walleye Spinning Rods | $79.97 | $109.99 | -27%

Items WITHOUT a visible was-price (single price only, no %off badge):
- Googan Squad Gold Series Casting Rod -- "$169.99" (no compare price shown; likely newly
  added to clearance category without a markdown yet, or a non-discounted clearance-tagged item)
- Savage Gear Battletek Series Casting Rods -- "$129.97" (Clearance badge but no was-price)
- G. Loomis IMX Pro Ned Rig Spinning Rods -- "$319.97" (Clearance badge, no was-price)
- Multi-variant range-priced items ("$99.98 - $119.99") never carry a %off badge in the listing
  markdown -- consistent with the was/now pair being per-SKU and a range meaning multiple SKUs
  at different discount depths rolled into one card.

Footer disclaimer (verbatim, confirms was/now pricing IS provided by the retailer itself, not
an inferred figure):
"*Price comparisons are based on the Manufacturer's Suggested Retail Price ("MSRP") or Original
Selling Price. Actual sales may not have occurred at this price."

Verdict: This listing page alone is sufficient to compute % off without any per-product fetch --
name, url, now-price, was-price (when present) and the retailer's own %off badge are all in the
plain-HTML listing markdown.
