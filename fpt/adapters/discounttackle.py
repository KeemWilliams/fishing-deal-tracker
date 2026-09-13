"""Discount Tackle (discounttackle.com) adapter.

Shopify storefront read via Shopify's own public JSON endpoints -- see
`fpt/adapters/_shopify_collection.py` for the shared implementation and
the endpoint/price-shape research (both endpoints verified live 2026-09-12,
robots.txt-allowed, <=3 requests to this host, >=10s apart).

Sale collection handle is `clearance` -- confirmed live and active:
`/collections/clearance/products.json` returned real, non-empty results
with `compare_at_price` present on every sampled variant and current
seasonal sale tags (`SummerMegaSale`, `LaborDay2023`). See
knowledge/research/fishing-additional-retailers-2026-09-12.md.

Canonical host is the bare apex `discounttackle.com` -- confirmed no `www.`
redirect on either `/robots.txt` or `/collections/...` requests;
`config/retailers.yaml` `allowed_hosts` for this retailer matches.
"""

from __future__ import annotations

from fpt.adapters._shopify_collection import ShopifyCollectionAdapter

SELLER_KEY = "discounttackle"
SELLER_NAME = "Discount Tackle"
BASE_URL = "https://discounttackle.com"
SALE_COLLECTION_HANDLE = "clearance"


class DiscountTackleAdapter(ShopifyCollectionAdapter):
    def __init__(self) -> None:
        super().__init__(
            slug=SELLER_KEY,
            name=SELLER_NAME,
            base_url=BASE_URL,
            sale_collection_handle=SALE_COLLECTION_HANDLE,
        )
