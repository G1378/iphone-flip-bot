"""Parts aren't tested before fitting: they're fitted and either work or they don't.
These tests pin down what happens when one doesn't."""
from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models.commerce_models import Expense
from app.models.config_models import WorkflowStatus
from app.models.enums import FaultStatus, RepairPartLineStatus, TestOutcome
from app.models.failure_models import PartFailure
from app.models.inventory_models import Part, PhoneFault
from app.models.task_models import Task
from app.services import finance as finance_service
from app.services import job_actions, part_failures, tasks as tasks_service
from app.services import orders as orders_service
from app.services import parts as parts_service
from app.services.job_actions import JobActionError
from app.services import phone_tickets, config_service
from tests.test_job_panels import _acts, _line, _panel
from tests.test_tasks import _buy, _order, _record, _stock, _ticket


async def _received_screen(session, actor_id, supplier="ScreenCo", cost="35"):
    """A screen that arrived on an order (so it knows its supplier)."""
    pt = await config_service.get_or_create_part_type(session, "Screen")
    order = await orders_service.create_order(
        session, supplier, [orders_service.OrderLineInput(part_type_id=pt.id, quantity=1, unit_cost=Decimal(cost))], actor_id)
    await orders_service.mark_ordered(session, order, actor_id)
    await orders_service.receive_order(session, await orders_service.get_order(session, order.internal_id), actor_id)
    return (await session.execute(select(Part).order_by(Part.id.desc()))).scalars().first()


async def _reserved_phone(session, actor_id, **kw):
    """A phone with a failed display and a screen reserved for it."""
    part = await _received_screen(session, actor_id, **kw)
    phone = await _buy(session, actor_id)
    await _record(session, phone, "display", TestOutcome.FAIL, actor_id)
    rp_id = _line(await _panel(session, phone), "reserve").ref
    await job_actions.reserve(session, phone.id, rp_id, actor_id)
    return phone, part, rp_id


async def _expenses(session):
    return (await session.execute(select(Expense))).scalars().all()


# ------------------------------------------------------------------ it works
async def test_confirming_a_fit_records_that_the_part_worked(session, actor_id):
    phone, part, rp_id = await _reserved_phone(session, actor_id)
    msg = await job_actions.fit_confirmed(session, phone.id, rp_id, actor_id)
    assert "confirmed working" in msg
    await session.refresh(part)
    assert part.status == "INSTALLED" and part.confirmed_working_in == phone.internal_id and part.confirmed_working_at
    assert part_failures.proven_tag(part) == f"✅ worked in {phone.internal_id}"
    assert (await _panel(session, phone)).stage == "VERIFY"
    assert not await session.scalar(select(PartFailure.id).limit(1))


async def test_parts_have_no_pre_test_ordering_any_more(session, actor_id):
    first = await _stock(session, actor_id, "Screen")
    second = await _stock(session, actor_id, "Screen")
    second.testing_status = TestOutcome.PASS      # the old label must not affect what's offered
    await session.flush()
    found = await parts_service.find_part_by_type_and_model(session, "screen", "")
    assert [p.internal_id for p in found][:2] == [first.internal_id, second.internal_id]


# ------------------------------------------------------------------ it doesn't: the three choices
async def test_scrapped_part_is_a_business_loss_never_charged_to_the_phone(session, actor_id):
    phone, part, rp_id = await _reserved_phone(session, actor_id, cost="35")
    msg = await job_actions.fail_fit(session, phone.id, rp_id, actor_id, action="scrap", reason="no touch")
    assert "scrapped" in msg and "£35.00 written off" in msg and "ScreenCo" in msg

    await session.refresh(part)
    assert part.status == "SCRAPPED" and part.installed_phone_id is None and part.reserved_for_repair_id is None
    (failure,) = (await session.execute(select(PartFailure))).scalars().all()
    assert (failure.action, failure.supplier, failure.reason, failure.was_installed) == ("SCRAP", "ScreenCo", "no touch", False)

    (expense,) = await _expenses(session)
    assert expense.amount == Decimal("35.00") and expense.phone_id is None and expense.category.name == "Failed parts"
    assert part.internal_id in expense.description

    cost = await finance_service.compute_true_cost(session, phone)
    assert cost.purchased_part_cost == 0 and cost.other_expenses == 0, "the phone is not charged"


