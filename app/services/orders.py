from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional, Sequence

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.config_models import PartTypeConfig
from app.models.enums import InventoryEventType, PartSourceType, RepairPartLineStatus
from app.models.inventory_models import Part
from app.models.repair_models import Repair, RepairPart
from app.models.commerce_models import Order, OrderLine
from app.services.events import log_event
from app.services.ids import next_internal_id


class OrderError(ValueError):
    pass


@dataclass
class OrderLineInput:
    part_type_id: int
    quantity: int
    unit_cost: Decimal
    phone_model_id: Optional[int] = None
    notes: Optional[str] = None


async def create_order(
    session: AsyncSession, supplier: Optional[str], lines: list[OrderLineInput], actor_discord_id: int,
    shipping_cost: Decimal = Decimal("0"), tax: Decimal = Decimal("0"),
    expected_delivery: Optional[dt.date] = None, notes: Optional[str] = None,
) -> Order:
    internal_id = await next_internal_id(session, "order")
    order = Order(
        internal_id=internal_id, supplier=supplier, status="DRAFT",
        shipping_cost=shipping_cost, tax=tax, order_date=dt.date.today(),
        expected_delivery=expected_delivery, notes=notes, lines=[],
    )
    session.add(order)
    await session.flush()

    for line in lines:
        order.lines.append(
            OrderLine(
                part_type_id=line.part_type_id, phone_model_id=line.phone_model_id,
                quantity=line.quantity, unit_cost=line.unit_cost, notes=line.notes,
            )
        )
    await session.flush()

    await log_event(
        session, event_type=InventoryEventType.ORDER_CREATED.value, entity_type="ORDER",
        entity_id=order.internal_id, actor_discord_id=actor_discord_id,
        new_state={"lines": len(lines), "supplier": supplier},
    )
    return order


async def mark_ordered(session: AsyncSession, order: Order, actor_discord_id: int) -> Order:
    order.status = "ORDERED"
    order.order_date = dt.date.today()
    await session.flush()
    return order


async def get_order(session: AsyncSession, internal_id: str) -> Optional[Order]:
    stmt = (
        select(Order)
        .where(Order.internal_id == internal_id.upper())
        .options(selectinload(Order.lines).selectinload(OrderLine.part_type))
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def list_orders(session: AsyncSession, status: Optional[str] = None) -> Sequence[Order]:
    stmt = select(Order).options(selectinload(Order.lines)).order_by(Order.created_at.desc())
    if status:
        stmt = stmt.where(Order.status == status)
    return (await session.execute(stmt)).scalars().all()


async def receive_order(session: AsyncSession, order: Order, actor_discord_id: int) -> list[Part]:
    """Mark an order RECEIVED and create one Part per received unit
    (source_type=PURCHASED, status=AVAILABLE)."""
    if order.status == "RECEIVED":
        raise OrderError(f"{order.internal_id} has already been received.")

    created: list[Part] = []
    for line in order.lines:
        to_create = line.quantity - line.received_quantity
        for _ in range(to_create):
            internal_id = await next_internal_id(session, "part")
            part = Part(
                internal_id=internal_id,
                part_type_id=line.part_type_id,
                source_type=PartSourceType.PURCHASED,
                source_order_line_id=line.id,
                cost=line.unit_cost,
                testing_status="NOT_TESTED",
                status="AVAILABLE",
                purchase_date=dt.date.today(),
            )
            session.add(part)
            await session.flush()
            created.append(part)
            if line.phone_model_id:
                from app.models.inventory_models import PartCompatibleModel
                session.add(PartCompatibleModel(part_id=part.id, phone_model_id=line.phone_model_id))
        line.received_quantity = line.quantity

    order.status = "RECEIVED"
    order.received_date = dt.date.today()
    await session.flush()

    await log_event(
        session, event_type=InventoryEventType.ORDER_RECEIVED.value, entity_type="ORDER",
        entity_id=order.internal_id, actor_discord_id=actor_discord_id,
        new_state={"parts_created": [p.internal_id for p in created]},
    )
    return created


@dataclass
class PartsNeededLine:
    part_type: PartTypeConfig
    required_qty: int
    in_stock_qty: int

    @property
    def to_order_qty(self) -> int:
        return max(0, self.required_qty - self.in_stock_qty)


async def parts_needed(session: AsyncSession) -> list[PartsNeededLine]:
    """Aggregate outstanding REQUIRED/RESERVED repair-part lines by part
    type and compare against AVAILABLE stock of that type."""
    required_stmt = (
        select(RepairPart.required_part_type_id, func.sum(RepairPart.quantity))
        .join(Repair, RepairPart.repair_id == Repair.id)
        .where(
            RepairPart.status.in_([RepairPartLineStatus.REQUIRED, RepairPartLineStatus.RESERVED]),
            Repair.status.in_(["PLANNED", "PARTS_RESERVED", "IN_PROGRESS"]),
        )
        .group_by(RepairPart.required_part_type_id)
    )
    required_rows = (await session.execute(required_stmt)).all()

    stock_stmt = (
        select(Part.part_type_id, func.count(Part.id)).where(Part.status == "AVAILABLE").group_by(Part.part_type_id)
    )
    stock_rows = dict((await session.execute(stock_stmt)).all())

    out: list[PartsNeededLine] = []
    for part_type_id, required_qty in required_rows:
        part_type = await session.get(PartTypeConfig, part_type_id)
        if part_type is None:
            continue
        out.append(
            PartsNeededLine(
                part_type=part_type,
                required_qty=int(required_qty or 0),
                in_stock_qty=int(stock_rows.get(part_type_id, 0)),
            )
        )
    return out
