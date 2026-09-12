from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional, Sequence

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.enums import InventoryEventType, PartSourceType, TestOutcome
from app.models.inventory_models import Part, PartCompatibleModel, Phone
from app.models.config_models import PhoneModelConfig
from app.models.repair_models import Repair, RepairPart, RepairPartLineStatus
from app.services import config_service
from app.services.events import log_event
from app.services.ids import next_internal_id


class PartStateError(ValueError):
    pass


@dataclass
class CreatePartInput:
    part_type_name: str
    source_type: PartSourceType
    cost: Decimal
    condition: Optional[str] = None
    grade_code: Optional[str] = None
    testing_status: TestOutcome = TestOutcome.NOT_TESTED
    location_code: Optional[str] = None
    notes: Optional[str] = None
    compatible_model_ids: Optional[list[int]] = None
    source_order_line_id: Optional[int] = None


async def create_part(session: AsyncSession, data: CreatePartInput, actor_discord_id: int) -> Part:
    part_type = await config_service.get_or_create_part_type(session, data.part_type_name)
    location = None
    if data.location_code:
        location = await config_service.get_location_by_code(session, data.location_code)

    internal_id = await next_internal_id(session, "part")
    part = Part(
        internal_id=internal_id,
        part_type_id=part_type.id,
        source_type=data.source_type,
        source_order_line_id=data.source_order_line_id,
        cost=data.cost,
        condition=data.condition,
        grade_code=data.grade_code,
        testing_status=data.testing_status,
        location_id=location.id if location else None,
        status="AVAILABLE",
        purchase_date=dt.date.today(),
        notes=data.notes,
    )
    session.add(part)
    await session.flush()

    for model_id in data.compatible_model_ids or []:
        session.add(PartCompatibleModel(part_id=part.id, phone_model_id=model_id))
    await session.flush()

    await log_event(
        session,
        event_type=InventoryEventType.PART_CREATED.value,
        entity_type="PART",
        entity_id=part.internal_id,
        actor_discord_id=actor_discord_id,
        new_state={"part_type": part_type.name, "source": data.source_type.value, "cost": str(data.cost)},
    )
    return part


