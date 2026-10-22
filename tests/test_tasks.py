from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models.enums import AcquisitionType
from app.models.repair_models import Repair
from app.models.task_models import Task, TaskTemplate
from app.services import phones as phones_service
from app.services import tasks as tasks_service
from app.services.task_templates import GENERAL_TEMPLATES, PHONE_TEMPLATES
from app.services.tasks import TaskError


async def _buy(session, actor_id) -> object:
    data = phones_service.BuyPhoneInput(
        manufacturer="Apple", model="iPhone 13", variant=None, storage="128GB", colour="Blue", imei=None,
        serial_number=None, carrier=None, purchase_price=Decimal("120.00"), purchase_date=dt.date(2026, 9, 1),
        seller_source="eBay", acquisition_type=AcquisitionType.PURCHASE if hasattr(AcquisitionType, "PURCHASE") else list(AcquisitionType)[0],
        notes=None, location_code=None,
    )
    return await phones_service.buy_phone(session, data, actor_id)


async def _tasks_for(session, phone_id):
    return (await session.execute(select(Task).where(Task.phone_id == phone_id))).scalars().all()


async def test_templates_are_seeded_once(session):
    count = (await session.execute(select(TaskTemplate))).scalars().all()
    assert len(count) == len(PHONE_TEMPLATES) + len(GENERAL_TEMPLATES)
    await tasks_service.seed_templates_if_empty(session)  # idempotent
    again = (await session.execute(select(TaskTemplate))).scalars().all()
    assert len(again) == len(count)


async def test_every_trigger_uses_a_real_status():
    phone_statuses = {"PURCHASED", "AWAITING_TEST", "TESTED", "REPAIR_REQUIRED", "AWAITING_PARTS", "READY_FOR_LISTING",
                      "LISTED", "SOLD", "SHIPPED", "COMPLETE", "SCRAPPED"}
    repair_statuses = {"PLANNED", "PARTS_RESERVED", "IN_PROGRESS", "COMPLETE", "CANCELLED"}
    for t in PHONE_TEMPLATES:
        for trig in tasks_service.parse_triggers(t.trigger) | tasks_service.parse_triggers(t.auto):
            kind, _, status = trig.partition(":")
            assert status in (phone_statuses if kind == "PHONE" else repair_statuses), (t.code, trig)


async def test_new_phone_gets_hidden_checklist_and_intake_released(session, actor_id):
    phone = await _buy(session, actor_id)
    stats = await tasks_service.reconcile_triggers(session)
    assert stats.checklists_created == len(PHONE_TEMPLATES)
    rows = await _tasks_for(session, phone.id)
    shown = [t for t in rows if t.released]
    assert shown and all(t.category in ("Intake", "Testing") for t in shown)
    assert any(not t.released for t in rows), "later-stage tasks must stay hidden"
    # No duplicates on a second pass
    again = await tasks_service.reconcile_triggers(session)
    assert again.checklists_created == 0 and again.released == 0


async def test_status_change_releases_next_stage_and_auto_completes(session, actor_id):
    phone = await _buy(session, actor_id)
    await tasks_service.reconcile_triggers(session)

    await phones_service.set_status(session, phone, "TESTED", actor_id)
    stats = await tasks_service.reconcile_triggers(session)
    assert stats.released >= 1
    rows = {t.title: t for t in await _tasks_for(session, phone.id)}
    assert rows["Decide route: repair, sell as-is, part-out or scrap"].released
    assert rows["Run the full test checklist (/testing)"].status == "DONE"  # auto-completed at TESTED

    await phones_service.set_status(session, phone, "LISTED", actor_id)
    await tasks_service.reconcile_triggers(session)
    rows = {t.title: t for t in await _tasks_for(session, phone.id)}
    assert rows["Create the eBay listing (/list)"].status == "DONE"
    assert rows["Verify the live listing"].released


async def test_repair_status_releases_bench_tasks(session, actor_id):
    phone = await _buy(session, actor_id)
    await tasks_service.reconcile_triggers(session)
    session.add(Repair(internal_id="RP-000001", phone_id=phone.id, status="PLANNED"))
    await session.flush()
    await tasks_service.reconcile_triggers(session)
    bench = [t for t in await _tasks_for(session, phone.id) if t.category == "Repair" and t.title.startswith("Photograph the phone before")]
    assert bench and not bench[0].released

    repair = (await session.execute(select(Repair))).scalar_one()
    repair.status = "IN_PROGRESS"
    await session.flush()
    await tasks_service.reconcile_triggers(session)
    assert bench[0].released

    repair.status = "COMPLETE"
    await session.flush()
    await tasks_service.reconcile_triggers(session)
    qc = [t for t in await _tasks_for(session, phone.id) if t.category == "Quality Control"]
    assert qc and all(t.released for t in qc)
    done = [t for t in await _tasks_for(session, phone.id) if t.title.startswith("Mark repair complete")]
    assert done[0].status == "DONE"


async def test_scrapping_cancels_irrelevant_tasks_but_keeps_scrap_tasks(session, actor_id):
    phone = await _buy(session, actor_id)
    await tasks_service.reconcile_triggers(session)
    await phones_service.set_status(session, phone, "SCRAPPED", actor_id)
    await tasks_service.reconcile_triggers(session)
    rows = await _tasks_for(session, phone.id)
    scrap = [t for t in rows if t.category == "Scrap"]
    assert scrap and all(t.released and t.status == "TODO" for t in scrap)
    assert all(t.status == "CANCELLED" for t in rows if t.category in ("Dispatch", "Listing", "Repair"))


