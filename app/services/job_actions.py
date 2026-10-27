"""The things a button in a job channel actually does.

Each function uses the same services as the matching slash command (so history,
costs and stock stay correct) and returns a one-line message for the channel.
No Discord imports. Anything the person can get wrong raises JobActionError
with a plain-English reason.
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal, InvalidOperation
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import FaultStatus, RepairPartLineStatus
from app.models.inventory_models import Part, Phone
from app.models.repair_models import RepairPart
from app.services import config_service
from app.services import deals as deals_service
from app.services import orders as orders_service
from app.services import part_failures
from app.services import parts as parts_service
from app.services import phone_tickets
from app.services import phones as phones_service
from app.services import repairs as repairs_service


class JobActionError(ValueError):
    pass


async def _phone(session: AsyncSession, phone_id: int) -> Phone:
    phone = await session.get(Phone, phone_id, populate_existing=True)  # never act on stale in-session state
    if phone is None:
        raise JobActionError("That phone no longer exists.")
    return phone


async def _line(session: AsyncSession, rp_id: int, phone_id: int) -> RepairPart:
    rp = await session.get(RepairPart, rp_id, populate_existing=True)
    if rp is None or rp.repair is None or rp.repair.phone_id != phone_id:
        raise JobActionError("That part requirement is no longer on this phone's repair.")
    if rp.status not in (RepairPartLineStatus.REQUIRED, RepairPartLineStatus.RESERVED):
        raise JobActionError(f"{rp.required_part_type.name} is already {rp.status.value.lower()}.")
    return rp


async def _supply(session: AsyncSession, phone: Phone, rp: RepairPart):
    """What the panel showed for this line: the engine's allocation, so the button
    acts on exactly the part the person was told about."""
    phones = await phone_tickets.load_phones(session)
    if phone.id not in {p.id for p in phones}:
        phones.append(phone)
    ctx = await phone_tickets.load_context(session, phones)
    return ctx, ctx.supply.get(rp.id, [])


# ---------------------------------------------------------------- parts
async def reserve(session: AsyncSession, phone_id: int, rp_id: int, actor: int, part_id: Optional[int] = None) -> str:
    phone = await _phone(session, phone_id)
    rp = await _line(session, rp_id, phone_id)
    if rp.status == RepairPartLineStatus.RESERVED:
        raise JobActionError(f"{phone_tickets.part_label(rp, phone)} is already reserved.")
    if part_id is not None:
        part = await session.get(Part, part_id)
        if part is not None:
            try:  # a part picked by hand must still be for this phone's exact model
                await parts_service.assert_part_fits(session, part, phone)
            except parts_service.PartStateError as e:
                raise JobActionError(str(e))
    else:
        _, units = await _supply(session, phone, rp)
        part = next((u.part for u in units if u.kind == "IN_STOCK"), None)
    if part is None:
        raise JobActionError(f"There's no {phone_tickets.part_label(rp, phone)} in stock to reserve.")
    try:
        await parts_service.reserve_part(session, part, rp.repair, rp, actor)
    except parts_service.PartStateError as e:
        raise JobActionError(str(e))
    return f"Reserved `{part.internal_id}` for {phone_tickets.part_label(rp, phone)}. Fit it, then press **Fitted**."


async def alternatives(session: AsyncSession, phone_id: int, rp_id: int) -> list[Part]:
    phone = await _phone(session, phone_id)
    rp = await _line(session, rp_id, phone_id)
    ctx, _ = await _supply(session, phone, rp)
    return parts_service.fitting_parts(ctx.available.get(rp.required_part_type_id, []), phone.phone_model_id)


async def fit(session: AsyncSession, phone_id: int, rp_id: int, actor: int) -> str:
    phone = await _phone(session, phone_id)
    rp = await _line(session, rp_id, phone_id)
    if rp.status != RepairPartLineStatus.RESERVED or rp.part is None:
        raise JobActionError(f"Reserve the {phone_tickets.part_label(rp, phone)} first.")
    try:
        await parts_service.install_part(session, rp.part, phone, rp, actor)
    except parts_service.PartStateError as e:
        raise JobActionError(str(e))
    return f"Fitted `{rp.part.internal_id}` ({phone_tickets.part_label(rp, phone)})."


async def fit_confirmed(session: AsyncSession, phone_id: int, rp_id: int, actor: int) -> str:
    """Fitted AND it works: the only way a fit is recorded from a job channel."""
    phone = await _phone(session, phone_id)
    rp = await _line(session, rp_id, phone_id)
    await fit(session, phone_id, rp_id, actor)
    part = await session.get(Part, rp.part_id, populate_existing=True)
    await part_failures.confirm_working(session, part, phone, actor)
    return f"Fitted `{part.internal_id}` ({phone_tickets.part_label(rp, phone)}) ✅ confirmed working."


async def fail_fit(session: AsyncSession, phone_id: int, rp_id: int, actor: int, *, action: str,
                   reason: str = "") -> str:
    """The reserved part didn't work when fitted: it never goes into the phone."""
    phone = await _phone(session, phone_id)
    rp = await _line(session, rp_id, phone_id)
    if rp.status != RepairPartLineStatus.RESERVED or rp.part_id is None:
        raise JobActionError(f"There's no reserved {phone_tickets.part_label(rp, phone)} to mark as failed.")
    part = await session.get(Part, rp.part_id, populate_existing=True)
    try:
        failure = await part_failures.record_failure(session, part=part, phone=phone, repair_part=rp, action=action,
                                                     reason=reason, actor=actor, installed=False)
    except part_failures.PartFailureError as e:
        raise JobActionError(str(e))
    return await _failure_message(session, part, phone_tickets.part_label(rp, phone), failure, phone)


