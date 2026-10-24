"""Parts are not tested before fitting: they are fitted and either work or they don't.

* `confirm_working` records that a part worked once it was in a phone.
* `record_failure` handles one that didn't: the part is scrapped, returned to
  its supplier or kept as faulty (the person chooses), the cost is written off
  as a business loss (never charged to the phone), and the requirement goes
  back on the repair list, unless the same part type has now failed
  DIAGNOSE_AFTER times on this phone, in which case the bot says it's probably
  not the part.
* Returns open a "chase the refund" ticket; `record_refund` closes it and
  reduces the loss.
* `supplier_quality` counts failures per supplier.

No Discord imports.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional, Sequence

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.commerce_models import Expense, Order, OrderLine
from app.models.config_models import ExpenseCategory, WorkflowStatus
from app.models.enums import RepairPartLineStatus
from app.models.failure_models import PartFailure
from app.models.inventory_models import Part, Phone
from app.models.repair_models import RepairPart
from app.models.task_models import Task
from app.services import parts as parts_service
from app.services import tasks as tasks_service
from app.services.events import log_event
from app.services.ids import next_internal_id

# After this many failed parts of the same type on one phone the bot stops
# offering "another one" and says the part is probably not the problem.
DIAGNOSE_AFTER = 2
LOSS_CATEGORY = "Failed parts"
RETURN_TICKET_CATEGORY = "Supplier returns"

ACTION_STATUS = {"SCRAP": "SCRAPPED", "RETURN": "RETURNED", "FAULTY": "FAULTY"}
ACTION_LABEL = {"SCRAP": "scrapped", "RETURN": "returned to supplier", "FAULTY": "kept as faulty"}


class PartFailureError(ValueError):
    pass


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


# ---------------------------------------------------------------- it works
async def confirm_working(session: AsyncSession, part: Part, phone: Phone, actor: int) -> None:
    """Someone checked: the part works in this phone."""
    part.confirmed_working_at = _now()
    part.confirmed_working_in = phone.internal_id
    await session.flush()
    await log_event(session, event_type="PART_CONFIRMED_WORKING", entity_type="PART", entity_id=part.internal_id,
                    actor_discord_id=actor, related_entity_type="PHONE", related_entity_id=phone.internal_id)


def proven_tag(part: Part) -> str:
    """Short label for lists: '' if it has never been proven in a phone."""
    return f"✅ worked in {part.confirmed_working_in}" if part.confirmed_working_at else ""


# ---------------------------------------------------------------- it doesn't
async def count_failures(session: AsyncSession, phone_id: int, part_type_id: int) -> int:
    return (await session.execute(
        select(func.count()).select_from(PartFailure).where(
            PartFailure.phone_id == phone_id, PartFailure.part_type_id == part_type_id))).scalar_one()


@dataclass
class FailureInfo:
    count: int
    part_type: str
    part_ids: list[str]


async def failure_info(session: AsyncSession) -> dict[tuple[int, int], FailureInfo]:
    """(phone_id, part_type_id) -> the parts of that type that have failed on that phone."""
    from app.models.config_models import PartTypeConfig
    rows = (await session.execute(
        select(PartFailure.phone_id, PartFailure.part_type_id, PartTypeConfig.name, Part.internal_id)
        .join(Part, Part.id == PartFailure.part_id)
        .join(PartTypeConfig, PartTypeConfig.id == PartFailure.part_type_id)
        .where(PartFailure.phone_id.is_not(None)).order_by(PartFailure.id))).all()
    out: dict[tuple[int, int], FailureInfo] = {}
    for phone_id, type_id, name, internal_id in rows:
        info = out.setdefault((phone_id, type_id), FailureInfo(0, name, []))
        info.count += 1
        info.part_ids.append(internal_id)
    return out


async def _supplier(session: AsyncSession, part: Part) -> tuple[Optional[Order], Optional[str]]:
    if part.source_order_line_id is None:
        return None, None
    line = await session.get(OrderLine, part.source_order_line_id)
    order = await session.get(Order, line.order_id) if line else None
    return order, (order.supplier if order else None)


async def _ensure_part_status(session: AsyncSession, code: str, label: str) -> None:
    """Part statuses are configurable; make sure the one we're about to use exists."""
    exists = (await session.execute(select(WorkflowStatus.id).where(
        WorkflowStatus.entity_type == "PART", WorkflowStatus.code == code))).first()
    if exists:
        return
    top = (await session.execute(select(func.coalesce(func.max(WorkflowStatus.sort_order), 0)).where(
        WorkflowStatus.entity_type == "PART"))).scalar_one()
    session.add(WorkflowStatus(entity_type="PART", code=code, label=label, sort_order=top + 1, is_terminal=True))
    await session.flush()


