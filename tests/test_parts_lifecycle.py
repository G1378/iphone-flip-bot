from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from app.models.enums import AcquisitionType, PartSourceType, RepairPartLineStatus, TestOutcome
from app.services import config_service, parts, phones, repairs


async def _make_phone_needing_screen(session, actor_id, today):
    data = phones.BuyPhoneInput(
        manufacturer="Apple", model="iPhone 13 Pro", variant=None, storage="128GB", colour="Graphite",
        imei="123456789012345", serial_number="SN001", carrier="Unlocked",
        purchase_price=Decimal("250.00"), purchase_date=today, seller_source="Auction",
        acquisition_type=AcquisitionType.REPAIR, notes=None, location_code=None,
    )
    phone = await phones.buy_phone(session, data, actor_id)
    screen_type = await config_service.get_or_create_part_type(session, "Screen")
    await phones.add_fault(session, phone, "Cracked screen", actor_id, required_part_type_id=screen_type.id)
    return phone


async def _make_available_screen_part(session, actor_id, cost="70.00"):
    data = parts.CreatePartInput(
        part_type_name="Screen", source_type=PartSourceType.DONOR, cost=Decimal(cost),
        condition="Good", grade_code="A-", testing_status=TestOutcome.PASS,
    )
    return await parts.create_part(session, data, actor_id)


@pytest.mark.asyncio
async def test_full_reserve_install_lifecycle(session, actor_id, today):
    phone = await _make_phone_needing_screen(session, actor_id, today)
    part = await _make_available_screen_part(session, actor_id)

    repair = await repairs.create_repair_for_open_faults(session, phone, actor_id)
    assert repair.status == "PLANNED"
    assert len(repair.parts) == 1
    rp = repair.parts[0]
    assert rp.status == RepairPartLineStatus.REQUIRED

    await parts.reserve_part(session, part, repair, rp, actor_id)
    assert part.status == "RESERVED"
    assert rp.status == RepairPartLineStatus.RESERVED

    # the line is RESERVED (not yet INSTALLED) so it's still "outstanding",
    # but since the only matching part is now reserved (no longer
    # AVAILABLE) there are no further candidates for it
    plan = await repairs.repair_plan(session, repair)
    assert len(plan) == 1
    assert plan[0].candidates == []

    await parts.install_part(session, part, phone, rp, actor_id)
    assert part.status == "INSTALLED"
    assert part.installed_phone_id == phone.id
    assert rp.cost_at_installation == Decimal("70.00")

    completed = await repairs.complete_repair(session, repair, actor_id)
    assert completed.status == "COMPLETE"
    assert completed.total_parts_cost() == Decimal("70.00")


@pytest.mark.asyncio
async def test_cannot_reserve_a_part_that_is_already_reserved(session, actor_id, today):
    phone = await _make_phone_needing_screen(session, actor_id, today)
    part = await _make_available_screen_part(session, actor_id)
    repair = await repairs.create_repair_for_open_faults(session, phone, actor_id)
    rp = repair.parts[0]

    await parts.reserve_part(session, part, repair, rp, actor_id)

    # simulate someone else trying to reserve the same part again for another repair line
    with pytest.raises(parts.PartStateError):
        await parts.reserve_part(session, part, repair, rp, actor_id)


@pytest.mark.asyncio
async def test_unreserve_returns_part_to_available(session, actor_id, today):
    phone = await _make_phone_needing_screen(session, actor_id, today)
    part = await _make_available_screen_part(session, actor_id)
    repair = await repairs.create_repair_for_open_faults(session, phone, actor_id)
    rp = repair.parts[0]

    await parts.reserve_part(session, part, repair, rp, actor_id)
    await parts.unreserve_part(session, part, rp, actor_id)

    assert part.status == "AVAILABLE"
    assert part.reserved_for_repair_id is None
    assert rp.status == RepairPartLineStatus.REQUIRED


@pytest.mark.asyncio
async def test_remove_installed_part_returns_it_to_stock(session, actor_id, today):
    phone = await _make_phone_needing_screen(session, actor_id, today)
    part = await _make_available_screen_part(session, actor_id)
    repair = await repairs.create_repair_for_open_faults(session, phone, actor_id)
    rp = repair.parts[0]

    await parts.reserve_part(session, part, repair, rp, actor_id)
    await parts.install_part(session, part, phone, rp, actor_id)

    await parts.remove_part(session, part, rp, actor_id, new_status="AVAILABLE", notes="wrong part fitted")
    assert part.status == "AVAILABLE"
    assert part.installed_phone_id is None
    assert rp.status == RepairPartLineStatus.REMOVED


@pytest.mark.asyncio
async def test_cannot_complete_repair_with_outstanding_parts(session, actor_id, today):
    phone = await _make_phone_needing_screen(session, actor_id, today)
    repair = await repairs.create_repair_for_open_faults(session, phone, actor_id)

    with pytest.raises(repairs.RepairError):
        await repairs.complete_repair(session, repair, actor_id)


@pytest.mark.asyncio
async def test_parts_needed_reports_shortfall(session, actor_id, today):
    from app.services.orders import parts_needed

    phone1 = await _make_phone_needing_screen(session, actor_id, today)
    phone2 = await _make_phone_needing_screen(session, actor_id, today)
    await repairs.create_repair_for_open_faults(session, phone1, actor_id)
    await repairs.create_repair_for_open_faults(session, phone2, actor_id)

    # only one screen in stock, but two are required
    await _make_available_screen_part(session, actor_id)

    needed = await parts_needed(session)
    screens = [n for n in needed if n.part_type.name == "Screen"]
    assert len(screens) == 1
    assert screens[0].required_qty == 2
    assert screens[0].in_stock_qty == 1
    assert screens[0].to_order_qty == 1


@pytest.mark.asyncio
async def test_scrap_part_marks_terminal_state(session, actor_id):
    part = await _make_available_screen_part(session, actor_id)
    await parts.scrap_part(session, part, actor_id, notes="cracked in storage")
    assert part.status == "SCRAPPED"
