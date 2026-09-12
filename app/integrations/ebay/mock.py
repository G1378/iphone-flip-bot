from __future__ import annotations

import datetime as dt
import itertools
from decimal import Decimal
from typing import Optional

from app.integrations.ebay.interface import (
    EbayClientInterface,
    EbayNotConfiguredError,
    EbayOrder,
    EbayOrderLineItem,
    ListingResult,
)


class MockEbayClient(EbayClientInterface):
    """In-memory eBay client used in tests. Never makes network calls.

    `configured=True` by default so business-logic tests can exercise the
    full publish/sync flow; set `configured=False` to test the
    'eBay not configured' code paths.
    """

    def __init__(self, configured: bool = True) -> None:
        self.configured = configured
        self._id_counter = itertools.count(1000)
        self.listings: dict[str, dict] = {}       # listing_id -> data
        self.pending_orders: list[EbayOrder] = []  # queued for get_orders_since

    def _ensure_configured(self) -> None:
        if not self.configured:
            raise EbayNotConfiguredError("eBay integration is not configured (mock).")

    async def create_listing(
        self, *, sku: str, title: str, description: str, price: Decimal, currency: str,
        quantity: int, condition_text: str, category_id: Optional[str],
    ) -> ListingResult:
        self._ensure_configured()
        listing_id = str(next(self._id_counter))
        offer_id = f"OFFER-{listing_id}"
        self.listings[listing_id] = {
            "sku": sku, "title": title, "price": price, "status": "ACTIVE", "offer_id": offer_id,
        }
        return ListingResult(listing_id=listing_id, offer_id=offer_id, sku=sku,
                              url=f"https://mock.ebay.local/itm/{listing_id}")

    async def end_listing(self, listing_id: str, reason: str = "NotAvailable") -> None:
        self._ensure_configured()
        if listing_id in self.listings:
            self.listings[listing_id]["status"] = "ENDED"

    async def get_orders_since(self, since: dt.datetime) -> list[EbayOrder]:
        self._ensure_configured()
        orders = [o for o in self.pending_orders if o.created_at >= since]
        return orders

    async def get_listing_status(self, listing_id: str) -> Optional[str]:
        self._ensure_configured()
        entry = self.listings.get(listing_id)
        return entry["status"] if entry else None

    # test helper, not part of the interface
    def queue_sale(self, *, order_id: str, sku: str, price: Decimal, fees: Decimal,
                    created_at: Optional[dt.datetime] = None) -> None:
        self.pending_orders.append(
            EbayOrder(
                order_id=order_id,
                line_items=[EbayOrderLineItem(sku=sku, quantity=1, line_item_cost=price)],
                total_amount=price,
                total_fees=fees,
                created_at=created_at or dt.datetime.now(dt.timezone.utc),
            )
        )
