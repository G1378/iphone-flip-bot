from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.enums import InventoryEventType, PartSourceType, TestOutcome
from app.models.inventory_models import Donor, Part
from app.services import config_service
from app.services.events import log_event
from app.services.ids import next_internal_id


@dataclass
class BuyDonorInput:
    manufacturer: str
    model: str
    variant: Optional[str]
    storage: Optional[str]
    colour: Optional[str]
    purchase_price: Decimal
    purchase_date: dt.date
    seller_source: Optional[str]
    fault_description: Optional[str]
    notes: Optional[str]
    location_code: Optional[str]


@dataclass
class RecoveredPartInput:
    part_type_name: str
    condition: Optional[str]
    test_result: TestOutcome
    grade_code: Optional[str]
    notes: Optional[str]
    location_code: Optional[str]
    discarded: bool = False


async def buy_donor(session: AsyncSession, data: BuyDonorInput, actor_discord_id: int) -> Donor:
    await config_service.get_or_create_phone_model(session, data.manufacturer, data.model, data.variant)
    location = None
    if data.location_code:
        location = await config_service.get_location_by_code(session, data.location_code)

    internal_id = await next_internal_id(session, "donor")
    donor = Donor(
        internal_id=internal_id,
        manufacturer=data.manufacturer,
        model=data.model,
        variant=data.variant,
        storage=data.storage,
        colour=data.colour,
        purchase_price=data.purchase_price,
        purchase_date=data.purchase_date,
        seller_source=data.seller_source,
        fault_description=data.fault_description,
        current_status="AWAITING_TEARDOWN",
        location_id=location.id if location else None,
        notes=data.notes,
    )
    session.add(donor)
    await session.flush()

    await log_event(
        session,
        event_type=InventoryEventType.DONOR_PURCHASED.value,
        entity_type="DONOR",
        entity_id=donor.internal_id,
        actor_discord_id=actor_discord_id,
        new_state={"model": donor.display_name(), "purchase_price": str(donor.purchase_price)},
    )
    return donor


async def get_donor(session: AsyncSession, internal_id: str) -> Optional[Donor]:
    stmt = (
        select(Donor)
        .where(Donor.internal_id == internal_id.upper())
        .options(selectinload(Donor.parts_recovered), selectinload(Donor.location))
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def list_donors(session: AsyncSession, status: Optional[str] = None) -> Sequence[Donor]:
    stmt = select(Donor).options(selectinload(Donor.location)).order_by(Donor.created_at.desc())
    if status:
        stmt = stmt.where(Donor.current_status == status)
    return (await session.execute(stmt)).scalars().all()


async def teardown(
    session: AsyncSession,
    donor: Donor,
    recovered: list[RecoveredPartInput],
    actor_discord_id: int,
) -> list[Part]:
    """Record teardown results. Creates a Part row for every recovered
    item (status AVAILABLE) and SCRAPPED Part rows for discarded items so
    the full teardown yield remains queryable. Cost allocation happens
    separately via services.allocation (spec section 11)."""
    created: list[Part] = []
    for item in recovered:
        part_type = await config_service.get_or_create_part_type(session, item.part_type_name)
        location = None
        if item.location_code and not item.discarded:
            location = await config_service.get_location_by_code(session, item.location_code)

        internal_id = await next_internal_id(session, "part")
        part = Part(
            internal_id=internal_id,
            part_type_id=part_type.id,
            source_type=PartSourceType.DONOR,
            source_donor=donor,  # relationship assignment keeps donor.parts_recovered populated in-memory
            cost=Decimal("0"),  # allocated later via allocation service
            condition=item.condition,
            grade_code=item.grade_code,
            testing_status=item.test_result,
            location_id=location.id if location else None,
            status="SCRAPPED" if item.discarded else "AVAILABLE",
            purchase_date=donor.purchase_date,
            notes=item.notes,
        )
        session.add(part)
        await session.flush()
        created.append(part)

        await log_event(
            session,
            event_type=InventoryEventType.PART_CREATED.value,
            entity_type="PART",
            entity_id=part.internal_id,
            actor_discord_id=actor_discord_id,
            related_entity_type="DONOR",
            related_entity_id=donor.internal_id,
            new_state={"part_type": part_type.name, "status": part.status, "test_result": item.test_result.value},
            notes="Recovered from donor teardown" if not item.discarded else "Discarded during teardown",
        )

    donor.current_status = "TORN_DOWN"
    donor.torn_down_at = dt.datetime.now(dt.timezone.utc)
    await session.flush()

    await log_event(
        session,
        event_type=InventoryEventType.DONOR_DISMANTLED.value,
        entity_type="DONOR",
        entity_id=donor.internal_id,
        actor_discord_id=actor_discord_id,
        new_state={"parts_recovered": len([r for r in recovered if not r.discarded]),
                   "parts_discarded": len([r for r in recovered if r.discarded])},
    )
    return created


async def value_recovered(session: AsyncSession, donor: Donor) -> Decimal:
    """Total value recovered from a donor = sum of parts that have been
    sold (via installed phone sale) is out of scope for MVP; here we
    report the sum of current allocated cost across all non-scrapped
    recovered parts, as a proxy for 'value extracted so far'."""
    stmt = select(Part).where(Part.source_donor_id == donor.id, Part.status != "SCRAPPED")
    parts = (await session.execute(stmt)).scalars().all()
    return sum((p.cost for p in parts), Decimal("0"))
