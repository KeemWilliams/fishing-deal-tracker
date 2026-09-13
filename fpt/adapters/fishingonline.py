"""Fishing Online (fishingonline.com) adapter.

Shopify storefront read via Shopify's own public JSON endpoints -- see
`fpt/adapters/_shopify_collection.py` for the shared implementation and
the endpoint/price-shape research (both endpoints verified live 2026-09-12,
robots.txt-allowed, <=3 requests to this host, >=10s apart).

Sale collection handle is `sale`, NOT `clearance` -- confirmed live:
`/collections/clearance` returns an empty product list on this host, while
`/collections/sale/products.json` returned real, non-empty results
(products with `compare_at_price` on their variants). See
knowledge/research/fishing-additional-retailers-2026-09-12.md.

Canonical host is `www.fishingonline.com`: a bare `fishingonline.com`
request (both `/robots.txt` and `/collections/...`) redirects there, and
robots.txt is itself served from the `www.` host -- `config/retailers.yaml`
`allowed_hosts` for this retailer is `www.fishingonline.com` to match.
"""

from __future__ import annotations

from fpt.adapters._shopify_collection import ShopifyCollectionAdapter

SELLER_KEY = "fishingonline"
SELLER_NAME = "Fishing Online"
BASE_URL = "https://www.fishingonline.com"
SALE_COLLECTION_HANDLE = "sale"


class FishingOnlineAdapter(ShopifyCollectionAdapter):
    def __init__(self) -> None:
        super().__init__(
            slug=SELLER_KEY,
            name=SELLER_NAME,
            base_url=BASE_URL,
            sale_collection_handle=SALE_COLLECTION_HANDLE,
        )
