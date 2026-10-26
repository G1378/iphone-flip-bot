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
from app.services import config_service, listing_builder
from app.services.events import log_event
from app.services.ids import next_internal_id


class ListingError(ValueError):
    pass


class ListingNotReady(ListingError):
    """Publishing was refused because something eBay needs is missing."""

    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__("Not ready to publish:\n" + "\n".join(f"• {p}" for p in problems))


@dataclass
class DraftListingInput:
    title: str
    description: str
    condition_text: str
    price: Decimal
    category_id: Optional[str] = None
    quantity: int = 1
    currency: str = "GBP"
    condition_description: Optional[str] = None
    aspects: Optional[dict] = None


async def create_draft(session: AsyncSession, phone: Phone, data: DraftListingInput, actor_discord_id: int) -> Listing:
    internal_id = await next_internal_id(session, "listing")
    listing = Listing(
        internal_id=internal_id,
        phone=phone,  # relationship assignment (not phone_id) keeps listing.phone populated in-memory
        title=data.title,
        description=data.description,
        condition_text=data.condition_text,
        condition_description=data.condition_description,
        aspects=data.aspects,
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


async def get_draft_for_phone(session: AsyncSession, phone_id: int) -> Optional[Listing]:
    """The phone's open DRAFT listing (the newest one), if any."""
    stmt = (select(Listing).where(Listing.phone_id == phone_id, Listing.status == "DRAFT")
            .order_by(Listing.id.desc()).limit(1).options(selectinload(Listing.phone)))
    return (await session.execute(stmt)).scalar_one_or_none()


async def build_or_refresh_draft(session: AsyncSession, phone: Phone, actor_discord_id: int
                                  ) -> tuple[Listing, "listing_builder.BuiltListing"]:
    """Auto-build the listing from the phone's record. Creates the draft, or rewrites the
    existing draft so a rebuild picks up changes (it replaces any hand edits to the draft).
    Leaves a price someone already set alone when no estimate is available."""
    built = await listing_builder.build_for_phone(session, phone)
    draft = await get_draft_for_phone(session, phone.id)
    if draft is None:
        draft = await create_draft(session, phone, DraftListingInput(
            title=built.title, description=built.description, condition_text=built.condition_text,
            price=built.price or Decimal("0"), condition_description=built.condition_description,
            aspects=built.aspects), actor_discord_id)
    else:
        draft.title, draft.description = built.title, built.description
        draft.condition_text, draft.condition_description = built.condition_text, built.condition_description
        draft.aspects = built.aspects
        if built.price is not None:
            draft.price = built.price
        await session.flush()
    return draft, built


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
    if not ebay.configured:
        raise EbayNotConfiguredError("eBay integration is not configured.")

    problems = listing_builder.blockers(listing, listing.phone)
    gaps = await listing_builder.setup_gaps(session)
    if gaps:
        problems.append("eBay setup isn't finished (" + ", ".join(gaps) + "): run `/config ebay-setup`")
    if problems:
        raise ListingNotReady(problems)

    async def setting(key: str) -> Optional[str]:
        return (await config_service.get_setting(session, key, "")) or None

    offer = await listing_builder.best_offer_terms(session, listing, listing.phone)

    result = await ebay.create_listing(
        sku=listing.internal_id,
        title=listing.title,
        description=listing.description,
        price=listing.price,
        currency=listing.currency,
        quantity=listing.quantity,
        condition_text=listing.condition_text,
        category_id=listing.category_id,
        condition_description=listing.condition_description,
        aspects=listing.aspects,
        image_urls=list(listing.phone.photo_urls or []),
        fulfillment_policy_id=await setting("ebay_fulfillment_policy_id"),
        payment_policy_id=await setting("ebay_payment_policy_id"),
        return_policy_id=await setting("ebay_return_policy_id"),
        merchant_location_key=await setting("ebay_merchant_location_key"),
        best_offer_enabled=offer.enabled, best_offer_auto_decline=offer.auto_decline,
        best_offer_auto_accept=offer.auto_accept,
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
        if not listing.ebay_offer_id:
            raise ListingError(f"{listing.internal_id} has no eBay offer id on record, so it can't be ended from here: "
                               f"end it on eBay (item {listing.ebay_listing_id}).")
        await ebay.end_listing(listing.ebay_offer_id, reason=reason)
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


@dataclass
class SetupResult:
    chosen: dict[str, Optional[object]]      # setting key -> PolicyInfo picked (or None)
    options: dict[str, list]                 # setting key -> every PolicyInfo available
    created_location: bool = False


_SETUP_KEYS = {
    "ebay_fulfillment_policy_id": "fulfillment_policies",
    "ebay_payment_policy_id": "payment_policies",
    "ebay_return_policy_id": "return_policies",
    "ebay_merchant_location_key": "locations",
}


def pick_default(options: list):
    """The one obvious choice: the only option, or the only one eBay marks as default."""
    if len(options) == 1:
        return options[0]
    defaults = [o for o in options if o.is_default]
    return defaults[0] if len(defaults) == 1 else None


async def configure_publishing(session: AsyncSession, ebay: EbayClientInterface, overrides: Optional[dict] = None,
                               postcode: Optional[str] = None) -> SetupResult:
    """Read the policies and stock location from the seller's eBay account and save the
    ones listings will use. Anything ambiguous is left for a person to choose (overrides)."""
    overrides = overrides or {}
    setup = await ebay.get_publishing_setup()
    created = False
    if postcode and not setup.locations:
        await ebay.create_location("workshop", "Workshop", postcode.strip())
        setup = await ebay.get_publishing_setup()
        created = True
    result = SetupResult(chosen={}, options={}, created_location=created)
    for key, attr in _SETUP_KEYS.items():
        options = list(getattr(setup, attr))
        result.options[key] = options
        wanted = overrides.get(key)
        if wanted:
            match = next((o for o in options if o.id == wanted), None)
            if match is None:
                raise ListingError(f"eBay has no {attr.replace('_', ' ')} with id '{wanted}'.")
            result.chosen[key] = match
        else:
            result.chosen[key] = pick_default(options)
        if result.chosen[key]:
            await config_service.set_setting(session, key, result.chosen[key].id)
    return result