async def _failure_message(session: AsyncSession, part: Part, type_name: str, failure, phone: Phone) -> str:
    text = f"❌ `{part.internal_id}` ({type_name}) didn't work: {part_failures.ACTION_LABEL[failure.action]}"
    if failure.supplier:
        text += f" · from {failure.supplier}"
    if failure.loss_amount:
        text += f" · £{failure.loss_amount} written off"
    info = (await part_failures.failure_info(session)).get((phone.id, part.part_type_id))
    if info and info.count >= part_failures.DIAGNOSE_AFTER:
        text += f"\n⚠️ {type_name} has now failed {info.count} times on this phone. It's probably not the part: diagnose it."
    elif failure.action == "RETURN":
        text += "\n📮 A ticket has been opened to chase the refund."
    return text


async def unfit(session: AsyncSession, phone_id: int, part_id: int, actor: int, *, disposition: str = "STOCK",
                reason: str = "") -> str:
    """A fitted part comes out. disposition: STOCK (wrong part, back on the shelf) or
    SCRAP / RETURN / FAULTY (it turned out not to work)."""
    phone = await _phone(session, phone_id)
    repair = await repairs_service.get_active_repair(session, phone)
    part = await session.get(Part, part_id, populate_existing=True)
    line = next((rp for rp in (repair.parts if repair else []) if rp.part_id == part_id
                 and rp.status == RepairPartLineStatus.INSTALLED), None)
    if part is None or line is None:
        raise JobActionError("That part isn't fitted on this repair.")
    disposition = (disposition or "STOCK").upper()
    if disposition != "STOCK":
        try:
            failure = await part_failures.record_failure(session, part=part, phone=phone, repair_part=line,
                                                         action=disposition, reason=reason, actor=actor, installed=True)
        except part_failures.PartFailureError as e:
            raise JobActionError(str(e))
        return await _failure_message(session, part, phone_tickets.part_label(line, phone), failure, phone)
    try:
        await parts_service.remove_part(session, part, line, actor, notes="Removed from job channel")
    except parts_service.PartStateError as e:
        raise JobActionError(str(e))
    repair.parts.append(RepairPart(required_part_type_id=line.required_part_type_id, quantity=1,
                                   status=RepairPartLineStatus.REQUIRED))
    await session.flush()
    return f"Removed `{part.internal_id}` back to stock. {phone_tickets.part_label(line, phone)} is back on the list."


def fitted_parts(repair) -> list[tuple[RepairPart, Part]]:
    return [(rp, rp.part) for rp in (repair.parts if repair else [])
            if rp.status == RepairPartLineStatus.INSTALLED and rp.part is not None]


