"""Task / ticket engine. Pure DB logic, no Discord imports.

Lifecycle of a phone checklist
------------------------------
1. `reconcile_triggers` notices a new phone and calls `ensure_phone_checklist`,
   which creates every active PHONE template as a hidden (released=False) task.
2. Whenever a phone (or its repair) enters a new status, tasks whose template
   `trigger` names that status are *released* (shown in the channel). Tasks
   whose `auto_complete_trigger` matches are ticked off automatically.
3. Scrapping a phone cancels everything that is no longer relevant; completing
   it cancels the conditional tasks that never fired (e.g. repair steps on a
   phone that never needed repairing).

Because it works by watching statuses, no other service needs to know tasks
exist - /status, listings, finance and eBay sync keep working untouched.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Optional, Sequence

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.inventory_models import Phone
from app.models.repair_models import Repair
from app.models.task_models import (
    OPEN_TASK_STATUSES, TASK_PRIORITIES, TASK_STATUSES, Task, TaskTemplate, TaskTriggerState,
)
from app.services.events import log_event
from app.services.ids import next_internal_id
from app.services.task_templates import GENERAL_TEMPLATES, PHONE_TEMPLATES

# A phone only gets an automatic checklist if we first see it at one of these
# stages. Phones that already existed mid-life when the feature was deployed
# are skipped (no surprise flood); use `/tasks phone` -> Generate checklist.
CHECKLIST_START_STATUSES = ("PURCHASED", "AWAITING_TEST")

PRIORITY_ORDER = {p: i for i, p in enumerate(reversed(TASK_PRIORITIES))}  # URGENT first


class TaskError(ValueError):
    pass


# ---------------------------------------------------------------- helpers
def parse_triggers(raw: Optional[str]) -> set[str]:
    return {t.strip().upper() for t in (raw or "").split(",") if t.strip()}


def _today() -> dt.date:
    return dt.date.today()


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _bump(task: Task) -> None:
    task.version = (task.version or 0) + 1


def normalise_priority(value: Optional[str]) -> str:
    v = (value or "NORMAL").strip().upper()
    aliases = {"MED": "NORMAL", "MEDIUM": "NORMAL", "L": "LOW", "H": "HIGH", "U": "URGENT", "N": "NORMAL"}
    v = aliases.get(v, v)
    if v not in TASK_PRIORITIES:
        raise TaskError(f"Priority must be one of: {', '.join(p.lower() for p in TASK_PRIORITIES)}.")
    return v


def parse_due(value: Optional[str]) -> Optional[dt.date]:
    """'2026-11-03', '3d' (3 days from now), 'today', 'tomorrow', or blank."""
    v = (value or "").strip().lower()
    if not v:
        return None
    if v == "today":
        return _today()
    if v == "tomorrow":
        return _today() + dt.timedelta(days=1)
    if v.endswith("d") and v[:-1].isdigit():
        return _today() + dt.timedelta(days=int(v[:-1]))
    try:
        return dt.date.fromisoformat(v)
    except ValueError:
        raise TaskError("Due date must look like `2026-11-03`, `3d`, `today` or `tomorrow`.")


# ---------------------------------------------------------------- templates
async def seed_templates_if_empty(session: AsyncSession) -> None:
    if (await session.execute(select(TaskTemplate.id).limit(1))).first():
        return
    for i, t in enumerate(PHONE_TEMPLATES):
        session.add(TaskTemplate(
            code=t.code, title=t.title, description=t.description or None, category=t.category,
            scope="PHONE", trigger=t.trigger, auto_complete_trigger=t.auto, priority=t.priority,
            due_after_days=t.due_days, sort_order=i, active=True,
        ))
    for i, t in enumerate(GENERAL_TEMPLATES):
        session.add(TaskTemplate(
            code=t.code, title=t.title, description=t.description or None, category=t.category,
            scope="GENERAL", priority=t.priority, recur_days=t.recur, sort_order=1000 + i, active=True,
        ))
    await session.flush()


async def list_templates(session: AsyncSession, scope: Optional[str] = None,
                         include_inactive: bool = False) -> Sequence[TaskTemplate]:
    stmt = select(TaskTemplate).order_by(TaskTemplate.scope, TaskTemplate.sort_order)
    if scope:
        stmt = stmt.where(TaskTemplate.scope == scope.upper())
    if not include_inactive:
        stmt = stmt.where(TaskTemplate.active.is_(True))
    return (await session.execute(stmt)).scalars().all()


async def get_template(session: AsyncSession, code: str) -> Optional[TaskTemplate]:
    return (await session.execute(select(TaskTemplate).where(TaskTemplate.code == code.strip().lower()))).scalar_one_or_none()


async def add_template(session: AsyncSession, *, code: str, title: str, scope: str, category: str,
                       trigger: Optional[str] = None, priority: str = "NORMAL",
                       recur_days: Optional[int] = None, due_after_days: Optional[int] = None,
                       description: Optional[str] = None) -> TaskTemplate:
    code = code.strip().lower().replace(" ", "_")
    scope = scope.strip().upper()
    if scope not in ("PHONE", "GENERAL"):
        raise TaskError("Scope must be `phone` or `general`.")
    if await get_template(session, code):
        raise TaskError(f"A template with code `{code}` already exists.")
    if scope == "PHONE":
        trig = parse_triggers(trigger)
        if not trig or not all(t.startswith(("PHONE:", "REPAIR:")) for t in trig):
            raise TaskError("Phone templates need a trigger like `PHONE:TESTED` or `REPAIR:COMPLETE` "
                            "(comma-separate several).")
    max_order = (await session.execute(select(func.coalesce(func.max(TaskTemplate.sort_order), 0)))).scalar_one()
    tpl = TaskTemplate(
        code=code, title=title.strip(), description=description, category=category.strip() or "General",
        scope=scope, trigger=",".join(sorted(parse_triggers(trigger))) or None,
        priority=normalise_priority(priority), recur_days=recur_days, due_after_days=due_after_days,
        sort_order=max_order + 1, active=True,
    )
    session.add(tpl)
    await session.flush()
    return tpl


async def set_template_active(session: AsyncSession, code: str, active: bool) -> TaskTemplate:
    tpl = await get_template(session, code)
    if tpl is None:
        raise TaskError(f"No template with code `{code}`.")
    tpl.active = active
    await session.flush()
    return tpl


# ---------------------------------------------------------------- creating tickets
async def _new_task(session: AsyncSession, **fields) -> Task:
    task = Task(internal_id=await next_internal_id(session, "task"), version=1, posted_version=0, **fields)
    session.add(task)
    await session.flush()
    return task


async def create_task(session: AsyncSession, title: str, actor_discord_id: Optional[int], *,
                      description: Optional[str] = None, priority: str = "NORMAL",
                      due_date: Optional[dt.date] = None, category: str = "General",
                      phone_internal_id: Optional[str] = None,
                      assignee_discord_id: Optional[int] = None) -> Task:
    """A manual ticket (the New Ticket button / `/tasks add`)."""
    title = title.strip()
    if not title:
        raise TaskError("A ticket needs a title.")
    phone_id = None
    if phone_internal_id and phone_internal_id.strip():
        phone = (await session.execute(
            select(Phone).where(Phone.internal_id == phone_internal_id.strip().upper())
        )).scalar_one_or_none()
        if phone is None:
            raise TaskError(f"No phone with ID `{phone_internal_id.strip().upper()}`.")
        phone_id = phone.id
    task = await _new_task(
        session, title=title[:200], description=description or None, category=(category or "General")[:40],
        scope="PHONE" if phone_id else "GENERAL", priority=normalise_priority(priority), status="TODO",
        phone_id=phone_id, due_date=due_date, assignee_discord_id=assignee_discord_id,
        released=True, created_by_discord_id=actor_discord_id,
    )
    await log_event(session, event_type="TASK_CREATED", entity_type="TASK", entity_id=task.internal_id,
                    actor_discord_id=actor_discord_id, new_state={"title": task.title})
    return task


async def _task_from_template(session: AsyncSession, tpl: TaskTemplate, *, phone_id: Optional[int],
                              released: bool) -> Task:
    due = _today() + dt.timedelta(days=tpl.due_after_days) if (released and tpl.due_after_days) else None
    return await _new_task(
        session, title=tpl.title, description=tpl.description, category=tpl.category, scope=tpl.scope,
        priority=tpl.priority, status="TODO", template_id=tpl.id, phone_id=phone_id, due_date=due,
        sort_order=tpl.sort_order, released=released,
    )


async def ensure_phone_checklist(session: AsyncSession, phone_id: int) -> int:
    """Create any missing checklist tasks for a phone (hidden until released).
    Safe to call repeatedly. Returns how many were created."""
    existing = set((await session.execute(
        select(Task.template_id).where(Task.phone_id == phone_id, Task.template_id.is_not(None))
    )).scalars().all())
    created = 0
    for tpl in await list_templates(session, scope="PHONE"):
        if tpl.id in existing:
            continue
        await _task_from_template(session, tpl, phone_id=phone_id, released=False)
        created += 1
    return created


async def setup_general(session: AsyncSession) -> int:
    """Open the first ticket for every active GENERAL template that has none yet."""
    have = set((await session.execute(
        select(Task.template_id).where(Task.scope == "GENERAL", Task.template_id.is_not(None))
    )).scalars().all())
    created = 0
    for tpl in await list_templates(session, scope="GENERAL"):
        if tpl.id in have:
            continue
        await _task_from_template(session, tpl, phone_id=None, released=True)
        created += 1
    return created


# ---------------------------------------------------------------- ticket changes
async def get_task(session: AsyncSession, internal_id: str) -> Optional[Task]:
    return (await session.execute(select(Task).where(Task.internal_id == internal_id.strip().upper()))).scalar_one_or_none()


async def get_task_by_message(session: AsyncSession, message_id: int) -> Optional[Task]:
    return (await session.execute(select(Task).where(Task.discord_message_id == message_id))).scalar_one_or_none()


async def set_task_status(session: AsyncSession, task: Task, new_status: str, actor_discord_id: Optional[int],
                          reason: Optional[str] = None) -> Task:
    new_status = new_status.upper()
    if new_status not in TASK_STATUSES:
        raise TaskError(f"Unknown status `{new_status}`.")
    if new_status == "BLOCKED" and not (reason or "").strip():
        raise TaskError("Say what it is blocked on.")
    old = task.status
    task.status = new_status
    if new_status == "BLOCKED":
        task.blocked_reason = reason.strip()
    elif new_status != "BLOCKED":
        task.blocked_reason = None
    if new_status == "DONE":
        task.completed_by_discord_id = actor_discord_id
        task.completed_at = _now()
    elif old == "DONE":
        task.completed_by_discord_id = None
        task.completed_at = None
    if new_status == "IN_PROGRESS" and task.assignee_discord_id is None and actor_discord_id:
        task.assignee_discord_id = actor_discord_id
    _bump(task)
    await session.flush()
    await log_event(session, event_type="TASK_STATUS_CHANGED", entity_type="TASK", entity_id=task.internal_id,
                    actor_discord_id=actor_discord_id, previous_state={"status": old},
                    new_state={"status": new_status}, notes=reason)
    return task


async def claim_task(session: AsyncSession, task: Task, actor_discord_id: int) -> Task:
    task.assignee_discord_id = None if task.assignee_discord_id == actor_discord_id else actor_discord_id
    _bump(task)
    await session.flush()
    return task


async def update_task(session: AsyncSession, task: Task, *, title: Optional[str] = None,
                      description: Optional[str] = None, priority: Optional[str] = None,
                      due_date: Optional[dt.date] = None, clear_due: bool = False,
                      note: Optional[str] = None, assignee_discord_id: Optional[int] = None,
                      actor_discord_id: Optional[int] = None) -> Task:
    if title:
        task.title = title.strip()[:200]
    if description is not None:
        task.description = description or None
    if priority:
        task.priority = normalise_priority(priority)
    if clear_due:
        task.due_date = None
        task.overdue_notified = False
    elif due_date:
        task.due_date = due_date
        task.overdue_notified = False
    if assignee_discord_id is not None:
        task.assignee_discord_id = assignee_discord_id
    if note and note.strip():
        stamp = _today().isoformat()
        line = f"{stamp}: {note.strip()}"
        task.notes = f"{task.notes}\n{line}" if task.notes else line
    _bump(task)
    await session.flush()
    return task


# ---------------------------------------------------------------- phone triggers
async def _release_or_complete(session: AsyncSession, phone_id: int, trigger: str) -> tuple[int, int]:
    """Apply one status trigger to a phone's checklist. Returns (released, auto_done)."""
    rows = (await session.execute(
        select(Task, TaskTemplate)
        .join(TaskTemplate, Task.template_id == TaskTemplate.id)
        .where(Task.phone_id == phone_id, Task.status.in_(OPEN_TASK_STATUSES))
    )).all()
    released = auto_done = 0
    for task, tpl in rows:
        if trigger in parse_triggers(tpl.auto_complete_trigger):
            task.status = "DONE"
            task.completed_at = _now()
            line = f"{_today().isoformat()}: auto-completed ({trigger})"
            task.notes = f"{task.notes}\n{line}" if task.notes else line
            _bump(task)
            auto_done += 1
        elif not task.released and trigger in parse_triggers(tpl.trigger):
            task.released = True
            if tpl.due_after_days and task.due_date is None:
                task.due_date = _today() + dt.timedelta(days=tpl.due_after_days)
            _bump(task)
            released += 1
    await session.flush()
    return released, auto_done


