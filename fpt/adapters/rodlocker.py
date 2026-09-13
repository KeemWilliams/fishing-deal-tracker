"""Rod Locker (rodlocker.com) adapter.

Shopify storefront read via Shopify's own public JSON endpoints -- see
`fpt/adapters/_shopify_collection.py` for the shared implementation and
the endpoint/price-shape research (both endpoints verified live 2026-09-12,
robots.txt-allowed, <=3 requests to this host, >=10s apart).

American Legacy Fishing Co. (americanlegacyfishing.com) 301-redirects
entirely to this host and is NOT tracked as a separate retailer -- treat
any reference to "American Legacy Fishing" as this same retailer going
forward (see the research doc's note on this).

Sale collection handle is `sale`, NOT `clearance` -- confirmed live:
`/collections/clearance` is empty on this host, matching the same gotcha
seen on fishingonline.com; `/collections/sale/products.json` returned
real, non-empty results (variants with `compare_at_price` present). See
knowledge/research/fishing-additional-retailers-2026-09-12.md.

Canonical host is the bare apex `rodlocker.com` -- confirmed no `www.`
redirect on either `/robots.txt` or `/collections/...` requests;
`config/retailers.yaml` `allowed_hosts` for this retailer matches.
"""

from __future__ import annotations

from fpt.adapters._shopify_collection import ShopifyCollectionAdapter

SELLER_KEY = "rodlocker"
SELLER_NAME = "Rod Locker"
BASE_URL = "https://rodlocker.com"
SALE_COLLECTION_HANDLE = "sale"


class RodLockerAdapter(ShopifyCollectionAdapter):
    def __init__(self) -> None:
        super().__init__(
            slug=SELLER_KEY,
            name=SELLER_NAME,
            base_url=BASE_URL,
            sale_collection_handle=SALE_COLLECTION_HANDLE,
        )
