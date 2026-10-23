"""Which buttons a job channel offers, and what pressing them does.

The rule under test: a button exists only when its action is possible and
relevant to where the phone is right now."""
from __future__ import annotations

import pytest
from sqlalchemy import select

from app.models.enums import FaultStatus, TestOutcome
from app.models.task_models import Task
from app.services import config_service, job_actions, job_panels, jobs as jobs_service, phone_tickets
from app.services import orders as orders_service
from app.services import phones as phones_service
from app.services import repairs as repairs_service
from app.services.job_actions import JobActionError
from tests.test_tasks import _buy, _order, _pass_everything, _record, _stock, _ticket


async def _panel(session, phone):
    phones = await phone_tickets.load_phones(session)
    ctx = await phone_tickets.load_context(session, phones)
    sit = phone_tickets.assess(phone, ctx)
    return None if sit is None else job_panels.build_panel(sit, ctx)


def _acts(panel):
    return [a.act for a in panel.actions]


def _line(panel, act):
    return next(a for a in panel.actions if a.act == act)


async def _failed_screen_phone(session, actor_id, in_stock=True):
    if in_stock:
        await _stock(session, actor_id, "Screen")
    phone = await _buy(session, actor_id)
    await _record(session, phone, "display", TestOutcome.FAIL, actor_id)
    return phone


# ------------------------------------------------------------------ testing
async def test_test_stage_offers_testing_tools_and_nothing_about_repairs(session, actor_id):
    phone = await _buy(session, actor_id)
    panel = await _panel(session, phone)
    assert panel.stage == "TEST"
    assert _acts(panel) == ["walk", "one", "fault", "edit", "move", "skip", "details"]
    assert "Run the tests" in _line(panel, "walk").label


async def test_continue_label_shows_progress(session, actor_id):
    phone = await _buy(session, actor_id)
    await _record(session, phone, "touchscreen", TestOutcome.PASS, actor_id)
    assert "Continue tests (1/20)" in _line(await _panel(session, phone), "walk").label


# ------------------------------------------------------------------ repair: parts in stock
async def test_part_in_stock_offers_reserve_not_order_or_fit(session, actor_id):
    phone = await _failed_screen_phone(session, actor_id)
    panel = await _panel(session, phone)
    assert panel.stage == "REPAIR"
    assert "reserve" in _acts(panel) and "PT-000001" in _line(panel, "reserve").label
    assert not {"order", "fit", "receive", "placed", "walk", "retest", "complete"} & set(_acts(panel))


async def test_reserve_then_fitted_then_verify(session, actor_id):
    phone = await _failed_screen_phone(session, actor_id)
    panel = await _panel(session, phone)
    rp_id = _line(panel, "reserve").ref

    msg = await job_actions.reserve(session, phone.id, rp_id, actor_id)
    assert "Reserved" in msg
    panel = await _panel(session, phone)
    assert "fit" in _acts(panel) and "reserve" not in _acts(panel)

    await job_actions.fit(session, phone.id, rp_id, actor_id)
    panel = await _panel(session, phone)
    assert panel.stage == "VERIFY"
    assert {"retest", "complete"} <= set(_acts(panel)) and not {"fit", "reserve", "order"} & set(_acts(panel))
    assert "unfit" in _acts(panel), "something is fitted, so removing it is now relevant"


async def test_fit_before_reserve_is_refused(session, actor_id):
    phone = await _failed_screen_phone(session, actor_id)
    rp_id = _line(await _panel(session, phone), "reserve").ref
    with pytest.raises(JobActionError, match="Reserve"):
        await job_actions.fit(session, phone.id, rp_id, actor_id)


async def test_other_options_only_when_there_is_a_choice(session, actor_id):
    phone = await _failed_screen_phone(session, actor_id)              # one screen in stock
    assert "alt" not in _acts(await _panel(session, phone))
    await _stock(session, actor_id, "Screen")                           # now two
    panel = await _panel(session, phone)
    assert "alt" in _acts(panel)
    rp_id = _line(panel, "alt").ref
    chosen = (await job_actions.alternatives(session, phone.id, rp_id))[1]
    await job_actions.reserve(session, phone.id, rp_id, actor_id, part_id=chosen.id)
    assert (await _panel(session, phone)).actions[0].label.endswith(f"({chosen.internal_id})")