async def _loss_category(session: AsyncSession) -> ExpenseCategory:
    cat = (await session.execute(select(ExpenseCategory).where(ExpenseCategory.name == LOSS_CATEGORY))).scalar_one_or_none()
    if cat is None:
        cat = ExpenseCategory(name=LOSS_CATEGORY)
        session.add(cat)
        await session.flush()
    return cat


async def record_failure(session: AsyncSession, *, part: Part, phone: Phone, repair_part: Optional[RepairPart],
                         action: str, reason: str, actor: int, installed: bool) -> PartFailure:
    """The part didn't work. `installed` is False when it was found bad at the
    moment of fitting (it never went into the phone), True when it came out later."""
    action = (action or "").upper()
    if action not in ACTION_STATUS:
        raise PartFailureError("Choose scrap, return to supplier, or keep as faulty.")
    status = ACTION_STATUS[action]
    reason = (reason or "").strip() or None
    if action == "RETURN":
        await _ensure_part_status(session, "RETURNED", "Returned to supplier")

    already = await count_failures(session, phone.id, part.part_type_id)
    order, supplier = await _supplier(session, part)
    repair = repair_part.repair if repair_part is not None else None
    note = f"Failed: {ACTION_LABEL[action]}" + (f" ({reason})" if reason else "")

    if installed:
        await parts_service.remove_part(session, part, repair_part, actor, new_status=status, notes=note)
    else:
        old = part.status
        part.status = status
        part.reserved_for_repair_id = None
        part.installed_phone_id = None
        if repair_part is not None:
            repair_part.part_id = None
            repair_part.status = RepairPartLineStatus.REQUIRED
            repair_part.reserved_at = None
        await session.flush()
        await log_event(session, event_type="PART_STATUS_CHANGED", entity_type="PART", entity_id=part.internal_id,
                        actor_discord_id=actor, previous_state={"status": old}, new_state={"status": status}, notes=note)

    # Put the requirement back for a replacement, unless this has now failed too many times.
    flagged = already + 1 >= DIAGNOSE_AFTER
    if repair is not None and repair_part is not None:
        if installed and not flagged:
            repair.parts.append(RepairPart(required_part_type_id=repair_part.required_part_type_id, quantity=1,
                                           status=RepairPartLineStatus.REQUIRED))
        elif not installed and flagged:
            repair_part.status = RepairPartLineStatus.CANCELLED

    # Business loss, never charged to the phone.
    loss = Decimal(part.cost or 0)
    expense = None
    if loss > 0:
        expense = Expense(
            internal_id=await next_internal_id(session, "expense"), category_id=(await _loss_category(session)).id,
            amount=loss, phone_id=None, incurred_at=dt.date.today(), created_by_discord_id=actor,
            description=f"{part.internal_id} {part.display_name()} failed ({ACTION_LABEL[action]})"
                        + (f", fitted in {phone.internal_id}" if phone else "") + (f": {reason}" if reason else ""))
        session.add(expense)
        await session.flush()

    failure = PartFailure(
        part_id=part.id, part_type_id=part.part_type_id, phone_id=phone.id,
        repair_part_id=repair_part.id if repair_part is not None else None,
        order_id=order.id if order else None, supplier=supplier, action=action, reason=reason,
        was_installed=installed, loss_amount=loss, expense_id=expense.id if expense else None,
        return_status="AWAITING_REFUND" if action == "RETURN" else None, created_by_discord_id=actor)
    session.add(failure)
    await session.flush()

    if action == "RETURN":
        who = supplier or "the supplier"
        ticket = await tasks_service.new_task(
            session, title=f"📮 Return {part.internal_id} ({part.display_name()}) to {who}",
            description=(f"Failed in {phone.internal_id}" + (f": {reason}" if reason else "") +
                         f"\nRefund expected: £{loss}\nPress **Done** when the refund arrives and enter the amount "
                         "(enter 0 if they won't refund)."),
            category=RETURN_TICKET_CATEGORY, scope="GENERAL", priority="NORMAL", status="TODO",
            due_date=dt.date.today() + dt.timedelta(days=14))
        failure.task_id = ticket.id
        await session.flush()

    await log_event(session, event_type="PART_FAILED", entity_type="PART", entity_id=part.internal_id,
                    actor_discord_id=actor, related_entity_type="PHONE", related_entity_id=phone.internal_id,
                    new_state={"action": action, "loss": str(loss), "supplier": supplier}, notes=reason)
    return failure


