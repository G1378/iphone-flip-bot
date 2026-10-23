"""Ticket service shared by both kinds of ticket.

* GENERAL tickets (business jobs, recurring routines, manual tickets) live here.
* PHONE tickets are created and closed automatically by
  app.services.phone_tickets; this module only handles what a person can do to
  any ticket (start, block, claim, note) plus the queries the Discord side needs.

Pure DB logic, no Discord imports.
"""
from __future__ import annotations

import datetime as dt
from typing import Optional, Sequence

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.inventory_models import Phone
from app.models.task_models import OPEN_TASK_STATUSES, TASK_PRIORITIES, TASK_STATUSES, Task, TaskTemplate
from app.services.events import log_event
from app.services.ids import next_internal_id
from app.services.task_templates import GENERAL_TEMPLATES

PRIORITY_ORDER = {p: i for i, p in enumerate(reversed(TASK_PRIORITIES))}  # URGENT first


class TaskError(ValueError):
    pass


# ---------------------------------------------------------------- helpers
def _today() -> dt.date:
    return dt.date.today()


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def bump(task: Task) -> None:
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


# ---------------------------------------------------------------- templates (general only)
async def seed_templates_if_empty(session: AsyncSession) -> None:
    if (await session.execute(select(TaskTemplate.id).limit(1))).first():
        return
    for i, t in enumerate(GENERAL_TEMPLATES):
        session.add(TaskTemplate(
            code=t.code, title=t.title, description=t.description or None, category=t.category,
            scope="GENERAL", priority=t.priority, recur_days=t.recur, sort_order=i, active=True,
        ))
    await session.flush()


async def list_templates(session: AsyncSession, include_inactive: bool = False) -> Sequence[TaskTemplate]:
    stmt = select(TaskTemplate).where(TaskTemplate.scope == "GENERAL").order_by(TaskTemplate.sort_order)
    if not include_inactive:
        stmt = stmt.where(TaskTemplate.active.is_(True))
    return (await session.execute(stmt)).scalars().all()


async def get_template(session: AsyncSession, code: str) -> Optional[TaskTemplate]:
    return (await session.execute(select(TaskTemplate).where(TaskTemplate.code == code.strip().lower()))).scalar_one_or_none()