# ------------------------------------------------------------------ repair: parts missing
async def test_missing_part_offers_order_only(session, actor_id):
    phone = await _failed_screen_phone(session, actor_id, in_stock=False)
    panel = await _panel(session, phone)
    assert "order" in _acts(panel) and "Order Screen" in _line(panel, "order").label
    assert not {"reserve", "fit", "receive", "alt"} & set(_acts(panel))


async def test_order_then_received_then_reserve(session, actor_id):
    phone = await _failed_screen_phone(session, actor_id, in_stock=False)
    rp_id = _line(await _panel(session, phone), "order").ref

    msg = await job_actions.order_part(session, phone.id, rp_id, actor_id, supplier="ScreenCo",
                                       unit_cost="£35.50", shipping="2.99", expected="2026-11-02")
    assert "ScreenCo" in msg and "PO-000001" in msg
    panel = await _panel(session, phone)
    assert "receive" in _acts(panel) and "order" not in _acts(panel)
    assert "PO-000001" in _line(panel, "receive").label

    order = await orders_service.get_order(session, "PO-000001")
    assert order.status == "ORDERED" and order.supplier == "ScreenCo" and str(order.shipping_cost) == "2.99"
    assert order.lines[0].phone_model_id is not None and order.lines[0].unit_cost == 35.5

    with pytest.raises(JobActionError, match="already covered"):
        await job_actions.order_part(session, phone.id, rp_id, actor_id, supplier="Other", unit_cost="30")

    await job_actions.receive(session, order.id, actor_id)
    panel = await _panel(session, phone)
    assert "reserve" in _acts(panel) and "receive" not in _acts(panel)


async def test_draft_order_offers_placed_button(session, actor_id):
    phone = await _buy(session, actor_id)
    await _record(session, phone, "display", TestOutcome.FAIL, actor_id)
    draft = await _order(session, actor_id, "Screen", placed=False)
    panel = await _panel(session, phone)
    assert "placed" in _acts(panel) and "order" not in _acts(panel)
    await job_actions.mark_placed(session, draft.id, actor_id)
    assert "receive" in _acts(await _panel(session, phone))


async def test_one_part_in_stock_one_missing_shows_both_buttons(session, actor_id):
    await _stock(session, actor_id, "Screen")
    phone = await _buy(session, actor_id)
    await _record(session, phone, "display", TestOutcome.FAIL, actor_id)
    await _record(session, phone, "battery_health", TestOutcome.FAIL, actor_id)
    panel = await _panel(session, phone)
    assert {"reserve", "order"} <= set(_acts(panel))
    assert [a.group for a in panel.actions if a.act in ("reserve", "order")] == ["line", "line"]


async def test_order_validation(session, actor_id):
    phone = await _failed_screen_phone(session, actor_id, in_stock=False)
    rp_id = _line(await _panel(session, phone), "order").ref
    for kwargs, msg in [(dict(supplier="", unit_cost="3"), "Who"), (dict(supplier="x", unit_cost="abc"), "number"),
                        (dict(supplier="x", unit_cost="-1"), "negative"),
                        (dict(supplier="x", unit_cost="3", expected="soon"), "2026-11-03")]:
        with pytest.raises(JobActionError, match=msg):
            await job_actions.order_part(session, phone.id, rp_id, actor_id, **kwargs)


# ------------------------------------------------------------------ faults, plan, wrong part
async def test_logging_a_fault_with_a_part_puts_it_on_the_panel(session, actor_id):
    phone = await _buy(session, actor_id)
    await job_actions.log_fault(session, phone.id, actor_id, description="Cracked back glass", part_type_name="Back Glass")
    panel = await _panel(session, phone)
    assert panel.stage == "REPAIR" and "order" in _acts(panel) and "Back Glass" in _line(panel, "order").label
    assert phone.current_status == "REPAIR_REQUIRED"


async def test_plan_button_only_when_faults_exist_but_no_repair(session, actor_id):
    phone = await _buy(session, actor_id)
    await phones_service.add_fault(session, phone, "Dead pixel", actor_id)   # no part, no repair yet
    panel = await _panel(session, phone)
    assert "plan" in _acts(panel)
    await job_actions.plan_repair(session, phone.id, actor_id)
    assert "plan" not in _acts(await _panel(session, phone))
    clean = await _buy(session, actor_id)
    with pytest.raises(JobActionError):
        await job_actions.plan_repair(session, clean.id, actor_id)


