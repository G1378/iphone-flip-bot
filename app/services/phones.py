from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional, Sequence

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.enums import AcquisitionType, InventoryEventType
from app.models.inventory_models import Phone, PhoneFault
from app.services import config_service
from app.services.events import log_event
from app.services.ids import next_internal_id


class InvalidStatusError(ValueError):
    pass


@dataclass
class BuyPhoneInput:
    manufacturer: str
    model: str
    variant: Optional[str]
    storage: Optional[str]
    colour: Optional[str]
    imei: Optional[str]
    serial_number: Optional[str]
    carrier: Optional[str]
    purchase_price: Decimal
    purchase_date: dt.date
    seller_source: Optional[str]
    acquisition_type: AcquisitionType
    notes: Optional[str]
    location_code: Optional[str]


async def buy_phone(session: AsyncSession, data: BuyPhoneInput, actor_discord_id: int) -> Phone:
    await config_service.get_or_create_phone_model(session, data.manufacturer, data.model, data.variant)

    location = None
    if data.location_code:
        location = await config_service.get_location_by_code(session, data.location_code)

    internal_id = await next_internal_id(session, "phone")
    phone = Phone(
        internal_id=internal_id,
        manufacturer=data.manufacturer,
        model=data.model,
        variant=data.variant,
        storage=data.storage,
        colour=data.colour,
        imei=data.imei,
        serial_number=data.serial_number,
        carrier=data.carrier,
        purchase_price=data.purchase_price,
        purchase_date=data.purchase_date,
        seller_source=data.seller_source,
        acquisition_type=data.acquisition_type,
        current_status="PURCHASED",
        location_id=location.id if location else None,
        notes=data.notes,
    )
    session.add(phone)
    await session.flush()

    await log_event(
        session,
        event_type=InventoryEventType.PHONE_PURCHASED.value,
        entity_type="PHONE",
        entity_id=phone.internal_id,
        actor_discord_id=actor_discord_id,
        new_state={
            "model": phone.display_name(),
            "purchase_price": str(phone.purchase_price),
            "status": phone.current_status,
        },
        notes=f"Acquired via {data.acquisition_type.value}",
    )
    return phone


async def get_phone(session: AsyncSession, internal_id: str) -> Optional[Phone]:
    stmt = (
        select(Phone)
        .where(Phone.internal_id == internal_id.upper())
        .options(
            selectinload(Phone.faults),
            selectinload(Phone.test_results),
            selectinload(Phone.repairs),
            selectinload(Phone.location),
            selectinload(Phone.installed_parts),
        )
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def set_status(session: AsyncSession, phone: Phone, new_status: str, actor_discord_id: int,
                      notes: Optional[str] = None) -> Phone:
    if not await config_service.is_valid_status(session, "PHONE", new_status):
        valid = await config_service.status_codes(session, "PHONE")
        raise InvalidStatusError(f"'{new_status}' is not a configured phone status. Valid: {', '.join(valid)}")
    old_status = phone.current_status
    phone.current_status = new_status
    await session.flush()
    await log_event(
        session,
        event_type=InventoryEventType.PHONE_STATUS_CHANGED.value,
        entity_type="PHONE",
        entity_id=phone.internal_id,
        actor_discord_id=actor_discord_id,
        previous_state={"status": old_status},
        new_state={"status": new_status},
        notes=notes,
    )
    return phone


async def move_phone(session: AsyncSession, phone: Phone, location_code: str, actor_discord_id: int) -> Phone:
    location = await config_service.add_location(session, location_code)  # idempotent get-or-create-ish
    old = phone.location.code if phone.location else None
    phone.location_id = location.id
    await session.flush()
    await log_event(
        session,
        event_type=InventoryEventType.PHONE_MOVED.value,
        entity_type="PHONE",
        entity_id=phone.internal_id,
        actor_discord_id=actor_discord_id,
        previous_state={"location": old},
        new_state={"location": location.code},
    )
    return phone


async def edit_phone(session: AsyncSession, phone: Phone, actor_discord_id: int, **fields) -> Phone:
    before = {k: str(getattr(phone, k)) for k in fields}
    for k, v in fields.items():
        setattr(phone, k, v)
    await session.flush()
    await log_event(
        session,
        event_type=InventoryEventType.PHONE_UPDATED.value,
        entity_type="PHONE",
        entity_id=phone.internal_id,
        actor_discord_id=actor_discord_id,
        previous_state=before,
        new_state={k: str(v) for k, v in fields.items()},
    )
    return phone


async def add_fault(session: AsyncSession, phone: Phone, description: str, actor_discord_id: int,
                     required_part_type_id: Optional[int] = None) -> PhoneFault:
    fault = PhoneFault(phone_id=phone.id, description=description, required_part_type_id=required_part_type_id)
    session.add(fault)
    await session.flush()
    await log_event(
        session,
        event_type=InventoryEventType.PHONE_FAULT_LOGGED.value,
        entity_type="PHONE",
        entity_id=phone.internal_id,
        actor_discord_id=actor_discord_id,
        new_state={"fault": description},
    )
    return fault


async def list_stock(session: AsyncSession, status: Optional[str] = None,
                      acquisition_type: Optional[AcquisitionType] = None) -> Sequence[Phone]:
    stmt = select(Phone).options(selectinload(Phone.location)).order_by(Phone.created_at.desc())
    if status:
        stmt = stmt.where(Phone.current_status == status)
    if acquisition_type:
        stmt = stmt.where(Phone.acquisition_type == acquisition_type)
    return (await session.execute(stmt)).scalars().all()


async def search_phones(session: AsyncSession, term: str) -> Sequence[Phone]:
    like = f"%{term}%"
    stmt = (
        select(Phone)
        .options(selectinload(Phone.location))
        .where(
            or_(
                Phone.internal_id.ilike(like),
                Phone.imei.ilike(like),
                Phone.serial_number.ilike(like),
                Phone.model.ilike(like),
                Phone.variant.ilike(like),
                Phone.storage.ilike(like),
                Phone.colour.ilike(like),
                Phone.current_status.ilike(like),
            )
        )
        .order_by(Phone.created_at.desc())
        .limit(25)
    )
    return (await session.execute(stmt)).scalars().all()
