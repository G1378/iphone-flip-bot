from __future__ import annotations

import datetime as dt
import itertools
from decimal import Decimal
from typing import Optional

from app.integrations.ebay.interface import (
    EbayApiError,
    EbayClientInterface,
    EbayItem,
    EbayNotConfiguredError,
    EbayOrder,
    EbayOrderLineItem,
    ListingResult,
    PolicyInfo,
    PriceStats,
    PublishingSetup,
)


class MockEbayClient(EbayClientInterface):
    """In-memory eBay client used in tests. Never makes network calls.

    `configured=True` by default so business-logic tests can exercise the
    full publish/sync flow; set `configured=False` to test the
    'eBay not configured' code paths.
    """

    def __init__(self, configured: bool = True) -> None:
        self.configured = configured
        self.buy_apis_configured = configured
        self._id_counter = itertools.count(1000)
        self.listings: dict[str, dict] = {}       # listing_id -> data
        self.pending_orders: list[EbayOrder] = []  # queued for get_orders_since
        self.uploaded_images: list[str] = []
        self.search_results: list[tuple[str, list[EbayItem]]] = []   # (query contains, items)
        self.searches: list[dict] = []                               # every search made, for tests
        self.setup = PublishingSetup(
            fulfillment_policies=[PolicyInfo("FP-1", "Tracked 24", True)],
            payment_policies=[PolicyInfo("PP-1", "eBay payments", True)],
            return_policies=[PolicyInfo("RP-1", "30 day returns", True)],
            locations=[PolicyInfo("workshop", "Workshop", True)],
        )
        self._sold_stats: dict[str, PriceStats] = {}     # query -> canned stats
        self._active_stats: dict[str, PriceStats] = {}

    def _ensure_configured(self) -> None:
        if not self.configured:
            raise EbayNotConfiguredError("eBay integration is not configured (mock).")

    async def create_listing(
        self, *, sku: str, title: str, description: str, price: Decimal, currency: str,
        quantity: int, condition_text: str, category_id: Optional[str],
        condition_description: Optional[str] = None, aspects: Optional[dict[str, list[str]]] = None,
        image_urls: Optional[list[str]] = None, fulfillment_policy_id: Optional[str] = None,
        payment_policy_id: Optional[str] = None, return_policy_id: Optional[str] = None,
        merchant_location_key: Optional[str] = None, best_offer_enabled: bool = False,
        best_offer_auto_decline: Optional[Decimal] = None, best_offer_auto_accept: Optional[Decimal] = None,
    ) -> ListingResult:
        self._ensure_configured()
        listing_id = str(next(self._id_counter))
        offer_id = f"OFFER-{listing_id}"
        self.listings[listing_id] = {
            "sku": sku, "title": title, "price": price, "status": "ACTIVE", "offer_id": offer_id,
            "description": description, "condition_description": condition_description, "aspects": aspects,
            "image_urls": image_urls, "fulfillment_policy_id": fulfillment_policy_id,
            "payment_policy_id": payment_policy_id, "return_policy_id": return_policy_id,
            "merchant_location_key": merchant_location_key, "best_offer_enabled": best_offer_enabled,
            "best_offer_auto_decline": best_offer_auto_decline, "best_offer_auto_accept": best_offer_auto_accept,
        }
        return ListingResult(listing_id=listing_id, offer_id=offer_id, sku=sku,
                              url=f"https://mock.ebay.local/itm/{listing_id}")

    async def end_listing(self, offer_id: str, reason: str = "NotAvailable") -> None:
        self._ensure_configured()
        # Like the real API: ending works on the OFFER id; a public listing id finds nothing.
        for data in self.listings.values():
            if data["offer_id"] == offer_id:
                data["status"] = "ENDED"
                return
        raise EbayApiError(f"Failed to end listing: no offer {offer_id}")

    async def upload_image(self, data: bytes, filename: str, content_type: str) -> str:
        self._ensure_configured()
        url = f"https://mock.ebay.local/img/{len(self.uploaded_images) + 1}-{filename}"
        self.uploaded_images.append(url)
        return url

    def queue_search_results(self, query_contains: str, items: list[EbayItem]) -> None:
        self.search_results.append((query_contains.lower(), items))

    async def search_items(self, query: str, *, category_id: Optional[str] = None, max_price: Optional[Decimal] = None,
                           limit: int = 50, newest_first: bool = True) -> list[EbayItem]:
        self._ensure_configured()
        self.searches.append({"query": query, "category_id": category_id, "max_price": max_price})
        for needle, items in self.search_results:
            if needle in query.lower():
                return list(items)[:limit]
        return []

    async def get_publishing_setup(self) -> PublishingSetup:
        self._ensure_configured()
        return self.setup

    async def create_location(self, key: str, name: str, postcode: str, country: str = "GB") -> None:
        self._ensure_configured()
        self.setup.locations.append(PolicyInfo(key, name, not self.setup.locations))

    async def get_orders_since(self, since: dt.datetime) -> list[EbayOrder]:
        self._ensure_configured()
        orders = [o for o in self.pending_orders if o.created_at >= since]
        return orders

    async def get_listing_status(self, offer_id: str) -> Optional[str]:
        self._ensure_configured()
        entry = next((d for d in self.listings.values() if d["offer_id"] == offer_id), None)
        return entry["status"] if entry else None

    async def get_sold_price_stats(self, query: str, condition_ids: list, days_back: int = 90) -> Optional[PriceStats]:
        self._ensure_configured()
        return self._sold_stats.get(query)

    async def get_active_listing_price_stats(self, query: str, condition_ids: list) -> Optional[PriceStats]:
        self._ensure_configured()
        return self._active_stats.get(query)

    # test helpers, not part of the interface
    def queue_sold_stats(self, query: str, stats: PriceStats) -> None:
        self._sold_stats[query] = stats

    def queue_active_listing_stats(self, query: str, stats: PriceStats) -> None:
        self._active_stats[query] = stats

    def queue_sale(self, *, order_id: str, sku: str, price: Decimal, fees: Decimal,
                    created_at: Optional[dt.datetime] = None, payment_status: Optional[str] = None,
                    cancel_state: Optional[str] = None) -> None:
        self.pending_orders.append(
            EbayOrder(
                order_id=order_id,
                line_items=[EbayOrderLineItem(sku=sku, quantity=1, line_item_cost=price)],
                total_amount=price,
                total_fees=fees,
                created_at=created_at or dt.datetime.now(dt.timezone.utc),
                payment_status=payment_status, cancel_state=cancel_state,
            )
        )
