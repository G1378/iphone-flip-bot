"""Visual condition (frame + back glass), battery health capture and colour matching.

Grades, recorded separately for the FRAME (the "Housing" part type) and the BACK GLASS so a
damaged frame and a damaged back glass are never confused:

    A*  like new                      \\
    A   excellent                      |  sell as is
    B   good                          /
    C   heavy wear                    sellable as it is. If a matching replacement is IN STOCK it
                                      is reserved for the phone ("marked replace"). Nothing is ever
                                      ordered for a C: no stock means it is simply sold as a C.
    D   must be replaced before sale  raises a fault + a required repair line, exactly like a failed
                                      test, so a missing part shows up as "needs ordering".

Colour: a replacement frame and back glass have to match EACH OTHER. They do not have to match the
phone's original colour: fitting both in a new colour is fine (the phone's colour then changes).
Fitting just one has to match the one that stays on the phone.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import FaultStatus, InventoryEventType, PartSourceType, RepairPartLineStatus
from app.models.inventory_models import Part, Phone, PhoneFault
from app.models.repair_models import Repair, RepairPart
from app.services import config_service
from app.services import phones as phones_service
from app.services.events import log_event

GRADES = ("A*", "A", "B", "C", "D")

# Battery health under this % counts as a failed battery test (the Battery part is needed),
# unless the tester overrides it. 80 itself passes.
BATTERY_MIN_HEALTH = 80

# Shown on listings and in Discord. Edit the wording here to change it everywhere.
GRADE_DESCRIPTIONS = {
    "A*": "like new, no visible marks",
    "A": "excellent, only very light signs of use",
    "B": "good, light scratches or scuffs",
    "C": "fair, noticeable wear such as scratches, scuffs or small dents",
    "D": "poor, heavy damage (cracked glass or bent frame)",
}

FRAME, GLASS = "frame", "back glass"
ROLE_LABEL = {FRAME: "Frame", GLASS: "Back glass"}
ROLE_ATTR = {FRAME: "frame_grade", GLASS: "back_glass_grade"}
DEFAULT_PART_TYPE = {FRAME: "Housing", GLASS: "Back Glass"}
_OTHER = {FRAME: GLASS, GLASS: FRAME}

_CLOSED_STATUSES = ("SOLD", "SHIPPED", "COMPLETE", "SCRAPPED")
_WORKING_STATUSES = ("PURCHASED", "AWAITING_TEST", "TESTED", "READY_FOR_LISTING", "LISTED")
_ACTIVE_LINES = (RepairPartLineStatus.REQUIRED, RepairPartLineStatus.RESERVED)


class CosmeticError(ValueError):
    pass


class ColourMismatchError(CosmeticError):
    pass


# ---------------------------------------------------------------- pure helpers
def normalise_grade(text: Optional[str]) -> Optional[str]:
    """'a*', 'A STAR', ' b ' -> 'A*', 'A*', 'B'. None if it isn't a grade."""
    t = (text or "").strip().upper().replace("STAR", "*").replace(" ", "")
    if t.startswith("GRADE"):
        t = t[5:]
    return t if t in GRADES else None


def part_role(part_type_name: Optional[str]) -> Optional[str]:
    """Which cosmetic role a part type plays: 'frame', 'back glass' or None (not cosmetic)."""
    n = (part_type_name or "").lower()
    if "back glass" in n or "backglass" in n or "rear glass" in n:
        return GLASS
    if "housing" in n or "frame" in n:
        return FRAME
    return None


def norm_colour(colour: Optional[str]) -> Optional[str]:
    c = " ".join((colour or "").lower().replace("-", " ").split())
    return c.replace("grey", "gray") or None


def colours_match(a: Optional[str], b: Optional[str]) -> bool:
    na, nb = norm_colour(a), norm_colour(b)
    return na is not None and na == nb


def worst_grade(*grades: Optional[str]) -> Optional[str]:
    known = [g for g in grades if g in GRADES]
    return max(known, key=GRADES.index) if known else None


