from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.commerce_models import Listing, Sale
from app.models.inventory_models import Donor, Part, Phone


@dataclass
class SearchResults:
    phones: Sequence[Phone]
    parts: Sequence[Part]
    donors: Sequence[Donor]
    listings: Sequence[Listing]
    sales: Sequence[Sale]

    def is_empty(self) -> bool:
        return not (self.phones or self.parts or self.donors or self.listings or self.sales)


async def global_search(session: AsyncSession, term: str) -> SearchResults:
    like = f"%{term}%"

    phones_stmt = (
        select(Phone)
        .options(selectinload(Phone.location))
        .where(
            or_(
                Phone.internal_id.ilike(like), Phone.imei.ilike(like), Phone.serial_number.ilike(like),
                Phone.model.ilike(like), Phone.variant.ilike(like), Phone.storage.ilike(like),
                Phone.colour.ilike(like), Phone.current_status.ilike(like),
            )
        )
        .limit(15)
    )
    parts_stmt = (
        select(Part)
        .options(selectinload(Part.part_type), selectinload(Part.location))
        .where(or_(Part.internal_id.ilike(like), Part.status.ilike(like)))
        .limit(15)
    )
    donors_stmt = (
        select(Donor)
        .where(or_(Donor.internal_id.ilike(like), Donor.model.ilike(like), Donor.current_status.ilike(like)))
        .limit(15)
    )
    listings_stmt = (
        select(Listing)
        .where(or_(Listing.internal_id.ilike(like), Listing.ebay_listing_id.ilike(like), Listing.title.ilike(like)))
        .limit(15)
    )
    sales_stmt = (
        select(Sale)
        .where(or_(Sale.internal_id.ilike(like), Sale.ebay_order_id.ilike(like)))
        .limit(15)
    )

    phones = (await session.execute(phones_stmt)).scalars().all()
    parts = (await session.execute(parts_stmt)).scalars().all()
    donors = (await session.execute(donors_stmt)).scalars().all()
    listings = (await session.execute(listings_stmt)).scalars().all()
    sales = (await session.execute(sales_stmt)).scalars().all()

    return SearchResults(phones=phones, parts=parts, donors=donors, listings=listings, sales=sales)