async def open_undiagnosed(session: AsyncSession, phone_id: int):
    """Open faults that no part is currently chasing (the ones 'Mark resolved' applies to)."""
    phone = await _phone(session, phone_id)
    phones = await phone_tickets.load_phones(session)
    if phone.id not in {p.id for p in phones}:
        phones.append(phone)
    ctx = await phone_tickets.load_context(session, phones)
    sit = phone_tickets.assess(phone, ctx)
    return list(sit.undiagnosed) if sit else []


async def resolve_fault(session: AsyncSession, phone_id: int, fault_id: int, actor: int) -> str:
    """The person fixed it another way (e.g. a board repair)."""
    from app.models.inventory_models import PhoneFault
    phone = await _phone(session, phone_id)
    fault = await session.get(PhoneFault, fault_id, populate_existing=True)
    if fault is None or fault.phone_id != phone.id or fault.status != FaultStatus.OPEN:
        raise JobActionError("That fault isn't open on this phone.")
    fault.status = FaultStatus.RESOLVED
    await session.flush()
    await repairs_service.release_fault_from_repair(session, fault)
    return f"Marked resolved: {fault.description}"


async def retry_part(session: AsyncSession, phone_id: int, fault_id: int, actor: int) -> str:
    """Another part failed, but the person wants to try one more anyway."""
    from app.models.inventory_models import PhoneFault
    phone = await _phone(session, phone_id)
    fault = await session.get(PhoneFault, fault_id, populate_existing=True)
    if fault is None or fault.phone_id != phone.id or fault.status != FaultStatus.OPEN or not fault.required_part_type_id:
        raise JobActionError("That fault isn't waiting on a part.")
    repair = await repairs_service.get_active_repair(session, phone)
    if repair is None:
        repair = await repairs_service.create_repair_for_open_faults(session, phone, actor)
    if not any(rp.required_part_type_id == fault.required_part_type_id and rp.status in (
            RepairPartLineStatus.REQUIRED, RepairPartLineStatus.RESERVED) for rp in repair.parts):
        repair.parts.append(RepairPart(required_part_type_id=fault.required_part_type_id, quantity=1,
                                       status=RepairPartLineStatus.REQUIRED))
        await session.flush()
    return "Added another to the list, even though the last one(s) failed."


# ---------------------------------------------------------------- ordering
def _money(raw: str, what: str, allow_blank: bool = False) -> Decimal:
    raw = (raw or "").strip().lstrip("£")
    if not raw and allow_blank:
        return Decimal("0")
    try:
        value = Decimal(raw)
    except InvalidOperation:
        raise JobActionError(f"{what} must be a number, e.g. 35.00.")
    if value < 0:
        raise JobActionError(f"{what} can't be negative.")
    return value


def _date(raw: str) -> Optional[dt.date]:
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        return dt.date.fromisoformat(raw)
    except ValueError:
        raise JobActionError("Expected delivery must look like 2026-11-03.")


async def order_part(session: AsyncSession, phone_id: int, rp_id: int, actor: int, *, supplier: str,
                     unit_cost: str, shipping: str = "", expected: str = "") -> str:
    """A separate order for one missing part, with its own supplier and price."""
    phone = await _phone(session, phone_id)
    rp = await _line(session, rp_id, phone_id)
    _, units = await _supply(session, phone, rp)
    if not any(u.kind == "TO_ORDER" for u in units):
        raise JobActionError(f"{phone_tickets.part_label(rp, phone)} is already covered by stock or an order.")
    need_colour = next((u.colour for u in units if u.kind == "TO_ORDER" and u.colour), None)
    supplier = (supplier or "").strip()
    if not supplier:
        raise JobActionError("Who are you ordering it from?")
    cost = _money(unit_cost, "Price")
    try:
        model = await config_service.phone_model_for(session, phone)
    except config_service.UnknownPhoneModelError as e:
        raise JobActionError(f"Can't order for {phone.internal_id}: {e}")
    order = await orders_service.create_order(
        session, supplier,
        [orders_service.OrderLineInput(part_type_id=rp.required_part_type_id, quantity=1, unit_cost=cost,
                                       phone_model_id=model.id, colour=need_colour)],
        actor, shipping_cost=_money(shipping, "Shipping", allow_blank=True), expected_delivery=_date(expected),
        notes=f"For {phone.internal_id}" + (f" ({need_colour})" if need_colour else ""))
    await orders_service.mark_ordered(session, order, actor)
    eta = f", due {order.expected_delivery.isoformat()}" if order.expected_delivery else ""
    col = f" in {need_colour}" if need_colour else ""
    return f"Ordered {phone_tickets.part_label(rp, phone)}{col} from {supplier}: `{order.internal_id}` £{cost}{eta}."