async def test_complete_cancels_never_needed_repair_tasks(session, actor_id):
    phone = await _buy(session, actor_id)
    await tasks_service.reconcile_triggers(session)
    await phones_service.set_status(session, phone, "COMPLETE", actor_id)
    await tasks_service.reconcile_triggers(session)
    rows = await _tasks_for(session, phone.id)
    assert all(t.status == "CANCELLED" for t in rows if t.category == "Repair" and not t.released)


async def test_legacy_phone_mid_life_gets_no_surprise_checklist(session, actor_id):
    phone = await _buy(session, actor_id)
    phone.current_status = "LISTED"
    await session.flush()
    stats = await tasks_service.reconcile_triggers(session)
    assert stats.checklists_created == 0
    assert not await _tasks_for(session, phone.id)
    # ...but it can be generated on demand
    assert await tasks_service.ensure_phone_checklist(session, phone.id) == len(PHONE_TEMPLATES)


async def test_ticket_lifecycle_and_block_requires_reason(session, actor_id):
    task = await tasks_service.create_task(session, "Buy IPA", actor_id, priority="high", due_date=dt.date(2026, 9, 5))
    assert task.internal_id.startswith("TK-") and task.priority == "HIGH" and task.released

    with pytest.raises(TaskError):
        await tasks_service.set_task_status(session, task, "BLOCKED", actor_id)
    await tasks_service.set_task_status(session, task, "BLOCKED", actor_id, reason="out of stock")
    assert task.status == "BLOCKED" and task.blocked_reason == "out of stock"

    await tasks_service.set_task_status(session, task, "IN_PROGRESS", actor_id)
    assert task.blocked_reason is None and task.assignee_discord_id == actor_id

    v = task.version
    await tasks_service.set_task_status(session, task, "DONE", actor_id)
    assert task.completed_by_discord_id == actor_id and task.completed_at and task.version > v

    await tasks_service.set_task_status(session, task, "TODO", actor_id)  # reopen
    assert task.completed_at is None


async def test_create_task_validations(session, actor_id):
    with pytest.raises(TaskError):
        await tasks_service.create_task(session, "  ", actor_id)
    with pytest.raises(TaskError):
        await tasks_service.create_task(session, "x", actor_id, phone_internal_id="IP-999999")
    with pytest.raises(TaskError):
        tasks_service.normalise_priority("whenever")
    assert tasks_service.parse_due("3d") == dt.date.today() + dt.timedelta(days=3)
    with pytest.raises(TaskError):
        tasks_service.parse_due("next-ish")


async def test_claim_toggles_and_update_appends_notes(session, actor_id):
    task = await tasks_service.create_task(session, "Tidy", actor_id)
    await tasks_service.claim_task(session, task, actor_id)
    assert task.assignee_discord_id == actor_id
    await tasks_service.claim_task(session, task, actor_id)
    assert task.assignee_discord_id is None
    await tasks_service.update_task(session, task, note="first")
    await tasks_service.update_task(session, task, note="second")
    assert "first" in task.notes and "second" in task.notes


async def test_general_setup_and_recurring(session, actor_id):
    n = await tasks_service.setup_general(session)
    assert n == len(GENERAL_TEMPLATES)
    assert await tasks_service.setup_general(session) == 0

    # Recurring ticket stays closed-and-quiet until recur_days have passed.
    tpl = await tasks_service.get_template(session, "rec_stock_check")
    task = (await session.execute(select(Task).where(Task.template_id == tpl.id))).scalar_one()
    await tasks_service.set_task_status(session, task, "DONE", actor_id)
    assert await tasks_service.reconcile_recurring(session) == 0
    task.created_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=8)
    await session.flush()
    assert await tasks_service.reconcile_recurring(session) >= 1
    open_ones = (await session.execute(select(Task).where(Task.template_id == tpl.id, Task.status == "TODO"))).scalars().all()
    assert len(open_ones) == 1 and open_ones[0].due_date is not None


async def test_recurring_does_not_start_before_setup(session):
    assert await tasks_service.reconcile_recurring(session) == 0


async def test_template_add_validation(session):
    with pytest.raises(TaskError):
        await tasks_service.add_template(session, code="x", title="x", scope="phone", category="c", trigger="nonsense")
    tpl = await tasks_service.add_template(session, code="Check Charger", title="Check charger", scope="phone",
                                           category="Testing", trigger="phone:tested")
    assert tpl.code == "check_charger" and tpl.trigger == "PHONE:TESTED"
    with pytest.raises(TaskError):
        await tasks_service.add_template(session, code="check_charger", title="dup", scope="general", category="c")
    await tasks_service.set_template_active(session, "check_charger", False)
    assert tpl.active is False


async def test_listing_orders_by_priority_and_hides_unreleased(session, actor_id):
    await _buy(session, actor_id)
    await tasks_service.reconcile_triggers(session)
    low = await tasks_service.create_task(session, "low one", actor_id, priority="low")
    urgent = await tasks_service.create_task(session, "urgent one", actor_id, priority="urgent")
    rows = await tasks_service.list_tasks(session)
    assert all(t.released for t in rows)
    assert rows.index(urgent) < rows.index(low)


async def test_post_queue_tracks_versions(session, actor_id):
    task = await tasks_service.create_task(session, "Post me", actor_id)
    assert task in await tasks_service.tasks_needing_post(session)
    task.posted_version = task.version
    assert task not in await tasks_service.tasks_needing_post(session)
    await tasks_service.update_task(session, task, note="changed")
    assert task in await tasks_service.tasks_needing_post(session)
