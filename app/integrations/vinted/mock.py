from __future__ import annotations

from decimal import Decimal
from typing import Optional

from app.integrations.vinted.interface import (
    VintedBlockedError, VintedClientInterface, VintedHealth, VintedItem, VintedNotConfiguredError,
)


class MockVintedClient(VintedClientInterface):
    """In-memory Vinted for tests and for when Vinted is switched off (configured=False)."""

    def __init__(self, configured: bool = True, reason: Optional[str] = None) -> None:
        self.configured = configured
        self.reason = reason
        self.search_results: list[tuple[str, list[VintedItem]]] = []
        self.descriptions: dict[str, Optional[str]] = {}
        self.searches: list[dict] = []
        self.description_lookups: list[str] = []
        self.block_after_searches: Optional[int] = None   # raise VintedBlockedError once this many searches happened

    def _ensure(self) -> None:
        if not self.configured:
            raise VintedNotConfiguredError(self.reason or "Vinted scanning is not enabled.")

    # test helpers
    def queue_search_results(self, query_contains: str, items: list[VintedItem]) -> None:
        self.search_results.append((query_contains.lower(), items))

    def queue_description(self, item_id: str, text: Optional[str]) -> None:
        self.descriptions[item_id] = text

    async def search_items(self, query: str, *, max_price: Optional[Decimal] = None, limit: int = 40) -> list[VintedItem]:
        self._ensure()
        if self.block_after_searches is not None and len(self.searches) >= self.block_after_searches:
            raise VintedBlockedError("blocked (mock)")
        self.searches.append({"query": query, "max_price": max_price})
        for needle, items in self.search_results:
            if needle in query.lower():
                return list(items)[:limit]
        return []

    async def get_description(self, item_id: str) -> Optional[str]:
        self._ensure()
        self.description_lookups.append(item_id)
        return self.descriptions.get(item_id)

    async def probe(self, query: str = "iPhone 13") -> list[str]:
        return ["(mock client: no real request was made)"] if self.configured else [self.reason or "Vinted is off."]

    def health(self) -> VintedHealth:
        return VintedHealth(configured=self.configured, reason=self.reason)
