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
    delivery_cost: Decimal = Decimal("0")          # postage the buyer was charged (part of total_amount)
    payment_status: Optional[str] = None           # eBay orderPaymentStatus: PAID, PENDING, FAILED...
    cancel_state: Optional[str] = None             # eBay cancelStatus.cancelState: NONE_REQUESTED, CANCELED...


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


@dataclass
class EbayItem:
    """A live eBay listing found by a search (what you'd be buying)."""
    item_id: str
    title: str
    price: Decimal                            # item price, or the current bid for an auction
    url: str
    shipping_cost: Optional[Decimal] = None   # None = eBay didn't say
    condition: Optional[str] = None
    condition_id: Optional[str] = None
    buying_options: list[str] = field(default_factory=list)   # FIXED_PRICE, AUCTION, BEST_OFFER
    seller: Optional[str] = None
    seller_feedback_pct: Optional[Decimal] = None
    seller_feedback_score: Optional[int] = None
    image_url: Optional[str] = None
    ends_at: Optional[dt.datetime] = None     # auctions

    @property
    def is_auction(self) -> bool:
        return "AUCTION" in self.buying_options


@dataclass
class PolicyInfo:
    """One of the seller's eBay business policies or stock locations."""
    id: str
    name: str
    is_default: bool = False


@dataclass
class PublishingSetup:
    """What the seller account already has configured on eBay, needed to publish."""
    fulfillment_policies: list[PolicyInfo] = field(default_factory=list)   # postage
    payment_policies: list[PolicyInfo] = field(default_factory=list)
    return_policies: list[PolicyInfo] = field(default_factory=list)
    locations: list[PolicyInfo] = field(default_factory=list)              # where the stock is


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
        condition_description: Optional[str] = None, aspects: Optional[dict[str, list[str]]] = None,
        image_urls: Optional[list[str]] = None, fulfillment_policy_id: Optional[str] = None,
        payment_policy_id: Optional[str] = None, return_policy_id: Optional[str] = None,
        merchant_location_key: Optional[str] = None, best_offer_enabled: bool = False,
        best_offer_auto_decline: Optional[Decimal] = None, best_offer_auto_accept: Optional[Decimal] = None,
    ) -> ListingResult: ...

    @abc.abstractmethod
    async def end_listing(self, offer_id: str, reason: str = "NotAvailable") -> None:
        """End a live listing. eBay ends listings by withdrawing the *offer*, so this takes
        the offer id (Listing.ebay_offer_id), NOT the public listing id."""

    @abc.abstractmethod
    async def upload_image(self, data: bytes, filename: str, content_type: str) -> str:
        """Upload a photo to eBay's picture hosting and return its public https URL."""

    @abc.abstractmethod
    async def get_publishing_setup(self) -> PublishingSetup:
        """The seller's postage/payment/returns policies and stock locations."""

    @abc.abstractmethod
    async def create_location(self, key: str, name: str, postcode: str, country: str = "GB") -> None:
        """Create the stock location listings are published from."""

    @abc.abstractmethod
    async def get_orders_since(self, since: dt.datetime) -> list[EbayOrder]: ...

    @abc.abstractmethod
    async def search_items(self, query: str, *, category_id: Optional[str] = None, max_price: Optional[Decimal] = None,
                           limit: int = 50, newest_first: bool = True) -> list[EbayItem]:
        """Search live eBay listings (Buy/Browse API: needs no seller login)."""

    @abc.abstractmethod
    async def get_listing_status(self, offer_id: str) -> Optional[str]: ...

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