async def release_all_for_phone(session: AsyncSession, phone_id: int) -> int:
    """Escape hatch: show every remaining hidden task for this phone."""
    rows = (await session.execute(
        select(Task).where(Task.phone_id == phone_id, Task.released.is_(False), Task.status == "TODO")
    )).scalars().all()
    for task in rows:
        task.released = True
        _bump(task)
    await session.flush()
    return len(rows)


async def _wrap_up(session: AsyncSession, phone_id: int, status: str) -> None:
    """Tidy leftovers when a phone's story ends."""
    if status not in ("SCRAPPED", "COMPLETE"):
        return
    trigger = f"PHONE:{status}"
    rows = (await session.execute(
        select(Task, TaskTemplate)
        .join(TaskTemplate, Task.template_id == TaskTemplate.id)
        .where(Task.phone_id == phone_id, Task.status.in_(OPEN_TASK_STATUSES))
    )).all()
    for task, tpl in rows:
        mine = trigger in parse_triggers(tpl.trigger)
        if status == "SCRAPPED" and not mine:
            task.status = "CANCELLED"
            _bump(task)
        elif status == "COMPLETE" and not task.released and not mine:
            task.status = "CANCELLED"
            _bump(task)
    await session.flush()


@dataclass
class ReconcileStats:
    checklists_created: int = 0
    released: int = 0
    auto_completed: int = 0
    recurring_opened: int = 0
    notes: list[str] = field(default_factory=list)