async def test_a_failed_part_puts_the_requirement_back(session, actor_id):
    phone, part, rp_id = await _reserved_phone(session, actor_id)
    await job_actions.fail_fit(session, phone.id, rp_id, actor_id, action="SCRAP")
    panel = await _panel(session, phone)
    assert panel.stage == "REPAIR" and "order" in _acts(panel) and "fit" not in _acts(panel)
    spare = await _stock(session, actor_id, "Screen")
    assert "reserve" in _acts(await _panel(session, phone)), "a replacement can be reserved straight away"
    assert spare.status == "AVAILABLE"


async def test_return_to_supplier_opens_a_refund_ticket_and_tracks_the_refund(session, actor_id):
    phone, part, rp_id = await _reserved_phone(session, actor_id, cost="40")
    msg = await job_actions.fail_fit(session, phone.id, rp_id, actor_id, action="return", reason="dead on arrival")
    assert "returned to supplier" in msg and "chase the refund" in msg
    await session.refresh(part)
    assert part.status == "RETURNED"
    assert (await session.execute(select(WorkflowStatus).where(
        WorkflowStatus.entity_type == "PART", WorkflowStatus.code == "RETURNED"))).scalar_one().is_terminal

    (failure,) = (await session.execute(select(PartFailure))).scalars().all()
    assert failure.return_status == "AWAITING_REFUND" and failure.task_id
    ticket = await session.get(Task, failure.task_id)
    assert ticket.scope == "GENERAL" and ticket.category == "Supplier returns" and "ScreenCo" in ticket.title
    assert ticket.due_date == dt.date.today() + dt.timedelta(days=14)

    await part_failures.record_refund(session, failure, Decimal("30.00"), actor_id)
    assert failure.return_status == "REFUNDED" and failure.refund_amount == Decimal("30.00")
    (expense,) = await _expenses(session)
    assert expense.amount == Decimal("10.00"), "the loss shrinks by what came back"
    await session.refresh(ticket)
    assert ticket.status == "DONE"
    with pytest.raises(part_failures.PartFailureError):
        await part_failures.record_refund(session, failure, Decimal("1"), actor_id)


async def test_a_refused_refund_keeps_the_full_loss(session, actor_id):
    phone, part, rp_id = await _reserved_phone(session, actor_id, cost="40")
    await job_actions.fail_fit(session, phone.id, rp_id, actor_id, action="RETURN")
    failure = (await session.execute(select(PartFailure))).scalar_one()
    await part_failures.record_refund(session, failure, Decimal("0"), actor_id)
    assert failure.return_status == "NO_REFUND" and (await _expenses(session))[0].amount == Decimal("40.00")
    with pytest.raises(part_failures.PartFailureError):
        await part_failures.record_refund(session, failure, Decimal("-5"), actor_id)


async def test_keep_as_faulty_sets_it_aside_and_it_is_never_offered_again(session, actor_id):
    phone, part, rp_id = await _reserved_phone(session, actor_id)
    await job_actions.fail_fit(session, phone.id, rp_id, actor_id, action="faulty")
    await session.refresh(part)
    assert part.status == "FAULTY"
    assert "reserve" not in _acts(await _panel(session, phone))
    assert (await session.execute(select(PartFailure))).scalar_one().return_status is None


async def test_a_part_from_stock_has_no_supplier_and_a_free_part_has_no_loss(session, actor_id):
    part = await _stock(session, actor_id, "Screen")       # existing stock, cost 30, no order
    phone = await _buy(session, actor_id)
    await _record(session, phone, "display", TestOutcome.FAIL, actor_id)
    rp_id = _line(await _panel(session, phone), "reserve").ref
    await job_actions.reserve(session, phone.id, rp_id, actor_id)
    await job_actions.fail_fit(session, phone.id, rp_id, actor_id, action="return")
    failure = (await session.execute(select(PartFailure))).scalar_one()
    assert failure.supplier is None
    assert "the supplier" in (await session.get(Task, failure.task_id)).title

    free = await _stock(session, actor_id, "Battery")
    free.cost = Decimal("0")
    await session.flush()
    p2 = await _buy(session, actor_id)
    await _record(session, p2, "battery_health", TestOutcome.FAIL, actor_id)
    rp2 = _line(await _panel(session, p2), "reserve").ref
    await job_actions.reserve(session, p2.id, rp2, actor_id)
    await job_actions.fail_fit(session, p2.id, rp2, actor_id, action="scrap")
    assert len(await _expenses(session)) == 1, "no expense for a part that cost nothing"


