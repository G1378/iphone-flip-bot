from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.integrations.ebay.interface import EbayClientInterface, EbayNotConfiguredError
from app.services import pricing as pricing_service

logger = logging.getLogger(__name__)


async def run_pricing_refresh(session: AsyncSession, ebay: EbayClientInterface, actor_discord_id: int | None = None) -> dict[str, int]:
    """Thin wrapper so both the scheduler and the /pricing refresh Discord
    command share one code path."""
    if not ebay.buy_apis_configured:
        raise EbayNotConfiguredError(
            "eBay pricing lookups need EBAY_CLIENT_ID and EBAY_CLIENT_SECRET to be set."
        )
    return await pricing_service.refresh_all_watched(session, ebay, actor_discord_id)
