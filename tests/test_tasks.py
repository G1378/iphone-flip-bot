from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.models.config_models import TestDefinition
from app.models.enums import AcquisitionType, PartSourceType, TestOutcome
from app.models.task_models import Task
from app.services import config_service
from app.services import orders as orders_service
from app.services import parts as parts_service
from app.services import phone_tickets
from app.services import phones as phones_service
from app.services import repairs as repairs_service
from app.services import tasks as tasks_service
from app.services import testing as testing_service
from app.services.phone_tickets import PhoneTicketError
from app.services.task_templates import GENERAL_TEMPLATES
from app.services.tasks import TaskError


# ------------------------------------------------------------------ helpers
async def _buy(session, actor_id):
    return await phones_service.buy_phone(session, phones_service.BuyPhoneInput(
        manufacturer="Apple", model="iPhone 13", variant=None, storage="128GB", colour="Blue", imei=None,
        serial_number=None, carrier=None, purchase_price=Decimal("120.00"), purchase_date=dt.date(2026, 9, 1),
        seller_source="eBay", acquisition_type=AcquisitionType.RESALE, notes=None, location_code=None), actor_id)


async def _open_tickets(session, phone):
    return (await session.execute(select(Task).where(Task.phone_id == phone.id, Task.status.in_(("TODO", "IN_PROGRESS", "BLOCKED"))))).scalars().all()


async def _ticket(session, phone):
    await phone_tickets.sync_phone_tickets(session)
    rows = await _open_tickets(session, phone)
    assert len(rows) <= 1, "a phone must never have two open tickets"
    return rows[0] if rows else None


async def _definition(session, code) -> TestDefinition:
    return (await session.execute(select(TestDefinition).where(TestDefinition.code == code))).scalar_one()


async def _record(session, phone, code, outcome, actor_id):
    return await testing_service.record_and_apply(session, phone, await _definition(session, code), outcome, actor_id)


async def _pass_everything(session, phone, actor_id, except_codes=()):
    for d in await config_service.list_test_definitions(session):
        if d.code not in except_codes:
            await testing_service.record_and_apply(session, phone, d, TestOutcome.PASS, actor_id)


async def _stock(session, actor_id, part_type: str, location: str | None = None):
    return await parts_service.create_part(session, parts_service.CreatePartInput(
        part_type_name=part_type, source_type=PartSourceType.EXISTING_STOCK, cost=Decimal("30"),
        location_code=location), actor_id)


async def _order(session, actor_id, part_type: str, placed: bool = True):
    pt = await config_service.get_or_create_part_type(session, part_type)
    order = await orders_service.create_order(
        session, "Supplier", [orders_service.OrderLineInput(part_type_id=pt.id, quantity=1, unit_cost=Decimal("25"))],
        actor_id, expected_delivery=dt.date(2026, 11, 2))
    if placed:
        await orders_service.mark_ordered(session, order, actor_id)
    return order


# ------------------------------------------------------------------ bought -> test ticket
async def test_new_phone_gets_exactly_one_test_ticket(session, actor_id):
    phone = await _buy(session, actor_id)
    stats = await phone_tickets.sync_phone_tickets(session)
    assert stats.opened == 1
    ticket = await _ticket(session, phone)
    assert ticket.stage == "TEST" and ticket.scope == "PHONE" and "/testing" in ticket.description
    again = await phone_tickets.sync_phone_tickets(session)
    assert not again.changed, "an unchanged phone must not touch its ticket"


async def test_test_progress_edits_the_same_ticket_in_place(session, actor_id):
    phone = await _buy(session, actor_id)
    first = await _ticket(session, phone)
    first_id, version_before = first.id, first.version
    assert "0/20" in first.description
    await _record(session, phone, "touchscreen", TestOutcome.PASS, actor_id)
    second = await _ticket(session, phone)
    assert second.id == first_id and second.version > version_before
    assert "1/20" in second.description and "Touchscreen" not in second.description.split("Still to test:")[1]


async def test_all_tests_passing_closes_test_ticket_and_opens_prep(session, actor_id):
    phone = await _buy(session, actor_id)
    first = await _ticket(session, phone)
    await _pass_everything(session, phone, actor_id)
    await testing_service.finish_walkthrough(session, phone, actor_id)
    nxt = await _ticket(session, phone)
    assert nxt.stage == "PREP" and nxt.id != first.id
    assert first.status == "DONE", "the finished step's ticket is closed (its message gets deleted)"


# ------------------------------------------------------------------ failed test -> swap part
async def test_failed_test_with_part_in_stock_says_swap_now(session, actor_id):
    await config_service.add_location(session, "B2")
    part = await _stock(session, actor_id, "Screen", "B2")
    phone = await _buy(session, actor_id)
    first = await _ticket(session, phone)
    await _record(session, phone, "display", TestOutcome.FAIL, actor_id)
    t = await _ticket(session, phone)
    assert t.stage == "REPAIR" and t.id != first.id
    assert "Swap now" in t.description and part.internal_id in t.description and "shelf B2" in t.description
    assert t.status == "TODO" and not t.system_blocked