def overall_grade(phone: Phone) -> Optional[str]:
    """The phone's headline grade: the worse of frame and back glass (needs both recorded)."""
    if phone.frame_grade in GRADES and phone.back_glass_grade in GRADES:
        return worst_grade(phone.frame_grade, phone.back_glass_grade)
    return None


def grade_of(phone: Phone, role: str) -> Optional[str]:
    return getattr(phone, ROLE_ATTR[role])


def missing_condition(phone: Phone) -> list[str]:
    """What still has to be recorded during testing."""
    out = []
    if phone.frame_grade not in GRADES:
        out.append("frame grade")
    if phone.back_glass_grade not in GRADES:
        out.append("back glass grade")
    if phone.battery_health is None and not phone.battery_health_na:
        out.append("battery health")
    return out


def condition_complete(phone: Phone) -> bool:
    return not missing_condition(phone)


def needs_replacing(phone: Phone) -> list[str]:
    """Roles graded D: not sellable until replaced."""
    return [r for r in (FRAME, GLASS) if grade_of(phone, r) == "D"]


def condition_lines(phone: Phone) -> list[str]:
    """Plain-text condition lines for a listing description."""
    out = []
    overall = overall_grade(phone)
    if overall:
        out.append(f"Cosmetic grade: {overall}")
    for role in (FRAME, GLASS):
        g = grade_of(phone, role)
        if g in GRADES:
            out.append(f"{ROLE_LABEL[role]}: grade {g} ({GRADE_DESCRIPTIONS[g]})")
    return out


def counterpart_colour(phone: Phone, repair: Optional[Repair], role: str) -> Optional[str]:
    """The colour a part for `role` has to match right now, or None if nothing constrains it.

    * the other part is already chosen/fitted in this repair -> match that part
    * the other part is also being replaced but isn't chosen yet -> free (checked when it is chosen)
    * the other part stays on the phone -> match the phone's current colour"""
    other = _OTHER[role]
    if repair is not None:
        for rp in repair.parts:
            if rp.status == RepairPartLineStatus.CANCELLED or part_role(rp.required_part_type.name) != other:
                continue
            if rp.part is not None and rp.status in (RepairPartLineStatus.RESERVED, RepairPartLineStatus.INSTALLED):
                return rp.part.colour
            if rp.status in _ACTIVE_LINES:
                return None
    return phone.colour or None


def check_colour(part: Part, phone: Phone, repair: Optional[Repair]) -> None:
    """Raise ColourMismatchError if `part` is a frame/back glass that can't go on this phone."""
    role = part_role(part.part_type.name if part.part_type else None)
    if role is None:
        return
    if not norm_colour(part.colour):
        raise ColourMismatchError(
            f"{part.internal_id} is a {part.part_type.name} with no colour recorded, so it can't be checked "
            f"against the phone. Set it with `/part-colour {part.internal_id} <colour>`.")
    want = counterpart_colour(phone, repair, role)
    if norm_colour(want) and not colours_match(part.colour, want):
        other = ROLE_LABEL[_OTHER[role]].lower()
        raise ColourMismatchError(
            f"{part.internal_id} is {part.colour}, but the {other} on {phone.internal_id} is {want}. "
            f"The frame and back glass have to match each other.")


def fits_colour(part: Part, want: Optional[str]) -> bool:
    """Stock filter: can this part be used where `want` is the colour to match (None = free)?"""
    role = part_role(part.part_type.name if part.part_type else None)
    if role is None:
        return True
    if not norm_colour(part.colour):
        return False
    return not norm_colour(want) or colours_match(part.colour, want)


