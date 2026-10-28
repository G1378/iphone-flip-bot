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
from app.models.repair_models import Repair, RepairPart, RepairPartLineStatus
from app.services import config_service, cosmetics
from app.services.events import log_event
from app.services.ids import next_internal_id


class PartStateError(ValueError):
    pass


class PartFitError(PartStateError):
    """The part is for a different phone model than the one it's being used on."""


def fitting_parts(parts: Sequence[Part], phone_model_id: Optional[int]) -> list[Part]:
    """Only the parts tagged for exactly this model, in the order given.
    Untagged parts and phones with no model match nothing (no 'universal' parts)."""
    return [p for p in parts if p.fits_model(phone_model_id)]


async def assert_part_fits(session: AsyncSession, part: Part, phone) -> None:
    """Hard block: raise PartFitError unless `part` is tagged for this phone's exact model."""
    model = await config_service.phone_model_for(session, phone)
    if not part.fits_model(model.id):
        raise PartFitError(
            f"{part.internal_id} is a {part.part_type.name} for **{part.model_label()}**, "
            f"but {phone.internal_id} is an **{model.model}**. Different models take different parts."
        )


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
    compatible_model_ids: Optional[list[int]] = None  # REQUIRED: every part is for specific models
    source_order_line_id: Optional[int] = None
    colour: Optional[str] = None  # frames / back glass: they must match each other


async def create_part(session: AsyncSession, data: CreatePartInput, actor_discord_id: int) -> Part:
    model_ids = list(dict.fromkeys(data.compatible_model_ids or []))
    if not model_ids:
        raise PartStateError(
            f"A {data.part_type_name} must be created for a specific phone model "
            "(e.g. 'Battery for iPhone 13 Pro'), not just 'Battery'.")
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
        colour=(data.colour or "").strip() or None,
        testing_status=data.testing_status,
        location_id=location.id if location else None,
        status="AVAILABLE",
        purchase_date=dt.date.today(),
        notes=data.notes,
    )
    session.add(part)
    await session.flush()

    for model_id in model_ids:
        session.add(PartCompatibleModel(part_id=part.id, phone_model_id=model_id))
    await session.flush()
    await session.refresh(part, ["compatible_model_links"])

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
    session: AsyncSession, part_type_id: int, phone_model_id: Optional[int],
) -> Sequence[Part]:
    """AVAILABLE parts of a type that fit exactly this phone model. A part for a
    different model (or one with no model set) is never returned: it can't be
    reserved or fitted, so it must not be offered."""
    stmt = (
        select(Part)
        .where(Part.part_type_id == part_type_id, Part.status == "AVAILABLE")
        .options(selectinload(Part.location), selectinload(Part.source_donor), selectinload(Part.compatible_model_links))
        .order_by(Part.created_at.asc(), Part.id.asc())
    )
    parts = (await session.execute(stmt)).scalars().all()
    return fitting_parts(parts, phone_model_id)


async def set_part_models(session: AsyncSession, part: Part, model_ids: Sequence[int], actor_discord_id: int) -> Part:
    """Correct which models a part is tagged for (e.g. it was ordered/entered for the wrong one).
    Not allowed while the part is reserved or fitted: that would silently break a live repair."""
    ids = list(dict.fromkeys(model_ids))
    if not ids:
        raise PartStateError("A part must be tagged with at least one phone model.")
    if part.status in ("RESERVED", "INSTALLED"):
        raise PartStateError(f"{part.internal_id} is {part.status}; release or remove it before changing its model.")
    before = part.model_label()
    for link in list(part.compatible_model_links):
        part.compatible_model_links.remove(link)
    await session.flush()
    for mid in ids:
        part.compatible_model_links.append(PartCompatibleModel(part_id=part.id, phone_model_id=mid))
    await session.flush()
    await session.refresh(part, ["compatible_model_links"])
    await log_event(
        session, event_type=InventoryEventType.MANUAL_ADJUSTMENT.value, entity_type="PART",
        entity_id=part.internal_id, actor_discord_id=actor_discord_id,
        previous_state={"models": before}, new_state={"models": part.model_label()},
    )
    return part


def _assert_colour(part: Part, phone, repair) -> None:
    try:
        cosmetics.check_colour(part, phone, repair)
    except cosmetics.ColourMismatchError as e:
        raise PartFitError(str(e))


async def set_part_colour(session: AsyncSession, part: Part, colour: str, actor_discord_id: int) -> Part:
    """Record a part's colour. Not allowed while it is reserved or fitted (it was matched on the old one)."""
    colour = (colour or "").strip()
    if not colour:
        raise PartStateError("Give the colour, e.g. 'Midnight'.")
    if part.status in ("RESERVED", "INSTALLED"):
        raise PartStateError(f"{part.internal_id} is {part.status}; release or remove it before changing its colour.")
    before, part.colour = part.colour, colour
    await session.flush()
    await log_event(
        session, event_type=InventoryEventType.MANUAL_ADJUSTMENT.value, entity_type="PART",
        entity_id=part.internal_id, actor_discord_id=actor_discord_id,
        previous_state={"colour": before}, new_state={"colour": colour},
    )
    return part


async def reserve_part(session: AsyncSession, part: Part, repair: Repair, repair_part: RepairPart,
                        actor_discord_id: int) -> Part:
    if part.status != "AVAILABLE":
        raise PartStateError(f"{part.internal_id} is not AVAILABLE (currently {part.status}).")
    await assert_part_fits(session, part, repair.phone)
    _assert_colour(part, repair.phone, repair)
    part.status = "RESERVED"
    part.reserved_for_repair_id = repair.id
    repair_part.part_id = part.id
    repair_part.part = part   # keep the relationship in step so colour checks see it straight away
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
    repair_part.part = None
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
    await assert_part_fits(session, part, phone)
    _assert_colour(part, phone, repair_part.repair)
    part.status = "INSTALLED"
    part.installed_phone_id = phone.id
    part.reserved_for_repair_id = None
    repair_part.part_id = part.id
    repair_part.part = part
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
    await cosmetics.on_part_installed(session, phone, part, repair_part, actor_discord_id)
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
    """Powers `/find-part screen 13 pro`: AVAILABLE parts whose type name matches
    and that are tagged for exactly that model. The model is required and must be
    a catalog model ('13' never returns '13 Pro' parts)."""
    from app.models.config_models import PartTypeConfig

    if not (model_term or "").strip():
        raise config_service.UnknownPhoneModelError("(no model given)")
    model = await config_service.resolve_phone_model(session, model_term)
    stmt = (
        select(Part)
        .join(PartTypeConfig, Part.part_type_id == PartTypeConfig.id)
        .options(
            selectinload(Part.part_type),
            selectinload(Part.location),
            selectinload(Part.source_donor),
            selectinload(Part.compatible_model_links),
        )
        .where(PartTypeConfig.name.ilike(f"%{part_type_term}%"), Part.status == "AVAILABLE")
        .order_by(Part.created_at.asc())
    )
    parts = (await session.execute(stmt)).scalars().all()
    return fitting_parts(parts, model.id)
