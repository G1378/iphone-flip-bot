"""Build an eBay listing from what we already know about a phone.

The bot does the typing; a person checks it and presses Publish. Everything here is a plain
function of the phone's record (model, storage, colour, lock status, grade, battery health,
the parts fitted) and a couple of business settings, so the same phone always produces the
same listing and a rebuild after any change just reflects it.

Nothing here claims more than the record says: unknowns are left out of the text and reported
back as warnings instead, so a missing fact is something to fix, never something invented.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import ROUND_DOWN, Decimal
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.commerce_models import Listing
from app.models.enums import LockStatus, PartSourceType
from app.models.inventory_models import Part, Phone
from app.services import config_service
from app.services import pricing as pricing_service

TITLE_MAX = 80


@dataclass
class BuiltListing:
    title: str
    description: str
    condition_text: str
    condition_description: str
    aspects: dict[str, list[str]]
    price: Optional[Decimal]
    warnings: list[str] = field(default_factory=list)   # worth a look, doesn't stop publishing


def storage_label(storage: Optional[str]) -> Optional[str]:
    """'128GB' -> '128 GB' (how eBay's Storage Capacity values are written)."""
    if not storage:
        return None
    m = re.fullmatch(r"\s*(\d+)\s*(GB|TB)\s*", storage, re.I)
    return f"{m.group(1)} {m.group(2).upper()}" if m else storage.strip()


def _network(phone: Phone) -> Optional[str]:
    if phone.lock_status == LockStatus.UNLOCKED:
        return "Unlocked"
    if phone.lock_status == LockStatus.LOCKED:
        return phone.carrier.strip() if phone.carrier else "Locked"
    return None


def grade_label(phone: Phone) -> Optional[str]:
    t = (phone.cosmetic_condition or "").strip()
    return t or None


def build_title(phone: Phone) -> str:
    """Most searched words first; optional extras are dropped from the end until it fits 80."""
    model = phone.model if phone.model.lower().startswith(phone.manufacturer.lower()) else f"{phone.manufacturer} {phone.model}"
    parts = [model, phone.variant, storage_label(phone.storage), phone.colour, _network(phone),
             f"Battery {phone.battery_health}%" if phone.battery_health else None]
    parts = [p for p in parts if p]
    while len(" ".join(parts)) > TITLE_MAX and len(parts) > 1:
        parts.pop()
    return " ".join(parts)[:TITLE_MAX]


def build_aspects(phone: Phone) -> dict[str, list[str]]:
    """Item specifics. eBay's required set varies by category, so only what we really know is sent."""
    a: dict[str, list[str]] = {"Brand": [phone.manufacturer], "Model": [phone.model if phone.model.lower().startswith(
        phone.manufacturer.lower()) else f"{phone.manufacturer} {phone.model}"]}
    if phone.storage:
        a["Storage Capacity"] = [storage_label(phone.storage)]
    if phone.colour:
        a["Colour"] = [phone.colour]
    net = _network(phone)
    if net:
        a["Network"] = [net]
    if phone.lock_status == LockStatus.UNLOCKED:
        a["Lock Status"] = ["Network Unlocked"]
    elif phone.lock_status == LockStatus.LOCKED:
        a["Lock Status"] = ["Network Locked"]
    return a


async def fitted_parts(session: AsyncSession, phone: Phone) -> list[Part]:
    """Parts currently fitted in this phone (queried, so it never depends on what was lazily loaded)."""
    stmt = (select(Part).where(Part.installed_phone_id == phone.id, Part.status == "INSTALLED")
            .options(selectinload(Part.part_type)).order_by(Part.id))
    return [p for p in (await session.execute(stmt)).scalars().all() if p.part_type]


def build_description(phone: Phone, fitted: list[Part], whats_included: str, extra: str) -> str:
    out = [f"{phone.manufacturer} {phone.model}".upper() if len(phone.model) < 30 else phone.model.upper(),
           " ".join(b for b in (phone.variant, storage_label(phone.storage), phone.colour, _network(phone)) if b)]
    cond = ["CONDITION"]
    cond.append(f"Cosmetic: {phone.cosmetic_condition}" if phone.cosmetic_condition else "Cosmetic condition: see photos.")
    if phone.functional_condition:
        cond.append(f"Working order: {phone.functional_condition}")
    if phone.battery_health:
        cond.append(f"Battery health: {phone.battery_health}%")
    blocks = ["\n".join(x for x in out if x), "\n".join(cond)]
    if fitted:
        lines = ["REPAIR HISTORY"] + [f"• Replacement {p.part_type.name.lower()} fitted" for p in fitted]
        blocks.append("\n".join(lines))
    if whats_included.strip():
        blocks.append("WHAT'S INCLUDED\n" + whats_included.strip())
    if extra.strip():
        blocks.append(extra.strip())
    return "\n\n".join(blocks)


def build_condition_description(phone: Phone, fitted: list[Part]) -> str:
    bits = []
    if phone.cosmetic_condition:
        bits.append(phone.cosmetic_condition.rstrip("."))
    if phone.functional_condition:
        bits.append(f"fully tested: {phone.functional_condition}".rstrip("."))
    if phone.battery_health:
        bits.append(f"battery health {phone.battery_health}%")
    if fitted:
        bits.append("repaired with replacement " + ", ".join(p.part_type.name.lower() for p in fitted))
    text = "; ".join(bits)
    return (text[:1].upper() + text[1:] + ".") if text else ""


def condition_text(phone: Phone) -> str:
    """The wording the eBay client maps to eBay's condition scale."""
    return phone.cosmetic_condition or phone.functional_condition or "Used"


async def build_for_phone(session: AsyncSession, phone: Phone) -> BuiltListing:
    whats = await config_service.get_setting(session, "listing_whats_included", "") or ""
    extra = await config_service.get_setting(session, "listing_extra_text", "") or ""
    price = await pricing_service.get_current_estimated_sale_price(session, phone)
    fitted = await fitted_parts(session, phone)

    warnings: list[str] = []
    if not phone.storage:
        warnings.append("Storage isn't set")
    if not phone.colour:
        warnings.append("Colour isn't set")
    if phone.lock_status == LockStatus.UNKNOWN:
        warnings.append("Lock status is unknown: set it so the listing can say Unlocked or Locked")
    if not phone.cosmetic_condition:
        warnings.append("No cosmetic condition recorded (the listing says 'see photos')")
    if not phone.battery_health:
        warnings.append("No battery health recorded")
    if not whats.strip():
        warnings.append("'What's included' not set: `/config business key:listing_whats_included value:...`")
    if price is None:
        warnings.append("No price estimate for this model yet: set the price yourself")
    for p in fitted:
        if p.source_type == PartSourceType.PURCHASED:
            warnings.append(f"The {p.part_type.name.lower()} was bought in: say in the listing if it isn't genuine Apple")
    return BuiltListing(
        title=build_title(phone), description=build_description(phone, fitted, whats, extra),
        condition_text=condition_text(phone), condition_description=build_condition_description(phone, fitted),
        aspects=build_aspects(phone), price=price, warnings=warnings,
    )


# ---------------------------------------------------------------- Best Offer
@dataclass
class BestOfferTerms:
    enabled: bool
    auto_decline: Optional[Decimal] = None   # offers BELOW this are declined by eBay without bothering you
    auto_accept: Optional[Decimal] = None    # offers at/above this are accepted by eBay
    note: str = ""

    def summary(self) -> str:
        if not self.enabled:
            return f"Best Offer off. {self.note}".strip()
        bits = [f"Offers below £{self.auto_decline} are declined automatically"] if self.auto_decline else []
        if self.auto_accept:
            bits.append(f"offers of £{self.auto_accept}+ are accepted automatically")
        return "Best Offer on. " + ("; ".join(bits) + "." if bits else "Every offer comes to you.")


async def best_offer_terms(session: AsyncSession, listing: Listing, phone: Phone) -> BestOfferTerms:
    """Best Offer is on unless you've turned it off. Offers that would take the profit under your
    minimum (the same rules as /analyse) are auto-declined so they never reach you; nothing is ever
    accepted automatically unless you set best_offer_auto_accept_pct."""
    from app.services import finance
    if (await config_service.get_setting(session, "best_offer_enabled", "true") or "true").strip().lower() != "true":
        return BestOfferTerms(False, note="(turned off in settings)")
    price = listing.price or Decimal("0")
    floor = await finance.minimum_sale_price(session, phone)
    if floor >= price:
        return BestOfferTerms(False, note=f"The asking price (£{price}) is already at your minimum profit (£{floor}), "
                                          "so there's no room to accept a lower offer.")
    accept = None
    pct = await config_service.get_setting_decimal(session, "best_offer_auto_accept_pct", Decimal("0"))
    if pct > 0:
        candidate = (price * pct / Decimal("100")).to_integral_value(rounding=ROUND_DOWN)
        if floor < candidate < price:
            accept = candidate
    return BestOfferTerms(True, auto_decline=floor, auto_accept=accept)


# ---------------------------------------------------------------- readiness
def blockers(listing: Listing, phone: Phone) -> list[str]:
    """What stops this listing being published right now. Pure, so the job-channel panel and
    the Publish button use the same answer."""
    out = []
    if not phone.photo_urls:
        out.append("No photos yet: post them with `/photos` in this channel")
    if not listing.price or listing.price <= 0:
        out.append("Set a price")
    if not (listing.title or "").strip():
        out.append("Title is empty")
    for need in ("Brand", "Model", "Storage Capacity", "Colour"):
        if not (listing.aspects or {}).get(need):
            out.append(f"{need} is missing from the item specifics")
    return out


async def setup_gaps(session: AsyncSession) -> list[str]:
    """What eBay needs that lives in your eBay account rather than on the phone."""
    gaps = []
    for key, label in (("ebay_fulfillment_policy_id", "postage policy"), ("ebay_payment_policy_id", "payment policy"),
                       ("ebay_return_policy_id", "returns policy"), ("ebay_merchant_location_key", "stock location")):
        if not await config_service.get_setting(session, key, ""):
            gaps.append(label)
    return gaps
