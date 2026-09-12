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
