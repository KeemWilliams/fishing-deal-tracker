"""PowerPro (fishshop.shimano.com) adapter.

One of four brand adapters sharing the Shimano North America Fishing
storefront -- see `fpt/adapters/shimano.py` for the full family
docstring/modeling rationale (one retailer per brand, shared
`base_url`/`allowed_hosts`). `powerpro.com` redirects to this storefront.

Sale collection handle is `last-cast-savings-powerpro` -- this was NOT
confirmed by the scout research (knowledge/research/fishing-manufacturer-
sites-2026-09-13.md flagged it as unconfirmed, guessed from the sibling
brands' `last-cast-savings-<brand>` naming convention), but was verified
live 2026-09-13 during this task: `GET /collections/last-cast-savings-
powerpro/products.json` returns a real, non-empty collection (4+ products
including the high-volume "Super 8 Slick V2" line, most of whose variants
do NOT carry a `compare_at_price` -- only 2 of 214 sampled variants on
that one product did -- so this collection mixes real markdowns with
non-discounted "on this collection page but not on sale" rows; the shared
adapter's `compare_at_price > price` guard handles this the same way it
handles Berkley's EDLP rows).
"""

from __future__ import annotations

from fpt.adapters._shopify_collection import ShopifyCollectionAdapter

SELLER_KEY = "powerpro"
SELLER_NAME = "PowerPro"
BASE_URL = "https://fishshop.shimano.com"
SALE_COLLECTION_HANDLE = "last-cast-savings-powerpro"


class PowerProAdapter(ShopifyCollectionAdapter):
    def __init__(self) -> None:
        super().__init__(
            slug=SELLER_KEY,
            name=SELLER_NAME,
            base_url=BASE_URL,
            sale_collection_handle=SALE_COLLECTION_HANDLE,
        )
