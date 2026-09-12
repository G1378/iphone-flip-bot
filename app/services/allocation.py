from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Optional, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.enums import AllocationMethod, InventoryEventType
from app.models.inventory_models import Donor, Part
from app.models.repair_models import CostAllocation
from app.services.events import log_event

TWO_DP = Decimal("0.01")


class AllocationError(ValueError):
    pass


@dataclass
class ManualAllocationLine:
    part_id: int
    amount: Decimal


@dataclass
class ProRataLine:
    part_id: int
    assigned_value: Decimal  # relative value used to compute the part's share


async def _supersede_current(session: AsyncSession, part_ids: Sequence[int]) -> None:
    if not part_ids:
        return
    stmt = select(CostAllocation).where(CostAllocation.part_id.in_(part_ids), CostAllocation.is_current.is_(True))
    rows = (await session.execute(stmt)).scalars().all()
    for row in rows:
        row.is_current = False
    await session.flush()


async def allocate_manual(
    session: AsyncSession, donor: Donor, lines: list[ManualAllocationLine], actor_discord_id: int,
    notes: Optional[str] = None,
) -> list[CostAllocation]:
    total = sum((l.amount for l in lines), Decimal("0"))
    if total > donor.purchase_price:
        raise AllocationError(
            f"Manual allocation total (£{total}) exceeds donor purchase price (£{donor.purchase_price})."
        )
    return await _apply_allocations(
        session, donor,
        {l.part_id: l.amount.quantize(TWO_DP, rounding=ROUND_HALF_UP) for l in lines},
        AllocationMethod.MANUAL, actor_discord_id, notes,
    )


async def allocate_pro_rata(
    session: AsyncSession, donor: Donor, lines: list[ProRataLine], actor_discord_id: int,
    notes: Optional[str] = None,
) -> list[CostAllocation]:
    """Allocate the donor's full purchase price across parts in proportion
    to each part's assigned relative value (spec example: Screen=£70,
    Camera=£40, Battery=£30, Housing=£30, Other=£10 -> total £180 = donor
    cost, so each part gets exactly its assigned value; if the assigned
    values don't sum to the donor cost, they're scaled proportionally so
    the full donor cost is always allocated and none is lost)."""
    value_total = sum((l.assigned_value for l in lines), Decimal("0"))
    if value_total <= 0:
        raise AllocationError("Assigned values must sum to more than zero.")

    donor_cost = donor.purchase_price
    allocations: dict[int, Decimal] = {}
    running = Decimal("0")
    for i, line in enumerate(lines):
        if i == len(lines) - 1:
            # last line takes the remainder to guarantee the totals reconcile exactly
            amount = (donor_cost - running).quantize(TWO_DP, rounding=ROUND_HALF_UP)
        else:
            share = (line.assigned_value / value_total) * donor_cost
            amount = share.quantize(TWO_DP, rounding=ROUND_HALF_UP)
        allocations[line.part_id] = amount
        running += amount

    return await _apply_allocations(session, donor, allocations, AllocationMethod.PRO_RATA, actor_discord_id, notes)


async def _apply_allocations(
    session: AsyncSession, donor: Donor, allocations: dict[int, Decimal], method: AllocationMethod,
    actor_discord_id: int, notes: Optional[str],
) -> list[CostAllocation]:
    part_ids = list(allocations.keys())
    stmt = select(Part).where(Part.id.in_(part_ids), Part.source_donor_id == donor.id)
    parts = {p.id: p for p in (await session.execute(stmt)).scalars().all()}
    missing = set(part_ids) - set(parts.keys())
    if missing:
        raise AllocationError(f"Part id(s) {missing} do not belong to donor {donor.internal_id}.")

    await _supersede_current(session, part_ids)

    batch_id = uuid.uuid4()
    created: list[CostAllocation] = []
    for part_id, amount in allocations.items():
        part = parts[part_id]
        row = CostAllocation(
            batch_id=batch_id,
            donor_id=donor.id,
            part_id=part_id,
            method=method,
            donor_total_cost_snapshot=donor.purchase_price,
            allocated_amount=amount,
            is_current=True,
            created_by_discord_id=actor_discord_id,
            notes=notes,
        )
        session.add(row)
        part.cost = amount  # keep the convenience cache in sync
        created.append(row)

        await log_event(
            session,
            event_type=InventoryEventType.PART_ALLOCATED_COST.value,
            entity_type="PART",
            entity_id=part.internal_id,
            actor_discord_id=actor_discord_id,
            related_entity_type="DONOR",
            related_entity_id=donor.internal_id,
            new_state={"allocated_amount": str(amount), "method": method.value},
        )
    await session.flush()
    return created


async def allocation_history(session: AsyncSession, part: Part) -> Sequence[CostAllocation]:
    stmt = (
        select(CostAllocation)
        .where(CostAllocation.part_id == part.id)
        .order_by(CostAllocation.created_at.desc())
    )
    return (await session.execute(stmt)).scalars().all()


async def unallocated_amount(session: AsyncSession, donor: Donor) -> Decimal:
    stmt = select(CostAllocation).where(CostAllocation.donor_id == donor.id, CostAllocation.is_current.is_(True))
    rows = (await session.execute(stmt)).scalars().all()
    allocated = sum((r.allocated_amount for r in rows), Decimal("0"))
    return donor.purchase_price - allocated
