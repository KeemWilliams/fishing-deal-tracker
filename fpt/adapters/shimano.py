"""Shimano (fishshop.shimano.com) adapter.

Shimano North America Fishing runs ONE Shopify storefront,
`fishshop.shimano.com`, for four brands: Shimano, G. Loomis, PowerPro, and
Jackall Lures (each brand gets its own `/pages/<brand>` landing page and
its own `/collections/last-cast-savings-<brand>` sale collection). The
brand's older domains (`fish.shimano.com`/`shimanofish.com`,
`gloomis.com`) are dead or offline as of 2026-09-13 -- see
knowledge/research/fishing-manufacturer-sites-2026-09-13.md.

This adapter is modeled as one retailer PER BRAND rather than one
retailer for the whole storefront: `fpt/adapters/_shopify_collection.py`
only supports a single sale-collection handle per instance, each brand's
collection is fully independent (own handle, own product catalog), and
`ParsedOffer.seller_key` is meant to identify the first-party seller of
that specific manufacturer's product -- conflating four different brands
under one `seller_key` would be misleading even though they share a
storefront domain. `retailers.base_url` is NOT unique-constrained (see
`db/migrations/002_reference_tables.up.sql`), so all four brand adapters
here safely share the same `base_url`/`allowed_hosts`
(`fishshop.shimano.com`) under four distinct `slug`s -- see also
`fpt/adapters/gloomis.py`, `fpt/adapters/powerpro.py`,
`fpt/adapters/jackall.py`.

Sale collection handle is `last-cast-savings-shimano` (verified live
2026-09-13, robots.txt allows `User-agent: *` on this host, no
bot-specific disallow). Highest average order value of any retailer in
this task -- premium reels/rods commonly $180-$770, 30-50% off.

Canonical host is `fishshop.shimano.com` -- no `www.` redirect observed.
"""

from __future__ import annotations

from fpt.adapters._shopify_collection import ShopifyCollectionAdapter

SELLER_KEY = "shimano"
SELLER_NAME = "Shimano"
BASE_URL = "https://fishshop.shimano.com"
SALE_COLLECTION_HANDLE = "last-cast-savings-shimano"


class ShimanoAdapter(ShopifyCollectionAdapter):
    def __init__(self) -> None:
        super().__init__(
            slug=SELLER_KEY,
            name=SELLER_NAME,
            base_url=BASE_URL,
            sale_collection_handle=SALE_COLLECTION_HANDLE,
        )