async def test_part_not_in_stock_waits_then_updates_as_it_is_ordered_and_arrives(session, actor_id):
    phone = await _buy(session, actor_id)
    await _ticket(session, phone)
    await _record(session, phone, "display", TestOutcome.FAIL, actor_id)

    t = await _ticket(session, phone)
    assert t.title == "📦 Waiting for parts" and t.status == "BLOCKED" and t.system_blocked
    assert "needs ordering" in t.description and "Screen" in t.blocked_reason
    ticket_id = t.id

    order = await _order(session, actor_id, "Screen")           # ordered, not here yet
    t = await _ticket(session, phone)
    assert t.id == ticket_id and "on order" in t.description and order.internal_id in t.description
    assert t.status == "BLOCKED" and order.internal_id in t.blocked_reason

    await orders_service.receive_order(session, await orders_service.get_order(session, order.internal_id), actor_id)
    t = await _ticket(session, phone)
    assert t.id == ticket_id, "same ticket, edited in place"
    assert t.title.startswith("🔧 Swap parts") and "Swap now" in t.description
    assert t.status == "TODO" and not t.system_blocked and t.blocked_reason is None


async def test_draft_order_is_flagged_as_not_placed(session, actor_id):
    phone = await _buy(session, actor_id)
    await _record(session, phone, "display", TestOutcome.FAIL, actor_id)
    order = await _order(session, actor_id, "Screen", placed=False)
    t = await _ticket(session, phone)
    assert "not placed yet" in t.description and order.internal_id in t.description


async def test_split_between_in_stock_now_and_to_order(session, actor_id):
    await _stock(session, actor_id, "Screen")                    # screen in stock, no battery
    phone = await _buy(session, actor_id)
    await _record(session, phone, "display", TestOutcome.FAIL, actor_id)
    await _record(session, phone, "battery_health", TestOutcome.FAIL, actor_id)
    t = await _ticket(session, phone)
    assert t.stage == "REPAIR" and t.title == "🔧 Swap parts · 1 now, 1 waiting"
    assert "Swap now" in t.description and "Waiting on parts" in t.description and "Battery" in t.description
    assert t.status == "TODO", "something can be done now, so it isn't blocked"


async def test_one_screen_in_stock_is_not_promised_to_two_phones(session, actor_id):
    await _stock(session, actor_id, "Screen")
    a, b = await _buy(session, actor_id), await _buy(session, actor_id)
    await _record(session, a, "display", TestOutcome.FAIL, actor_id)
    await _record(session, b, "display", TestOutcome.FAIL, actor_id)
    ta, tb = await _ticket(session, a), await _ticket(session, b)
    assert "Swap now" in ta.description
    assert "needs ordering" in tb.description and tb.status == "BLOCKED"


async def test_fault_with_no_single_part_asks_for_diagnosis(session, actor_id):
    phone = await _buy(session, actor_id)
    await _record(session, phone, "water_damage_indicator", TestOutcome.FAIL, actor_id)
    t = await _ticket(session, phone)
    assert t.stage == "REPAIR" and t.title == "🔍 Diagnose the fault" and "Needs diagnosis" in t.description


# ------------------------------------------------------------------ swap -> verify -> list
async def test_fitting_the_part_moves_to_verify_then_list(session, actor_id):
    part = await _stock(session, actor_id, "Screen")
    phone = await _buy(session, actor_id)
    await _record(session, phone, "display", TestOutcome.FAIL, actor_id)
    repair_ticket = await _ticket(session, phone)
    assert repair_ticket.stage == "REPAIR"

    repair = await repairs_service.get_active_repair(session, phone)
    line = repair.parts[0]
    await parts_service.reserve_part(session, part, repair, line, actor_id)
    await parts_service.install_part(session, part, phone, line, actor_id)

    verify = await _ticket(session, phone)
    assert verify.stage == "VERIFY" and repair_ticket.status == "DONE"

    msg = await phone_tickets.manual_complete(session, phone, "VERIFY", actor_id)
    assert "Ready for listing" in msg and phone.current_status == "READY_FOR_LISTING"
    listing = await _ticket(session, phone)
    assert listing.stage == "LIST" and verify.status == "DONE"


async def test_done_on_repair_installs_what_is_in_stock(session, actor_id):
    part = await _stock(session, actor_id, "Screen")
    phone = await _buy(session, actor_id)
    await _record(session, phone, "display", TestOutcome.FAIL, actor_id)
    await _ticket(session, phone)
    assert "Screen" in await phone_tickets.preview_manual_complete(session, phone, "REPAIR")
    await phone_tickets.manual_complete(session, phone, "REPAIR", actor_id)
    assert part.status == "INSTALLED" and part.installed_phone_id == phone.id
    assert (await _ticket(session, phone)).stage == "VERIFY"


