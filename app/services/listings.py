from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.integrations.ebay.interface import EbayClientInterface, EbayNotConfiguredError
from app.models.commerce_models import Listing
from app.models.enums import InventoryEventType
from app.models.inventory_models import Phone
from app.services.events import log_event
from app.services.ids import next_internal_id


class ListingError(ValueError):
    pass


@dataclass
class DraftListingInput:
    title: str
    description: str
    condition_text: str
    price: Decimal
    category_id: Optional[str] = None
    quantity: int = 1
    currency: str = "GBP"


async def create_draft(session: AsyncSession, phone: Phone, data: DraftListingInput, actor_discord_id: int) -> Listing:
    internal_id = await next_internal_id(session, "listing")
    listing = Listing(
        internal_id=internal_id,
        phone=phone,  # relationship assignment (not phone_id) keeps listing.phone populated in-memory
        title=data.title,
        description=data.description,
        condition_text=data.condition_text,
        category_id=data.category_id,
        price=data.price,
        currency=data.currency,
        quantity=data.quantity,
        status="DRAFT",
        created_by_discord_id=actor_discord_id,
    )
    session.add(listing)
    await session.flush()

    await log_event(
        session, event_type=InventoryEventType.LISTING_CREATED.value, entity_type="LISTING",
        entity_id=listing.internal_id, actor_discord_id=actor_discord_id,
        related_entity_type="PHONE", related_entity_id=phone.internal_id,
        new_state={"title": data.title, "price": str(data.price)},
    )
    return listing


async def get_listing(session: AsyncSession, internal_id: str) -> Optional[Listing]:
    stmt = (
        select(Listing)
        .where(Listing.internal_id == internal_id.upper())
        .options(selectinload(Listing.phone))
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def publish(
    session: AsyncSession, listing: Listing, ebay: EbayClientInterface, actor_discord_id: int,
) -> Listing:
    """Publish a DRAFT listing to eBay. Requires explicit prior confirmation
    from the Discord UI layer - this function itself performs no
    confirmation, it just executes the publish once called."""
    if listing.status != "DRAFT":
        raise ListingError(f"{listing.internal_id} is not a DRAFT (currently {listing.status}).")

    result = await ebay.create_listing(
        sku=listing.internal_id,
        title=listing.title,
        description=listing.description,
        price=listing.price,
        currency=listing.currency,
        quantity=listing.quantity,
        condition_text=listing.condition_text,
        category_id=listing.category_id,
    )

    listing.ebay_listing_id = result.listing_id
    listing.ebay_offer_id = result.offer_id
    listing.ebay_sku = result.sku
    listing.ebay_url = result.url
    listing.status = "ACTIVE"
    listing.listed_at = dt.datetime.now(dt.timezone.utc)

    listing.phone.current_status = "LISTED"
    await session.flush()

    await log_event(
        session, event_type=InventoryEventType.LISTING_PUBLISHED.value, entity_type="LISTING",
        entity_id=listing.internal_id, actor_discord_id=actor_discord_id,
        related_entity_type="PHONE", related_entity_id=listing.phone.internal_id,
        new_state={"ebay_listing_id": result.listing_id, "url": result.url},
    )
    return listing


async def end_listing(session: AsyncSession, listing: Listing, ebay: EbayClientInterface, actor_discord_id: int,
                       reason: str = "NotAvailable") -> Listing:
    if listing.status != "ACTIVE":
        raise ListingError(f"{listing.internal_id} is not ACTIVE (currently {listing.status}).")
    if listing.ebay_listing_id:
        await ebay.end_listing(listing.ebay_listing_id, reason=reason)
    listing.status = "ENDED"
    listing.ended_at = dt.datetime.now(dt.timezone.utc)
    await session.flush()

    await log_event(
        session, event_type=InventoryEventType.LISTING_ENDED.value, entity_type="LISTING",
        entity_id=listing.internal_id, actor_discord_id=actor_discord_id, notes=reason,
    )
    return listing


async def list_active(session: AsyncSession) -> Sequence[Listing]:
    stmt = select(Listing).where(Listing.status == "ACTIVE").options(selectinload(Listing.phone))
    return (await session.execute(stmt)).scalars().all()
