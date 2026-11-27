from __future__ import annotations

import abc
import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional


class VintedNotConfiguredError(RuntimeError):
    """Vinted scanning is off (VINTED_ENABLED) or has no proxy while one is required."""


class VintedBlockedError(RuntimeError):
    """Vinted is refusing us (captcha / 403 / 429) on every route we have. Back off; don't hammer."""


class VintedApiError(RuntimeError):
    """A Vinted request failed in a way that isn't a block (bad response, network error)."""


@dataclass
class VintedItem:
    """A live Vinted listing found by a search."""
    item_id: str
    title: str
    price: Decimal                              # the item price (what the seller asks)
    url: str
    buyer_fee: Optional[Decimal] = None         # Buyer Protection fee, when Vinted states it. None = unknown
    condition: Optional[str] = None             # Vinted's own wording: "New with tags", "Very good", ...
    brand: Optional[str] = None
    description: Optional[str] = None           # usually NOT in search results: see get_description
    seller: Optional[str] = None
    seller_id: Optional[str] = None
    seller_feedback_pct: Optional[Decimal] = None
    seller_reviews: Optional[int] = None
    image_url: Optional[str] = None
    listed_at: Optional[dt.datetime] = None
    extra: dict = field(default_factory=dict)


@dataclass
class VintedHealth:
    """What /deal-status shows: is the scraper alive, or being blocked?"""
    configured: bool
    reason: Optional[str] = None                # why it is off, when it is
    routes: int = 0
    routes_available: int = 0
    cooling: list[tuple[str, int]] = field(default_factory=list)   # (route name, minutes left)
    requests_ok: int = 0
    requests_blocked: int = 0
    last_ok_at: Optional[dt.datetime] = None
    last_error: Optional[str] = None


class VintedClientInterface(abc.ABC):
    configured: bool

    @abc.abstractmethod
    async def search_items(self, query: str, *, max_price: Optional[Decimal] = None, limit: int = 40) -> list[VintedItem]:
        """Newest listings matching `query`. Raises VintedBlockedError / VintedApiError."""

    @abc.abstractmethod
    async def get_description(self, item_id: str) -> Optional[str]:
        """The seller's description (search results don't carry it). None if it can't be read.
        Raises VintedBlockedError if Vinted is blocking us (so callers stop instead of retrying)."""

    @abc.abstractmethod
    def health(self) -> VintedHealth: ...

    async def probe(self, query: str = "iPhone 13") -> list[str]:
        """One home-page request and one search, reported step by step (for /vinted-test). Never raises."""
        return ["Not available for this client."]

    async def aclose(self) -> None:  # pragma: no cover - nothing to close for the mock
        return None