async def test_wrong_part_can_be_removed_and_requirement_returns(session, actor_id):
    phone = await _failed_screen_phone(session, actor_id)
    rp_id = _line(await _panel(session, phone), "reserve").ref
    await job_actions.reserve(session, phone.id, rp_id, actor_id)
    await job_actions.fit(session, phone.id, rp_id, actor_id)
    repair = await repairs_service.get_active_repair(session, phone)
    (_, part), = job_actions.fitted_parts(repair)
    await job_actions.unfit(session, phone.id, part.id, actor_id)
    panel = await _panel(session, phone)
    assert panel.stage == "REPAIR" and ({"reserve", "order"} & set(_acts(panel)))
    assert "unfit" not in _acts(panel)


async def test_edit_and_move(session, actor_id):
    phone = await _buy(session, actor_id)
    await job_actions.edit_details(session, phone.id, actor_id, battery="87%", cosmetic="Light scratches")
    assert phone.battery_health == 87 and phone.cosmetic_condition == "Light scratches"
    with pytest.raises(JobActionError):
        await job_actions.edit_details(session, phone.id, actor_id, battery="150")
    with pytest.raises(JobActionError):
        await job_actions.edit_details(session, phone.id, actor_id)
    await job_actions.move(session, phone.id, actor_id, "b2")
    await session.refresh(phone)
    assert phone.location.code == "B2"


# ------------------------------------------------------------------ later steps
async def test_prep_and_list_panels_are_minimal_for_now(session, actor_id):
    phone = await _buy(session, actor_id)
    await phones_service.set_status(session, phone, "TESTED", actor_id)
    assert _acts(await _panel(session, phone)) == ["done", "fault", "details"]
    await phones_service.set_status(session, phone, "READY_FOR_LISTING", actor_id)
    panel = await _panel(session, phone)
    assert panel.stage == "LIST" and "listed" in _line(panel, "done").label


async def test_panel_hash_is_stable_until_something_changes(session, actor_id):
    phone = await _buy(session, actor_id)
    h1 = (await _panel(session, phone)).content_hash()
    assert h1 == (await _panel(session, phone)).content_hash()
    await _record(session, phone, "touchscreen", TestOutcome.PASS, actor_id)
    assert h1 != (await _panel(session, phone)).content_hash()


# ------------------------------------------------------------------ job lifecycle (DB side)
async def test_one_job_channel_per_phone_and_closes_when_listed(session, actor_id):
    phone = await _buy(session, actor_id)
    first = await jobs_service.register_job(session, phone_id=phone.id, guild_id=1, channel_id=100, created_by=actor_id)
    assert first is not None
    assert await jobs_service.register_job(session, phone_id=phone.id, guild_id=1, channel_id=101, created_by=actor_id) is None
    assert await jobs_service.jobs_to_close(session) == []
    await phones_service.set_status(session, phone, "LISTED", actor_id)
    assert [j.channel_id for j in await jobs_service.jobs_to_close(session)] == [100]


async def test_a_phone_with_a_job_gets_in_progress_tickets_through_every_step(session, actor_id):
    phone = await _failed_screen_phone(session, actor_id)
    await jobs_service.register_job(session, phone_id=phone.id, guild_id=1, channel_id=100, created_by=actor_id)
    t = await _ticket(session, phone)
    t.status = "TODO"                                   # ticket predates the channel
    rp_id = _line(await _panel(session, phone), "reserve").ref
    await job_actions.reserve(session, phone.id, rp_id, actor_id)
    await job_actions.fit(session, phone.id, rp_id, actor_id)
    nxt = await _ticket(session, phone)                 # step changed: REPAIR -> VERIFY
    assert nxt.stage == "VERIFY" and nxt.id != t.id
    assert nxt.status == "IN_PROGRESS" and nxt.assignee_discord_id == actor_id


async def test_waiting_ticket_with_a_job_resumes_in_progress_when_part_arrives(session, actor_id):
    phone = await _failed_screen_phone(session, actor_id, in_stock=False)
    await jobs_service.register_job(session, phone_id=phone.id, guild_id=1, channel_id=100, created_by=actor_id)
    t = await _ticket(session, phone)
    assert t.status == "BLOCKED" and t.system_blocked
    await _stock(session, actor_id, "Screen")
    t = await _ticket(session, phone)
    assert t.status == "IN_PROGRESS"