async def add_template(session: AsyncSession, *, code: str, title: str, category: str, priority: str = "NORMAL",
                       recur_days: Optional[int] = None, description: Optional[str] = None) -> TaskTemplate:
    code = code.strip().lower().replace(" ", "_")
    if not code or not title.strip():
        raise TaskError("A template needs a code and a title.")
    if await get_template(session, code):
        raise TaskError(f"A template with code `{code}` already exists.")
    if recur_days is not None and recur_days < 1:
        raise TaskError("Repeat interval must be at least 1 day.")
    max_order = (await session.execute(select(func.coalesce(func.max(TaskTemplate.sort_order), 0)))).scalar_one()
    tpl = TaskTemplate(
        code=code, title=title.strip(), description=description, category=category.strip() or "General",
        scope="GENERAL", priority=normalise_priority(priority), recur_days=recur_days,
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


# ---------------------------------------------------------------- creating general tickets
async def new_task(session: AsyncSession, **fields) -> Task:
    task = Task(internal_id=await next_internal_id(session, "task"), version=1, posted_version=0, **fields)
    session.add(task)
    await session.flush()
    return task


async def create_task(session: AsyncSession, title: str, actor_discord_id: Optional[int], *,
                      description: Optional[str] = None, priority: str = "NORMAL",
                      due_date: Optional[dt.date] = None, category: str = "General",
                      assignee_discord_id: Optional[int] = None) -> Task:
    """A manual GENERAL ticket (New ticket button / `/tasks add`). Tickets about a
    specific phone are created automatically and cannot be added by hand."""
    title = title.strip()
    if not title:
        raise TaskError("A ticket needs a title.")
    task = await new_task(
        session, title=title[:200], description=description or None, category=(category or "General")[:40],
        scope="GENERAL", priority=normalise_priority(priority), status="TODO", due_date=due_date,
        assignee_discord_id=assignee_discord_id, created_by_discord_id=actor_discord_id,
    )
    await log_event(session, event_type="TASK_CREATED", entity_type="TASK", entity_id=task.internal_id,
                    actor_discord_id=actor_discord_id, new_state={"title": task.title})
    return task


async def setup_general(session: AsyncSession) -> int:
    """Open the first ticket for every active GENERAL template that has none yet."""
    have = set((await session.execute(
        select(Task.template_id).where(Task.scope == "GENERAL", Task.template_id.is_not(None))
    )).scalars().all())
    created = 0
    for tpl in await list_templates(session):
        if tpl.id in have:
            continue
        await _task_from_template(session, tpl)
        created += 1
    return created


async def _task_from_template(session: AsyncSession, tpl: TaskTemplate) -> Task:
    return await new_task(
        session, title=tpl.title, description=tpl.description, category=tpl.category, scope="GENERAL",
        priority=tpl.priority, status="TODO", template_id=tpl.id, sort_order=tpl.sort_order,
    )


async def reconcile_recurring(session: AsyncSession) -> int:
    """Re-open a recurring ticket once the previous one is closed and `recur_days`
    have passed since it was created. Templates that have never produced a ticket
    are left alone until `/tasks setup-general` is run."""
    opened = 0
    now = _now()
    for tpl in await list_templates(session):
        if not tpl.recur_days:
            continue
        tasks = (await session.execute(
            select(Task).where(Task.template_id == tpl.id).order_by(Task.created_at.desc())
        )).scalars().all()
        if not tasks or any(t.status in OPEN_TASK_STATUSES for t in tasks):
            continue
        if now - tasks[0].created_at >= dt.timedelta(days=tpl.recur_days):
            task = await _task_from_template(session, tpl)
            task.due_date = _today() + dt.timedelta(days=max(1, min(tpl.recur_days, 7)))
            opened += 1
    return opened


# ---------------------------------------------------------------- changes any ticket supports
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
    task.blocked_reason = reason.strip() if new_status == "BLOCKED" else None
    task.system_blocked = False  # a person has taken over; the engine won't flip it back
    if new_status == "DONE":
        task.completed_by_discord_id = actor_discord_id
        task.completed_at = _now()
    elif old == "DONE":
        task.completed_by_discord_id = None
        task.completed_at = None
    if new_status == "IN_PROGRESS" and task.assignee_discord_id is None and actor_discord_id:
        task.assignee_discord_id = actor_discord_id
    bump(task)
    await session.flush()
    await log_event(session, event_type="TASK_STATUS_CHANGED", entity_type="TASK", entity_id=task.internal_id,
                    actor_discord_id=actor_discord_id, previous_state={"status": old},
                    new_state={"status": new_status}, notes=reason)
    return task


async def claim_task(session: AsyncSession, task: Task, actor_discord_id: int) -> Task:
    task.assignee_discord_id = None if task.assignee_discord_id == actor_discord_id else actor_discord_id
    bump(task)
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
        line = f"{_today().isoformat()}: {note.strip()}"
        task.notes = f"{task.notes}\n{line}" if task.notes else line
    bump(task)
    await session.flush()
    return task


# ---------------------------------------------------------------- queries
async def list_tasks(session: AsyncSession, *, status: Optional[str] = None, phone_internal_id: Optional[str] = None,
                     category: Optional[str] = None, assignee_discord_id: Optional[int] = None,
                     scope: Optional[str] = None, open_only: bool = True) -> Sequence[Task]:
    stmt = select(Task)
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


async def open_ticket_for_phone(session: AsyncSession, phone_id: int) -> Optional[Task]:
    return (await session.execute(
        select(Task).where(Task.phone_id == phone_id, Task.status.in_(OPEN_TASK_STATUSES)))).scalar_one_or_none()


async def phone_labels(session: AsyncSession, phone_ids: set[int]) -> dict[int, tuple[str, str]]:
    if not phone_ids:
        return {}
    rows = (await session.execute(select(Phone).where(Phone.id.in_(phone_ids)))).scalars().all()
    return {p.id: (p.internal_id, p.display_name()) for p in rows}


async def summary_counts(session: AsyncSession) -> dict:
    rows = (await session.execute(select(Task.status, func.count()).group_by(Task.status))).all()
    by_status = {s: n for s, n in rows}
    open_ = Task.status.in_(OPEN_TASK_STATUSES)
    overdue = (await session.execute(select(func.count()).where(open_, Task.due_date < _today()))).scalar_one()
    urgent = (await session.execute(select(func.count()).where(open_, Task.priority == "URGENT"))).scalar_one()
    phone_open = (await session.execute(select(func.count()).where(open_, Task.phone_id.is_not(None)))).scalar_one()
    return {"by_status": by_status, "overdue": overdue, "urgent": urgent, "phone_open": phone_open}


# ---------------------------------------------------------------- Discord bookkeeping queries
async def tasks_needing_post(session: AsyncSession, limit: int = 15) -> Sequence[Task]:
    stmt = select(Task).where(Task.posted_version < Task.version).order_by(Task.id).limit(limit)
    return (await session.execute(stmt)).scalars().all()


async def tasks_overdue_unnotified(session: AsyncSession) -> Sequence[Task]:
    return (await session.execute(
        select(Task).where(Task.status.in_(OPEN_TASK_STATUSES), Task.due_date < _today(),
                           Task.overdue_notified.is_(False), Task.discord_message_id.is_not(None)).limit(10))).scalars().all()


async def closed_phone_tickets_to_delete(session: AsyncSession) -> Sequence[Task]:
    """A phone's finished step vanishes from the channel straight away."""
    return (await session.execute(
        select(Task).where(Task.phone_id.is_not(None), Task.status.in_(("DONE", "CANCELLED")),
                           Task.discord_message_id.is_not(None)).limit(20))).scalars().all()


async def tasks_to_archive(session: AsyncSession, older_than_days: int) -> Sequence[Task]:
    """Closed GENERAL tickets are tidied away after a few days."""
    cutoff = _now() - dt.timedelta(days=older_than_days)
    return (await session.execute(
        select(Task).where(Task.phone_id.is_(None), Task.status.in_(("DONE", "CANCELLED")),
                           Task.discord_message_id.is_not(None), Task.updated_at < cutoff).limit(20))).scalars().all()
