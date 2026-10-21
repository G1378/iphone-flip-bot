from __future__ import annotations

from decimal import Decimal

import pytest

from app.models.enums import AcquisitionType, FaultStatus, RepairPartLineStatus, TestOutcome
from app.services import config_service, phones, repairs
from app.services import testing as testing_service


async def _phone(session, actor_id, today, status="PURCHASED", functional_condition=None):
    data = phones.BuyPhoneInput(
        manufacturer="Apple", model="iPhone 13", variant=None, storage="128GB", colour="Blue",
        imei="1", serial_number="S1", carrier="Unlocked", purchase_price=Decimal("200.00"),
        purchase_date=today, seller_source="eBay", acquisition_type=AcquisitionType.RESALE,
        notes=None, location_code=None,
    )
    phone = await phones.buy_phone(session, data, actor_id)
    if status != "PURCHASED":
        await phones.set_status(session, phone, status, actor_id)
    if functional_condition:
        await phones.edit_phone(session, phone, actor_id, functional_condition=functional_condition)
    return phone


async def _test(session, name):
    return await config_service.find_test_definition(session, name)


async def test_default_tests_are_mapped_to_part_types(session):
    assert (await _test(session, "Display")).part_type.name == "Screen"
    assert (await _test(session, "Battery Health")).part_type.name == "Battery"
    assert (await _test(session, "Water Damage Indicator")).part_type is None


async def test_fail_flags_part_and_moves_working_phone_to_repair(session, actor_id, today):
    phone = await _phone(session, actor_id, today, status="READY_FOR_LISTING", functional_condition="Working")
    out = await testing_service.record_and_apply(session, phone, await _test(session, "Display"), TestOutcome.FAIL, actor_id)

    assert out.flagged and out.part_type.name == "Screen"
    assert (out.previous_status, out.new_status) == ("READY_FOR_LISTING", "REPAIR_REQUIRED")
    assert phone.current_status == "REPAIR_REQUIRED"
    assert phone.functional_condition == "Needs repair"
    assert out.fault.required_part_type.name == "Screen"
    assert [(rp.required_part_type.name, rp.status) for rp in out.repair.parts] == [("Screen", RepairPartLineStatus.REQUIRED)]


async def test_two_failures_needing_same_part_give_one_line_and_other_part_is_added(session, actor_id, today):
    phone = await _phone(session, actor_id, today)
    await testing_service.record_and_apply(session, phone, await _test(session, "Touchscreen"), TestOutcome.FAIL, actor_id)
    await testing_service.record_and_apply(session, phone, await _test(session, "Display"), TestOutcome.FAIL, actor_id)
    out = await testing_service.record_and_apply(session, phone, await _test(session, "Battery Health"), TestOutcome.FAIL, actor_id)

    # all three failures land on the same single repair
    assert sorted(rp.required_part_type.name for rp in out.repair.parts) == ["Battery", "Screen"]
    report = await testing_service.build_report(session, phone)
    assert len(report.failed) == 3 and sorted(report.parts_needed) == ["Battery", "Screen"]
    assert report.repair_id == out.repair.internal_id


async def test_unmapped_failure_still_flags_phone_without_part(session, actor_id, today):
    phone = await _phone(session, actor_id, today)
    out = await testing_service.record_and_apply(session, phone, await _test(session, "Water Damage Indicator"), TestOutcome.FAIL, actor_id)
    assert out.flagged and out.part_type is None
    assert phone.current_status == "REPAIR_REQUIRED"
    assert out.repair.parts == []


async def test_refailing_same_test_does_not_duplicate_fault(session, actor_id, today):
    phone = await _phone(session, actor_id, today)
    d = await _test(session, "Display")
    a = await testing_service.record_and_apply(session, phone, d, TestOutcome.FAIL, actor_id)
    b = await testing_service.record_and_apply(session, phone, d, TestOutcome.FAIL, actor_id)
    assert a.fault.id == b.fault.id and len(b.repair.parts) == 1


async def test_later_pass_resolves_fault_and_drops_unneeded_part(session, actor_id, today):
    phone = await _phone(session, actor_id, today)
    d = await _test(session, "Display")
    out = await testing_service.record_and_apply(session, phone, d, TestOutcome.FAIL, actor_id)
    back = await testing_service.record_and_apply(session, phone, d, TestOutcome.PASS, actor_id)

    assert back.fault_resolved and out.fault.status == FaultStatus.RESOLVED
    assert out.repair.parts[0].status == RepairPartLineStatus.CANCELLED


async def test_pass_keeps_part_if_another_open_fault_still_needs_it(session, actor_id, today):
    phone = await _phone(session, actor_id, today)
    await testing_service.record_and_apply(session, phone, await _test(session, "Touchscreen"), TestOutcome.FAIL, actor_id)
    out = await testing_service.record_and_apply(session, phone, await _test(session, "Display"), TestOutcome.FAIL, actor_id)
    await testing_service.record_and_apply(session, phone, await _test(session, "Display"), TestOutcome.PASS, actor_id)
    assert out.repair.parts[0].status == RepairPartLineStatus.REQUIRED  # touchscreen still needs the screen


async def test_sold_phone_result_recorded_but_not_reopened(session, actor_id, today):
    phone = await _phone(session, actor_id, today, status="SOLD")
    out = await testing_service.record_and_apply(session, phone, await _test(session, "Display"), TestOutcome.FAIL, actor_id)
    assert not out.flagged and phone.current_status == "SOLD" and out.fault is None


async def test_listed_phone_is_pulled_and_warned(session, actor_id, today):
    phone = await _phone(session, actor_id, today, status="LISTED")
    out = await testing_service.record_and_apply(session, phone, await _test(session, "Display"), TestOutcome.FAIL, actor_id)
    assert out.was_listed and phone.current_status == "REPAIR_REQUIRED"


async def test_not_working_condition_text_is_left_alone(session, actor_id, today):
    phone = await _phone(session, actor_id, today, functional_condition="Not working")
    await testing_service.record_and_apply(session, phone, await _test(session, "Display"), TestOutcome.FAIL, actor_id)
    assert phone.functional_condition == "Not working"


async def test_clean_walkthrough_marks_phone_tested(session, actor_id, today):
    phone = await _phone(session, actor_id, today)
    for d in await config_service.list_test_definitions(session):
        await testing_service.record_and_apply(session, phone, d, TestOutcome.PASS, actor_id)
    report = await testing_service.finish_walkthrough(session, phone, actor_id)
    assert not report.failed and phone.current_status == "TESTED"


async def test_walkthrough_with_skips_does_not_mark_tested(session, actor_id, today):
    phone = await _phone(session, actor_id, today)
    first = (await config_service.list_test_definitions(session))[0]
    await testing_service.record_and_apply(session, phone, first, TestOutcome.PASS, actor_id)
    report = await testing_service.finish_walkthrough(session, phone, actor_id)
    assert report.not_tested > 0 and phone.current_status == "PURCHASED"


async def test_repair_flow_still_works_with_test_raised_faults(session, actor_id, today):
    phone = await _phone(session, actor_id, today)
    out = await testing_service.record_and_apply(session, phone, await _test(session, "Display"), TestOutcome.FAIL, actor_id)
    same = await repairs.create_repair_for_open_faults(session, phone, actor_id)  # what /repair does
    assert same.id == out.repair.id and len(same.parts) == 1
