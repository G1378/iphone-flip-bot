"""What will it cost to fix this phone? Read the listing, name the parts, price them.

The deal scanner used to deduct ONE flat figure (`default_repair_cost_assumption`) from every faulty
phone. That is wrong in both directions: a battery swap is a £20 job and a screen is £55+, and some
faults (water damage, no power, Face ID) are not a priced repair at all - they are a gamble.

So for every listing that is faulty:

  1. `scan_faults` reads the title (and the description where we have it) and says which part types
     it needs, or that the fault is "board level" and can't be priced.
  2. `estimate_repair` prices each part, in this order of trust:
        a. what YOU actually paid for that part for exactly this model (Part rows you bought),
        b. the `repair_part_cost_defaults` business setting,
        c. the built-in defaults below (placeholders: tune them with /config business).
     then adds your contingency % and per-repair labour.
  3. If the listing just says "faulty / spares or repair / untested" with no fault named, we fall
     back to the old flat assumption, and say so on the card.

Everything here is text matching, which is fallible; the card always shows its workings so a person
makes the final call. Where the matching is unsure it errs towards LOWER profit (more repair, or a
skip), never higher.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from typing import Optional

from sqlalchemy import exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.config_models import PartTypeConfig, PhoneModelConfig
from app.models.enums import PartSourceType
from app.models.inventory_models import Part, PartCompatibleModel
from app.services import config_service

TWO = Decimal("0.01")

# Placeholders (GBP, parts + postage). They only apply until you have bought that part for that
# model yourself, or set your own with `/config business repair_part_cost_defaults ...`.
BUILTIN_PART_COSTS: dict[str, Decimal] = {
    "screen": Decimal("55"),
    "battery": Decimal("20"),
    "back glass": Decimal("30"),
    "charging port / flex": Decimal("20"),
    "rear camera": Decimal("35"),
    "front camera": Decimal("20"),
    "speaker": Decimal("15"),
    "housing": Decimal("35"),
    "home button": Decimal("20"),
}

BATTERY_SERVICE_BELOW = 80   # battery health % under which a battery is treated as a needed repair

# ----------------------------------------------------------------------------------- text rules
_BAD = (r"(?:crack(?:ed|s)?|smash(?:ed)?|shatter(?:ed)?|broke(?:n)?|damag(?:e|ed)|faulty|fault|"
        r"flicker(?:s|ing)?|lines?|burn(?:t|ed)?|unresponsive|ghost(?:ing)?|bleed(?:ing)?|"
        r"not\s+working|stopped\s+working|doesn'?t\s+work|won'?t\s+work|issues?|problems?|"
        r"dead\s+pixels?|spots?|chipped)")
_SEP = r"[ \t:\-/]+"                    # spaces/colon/dash only: a comma or full stop ends the thought
_GAP = rf"(?:{_SEP}[\w']+){{0,3}}?{_SEP}"   # up to three words between a part and its fault

# "no cracks", "never been repaired", "not damaged", "no issues": the seller saying it is FINE.
_NEGATED_FAULT = re.compile(
    r"\b(?:no|not|never|without|zero|isn'?t|hasn'?t|haven'?t|nothing)\b"
    # up to two filler words, but never an accessory noun: "no charger, cracked screen" is NOT a denial
    r"(?:\s+(?!(?:charger|box|case|cable|sim|accessories|receipt|warranty|returns?)\b)\w+){0,2}?\s+"
    r"(?:fault\w*|crack\w*|smash\w*|shatter\w*|damag\w*|broke\w*|repair\w*|dead|issue\w*|problem\w*|"
    r"scratch\w*|chip\w*|flicker\w*|burn\w*|marks?)\b")
_NOISE = re.compile(
    rf"\b(?:{_BAD}\s+(?:(?:screen|glass)\s+)?protectors?|(?:screen|glass)\s+protectors?\s+{_BAD}|"
    rf"tempered\s+glass\s+{_BAD}|{_BAD}\s+tempered\s+glass|(?:case|cover)\s+{_BAD}|{_BAD}\s+(?:case|cover))\b"
    r"|\b(?:screen\s+protectors?|tempered\s+glass|glass\s+protectors?)\b")

_SCREEN = re.compile(rf"\b(?:screen|display|lcd|oled|digiti[sz]er)\b{_GAP}{_BAD}\b|\b{_BAD}\b{_GAP}(?:screen|display|lcd|oled)\b")
_BACK = re.compile(rf"\b(?:back|rear)\b(?!\W+(?:camera|cam|lens))(?:\W+(?:glass|cover|panel))?{_GAP}{_BAD}\b|"
                   rf"\b{_BAD}\b(?:{_SEP}[\w']+){{0,2}}?{_SEP}(?:back|rear)\b(?!\W+(?:camera|cam|lens))")
_REAR_CAM = re.compile(rf"\b(?:rear|back|main)\s+(?:camera|cam|lens)\b{_GAP}{_BAD}\b|\b{_BAD}\b{_GAP}(?:rear|back|main)\s+(?:camera|cam|lens)\b|"
                       rf"\bcamera\b(?<!front camera)(?<!selfie camera){_GAP}{_BAD}\b")
_FRONT_CAM = re.compile(rf"\b(?:front|selfie)\s+(?:camera|cam)\b{_GAP}{_BAD}\b|\b{_BAD}\b{_GAP}(?:front|selfie)\s+camera\b")
_BATTERY_BAD = re.compile(rf"\bbattery\b{_GAP}(?:{_BAD}|poor|weak|drain\w*|swollen|bad|dying|needs?\s+replac\w*|service)\b|\bservice\s+battery\b")
_BATTERY_PCT = re.compile(r"\bbattery(?:\s+(?:health|capacity|life|condition))?\D{0,14}?(\d{2,3})\s*%")
_CHARGING = re.compile(r"\b(?:not|won'?t|doesn'?t|isn'?t|no|stopped)\s+(?:\w+\s+)?charg(?:e|es|ing)\b|"
                       rf"\bcharg(?:ing|er)\s+(?:port|socket)\b{_GAP}{_BAD}\b|\bcharging\s+(?:issues?|problems?|fault)\b")
_SPEAKER = re.compile(rf"\b(?:speaker|loudspeaker|earpiece|ear\s+piece)\b{_GAP}{_BAD}\b|\bno\s+sound\b|\bmic(?:rophone)?\b{_GAP}{_BAD}\b")
_BUTTON = re.compile(rf"\b(?:power|volume|side|mute)\s+(?:button|switch)\b{_GAP}{_BAD}\b")
_BARE_CRACK = re.compile(r"\b(?:cracked|smashed|shattered)\b")

# Faults that are not a priced repair: logic-board / water / biometrics. A phone with one of these is
# a gamble, so they are never put on the buy page (there is deliberately no setting to allow them).
_BOARD = (
    (re.compile(r"\bno\s+power\b|\b(?:won'?t|will\s+not|doesn'?t|does\s+not|not)\s+(?:turn(?:ing)?\s+on|power(?:ing)?\s+on|switch(?:ing)?\s+on|boot(?:ing)?)\b"), "won't power on"),
    (re.compile(r"\bboot\s*loop\b|\bbootloop\b|\bstuck\s+on\s+(?:the\s+)?apple\b|\bapple\s+logo\b|\brestart(?:s|ing)\s+(?:itself|loop|randomly)\b"), "boot loop"),
    (re.compile(r"\bwater\s+damag\w*|\bliquid\s+damag\w*|\bdropped\s+in\s+(?:the\s+)?water\b|\bwater\s+ingress\b|\bwent\s+(?:in|through)\s+the\s+wash\b|\bsubmerged\b"), "water damage"),
    (re.compile(r"\bmother\s*board\b|\blogic\s*board\b|\bbaseband\b|\bno\s+(?:service|signal)\b|\bno\s+wi-?fi\b|\bwi-?fi\s+(?:not|greyed|grayed|issue)\w*"), "board-level fault"),
    (re.compile(r"\b(?:face|touch)\s*id\b(?:\W+\w+){0,3}?\W+(?:not|broken|faulty|disabled|gone|missing|issue\w*|fault\w*|fail\w*|doesn'?t|won'?t|unavailable)\b|\bno\s+(?:face|touch)\s*id\b|\b(?:face|touch)\s*id\s+(?:not\s+working|unavailable)\b"), "Face ID / Touch ID fault"),
    (re.compile(r"\bdead\b(?!\s*pixels?)"), "dead"),
)


def _clean(text: str) -> str:
    low = (text or "").lower().replace("’", "'")
    low = _NEGATED_FAULT.sub(" ", low)
    return _NOISE.sub(" ", low)


@dataclass
class FaultScan:
    parts: list[str] = field(default_factory=list)      # part type names, unique, in order found
    board_reason: Optional[str] = None                   # set => not a priced repair
    battery_health: Optional[int] = None

    @property
    def found_anything(self) -> bool:
        return bool(self.parts or self.board_reason)


def scan_faults(text: str) -> FaultScan:
    """Which parts does this listing say need replacing? Pure text, no DB."""
    low = _clean(text)
    out = FaultScan()

    def add(part: str) -> None:
        if part not in out.parts:
            out.parts.append(part)

    for rx, reason in _BOARD:
        if rx.search(low):
            out.board_reason = reason
            break

    claimed: list[tuple[int, int]] = []      # text spans a specific rule has explained

    def hit(rx: re.Pattern, part: str) -> bool:
        found = False
        for m in rx.finditer(low):
            claimed.append(m.span())
            found = True
        if found:
            add(part)
        return found

    hit(_SCREEN, "Screen")
    hit(_BACK, "Back Glass")
    hit(_REAR_CAM, "Rear Camera")
    hit(_FRONT_CAM, "Front Camera")
    for m in _BARE_CRACK.finditer(low):
        # "iPhone 13 cracked": nearly always the screen, so it is priced as one.
        if not any(a < m.end() and m.start() < b for a, b in claimed):
            add("Screen")
    pct = _BATTERY_PCT.search(low)
    if pct:
        out.battery_health = int(pct.group(1))
    if _BATTERY_BAD.search(low) or (out.battery_health is not None and 0 < out.battery_health < BATTERY_SERVICE_BELOW):
        add("Battery")
    if _CHARGING.search(low):
        add("Charging Port / Flex")
    if _SPEAKER.search(low):
        add("Speaker")
    if _BUTTON.search(low):
        add("Housing")
    return out


# -------------------------------------------------------------------------------------- pricing
@dataclass
class RepairSettings:
    labour: Decimal = Decimal("0")
    contingency_pct: Decimal = Decimal("10")
    defaults: dict[str, Decimal] = field(default_factory=lambda: dict(BUILTIN_PART_COSTS))
    # (part, model id) -> (average, n) memo so a scan doesn't re-query per listing
    own_cost_cache: dict[tuple[str, int], Optional[tuple[Decimal, int]]] = field(default_factory=dict)


def parse_part_costs(text: str) -> dict[str, Decimal]:
    """'screen=55, battery=20' -> {'screen': 55, 'battery': 20}. Bad pieces are ignored."""
    out: dict[str, Decimal] = {}
    for piece in (text or "").split(","):
        if "=" not in piece:
            continue
        name, _, val = piece.partition("=")
        try:
            out[name.strip().lower()] = Decimal(val.strip().replace("£", ""))
        except Exception:  # noqa: BLE001 - a typo in one entry must not break scanning
            continue
    return out


async def load_settings(session: AsyncSession) -> RepairSettings:
    defaults = dict(BUILTIN_PART_COSTS)
    defaults.update(parse_part_costs(await config_service.get_setting(session, "repair_part_cost_defaults", "") or ""))
    return RepairSettings(
        labour=await config_service.get_setting_decimal(session, "repair_labour_gbp", Decimal("0")),
        contingency_pct=await config_service.get_setting_decimal(session, "repair_contingency_pct", Decimal("10")),
        defaults=defaults,
    )


async def own_part_cost(session: AsyncSession, part_name: str, phone_model_id: int,
                         settings: Optional[RepairSettings] = None) -> Optional[tuple[Decimal, int]]:
    """Average of the last 10 prices YOU paid for this part type for exactly this model.
    Donor parts are excluded: their cost is an allocation, not what a replacement costs to buy."""
    key = (part_name.lower(), phone_model_id)
    if settings is not None and key in settings.own_cost_cache:
        return settings.own_cost_cache[key]
    stmt = (
        select(Part.cost)
        .join(PartTypeConfig, Part.part_type_id == PartTypeConfig.id)
        .where(
            func.lower(PartTypeConfig.name) == part_name.lower(),
            Part.source_type == PartSourceType.PURCHASED,
            Part.cost > 0,
            exists().where(PartCompatibleModel.part_id == Part.id, PartCompatibleModel.phone_model_id == phone_model_id),
        )
        .order_by(Part.id.desc())
        .limit(10)
    )
    costs = [c for c in (await session.execute(stmt)).scalars().all()]
    result = (sum(costs, Decimal("0")) / len(costs), len(costs)) if costs else None
    if result:
        result = (result[0].quantize(TWO, rounding=ROUND_HALF_UP), result[1])
    if settings is not None:
        settings.own_cost_cache[key] = result
    return result


@dataclass
class RepairEstimate:
    cost: Decimal
    basis: str
    parts: list[str]
    is_flat_assumption: bool = False       # no fault was named: used the flat default
    board_reason: Optional[str] = None     # set => can't be priced; caller should skip the listing
    battery_health: Optional[int] = None


async def estimate_repair(session: AsyncSession, phone_model: PhoneModelConfig, text: str,
                           flat_fallback: Decimal, settings: Optional[RepairSettings] = None) -> RepairEstimate:
    settings = settings or await load_settings(session)
    faults = scan_faults(text)
    if faults.board_reason:
        return RepairEstimate(cost=flat_fallback, basis=f"{faults.board_reason}: can't be priced", parts=faults.parts,
                              board_reason=faults.board_reason, battery_health=faults.battery_health)
    if not faults.parts:
        return RepairEstimate(cost=flat_fallback, basis=f"no specific fault named: flat £{flat_fallback} assumption",
                              parts=[], is_flat_assumption=True, battery_health=faults.battery_health)

    pieces: list[str] = []
    parts_total = Decimal("0")
    for part in faults.parts:
        own = await own_part_cost(session, part, phone_model.id, settings)
        if own:
            price, why = own[0], f"your avg of {own[1]} buy{'s' if own[1] != 1 else ''}"
        elif part.lower() in settings.defaults:
            price, why = settings.defaults[part.lower()], "default"
        else:
            price, why = flat_fallback, "flat assumption"
        parts_total += price
        pieces.append(f"{part.lower()} £{price:.0f} ({why})")
    contingency = (parts_total * settings.contingency_pct / Decimal("100")).quantize(TWO, rounding=ROUND_HALF_UP)
    cost = (parts_total + contingency + settings.labour).quantize(TWO, rounding=ROUND_HALF_UP)
    extras = []
    if contingency:
        extras.append(f"{settings.contingency_pct.normalize():f}% contingency")
    if settings.labour:
        extras.append(f"£{settings.labour} labour")
    basis = " + ".join(pieces + extras)
    return RepairEstimate(cost=cost, basis=basis, parts=faults.parts, battery_health=faults.battery_health)