def pick_pair_colour(phone_colour: Optional[str], frames: list[Part], glasses: list[Part]) -> Optional[str]:
    """Both frame and back glass are being replaced: the colour that stock covers best.

    A colour with both in stock wins; the phone's own colour wins ties, then the oldest stock."""
    seen: list[str] = []
    for p in list(frames) + list(glasses):
        c = norm_colour(p.colour)
        if c and c not in seen:
            seen.append(c)
    if not seen:
        return None

    def score(c: str):
        has_f = any(norm_colour(p.colour) == c for p in frames)
        has_g = any(norm_colour(p.colour) == c for p in glasses)
        return (has_f + has_g, c == norm_colour(phone_colour), -seen.index(c))

    best = max(seen, key=score)
    for p in list(frames) + list(glasses):
        if norm_colour(p.colour) == best:
            return p.colour
    return best


# ---------------------------------------------------------------- battery
async def set_battery(session: AsyncSession, phone: Phone, actor: int, *,
                      percent: Optional[int] = None, not_applicable: bool = False) -> Phone:
    if not_applicable:
        return await phones_service.edit_phone(session, phone, actor, battery_health=None, battery_health_na=True)
    if percent is None or not 0 <= int(percent) <= 100:
        raise CosmeticError("Battery health must be a whole number between 0 and 100.")
    return await phones_service.edit_phone(session, phone, actor, battery_health=int(percent), battery_health_na=False)


# ---------------------------------------------------------------- grading
@dataclass
class GradeOutcome:
    role: str
    previous: Optional[str]
    grade: str
    fault_raised: bool = False
    fault_cleared: bool = False
    reserved: Optional[Part] = None       # C: a matching part was in stock and is now marked for replacement
    released: Optional[Part] = None       # the replacement was dropped (graded back up / kept as is)
    no_stock: bool = False                # C: nothing suitable in stock, sold as is, NOT ordered
    repair_id: Optional[str] = None
    new_status: Optional[str] = None
    was_listed: bool = False
    notes: list[str] = field(default_factory=list)

    def summary(self) -> str:
        label = ROLE_LABEL[self.role]
        bits = [f"**{label}** grade **{self.grade}**"]
        if self.fault_raised:
            bits.append("must be replaced before sale: added to the repair")
        if self.fault_cleared:
            bits.append("replacement no longer required")
        if self.reserved:
            bits.append(f"marked replace: `{self.reserved.internal_id}` reserved from stock")
        if self.released:
            bits.append(f"`{self.released.internal_id}` released back to stock")
        if self.no_stock:
            bits.append("no matching one in stock: sells as a C, nothing ordered")
        return " - ".join(bits)


async def part_type_for_role(session: AsyncSession, role: str):
    from app.models.config_models import PartTypeConfig
    rows = (await session.execute(select(PartTypeConfig).where(PartTypeConfig.active.is_(True)).order_by(PartTypeConfig.id))).scalars().all()
    for pt in rows:
        if part_role(pt.name) == role:
            return pt
    return await config_service.get_or_create_part_type(session, DEFAULT_PART_TYPE[role])


def _grade_fault_prefix(role: str) -> str:
    return f"{ROLE_LABEL[role]} graded D"


async def _open_grade_fault(session: AsyncSession, phone: Phone, role: str, part_type_id: int) -> Optional[PhoneFault]:
    rows = (await session.execute(select(PhoneFault).where(
        PhoneFault.phone_id == phone.id, PhoneFault.required_part_type_id == part_type_id,
        PhoneFault.status == FaultStatus.OPEN))).scalars().all()
    return next((f for f in rows if f.description.startswith(_grade_fault_prefix(role))), None)


async def _has_other_open_fault(session: AsyncSession, phone: Phone, part_type_id: int, ignore_id: Optional[int]) -> bool:
    rows = (await session.execute(select(PhoneFault).where(
        PhoneFault.phone_id == phone.id, PhoneFault.required_part_type_id == part_type_id,
        PhoneFault.status == FaultStatus.OPEN))).scalars().all()
    return any(f.id != ignore_id for f in rows)