async def receive(session: AsyncSession, order_id: int, actor: int) -> str:
    from app.models.commerce_models import Order
    order = await session.get(Order, order_id)
    if order is None:
        raise JobActionError("That order no longer exists.")
    order = await orders_service.get_order(session, order.internal_id)
    try:
        created = await orders_service.receive_order(session, order, actor)
    except orders_service.OrderError as e:
        raise JobActionError(str(e))
    return f"Received `{order.internal_id}`: {', '.join(p.internal_id for p in created) or 'nothing new'} now in stock."


async def mark_placed(session: AsyncSession, order_id: int, actor: int) -> str:
    from app.models.commerce_models import Order
    order = await session.get(Order, order_id)
    if order is None:
        raise JobActionError("That order no longer exists.")
    try:
        await orders_service.mark_ordered(session, order, actor)
    except orders_service.OrderError as e:
        raise JobActionError(str(e))
    return f"`{order.internal_id}` marked as ordered."


# ---------------------------------------------------------------- repair + phone
async def plan_repair(session: AsyncSession, phone_id: int, actor: int) -> str:
    phone = await _phone(session, phone_id)
    if not any(f.status == FaultStatus.OPEN for f in phone.faults):
        raise JobActionError("There are no open faults to plan a repair for.")
    repair = await repairs_service.create_repair_for_open_faults(session, phone, actor)
    return f"Repair `{repair.internal_id}` planned from the open faults."


async def log_fault(session: AsyncSession, phone_id: int, actor: int, *, description: str,
                    part_type_name: str = "") -> str:
    """Same as /fault, plus it adds the part to the repair so the panel can act on it."""
    phone = await _phone(session, phone_id)
    description = (description or "").strip()
    if not description:
        raise JobActionError("Describe the fault.")
    part_type_id = None
    if (part_type_name or "").strip():
        part_type_id = (await config_service.get_or_create_part_type(session, part_type_name.strip())).id
    await phones_service.add_fault(session, phone, description, actor, required_part_type_id=part_type_id)
    if phone.current_status in ("PURCHASED", "AWAITING_TEST", "TESTED"):
        await phones_service.set_status(session, phone, "REPAIR_REQUIRED", actor, notes="Fault logged")
    if part_type_id:
        await repairs_service.sync_repair_with_open_faults(session, phone, actor)
    return f"Fault logged: {description}" + (f" (needs {part_type_name.strip()})" if part_type_id else "")


async def edit_details(session: AsyncSession, phone_id: int, actor: int, *, battery: str = "", cosmetic: str = "",
                       functional: str = "", colour: str = "", notes: str = "") -> str:
    phone = await _phone(session, phone_id)
    fields: dict = {}
    if battery.strip():
        try:
            pct = int(battery.strip().rstrip("%"))
        except ValueError:
            raise JobActionError("Battery health must be a whole number, e.g. 87.")
        if not 0 <= pct <= 100:
            raise JobActionError("Battery health must be between 0 and 100.")
        fields["battery_health"] = pct
    for key, value in (("cosmetic_condition", cosmetic), ("functional_condition", functional),
                       ("colour", colour), ("notes", notes)):
        if value.strip():
            fields[key] = value.strip()
    if not fields:
        raise JobActionError("Nothing to update. Fill in at least one box.")
    await phones_service.edit_phone(session, phone, actor, **fields)
    return "Updated: " + ", ".join(k.replace("_", " ") for k in fields)


