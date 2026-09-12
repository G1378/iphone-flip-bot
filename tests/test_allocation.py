from __future__ import annotations

from decimal import Decimal

import pytest

from app.services import allocation, donors
from app.models.enums import TestOutcome


async def _make_donor(session, actor_id, today, price="180.00"):
    data = donors.BuyDonorInput(
        manufacturer="Apple", model="iPhone 13 Pro", variant=None, storage="128GB", colour="Graphite",
        purchase_price=Decimal(price), purchase_date=today, seller_source="CEX",
        fault_description="Cracked housing", notes=None, location_code=None,
    )
    return await donors.buy_donor(session, data, actor_id)


async def _teardown_standard(session, donor, actor_id):
    recovered = [
        donors.RecoveredPartInput("Screen", "Good", TestOutcome.PASS, "A-", None, "B1"),
        donors.RecoveredPartInput("Rear Camera", "Good", TestOutcome.PASS, "A", None, "C2"),
        donors.RecoveredPartInput("Battery", "89% health", TestOutcome.PASS, "B", None, "D1"),
        donors.RecoveredPartInput("Housing", "Cracked", TestOutcome.FAIL, "SCRAP", "Donor's original fault", None, discarded=True),
    ]
    return await donors.teardown(session, donor, recovered, actor_id)


@pytest.mark.asyncio
async def test_pro_rata_allocation_reconciles_exactly_to_donor_cost(session, actor_id, today):
    donor = await _make_donor(session, actor_id, today)
    parts = await _teardown_standard(session, donor, actor_id)
    screen, camera, battery, housing = parts

    lines = [
        allocation.ProRataLine(part_id=screen.id, assigned_value=Decimal("70")),
        allocation.ProRataLine(part_id=camera.id, assigned_value=Decimal("40")),
        allocation.ProRataLine(part_id=battery.id, assigned_value=Decimal("30")),
        # housing was discarded/scrapped - still allocate some notional value ("Other")
        allocation.ProRataLine(part_id=housing.id, assigned_value=Decimal("40")),
    ]
    created = await allocation.allocate_pro_rata(session, donor, lines, actor_id)

    total_allocated = sum((c.allocated_amount for c in created), Decimal("0"))
    assert total_allocated == donor.purchase_price  # exact reconciliation, no rounding leakage

    by_part = {c.part_id: c.allocated_amount for c in created}
    assert by_part[screen.id] == Decimal("70.00")
    assert by_part[camera.id] == Decimal("40.00")
    assert by_part[battery.id] == Decimal("30.00")

    # Part.cost cache kept in sync
    await session.refresh(screen)
    assert screen.cost == Decimal("70.00")


@pytest.mark.asyncio
async def test_pro_rata_scales_when_assigned_values_dont_sum_to_donor_cost(session, actor_id, today):
    """Spec doesn't mandate assigned values sum exactly to donor cost - the
    system should scale proportionally so the *full* donor cost is always
    allocated (none lost, none double counted)."""
    donor = await _make_donor(session, actor_id, today, price="100.00")
    parts = await _teardown_standard(session, donor, actor_id)
    screen, camera, battery, housing = parts

    # assigned values sum to 200, double the donor cost -> should scale to 50%
    lines = [
        allocation.ProRataLine(part_id=screen.id, assigned_value=Decimal("100")),
        allocation.ProRataLine(part_id=camera.id, assigned_value=Decimal("60")),
        allocation.ProRataLine(part_id=battery.id, assigned_value=Decimal("40")),
    ]
    created = await allocation.allocate_pro_rata(session, donor, lines, actor_id)
    total_allocated = sum((c.allocated_amount for c in created), Decimal("0"))
    assert total_allocated == Decimal("100.00")

    by_part = {c.part_id: c.allocated_amount for c in created}
    assert by_part[screen.id] == Decimal("50.00")
    assert by_part[camera.id] == Decimal("30.00")
    assert by_part[battery.id] == Decimal("20.00")


@pytest.mark.asyncio
async def test_manual_allocation_rejects_total_exceeding_donor_cost(session, actor_id, today):
    donor = await _make_donor(session, actor_id, today, price="50.00")
    parts = await _teardown_standard(session, donor, actor_id)
    screen = parts[0]

    with pytest.raises(allocation.AllocationError):
        await allocation.allocate_manual(
            session, donor, [allocation.ManualAllocationLine(part_id=screen.id, amount=Decimal("999.00"))], actor_id,
        )


@pytest.mark.asyncio
async def test_reallocation_preserves_history_never_overwrites(session, actor_id, today):
    donor = await _make_donor(session, actor_id, today, price="100.00")
    parts = await _teardown_standard(session, donor, actor_id)
    screen = parts[0]

    await allocation.allocate_manual(
        session, donor, [allocation.ManualAllocationLine(part_id=screen.id, amount=Decimal("60.00"))], actor_id,
        notes="first pass",
    )
    await allocation.allocate_manual(
        session, donor, [allocation.ManualAllocationLine(part_id=screen.id, amount=Decimal("75.00"))], actor_id,
        notes="corrected pass",
    )

    history = await allocation.allocation_history(session, screen)
    assert len(history) == 2
    current = [h for h in history if h.is_current]
    assert len(current) == 1
    assert current[0].allocated_amount == Decimal("75.00")
    superseded = [h for h in history if not h.is_current]
    assert superseded[0].allocated_amount == Decimal("60.00")

    await session.refresh(screen)
    assert screen.cost == Decimal("75.00")


@pytest.mark.asyncio
async def test_donor_teardown_creates_parts_with_correct_provenance(session, actor_id, today):
    donor = await _make_donor(session, actor_id, today)
    parts = await _teardown_standard(session, donor, actor_id)
    screen, camera, battery, housing = parts

    assert screen.status == "AVAILABLE"
    assert screen.source_donor_id == donor.id
    assert housing.status == "SCRAPPED"  # discarded during teardown
    assert screen.internal_id.startswith("PT-")
    assert donor.current_status == "TORN_DOWN"
