from __future__ import annotations

import abc
import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional


class EbayNotConfiguredError(RuntimeError):
    """Raised when an eBay operation is attempted but no credentials are configured.

    Per spec section 17: 'If credentials are not configured, the
    application should run normally with eBay integration disabled and
    clearly report that state.' Callers (Discord cogs, background jobs)
    catch this and show a clear 'eBay not configured' message instead of
    crashing.
    """


class EbayApiError(RuntimeError):
    """A real eBay API call failed (network, auth, or business error)."""


@dataclass
class ListingResult:
    listing_id: str
    offer_id: str
    sku: str
    url: Optional[str] = None


@dataclass
class EbayOrderLineItem:
    sku: str
    quantity: int
    line_item_cost: Decimal


@dataclass
class EbayOrder:
    order_id: str
    line_items: list[EbayOrderLineItem]
    total_amount: Decimal
    total_fees: Decimal
    created_at: dt.datetime
    buyer_username: Optional[str] = None
    fulfillment_status: Optional[str] = None


@dataclass
class PriceStats:
    """Aggregate pricing stats for a search query + condition, from either
    actual sold comps or (as a fallback) current active listing prices."""
    source: str          # "SOLD" | "ACTIVE_LISTING_ESTIMATE"
    sample_count: int
    avg_price: Decimal
    median_price: Decimal
    min_price: Decimal
    max_price: Decimal
    currency: str = "GBP"


class EbayClientInterface(abc.ABC):
    """Everything the rest of the app needs from eBay. Two implementations:
    - integrations.ebay.client.EbayClient  (real REST calls)
    - integrations.ebay.mock.MockEbayClient (in-memory, used in tests and
      when EBAY_ENV is unset locally)
    """

    configured: bool

    @abc.abstractmethod
    async def create_listing(
        self, *, sku: str, title: str, description: str, price: Decimal, currency: str,
        quantity: int, condition_text: str, category_id: Optional[str],
    ) -> ListingResult: ...

    @abc.abstractmethod
    async def end_listing(self, listing_id: str, reason: str = "NotAvailable") -> None: ...

    @abc.abstractmethod
    async def get_orders_since(self, since: dt.datetime) -> list[EbayOrder]: ...

    @abc.abstractmethod
    async def get_listing_status(self, listing_id: str) -> Optional[str]: ...

    @abc.abstractmethod
    async def get_sold_price_stats(self, query: str, condition_ids: list[str], days_back: int = 90) -> Optional[PriceStats]:
        """Actual sold comps via eBay's Marketplace Insights API. This is a
        'limited release' API that requires separate eBay approval beyond a
        normal developer account - implementations should return None
        (never raise) when it's unavailable/unauthorized, so callers can
        fall back to get_active_listing_price_stats cleanly."""
        ...

    @abc.abstractmethod
    async def get_active_listing_price_stats(self, query: str, condition_ids: list[str]) -> Optional[PriceStats]:
        """Current asking prices via eBay's Browse API (always available
        with a standard developer account). Used as a fallback estimate
        when sold data isn't available - typically runs 10-20% above
        actual sold prices, since these are asking prices, not sold ones."""
        ...
