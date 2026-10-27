from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional, Sequence

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.config_models import PartTypeConfig, PhoneModelConfig
from app.models.enums import InventoryEventType, PartSourceType, RepairPartLineStatus
from app.models.inventory_models import Part, PartCompatibleModel
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
    phone_model_id: Optional[int] = None  # REQUIRED: "Battery" alone is not an orderable part
    notes: Optional[str] = None
    colour: Optional[str] = None          # frames / back glass: what colour is being ordered


async def create_order(
    session: AsyncSession, supplier: Optional[str], lines: list[OrderLineInput], actor_discord_id: int,
    shipping_cost: Decimal = Decimal("0"), tax: Decimal = Decimal("0"),
    expected_delivery: Optional[dt.date] = None, notes: Optional[str] = None,
) -> Order:
    if not lines:
        raise OrderError("An order needs at least one line.")
    for line in lines:
        if line.phone_model_id is None:
            name = (await session.get(PartTypeConfig, line.part_type_id)).name
            raise OrderError(
                f"{name} needs a phone model (e.g. {name} for iPhone 13 Pro). "
                "Parts differ between base, Pro and Pro Max models, so every order line is for one exact model.")
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
                colour=(line.colour or "").strip() or None,
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
        .options(selectinload(Order.lines).selectinload(OrderLine.part_type),
                 selectinload(Order.lines).selectinload(OrderLine.phone_model))
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

    missing = [l.part_type.name for l in order.lines if l.phone_model_id is None]
    if missing:
        raise OrderError(f"{order.internal_id} has lines with no phone model ({', '.join(missing)}). "
                         "Parts must be received against an exact model; recreate the order with the model set.")

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
                colour=line.colour,
                testing_status="NOT_TESTED",
                status="AVAILABLE",
                purchase_date=dt.date.today(),
            )
            session.add(part)
            await session.flush()
            session.add(PartCompatibleModel(part_id=part.id, phone_model_id=line.phone_model_id))
            await session.flush()
            await session.refresh(part, ["compatible_model_links"])
            created.append(part)
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
    phone_model: Optional[PhoneModelConfig]
    required_qty: int
    in_stock_qty: int

    @property
    def to_order_qty(self) -> int:
        return max(0, self.required_qty - self.in_stock_qty)

    @property
    def label(self) -> str:
        """'Battery (iPhone 13 Pro)': never just 'Battery'."""
        return f"{self.part_type.name} ({self.phone_model.model if self.phone_model else 'model not set'})"


async def parts_needed(session: AsyncSession) -> list[PartsNeededLine]:
    """Outstanding REQUIRED/RESERVED repair-part lines, grouped by part type AND
    the phone model they are for, against AVAILABLE stock tagged for that model.
    (A part tagged for several models counts toward each of them.)"""
    rows = (await session.execute(
        select(RepairPart, Repair)
        .join(Repair, RepairPart.repair_id == Repair.id)
        .where(
            RepairPart.status.in_([RepairPartLineStatus.REQUIRED, RepairPartLineStatus.RESERVED]),
            Repair.status.in_(["PLANNED", "PARTS_RESERVED", "IN_PROGRESS"]),
        )
    )).all()

    required: dict[tuple[int, Optional[int]], int] = {}
    for rp, repair in rows:
        key = (rp.required_part_type_id, repair.phone.phone_model_id)
        required[key] = required.get(key, 0) + (rp.quantity or 0)

    stock_parts = (await session.execute(select(Part).where(Part.status == "AVAILABLE"))).scalars().all()

    out: list[PartsNeededLine] = []
    for (part_type_id, model_id), qty in required.items():
        part_type = await session.get(PartTypeConfig, part_type_id)
        if part_type is None:
            continue
        in_stock = sum(1 for p in stock_parts if p.part_type_id == part_type_id and p.fits_model(model_id))
        out.append(PartsNeededLine(
            part_type=part_type,
            phone_model=await session.get(PhoneModelConfig, model_id) if model_id else None,
            required_qty=int(qty), in_stock_qty=in_stock,
        ))
    out.sort(key=lambda l: (l.phone_model.model if l.phone_model else "", l.part_type.name))
    return out