async def move(session: AsyncSession, phone_id: int, actor: int, location_code: str) -> str:
    phone = await _phone(session, phone_id)
    code = (location_code or "").strip().upper()
    if not code:
        raise JobActionError("Where is it going?")
    await phones_service.move_phone(session, phone, code, actor)
    return f"Moved to {code}."


# ---------------------------------------------------------------- eBay listing
async def _draft(session: AsyncSession, phone_id: int, listing_id: int):
    from app.models.commerce_models import Listing
    listing = await session.get(Listing, listing_id, populate_existing=True)
    if listing is None or listing.phone_id != phone_id or listing.status != "DRAFT":
        raise JobActionError("That draft listing isn't there any more: press **Build eBay listing** again.")
    return listing


async def build_listing(session: AsyncSession, phone_id: int, actor: int) -> str:
    """Write the eBay listing from the phone's record (or rewrite the open draft)."""
    from app.services import listings as listings_service
    phone = await _phone(session, phone_id)
    draft, built = await listings_service.build_or_refresh_draft(session, phone, actor)
    price = f"£{draft.price}" if draft.price and draft.price > 0 else "no price yet"
    msg = f"Built {draft.internal_id}: **{draft.title}** · {price}."
    if built.warnings:
        msg += "\n" + "\n".join(f"⚠️ {w}" for w in built.warnings)
    return msg


async def edit_listing(session: AsyncSession, phone_id: int, listing_id: int, actor: int, *, title: str,
                       price: str, condition_text: str, description: str) -> str:
    listing = await _draft(session, phone_id, listing_id)
    try:
        value = Decimal(price.strip().lstrip("£"))
    except InvalidOperation:
        raise JobActionError(f"Couldn't read '{price}' as a price.")
    if value <= 0:
        raise JobActionError("The price has to be more than £0.")
    if not title.strip():
        raise JobActionError("The title can't be empty.")
    listing.title = title.strip()[:80]
    listing.price = value
    listing.condition_text = condition_text.strip() or listing.condition_text
    listing.description = description.strip() or listing.description
    await session.flush()
    return f"Updated {listing.internal_id}: **{listing.title}** · £{listing.price}."


async def publish_listing(session: AsyncSession, phone_id: int, listing_id: int, actor: int, ebay) -> str:
    """Push the draft live. The button is the person's decision; this just does it."""
    from app.integrations.ebay.interface import EbayApiError, EbayNotConfiguredError
    from app.services import listings as listings_service
    listing = await _draft(session, phone_id, listing_id)
    try:
        published = await listings_service.publish(session, listing, ebay, actor)
    except EbayNotConfiguredError:
        raise JobActionError("eBay isn't connected yet (see `/config ebay`). The draft is saved.")
    except listings_service.ListingNotReady as e:
        raise JobActionError(str(e))
    except EbayApiError as e:
        raise JobActionError(f"eBay refused it: {e}")
    return f"🚀 Published {published.internal_id} at £{published.price}: {published.ebay_url or published.ebay_listing_id}"


async def find_ebay_parts(session: AsyncSession, phone_id: int, ebay) -> str:
    """Cheapest eBay listings for every part this phone still has to order: exact model only."""
    from app.integrations.ebay.interface import EbayApiError, EbayNotConfiguredError
    phone = await _phone(session, phone_id)
    ctx = await phone_tickets.load_context(session, [phone])
    model = await config_service.phone_model_for(session, phone)
    wanted = [rp for rp in (phone_tickets.assess(phone, ctx).outstanding)
              if ctx.supply.get(rp.id) and ctx.supply[rp.id][0].kind == "TO_ORDER"]
    if not wanted:
        raise JobActionError("Nothing here needs ordering.")
    blocks = []
    try:
        for rp in wanted:
            offers = await deals_service.find_parts_on_ebay(session, ebay, rp.required_part_type.name, model)
            blocks.append(deals_service.format_part_offers(rp.required_part_type.name, model, offers))
    except EbayNotConfiguredError:
        raise JobActionError("eBay isn't connected yet (see `/config ebay`).")
    except EbayApiError as e:
        raise JobActionError(f"eBay search failed: {e}")
    return "\n\n".join(blocks)