async def _drop_optional_line(session: AsyncSession, phone: Phone, part_type_id: int, actor: int) -> Optional[Part]:
    """Cancel this phone's not-required replacement line for a part type (and free its part).
    A line that an open fault still needs is left alone."""
    from app.services import parts as parts_service
    from app.services import repairs as repairs_service
    if await _has_other_open_fault(session, phone, part_type_id, None):
        return None
    repair = await repairs_service.get_active_repair(session, phone)
    if repair is None:
        return None
    freed: Optional[Part] = None
    for rp in repair.parts:
        if rp.required_part_type_id != part_type_id or rp.status not in _ACTIVE_LINES:
            continue
        if rp.status == RepairPartLineStatus.RESERVED and rp.part_id:
            part = await session.get(Part, rp.part_id)
            if part is not None and part.status == "RESERVED":
                await parts_service.unreserve_part(session, part, rp, actor)
                freed = part
        rp.status = RepairPartLineStatus.CANCELLED
    await session.flush()
    await _cancel_repair_if_empty(session, phone, repair, actor)
    return freed


async def _cancel_repair_if_empty(session: AsyncSession, phone: Phone, repair: Repair, actor: int) -> None:
    from app.services import repairs as repairs_service
    live = [rp for rp in repair.parts if rp.status in _ACTIVE_LINES or rp.status == RepairPartLineStatus.INSTALLED]
    open_faults = (await session.execute(select(PhoneFault).where(
        PhoneFault.phone_id == phone.id, PhoneFault.status == FaultStatus.OPEN))).scalars().all()
    if not live and not open_faults and repair.status in ("PLANNED", "PARTS_RESERVED"):
        await repairs_service.cancel_repair(session, repair, actor, notes="Nothing left to repair")


cancel_repair_if_empty = _cancel_repair_if_empty


async def _mark_replace_from_stock(session: AsyncSession, phone: Phone, role: str, part_type, grade: str,
                                   actor: int, out: GradeOutcome) -> None:
    """Grade C: if a matching part is in stock, reserve it for this phone. Never orders."""
    from app.services import parts as parts_service
    from app.services import repairs as repairs_service
    repair = await repairs_service.get_active_repair(session, phone)
    if repair and any(rp.required_part_type_id == part_type.id and rp.status in _ACTIVE_LINES for rp in repair.parts):
        return  # already on the repair (reserved earlier, or needed for a fault)
    stock = await parts_service.find_compatible_available_parts(session, part_type.id, phone.phone_model_id)
    want = counterpart_colour(phone, repair, role)
    here = GRADES.index(grade)

    def better(p: Part) -> bool:  # a replacement is only worth fitting if it beats what's there
        return p.grade_code not in GRADES or GRADES.index(p.grade_code) < here

    candidates = [p for p in stock if fits_colour(p, want) and better(p)]
    candidates.sort(key=lambda p: GRADES.index(p.grade_code) if p.grade_code in GRADES else len(GRADES))
    if not candidates:
        out.no_stock = True
        return
    if repair is None:
        repair = await repairs_service.create_repair_for_open_faults(session, phone, actor)
    line = RepairPart(required_part_type_id=part_type.id, quantity=1, status=RepairPartLineStatus.REQUIRED)
    repair.parts.append(line)
    await session.flush()
    try:
        await parts_service.reserve_part(session, candidates[0], repair, line, actor)
    except Exception:
        line.status = RepairPartLineStatus.CANCELLED
        await session.flush()
        out.no_stock = True
        return
    out.reserved = candidates[0]
    out.repair_id = repair.internal_id