async def reconcile_triggers(session: AsyncSession) -> ReconcileStats:
    stats = ReconcileStats()

    # ---- phones
    states = {s.entity_id: s for s in (await session.execute(
        select(TaskTriggerState).where(TaskTriggerState.entity_type == "PHONE"))).scalars().all()}
    for phone_id, status in (await session.execute(select(Phone.id, Phone.current_status))).all():
        st = states.get(phone_id)
        if st is None:
            st = TaskTriggerState(entity_type="PHONE", entity_id=phone_id, last_status=status)
            session.add(st)
            if status in CHECKLIST_START_STATUSES:
                stats.checklists_created += await ensure_phone_checklist(session, phone_id)
                r, a = await _release_or_complete(session, phone_id, f"PHONE:{status}")
                stats.released += r
                stats.auto_completed += a
        elif st.last_status != status:
            st.last_status = status
            r, a = await _release_or_complete(session, phone_id, f"PHONE:{status}")
            stats.released += r
            stats.auto_completed += a
            await _wrap_up(session, phone_id, status)

    # ---- repairs (drive the bench / QC steps of the owning phone)
    rstates = {s.entity_id: s for s in (await session.execute(
        select(TaskTriggerState).where(TaskTriggerState.entity_type == "REPAIR"))).scalars().all()}
    for repair_id, phone_id, status in (await session.execute(
            select(Repair.id, Repair.phone_id, Repair.status))).all():
        st = rstates.get(repair_id)
        if st is None:
            session.add(TaskTriggerState(entity_type="REPAIR", entity_id=repair_id, last_status=status))
        elif st.last_status == status:
            continue
        else:
            st.last_status = status
        r, a = await _release_or_complete(session, phone_id, f"REPAIR:{status}")
        stats.released += r
        stats.auto_completed += a

    # ---- recurring general tickets
    stats.recurring_opened = await reconcile_recurring(session)
    await session.flush()
    return stats