async def test_invalid_choices_and_missing_reservations_are_refused(session, actor_id):
    phone, part, rp_id = await _reserved_phone(session, actor_id)
    with pytest.raises(JobActionError, match="scrap, return"):
        await job_actions.fail_fit(session, phone.id, rp_id, actor_id, action="bin it")
    await job_actions.fail_fit(session, phone.id, rp_id, actor_id, action="scrap")
    with pytest.raises(JobActionError, match="no reserved"):
        await job_actions.fail_fit(session, phone.id, rp_id, actor_id, action="scrap")


# ------------------------------------------------------------------ "probably not the part"
async def test_second_failed_part_flags_diagnosis_instead_of_offering_another(session, actor_id):
    phone, part, rp_id = await _reserved_phone(session, actor_id)
    await job_actions.fail_fit(session, phone.id, rp_id, actor_id, action="scrap")
    await _stock(session, actor_id, "Screen")
    rp_id = _line(await _panel(session, phone), "reserve").ref
    await job_actions.reserve(session, phone.id, rp_id, actor_id)
    msg = await job_actions.fail_fit(session, phone.id, rp_id, actor_id, action="faulty")
    assert "failed 2 times" in msg and "probably not the part" in msg

    panel = await _panel(session, phone)
    assert panel.stage == "REPAIR" and panel.title == "🔍 Diagnose the fault"
    assert "Probably not the part" in panel.body and "failed 2 times" in panel.body
    assert not {"order", "reserve", "fit"} & set(_acts(panel)), "it stops pushing another part"
    assert {"retry", "resolve", "retest"} <= set(_acts(panel))
    assert "Try another Screen anyway" in _line(panel, "retry").label


async def test_try_another_anyway_and_mark_resolved(session, actor_id):
    phone, part, rp_id = await _reserved_phone(session, actor_id)
    await job_actions.fail_fit(session, phone.id, rp_id, actor_id, action="scrap")
    await _stock(session, actor_id, "Screen")
    rp_id = _line(await _panel(session, phone), "reserve").ref
    await job_actions.reserve(session, phone.id, rp_id, actor_id)
    await job_actions.fail_fit(session, phone.id, rp_id, actor_id, action="scrap")

    panel = await _panel(session, phone)
    await job_actions.retry_part(session, phone.id, _line(panel, "retry").ref, actor_id)
    again = await _panel(session, phone)
    assert "order" in _acts(again) and "retry" not in _acts(again)

    # ...or the person fixes it another way and says so.
    fault = (await session.execute(select(PartFailure))).scalars().first()
    faults = await job_actions.open_undiagnosed(session, phone.id)
    assert faults == []  # retry put a line back, so nothing is 'undiagnosed' now
    f = (await session.execute(select(PhoneFault).where(PhoneFault.phone_id == phone.id))).scalars().first()
    await job_actions.resolve_fault(session, phone.id, f.id, actor_id)
    assert f.status == FaultStatus.RESOLVED
    with pytest.raises(JobActionError):
        await job_actions.resolve_fault(session, phone.id, f.id, actor_id)


async def test_failures_are_counted_per_phone_not_across_the_stock(session, actor_id):
    phone_a, _, rp_a = await _reserved_phone(session, actor_id)
    await job_actions.fail_fit(session, phone_a.id, rp_a, actor_id, action="scrap")   # A: one failure
    await _stock(session, actor_id, "Screen")                                          # goes to A (waiting longest)
    await _stock(session, actor_id, "Screen")                                          # goes to B
    phone_b = await _buy(session, actor_id)
    await _record(session, phone_b, "display", TestOutcome.FAIL, actor_id)
    rp_b = _line(await _panel(session, phone_b), "reserve").ref
    await job_actions.reserve(session, phone_b.id, rp_b, actor_id)
    await job_actions.fail_fit(session, phone_b.id, rp_b, actor_id, action="scrap")   # B: one failure
    for phone in (phone_a, phone_b):
        panel = await _panel(session, phone)
        assert "retry" not in _acts(panel) and "Probably not the part" not in panel.body, "one failure each is not a pattern"