async def test_done_on_repair_with_nothing_in_stock_is_refused(session, actor_id):
    phone = await _buy(session, actor_id)
    await _record(session, phone, "display", TestOutcome.FAIL, actor_id)
    await _ticket(session, phone)
    with pytest.raises(PhoneTicketError):
        await phone_tickets.manual_complete(session, phone, "REPAIR", actor_id)


async def test_a_passing_retest_withdraws_the_part_request(session, actor_id):
    phone = await _buy(session, actor_id)
    await _record(session, phone, "display", TestOutcome.FAIL, actor_id)
    assert (await _ticket(session, phone)).stage == "REPAIR"
    await _record(session, phone, "display", TestOutcome.PASS, actor_id)  # it was a fluke
    await repairs_service.cancel_repair(session, await repairs_service.get_active_repair(session, phone), actor_id)
    await phones_service.set_status(session, phone, "TESTED", actor_id)
    assert (await _ticket(session, phone)).stage == "PREP"


# ------------------------------------------------------------------ Done button on the other steps
async def test_done_on_test_skips_testing_and_it_stays_skipped(session, actor_id):
    phone = await _buy(session, actor_id)
    await _ticket(session, phone)
    await phone_tickets.manual_complete(session, phone, "TEST", actor_id)
    assert phone.current_status == "TESTED"
    assert (await _ticket(session, phone)).stage == "PREP"


async def test_prep_then_list_then_listed_needs_no_ticket(session, actor_id):
    phone = await _buy(session, actor_id)
    await phones_service.set_status(session, phone, "TESTED", actor_id)
    assert (await _ticket(session, phone)).stage == "PREP"
    await phone_tickets.manual_complete(session, phone, "PREP", actor_id)
    assert (await _ticket(session, phone)).stage == "LIST"
    await phones_service.set_status(session, phone, "LISTED", actor_id)   # what /list does
    assert await _ticket(session, phone) is None


async def test_a_listed_phone_that_fails_again_gets_a_repair_ticket(session, actor_id):
    phone = await _buy(session, actor_id)
    await phones_service.set_status(session, phone, "LISTED", actor_id)
    assert await _ticket(session, phone) is None
    await _record(session, phone, "speakers", TestOutcome.FAIL, actor_id)
    t = await _ticket(session, phone)
    assert t.stage == "REPAIR" and "Speaker" in t.description


async def test_sale_to_close_out(session, actor_id):
    phone = await _buy(session, actor_id)
    await phones_service.set_status(session, phone, "SOLD", actor_id)
    assert (await _ticket(session, phone)).stage == "SHIP"
    await phone_tickets.manual_complete(session, phone, "SHIP", actor_id)
    assert phone.current_status == "SHIPPED" and (await _ticket(session, phone)).stage == "CLOSE"
    await phone_tickets.manual_complete(session, phone, "CLOSE", actor_id)
    assert phone.current_status == "COMPLETE" and await _ticket(session, phone) is None


async def test_scrapping_closes_the_ticket(session, actor_id):
    phone = await _buy(session, actor_id)
    t = await _ticket(session, phone)
    await phones_service.set_status(session, phone, "SCRAPPED", actor_id)
    assert await _ticket(session, phone) is None and t.status == "DONE"


# ------------------------------------------------------------------ guarantees
async def test_database_refuses_a_second_open_ticket_for_one_phone(session, actor_id):
    phone = await _buy(session, actor_id)
    await _ticket(session, phone)
    with pytest.raises(IntegrityError):
        async with session.begin_nested():
            await tasks_service.new_task(session, title="dupe", scope="PHONE", phone_id=phone.id, stage="TEST", status="TODO")


async def test_a_person_blocking_a_ticket_is_respected_by_the_engine(session, actor_id):
    phone = await _buy(session, actor_id)
    t = await _ticket(session, phone)
    await tasks_service.set_task_status(session, t, "BLOCKED", actor_id, reason="seller dispute")
    await _record(session, phone, "touchscreen", TestOutcome.PASS, actor_id)
    t = await _ticket(session, phone)
    assert t.status == "BLOCKED" and t.blocked_reason == "seller dispute"


async def test_started_ticket_keeps_its_owner_when_content_changes(session, actor_id):
    phone = await _buy(session, actor_id)
    t = await _ticket(session, phone)
    await tasks_service.set_task_status(session, t, "IN_PROGRESS", actor_id)
    await _record(session, phone, "touchscreen", TestOutcome.PASS, actor_id)
    t = await _ticket(session, phone)
    assert t.status == "IN_PROGRESS" and t.assignee_discord_id == actor_id