async def reconcile_recurring(session: AsyncSession) -> int:
    """Re-open a recurring GENERAL ticket once the previous one is closed and
    `recur_days` have passed since it was created. Templates that have never
    produced a ticket are left alone until `/tasks setup-general` is run."""
    opened = 0
    now = _now()
    for tpl in await list_templates(session, scope="GENERAL"):
        if not tpl.recur_days:
            continue
        tasks = (await session.execute(
            select(Task).where(Task.template_id == tpl.id).order_by(Task.created_at.desc())
        )).scalars().all()
        if not tasks:
            continue
        if any(t.status in OPEN_TASK_STATUSES for t in tasks):
            continue
        if now - tasks[0].created_at >= dt.timedelta(days=tpl.recur_days):
            task = await _task_from_template(session, tpl, phone_id=None, released=True)
            task.due_date = _today() + dt.timedelta(days=max(1, min(tpl.recur_days, 7)))
            opened += 1
    return opened


# ---------------------------------------------------------------- queries
async def list_tasks(session: AsyncSession, *, status: Optional[str] = None, phone_internal_id: Optional[str] = None,
                     category: Optional[str] = None, assignee_discord_id: Optional[int] = None,
                     scope: Optional[str] = None, open_only: bool = True,
                     include_hidden: bool = False) -> Sequence[Task]:
    stmt = select(Task)
    if not include_hidden:
        stmt = stmt.where(Task.released.is_(True))
    if status:
        stmt = stmt.where(Task.status == status.upper())
    elif open_only:
        stmt = stmt.where(Task.status.in_(OPEN_TASK_STATUSES))
    if phone_internal_id:
        stmt = stmt.join(Phone, Task.phone_id == Phone.id).where(Phone.internal_id == phone_internal_id.strip().upper())
    if category:
        stmt = stmt.where(func.lower(Task.category) == category.strip().lower())
    if assignee_discord_id:
        stmt = stmt.where(Task.assignee_discord_id == assignee_discord_id)
    if scope:
        stmt = stmt.where(Task.scope == scope.upper())
    rows = (await session.execute(stmt)).scalars().all()
    return sorted(rows, key=lambda t: (PRIORITY_ORDER.get(t.priority, 9), t.due_date or dt.date.max, t.sort_order, t.id))