# ---------------------------------------------------------------- refunds
async def failure_for_task(session: AsyncSession, task_id: int) -> Optional[PartFailure]:
    return (await session.execute(select(PartFailure).where(PartFailure.task_id == task_id))).scalar_one_or_none()


async def record_refund(session: AsyncSession, failure: PartFailure, amount: Decimal, actor: int) -> PartFailure:
    """The supplier refunded (or refused). The business loss shrinks by what came back."""
    if failure.return_status != "AWAITING_REFUND":
        raise PartFailureError("This return isn't waiting for a refund.")
    if amount < 0:
        raise PartFailureError("A refund can't be negative.")
    failure.refund_amount = amount
    failure.refunded_at = dt.date.today()
    failure.return_status = "REFUNDED" if amount > 0 else "NO_REFUND"
    if failure.expense_id:
        expense = await session.get(Expense, failure.expense_id)
        if expense is not None:
            expense.amount = max(Decimal(failure.loss_amount) - amount, Decimal("0"))
    if failure.task_id:
        ticket = await session.get(Task, failure.task_id)
        if ticket is not None and ticket.status not in ("DONE", "CANCELLED"):
            await tasks_service.set_task_status(session, ticket, "DONE", actor)
    await session.flush()
    part = await session.get(Part, failure.part_id)
    await log_event(session, event_type="PART_REFUND_RECEIVED", entity_type="PART",
                    entity_id=part.internal_id if part else str(failure.part_id), actor_discord_id=actor,
                    new_state={"refund": str(amount), "status": failure.return_status})
    return failure


# ---------------------------------------------------------------- supplier quality
@dataclass
class SupplierQuality:
    supplier: str
    received: int
    failures: int
    loss: Decimal
    refunded: Decimal
    awaiting_refund: int
    awaiting_amount: Decimal

    @property
    def failure_rate(self) -> Optional[float]:
        return (self.failures / self.received) if self.received else None


async def supplier_quality(session: AsyncSession, since: Optional[dt.date] = None) -> Sequence[SupplierQuality]:
    """Per supplier: parts received, how many failed, and the money. Worst first."""
    recv_stmt = (select(Order.supplier, func.coalesce(func.sum(OrderLine.received_quantity), 0))
                 .join(OrderLine, OrderLine.order_id == Order.id).group_by(Order.supplier))
    fail_stmt = select(PartFailure).where(PartFailure.supplier.is_not(None))
    if since:
        recv_stmt = recv_stmt.where(func.date(Order.created_at) >= since)
        fail_stmt = fail_stmt.where(func.date(PartFailure.created_at) >= since)
    received = {s: int(n) for s, n in (await session.execute(recv_stmt)).all()}
    stats: dict[str, SupplierQuality] = {s: SupplierQuality(s, n, 0, Decimal(0), Decimal(0), 0, Decimal(0))
                                         for s, n in received.items()}
    for f in (await session.execute(fail_stmt)).scalars().all():
        q = stats.setdefault(f.supplier, SupplierQuality(f.supplier, 0, 0, Decimal(0), Decimal(0), 0, Decimal(0)))
        q.failures += 1
        q.loss += Decimal(f.loss_amount or 0)
        q.refunded += Decimal(f.refund_amount or 0)
        if f.return_status == "AWAITING_REFUND":
            q.awaiting_refund += 1
            q.awaiting_amount += Decimal(f.loss_amount or 0)
    return sorted(stats.values(), key=lambda q: (-(q.failure_rate or 0), -q.failures, q.supplier))