async def test_phone_needing_nothing_has_no_ticket_and_engine_is_quiet(session, actor_id):
    phone = await _buy(session, actor_id)
    await phones_service.set_status(session, phone, "LISTED", actor_id)
    stats = await phone_tickets.sync_phone_tickets(session)
    assert not stats.changed and not await _open_tickets(session, phone)


# ------------------------------------------------------------------ general tickets (unchanged behaviour)
async def test_general_templates_seeded_once_and_phone_tickets_are_not_templated(session):
    rows = await tasks_service.list_templates(session, include_inactive=True)
    assert len(rows) == len(GENERAL_TEMPLATES) and all(r.scope == "GENERAL" for r in rows)
    await tasks_service.seed_templates_if_empty(session)
    assert len(await tasks_service.list_templates(session, include_inactive=True)) == len(rows)


async def test_general_setup_and_recurring(session, actor_id):
    assert await tasks_service.setup_general(session) == len(GENERAL_TEMPLATES)
    assert await tasks_service.setup_general(session) == 0
    tpl = await tasks_service.get_template(session, "rec_stock_check")
    task = (await session.execute(select(Task).where(Task.template_id == tpl.id))).scalar_one()
    assert task.phone_id is None and task.stage is None
    await tasks_service.set_task_status(session, task, "DONE", actor_id)
    assert await tasks_service.reconcile_recurring(session) == 0
    task.created_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=8)
    await session.flush()
    assert await tasks_service.reconcile_recurring(session) >= 1


async def test_recurring_does_not_start_before_setup(session):
    assert await tasks_service.reconcile_recurring(session) == 0


async def test_manual_tickets_are_general_only_and_validated(session, actor_id):
    t = await tasks_service.create_task(session, "Buy IPA", actor_id, priority="high", due_date=dt.date(2026, 9, 5))
    assert t.scope == "GENERAL" and t.phone_id is None and t.priority == "HIGH"
    with pytest.raises(TaskError):
        await tasks_service.create_task(session, "  ", actor_id)
    with pytest.raises(TaskError):
        tasks_service.normalise_priority("whenever")
    assert tasks_service.parse_due("3d") == dt.date.today() + dt.timedelta(days=3)
    with pytest.raises(TaskError):
        tasks_service.parse_due("next-ish")


async def test_ticket_lifecycle_block_reason_claim_and_notes(session, actor_id):
    task = await tasks_service.create_task(session, "Tidy", actor_id)
    with pytest.raises(TaskError):
        await tasks_service.set_task_status(session, task, "BLOCKED", actor_id)
    await tasks_service.set_task_status(session, task, "BLOCKED", actor_id, reason="no bags")
    await tasks_service.set_task_status(session, task, "IN_PROGRESS", actor_id)
    assert task.blocked_reason is None and task.assignee_discord_id == actor_id
    await tasks_service.claim_task(session, task, actor_id)
    assert task.assignee_discord_id is None
    await tasks_service.update_task(session, task, note="first")
    await tasks_service.update_task(session, task, note="second")
    assert "first" in task.notes and "second" in task.notes
    await tasks_service.set_task_status(session, task, "DONE", actor_id)
    assert task.completed_at and task.completed_by_discord_id == actor_id


async def test_template_add_validation(session):
    tpl = await tasks_service.add_template(session, code="Check Charger", title="Check charger", category="Workshop", recur_days=7)
    assert tpl.code == "check_charger" and tpl.scope == "GENERAL"
    with pytest.raises(TaskError):
        await tasks_service.add_template(session, code="check_charger", title="dup", category="x")
    with pytest.raises(TaskError):
        await tasks_service.add_template(session, code="zero", title="z", category="x", recur_days=0)
    await tasks_service.set_template_active(session, "check_charger", False)
    assert tpl.active is False


async def test_discord_bookkeeping_queues(session, actor_id):
    task = await tasks_service.create_task(session, "Post me", actor_id)
    assert task in await tasks_service.tasks_needing_post(session)
    task.posted_version = task.version
    assert task not in await tasks_service.tasks_needing_post(session)
    await tasks_service.update_task(session, task, note="changed")
    assert task in await tasks_service.tasks_needing_post(session)

    # A finished phone step is deleted from the channel right away; a general ticket waits.
    phone = await _buy(session, actor_id)
    t = await _ticket(session, phone)
    t.discord_message_id = 111
    await phones_service.set_status(session, phone, "SCRAPPED", actor_id)
    await _ticket(session, phone)
    assert t in await tasks_service.closed_phone_tickets_to_delete(session)
    task.discord_message_id, task.status = 222, "DONE"
    await session.flush()
    assert task not in await tasks_service.closed_phone_tickets_to_delete(session)
    assert task not in await tasks_service.tasks_to_archive(session, 3)
