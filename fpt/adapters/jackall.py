"""Jackall Lures (fishshop.shimano.com) adapter.

One of four brand adapters sharing the Shimano North America Fishing
storefront -- see `fpt/adapters/shimano.py` for the full family
docstring/modeling rationale (one retailer per brand, shared
`base_url`/`allowed_hosts`).

Sale collection handle is `last-cast-savings-jackall` (verified live
2026-09-13, real strikethrough discounts present -- e.g. compare_at_price
$24.99 vs price $10.07 on the "DUNKLE" lure).
"""

from __future__ import annotations

from fpt.adapters._shopify_collection import ShopifyCollectionAdapter

SELLER_KEY = "jackall"
SELLER_NAME = "Jackall Lures"
BASE_URL = "https://fishshop.shimano.com"
SALE_COLLECTION_HANDLE = "last-cast-savings-jackall"


class JackallAdapter(ShopifyCollectionAdapter):
    def __init__(self) -> None:
        super().__init__(
            slug=SELLER_KEY,
            name=SELLER_NAME,
            base_url=BASE_URL,
            sale_collection_handle=SALE_COLLECTION_HANDLE,
        )