async def phone_checklist(session: AsyncSession, phone_id: int) -> Sequence[Task]:
    rows = (await session.execute(select(Task).where(Task.phone_id == phone_id))).scalars().all()
    return sorted(rows, key=lambda t: (t.sort_order, t.id))


async def phone_labels(session: AsyncSession, phone_ids: set[int]) -> dict[int, tuple[str, str]]:
    if not phone_ids:
        return {}
    rows = (await session.execute(select(Phone).where(Phone.id.in_(phone_ids)))).scalars().all()
    return {p.id: (p.internal_id, p.display_name()) for p in rows}


async def summary_counts(session: AsyncSession) -> dict:
    rows = (await session.execute(
        select(Task.status, func.count()).where(Task.released.is_(True)).group_by(Task.status))).all()
    by_status = {s: n for s, n in rows}
    overdue = (await session.execute(
        select(func.count()).where(Task.released.is_(True), Task.status.in_(OPEN_TASK_STATUSES),
                                   Task.due_date < _today()))).scalar_one()
    urgent = (await session.execute(
        select(func.count()).where(Task.released.is_(True), Task.status.in_(OPEN_TASK_STATUSES),
                                   Task.priority == "URGENT"))).scalar_one()
    hidden = (await session.execute(
        select(func.count()).where(Task.released.is_(False), Task.status == "TODO"))).scalar_one()
    return {"by_status": by_status, "overdue": overdue, "urgent": urgent, "upcoming_hidden": hidden}


# ---------------------------------------------------------------- Discord bookkeeping queries
async def tasks_needing_post(session: AsyncSession, limit: int = 15) -> Sequence[Task]:
    stmt = (select(Task).where(Task.released.is_(True), Task.posted_version < Task.version)
            .order_by(Task.id).limit(limit))
    return (await session.execute(stmt)).scalars().all()


async def tasks_overdue_unnotified(session: AsyncSession) -> Sequence[Task]:
    return (await session.execute(
        select(Task).where(Task.released.is_(True), Task.status.in_(OPEN_TASK_STATUSES),
                           Task.due_date < _today(), Task.overdue_notified.is_(False),
                           Task.discord_message_id.is_not(None)).limit(10))).scalars().all()


async def tasks_to_archive(session: AsyncSession, older_than_days: int) -> Sequence[Task]:
    cutoff = _now() - dt.timedelta(days=older_than_days)
    return (await session.execute(
        select(Task).where(Task.status.in_(("DONE", "CANCELLED")), Task.discord_message_id.is_not(None),
                           Task.updated_at < cutoff).limit(20))).scalars().all()
