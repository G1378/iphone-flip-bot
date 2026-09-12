from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.enums import FaultStatus, InventoryEventType, RepairPartLineStatus
from app.models.inventory_models import Part, Phone, PhoneFault
from app.models.repair_models import CostAllocation, Repair, RepairPart
from app.services.events import log_event
from app.services.ids import next_internal_id
from app.services.parts import find_compatible_available_parts


class RepairError(ValueError):
    pass


async def get_repair(session: AsyncSession, internal_id: str) -> Optional[Repair]:
    stmt = (
        select(Repair)
        .where(Repair.internal_id == internal_id.upper())
        .options(
            selectinload(Repair.phone),
            selectinload(Repair.parts).selectinload(RepairPart.required_part_type),
            selectinload(Repair.parts).selectinload(RepairPart.part),
            selectinload(Repair.faults),
        )
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def create_repair_for_open_faults(session: AsyncSession, phone: Phone, actor_discord_id: int) -> Repair:
    """Create (or return existing PLANNED/IN_PROGRESS) repair covering all
    of a phone's currently OPEN faults, with one RepairPart requirement
    line per fault that has a known required_part_type_id."""
    existing_stmt = (
        select(Repair)
        .where(Repair.phone_id == phone.id, Repair.status.in_(["PLANNED", "PARTS_RESERVED", "IN_PROGRESS"]))
        .order_by(Repair.created_at.desc())
    )
    existing = (await session.execute(existing_stmt)).scalar_one_or_none()
    if existing:
        return existing

    open_faults_stmt = select(PhoneFault).where(PhoneFault.phone_id == phone.id, PhoneFault.status == FaultStatus.OPEN)
    faults = (await session.execute(open_faults_stmt)).scalars().all()

    internal_id = await next_internal_id(session, "repair")
    # Initialise the collections explicitly (parts=[], faults=[]) so the
    # in-memory relationship attributes are already "loaded" - appending to
    # them below then stays entirely in-memory instead of triggering an
    # implicit (and, in async SQLAlchemy, unsafe) lazy-load round trip.
    repair = Repair(internal_id=internal_id, phone=phone, status="PLANNED",
                     started_at=dt.datetime.now(dt.timezone.utc), parts=[], faults=[])
    session.add(repair)
    await session.flush()

    for fault in faults:
        # Assigning via the relationship (not the raw FK column) keeps the
        # in-memory repair.faults / repair.parts collections correctly
        # populated for the object we're about to return, without needing
        # an extra async refresh() round trip or a fresh re-query.
        repair.faults.append(fault)
        if fault.required_part_type_id:
            repair.parts.append(
                RepairPart(
                    required_part_type_id=fault.required_part_type_id,
                    quantity=1,
                    status=RepairPartLineStatus.REQUIRED,
                )
            )
    await session.flush()

    await log_event(
        session,
        event_type=InventoryEventType.REPAIR_CREATED.value,
        entity_type="REPAIR",
        entity_id=repair.internal_id,
        actor_discord_id=actor_discord_id,
        related_entity_type="PHONE",
        related_entity_id=phone.internal_id,
        new_state={"faults": [f.description for f in faults]},
    )
    return repair


@dataclass
class RequirementCandidates:
    repair_part: RepairPart
    candidates: Sequence[Part]


async def repair_plan(session: AsyncSession, repair: Repair, phone_model_id: Optional[int] = None) -> list[RequirementCandidates]:
    """For each still-outstanding requirement line, find compatible AVAILABLE parts."""
    out: list[RequirementCandidates] = []
    for rp in repair.parts:
        if rp.status in (RepairPartLineStatus.INSTALLED, RepairPartLineStatus.CANCELLED):
            continue
        candidates = await find_compatible_available_parts(session, rp.required_part_type_id, phone_model_id)
        out.append(RequirementCandidates(repair_part=rp, candidates=candidates))
    return out


async def complete_repair(session: AsyncSession, repair: Repair, actor_discord_id: int) -> Repair:
    outstanding = [rp for rp in repair.parts if rp.status not in (RepairPartLineStatus.INSTALLED, RepairPartLineStatus.CANCELLED)]
    if outstanding:
        names = ", ".join(rp.required_part_type.name for rp in outstanding)
        raise RepairError(f"Cannot complete repair: still outstanding parts -> {names}")

    repair.status = "COMPLETE"
    repair.completed_at = dt.datetime.now(dt.timezone.utc)
    for fault in repair.faults:
        fault.status = FaultStatus.RESOLVED
        fault.resolved_at = repair.completed_at
    await session.flush()

    await log_event(
        session,
        event_type=InventoryEventType.REPAIR_COMPLETED.value,
        entity_type="REPAIR",
        entity_id=repair.internal_id,
        actor_discord_id=actor_discord_id,
        related_entity_type="PHONE",
        related_entity_id=repair.phone.internal_id,
        new_state={"total_cost": str(repair.total_cost())},
    )
    return repair


async def cancel_repair(session: AsyncSession, repair: Repair, actor_discord_id: int, notes: Optional[str] = None) -> Repair:
    from app.services.parts import unreserve_part

    for rp in repair.parts:
        if rp.status == RepairPartLineStatus.RESERVED and rp.part_id:
            part = await session.get(Part, rp.part_id)
            if part:
                await unreserve_part(session, part, rp, actor_discord_id)
        rp.status = RepairPartLineStatus.CANCELLED

    repair.status = "CANCELLED"
    await session.flush()

    await log_event(
        session,
        event_type=InventoryEventType.REPAIR_CANCELLED.value,
        entity_type="REPAIR",
        entity_id=repair.internal_id,
        actor_discord_id=actor_discord_id,
        notes=notes,
    )
    return repair