# ------------------------------------------------------------------ failing after it was fitted
async def test_a_fitted_part_that_later_fails_is_written_off_not_charged_to_the_phone(session, actor_id):
    phone, part, rp_id = await _reserved_phone(session, actor_id, cost="35")
    await job_actions.fit_confirmed(session, phone.id, rp_id, actor_id)
    assert (await finance_service.compute_true_cost(session, phone)).purchased_part_cost == Decimal("35.00")

    from app.models.repair_models import Repair
    repair = (await session.execute(select(Repair))).scalar_one()
    await session.refresh(repair)
    msg = await job_actions.unfit(session, phone.id, part.id, actor_id, disposition="scrap", reason="died in soak test")
    assert "didn't work" in msg and "died in soak test" not in msg

    await session.refresh(part)
    assert part.status == "SCRAPPED"
    assert (await finance_service.compute_true_cost(session, phone)).purchased_part_cost == 0, "no longer charged to the phone"
    (expense,) = await _expenses(session)
    assert expense.amount == Decimal("35.00") and expense.phone_id is None
    failure = (await session.execute(select(PartFailure))).scalar_one()
    assert failure.was_installed
    panel = await _panel(session, phone)
    assert panel.stage == "REPAIR" and "order" in _acts(panel), "first failure: a replacement goes back on the list"


async def test_removing_a_wrong_part_to_stock_is_not_a_failure(session, actor_id):
    phone, part, rp_id = await _reserved_phone(session, actor_id)
    await job_actions.fit_confirmed(session, phone.id, rp_id, actor_id)
    await job_actions.unfit(session, phone.id, part.id, actor_id)          # default: back to stock
    await session.refresh(part)
    assert part.status == "AVAILABLE" and part.confirmed_working_at, "it keeps its 'worked in' history"
    assert not await session.scalar(select(PartFailure.id).limit(1)) and await _expenses(session) == []


async def test_done_backup_on_repair_also_confirms_working(session, actor_id):
    part = await _stock(session, actor_id, "Screen")
    phone = await _buy(session, actor_id)
    await _record(session, phone, "display", TestOutcome.FAIL, actor_id)
    await _ticket(session, phone)
    assert "fitted and working" in await phone_tickets.preview_manual_complete(session, phone, "REPAIR")
    await phone_tickets.manual_complete(session, phone, "REPAIR", actor_id)
    await session.refresh(part)
    assert part.status == "INSTALLED" and part.confirmed_working_at


# ------------------------------------------------------------------ supplier quality
async def test_supplier_quality_counts_failures_per_supplier(session, actor_id):
    # ScreenCo sends 3 screens and one of them is dud. CheapParts sends 1 and it's fine.
    pt = await config_service.get_or_create_part_type(session, "Screen")
    order = await orders_service.create_order(
        session, "ScreenCo", [orders_service.OrderLineInput(part_type_id=pt.id, quantity=3, unit_cost=Decimal("30"))], actor_id)
    await orders_service.mark_ordered(session, order, actor_id)
    await orders_service.receive_order(session, await orders_service.get_order(session, order.internal_id), actor_id)
    await _received_screen(session, actor_id, supplier="CheapParts", cost="20")

    phone = await _buy(session, actor_id)                   # oldest stock is used first: a ScreenCo screen
    await _record(session, phone, "display", TestOutcome.FAIL, actor_id)
    rp_id = _line(await _panel(session, phone), "reserve").ref
    await job_actions.reserve(session, phone.id, rp_id, actor_id)
    await job_actions.fail_fit(session, phone.id, rp_id, actor_id, action="return")

    ranked = await part_failures.supplier_quality(session)
    assert [q.supplier for q in ranked] == ["ScreenCo", "CheapParts"], "worst supplier first"
    screenco, cheap = ranked
    assert (screenco.received, screenco.failures) == (3, 1) and screenco.failure_rate == pytest.approx(1 / 3)
    assert screenco.loss == Decimal("30.00") and screenco.awaiting_refund == 1 and screenco.awaiting_amount == Decimal("30.00")
    assert (cheap.received, cheap.failures, cheap.failure_rate) == (1, 0, 0)

    failure = (await session.execute(select(PartFailure))).scalar_one()
    await part_failures.record_refund(session, failure, Decimal("10"), actor_id)
    after = {q.supplier: q for q in await part_failures.supplier_quality(session)}["ScreenCo"]
    assert after.awaiting_refund == 0 and after.refunded == Decimal("10")


async def test_supplier_quality_ignores_parts_that_did_not_come_from_an_order(session, actor_id):
    phone = await _buy(session, actor_id)
    await _stock(session, actor_id, "Screen")
    await _record(session, phone, "display", TestOutcome.FAIL, actor_id)
    rp_id = _line(await _panel(session, phone), "reserve").ref
    await job_actions.reserve(session, phone.id, rp_id, actor_id)
    await job_actions.fail_fit(session, phone.id, rp_id, actor_id, action="scrap")
    assert list(await part_failures.supplier_quality(session)) == []