async def get_part(session: AsyncSession, internal_id: str) -> Optional[Part]:
    stmt = (
        select(Part)
        .where(Part.internal_id == internal_id.upper())
        .options(selectinload(Part.part_type), selectinload(Part.location), selectinload(Part.source_donor))
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def find_compatible_available_parts(
    session: AsyncSession, part_type_id: int, phone_model_id: Optional[int] = None,
) -> Sequence[Part]:
    """Parts matching a required type that are AVAILABLE (not reserved/installed).

    If phone_model_id is given, prefers parts explicitly marked compatible
    with that model, but MVP falls back to *all* AVAILABLE parts of that
    type if no explicit compatibility rows exist (many small operations
    won't bother tagging compatibility for every part) - callers should
    treat the phone_model filter as a ranking hint, not a hard filter,
    which is why this returns all AVAILABLE parts of the type ordered with
    compatible ones first.
    """
    stmt = (
        select(Part)
        .where(Part.part_type_id == part_type_id, Part.status == "AVAILABLE")
        .options(selectinload(Part.location), selectinload(Part.source_donor), selectinload(Part.compatible_model_links))
        .order_by(Part.testing_status.desc(), Part.created_at.asc())
    )
    parts = (await session.execute(stmt)).scalars().all()
    if phone_model_id is None:
        return parts

    def is_compatible(p: Part) -> bool:
        if not p.compatible_model_links:
            return True  # untagged = assumed universally compatible for this part type (MVP)
        return any(link.phone_model_id == phone_model_id for link in p.compatible_model_links)

    compatible = [p for p in parts if is_compatible(p)]
    incompatible = [p for p in parts if not is_compatible(p)]
    return compatible + incompatible


async def reserve_part(session: AsyncSession, part: Part, repair: Repair, repair_part: RepairPart,
                        actor_discord_id: int) -> Part:
    if part.status != "AVAILABLE":
        raise PartStateError(f"{part.internal_id} is not AVAILABLE (currently {part.status}).")
    part.status = "RESERVED"
    part.reserved_for_repair_id = repair.id
    repair_part.part_id = part.id
    repair_part.status = RepairPartLineStatus.RESERVED
    repair_part.reserved_at = dt.datetime.now(dt.timezone.utc)
    await session.flush()

    await log_event(
        session,
        event_type=InventoryEventType.PART_RESERVED.value,
        entity_type="PART",
        entity_id=part.internal_id,
        actor_discord_id=actor_discord_id,
        related_entity_type="REPAIR",
        related_entity_id=repair.internal_id,
        previous_state={"status": "AVAILABLE"},
        new_state={"status": "RESERVED"},
    )
    return part


async def unreserve_part(session: AsyncSession, part: Part, repair_part: RepairPart, actor_discord_id: int) -> Part:
    if part.status != "RESERVED":
        raise PartStateError(f"{part.internal_id} is not RESERVED (currently {part.status}).")
    part.status = "AVAILABLE"
    part.reserved_for_repair_id = None
    repair_part.part_id = None
    repair_part.status = RepairPartLineStatus.REQUIRED
    repair_part.reserved_at = None
    await session.flush()

    await log_event(
        session,
        event_type=InventoryEventType.PART_UNRESERVED.value,
        entity_type="PART",
        entity_id=part.internal_id,
        actor_discord_id=actor_discord_id,
        previous_state={"status": "RESERVED"},
        new_state={"status": "AVAILABLE"},
    )
    return part


async def install_part(session: AsyncSession, part: Part, phone: Phone, repair_part: RepairPart,
                        actor_discord_id: int) -> Part:
    if part.status not in ("RESERVED", "AVAILABLE"):
        raise PartStateError(f"{part.internal_id} cannot be installed from status {part.status}.")
    part.status = "INSTALLED"
    part.installed_phone_id = phone.id
    part.reserved_for_repair_id = None
    repair_part.status = RepairPartLineStatus.INSTALLED
    repair_part.installed_at = dt.datetime.now(dt.timezone.utc)
    repair_part.cost_at_installation = part.cost
    await session.flush()

    await log_event(
        session,
        event_type=InventoryEventType.PART_INSTALLED.value,
        entity_type="PART",
        entity_id=part.internal_id,
        actor_discord_id=actor_discord_id,
        related_entity_type="PHONE",
        related_entity_id=phone.internal_id,
        new_state={"status": "INSTALLED", "cost_at_installation": str(part.cost)},
    )
    return part


async def remove_part(session: AsyncSession, part: Part, repair_part: Optional[RepairPart],
                       actor_discord_id: int, new_status: str = "AVAILABLE", notes: Optional[str] = None) -> Part:
    if part.status != "INSTALLED":
        raise PartStateError(f"{part.internal_id} is not INSTALLED (currently {part.status}).")
    old_phone_id = part.installed_phone_id
    part.status = new_status
    part.installed_phone_id = None
    if repair_part:
        repair_part.status = RepairPartLineStatus.REMOVED
        repair_part.removed_at = dt.datetime.now(dt.timezone.utc)
    await session.flush()

    await log_event(
        session,
        event_type=InventoryEventType.PART_REMOVED.value,
        entity_type="PART",
        entity_id=part.internal_id,
        actor_discord_id=actor_discord_id,
        previous_state={"status": "INSTALLED", "installed_phone_id": old_phone_id},
        new_state={"status": new_status},
        notes=notes,
    )
    return part


async def scrap_part(session: AsyncSession, part: Part, actor_discord_id: int, notes: Optional[str] = None) -> Part:
    old_status = part.status
    part.status = "SCRAPPED"
    part.installed_phone_id = None
    part.reserved_for_repair_id = None
    await session.flush()

    await log_event(
        session,
        event_type=InventoryEventType.PART_SCRAPPED.value,
        entity_type="PART",
        entity_id=part.internal_id,
        actor_discord_id=actor_discord_id,
        previous_state={"status": old_status},
        new_state={"status": "SCRAPPED"},
        notes=notes,
    )
    return part


async def move_part(session: AsyncSession, part: Part, location_code: str, actor_discord_id: int) -> Part:
    location = await config_service.add_location(session, location_code)
    old = part.location.code if part.location else None
    part.location_id = location.id
    await session.flush()

    await log_event(
        session,
        event_type=InventoryEventType.PART_MOVED.value,
        entity_type="PART",
        entity_id=part.internal_id,
        actor_discord_id=actor_discord_id,
        previous_state={"location": old},
        new_state={"location": location.code},
    )
    return part


async def search_parts(session: AsyncSession, term: str) -> Sequence[Part]:
    like = f"%{term}%"
    stmt = (
        select(Part)
        .join(Part.part_type)
        .options(selectinload(Part.part_type), selectinload(Part.location))
        .where(
            or_(
                Part.internal_id.ilike(like),
                Part.status.ilike(like),
                Part.condition.ilike(like),
            )
        )
        .order_by(Part.created_at.desc())
        .limit(25)
    )
    return (await session.execute(stmt)).scalars().all()


async def find_part_by_type_and_model(session: AsyncSession, part_type_term: str, model_term: str) -> Sequence[Part]:
    """Powers `/find-part screen 13 pro` - fuzzy match on part type name and,
    if the part has tagged compatibility, on phone model text. Parts with no
    compatibility tags are treated as matching any model (MVP default)."""
    from app.models.config_models import PartTypeConfig

    like_type = f"%{part_type_term}%"
    stmt = (
        select(Part)
        .join(PartTypeConfig, Part.part_type_id == PartTypeConfig.id)
        .options(
            selectinload(Part.part_type),
            selectinload(Part.location),
            selectinload(Part.source_donor),
            selectinload(Part.compatible_model_links),
        )
        .where(PartTypeConfig.name.ilike(like_type), Part.status == "AVAILABLE")
        .order_by(Part.created_at.asc())
    )
    parts = (await session.execute(stmt)).scalars().all()
    if not model_term:
        return parts

    needle = model_term.lower()
    results: list[Part] = []
    for p in parts:
        if not p.compatible_model_links:
            results.append(p)
            continue
        for link in p.compatible_model_links:
            model = await session.get(PhoneModelConfig, link.phone_model_id)
            if model and needle in f"{model.display_name()} {model.variant or ''}".lower():
                results.append(p)
                break
    return results