async def set_grade(session: AsyncSession, phone: Phone, role: str, grade: str, actor: int) -> GradeOutcome:
    """Record the frame / back glass grade and act on it (see module docstring)."""
    from app.services import repairs as repairs_service
    grade = normalise_grade(grade)
    if role not in ROLE_ATTR or grade is None:
        raise CosmeticError(f"Grade must be one of {', '.join(GRADES)}.")
    previous = grade_of(phone, role)
    out = GradeOutcome(role=role, previous=previous, grade=grade, new_status=phone.current_status)

    await phones_service.edit_phone(session, phone, actor, **{ROLE_ATTR[role]: grade})
    overall = overall_grade(phone)
    if overall:
        await phones_service.edit_phone(session, phone, actor, cosmetic_condition=f"Grade {overall}")

    if phone.current_status in _CLOSED_STATUSES:
        return out  # recorded, but a sold/scrapped phone isn't reopened

    part_type = await part_type_for_role(session, role)
    grade_fault = await _open_grade_fault(session, phone, role, part_type.id)

    if grade == "D":
        # Must be replaced before sale: same path as a failed test.
        if grade_fault is None:
            grade_fault = await phones_service.add_fault(
                session, phone, f"{_grade_fault_prefix(role)}: replace before sale", actor,
                required_part_type_id=part_type.id)
            out.fault_raised = True
        if phone.current_status in _WORKING_STATUSES:
            out.was_listed = phone.current_status == "LISTED"
            await phones_service.set_status(session, phone, "REPAIR_REQUIRED", actor,
                                            notes=f"{ROLE_LABEL[role]} graded D")
            out.new_status = phone.current_status
        repair = await repairs_service.sync_repair_with_open_faults(session, phone, actor)
        out.repair_id = repair.internal_id
        return out

    if grade_fault is not None:   # was D, no longer
        grade_fault.status = FaultStatus.RESOLVED
        grade_fault.resolved_at = dt.datetime.now(dt.timezone.utc)
        await session.flush()
        await repairs_service.release_fault_from_repair(session, grade_fault)
        out.fault_cleared = True

    if grade == "C":
        await _mark_replace_from_stock(session, phone, role, part_type, grade, actor, out)
    else:   # A*, A, B: sells as is, so any optional replacement is let go
        out.released = await _drop_optional_line(session, phone, part_type.id, actor)

    if out.fault_cleared:
        from app.services import testing as testing_service
        await testing_service.settle_after_fault_cleared(session, phone, actor, "Grade no longer D")
        out.new_status = phone.current_status
    return out


async def waive_replacement(session: AsyncSession, phone: Phone, role: str, actor: int) -> Optional[Part]:
    """Sell as graded: drop the optional (grade C) replacement and free the reserved part.
    Refuses for a D, which has to be replaced."""
    if grade_of(phone, role) == "D":
        raise CosmeticError(f"The {ROLE_LABEL[role].lower()} is graded D: it has to be replaced before sale. "
                            "Re-grade it first if that's wrong.")
    part_type = await part_type_for_role(session, role)
    return await _drop_optional_line(session, phone, part_type.id, actor)


# ---------------------------------------------------------------- after a part is fitted
async def on_part_installed(session: AsyncSession, phone: Phone, part: Part, repair_part: RepairPart,
                            actor: int) -> None:
    """A frame / back glass went in: its grade becomes the phone's grade for that part, and when
    both were replaced the phone takes the new (matching) colour."""
    role = part_role(part.part_type.name if part.part_type else None)
    if role is None:
        return
    if part.grade_code in GRADES:
        new_grade: Optional[str] = part.grade_code
    elif part.source_type == PartSourceType.PURCHASED:
        new_grade = "A*"          # bought in new
    else:
        new_grade = None          # used part with no grade: has to be graded again
    fields: dict = {ROLE_ATTR[role]: new_grade}
    repair = repair_part.repair
    installed_roles = {part_role(rp.required_part_type.name) for rp in repair.parts
                       if rp.status == RepairPartLineStatus.INSTALLED}
    if {FRAME, GLASS} <= installed_roles and part.colour:
        fields["colour"] = part.colour
    await phones_service.edit_phone(session, phone, actor, **fields)
    overall = overall_grade(phone)
    await phones_service.edit_phone(session, phone, actor, cosmetic_condition=f"Grade {overall}" if overall else None)
    await log_event(
        session, event_type=InventoryEventType.PHONE_UPDATED.value, entity_type="PHONE",
        entity_id=phone.internal_id, actor_discord_id=actor,
        new_state={ROLE_ATTR[role]: new_grade}, notes=f"{part.internal_id} fitted")
