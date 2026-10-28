"""Automatic phone tickets: exactly one open ticket per phone, showing only
what needs doing next.

How it works
------------
Nothing here is scheduled by hand and no other service knows tickets exist.
Every few seconds `sync_phone_tickets` looks at each phone's *real* state (test
results, faults, repair lines, parts stock, purchase orders, status) and decides
which step the phone is on:

    TEST    run the test walkthrough
    REPAIR  swap parts. Split into "swap now" (in stock) and "waiting"
            (on order / needs ordering)
    VERIFY  all parts fitted: re-test and close the repair
    PREP    clean, grade, photograph, price
    LIST    create the eBay listing
    SHIP    final check, pack, post (after a sale)
    CLOSE   shipped: confirm delivery and payout

A step that has finished is detected from the data, its ticket is closed (the
Discord message is deleted) and the next step's ticket is opened. While a step
is still running the ticket is edited in place, e.g. when a part arrives the
"waiting" line moves up into "swap now".

The engine only *reads* inventory. It never reserves, installs or changes a
status on its own; those happen through the normal commands or the ticket's Done
button (see `manual_complete`).
"""
from __future__ import annotations

import datetime as dt
import hashlib
from dataclasses import dataclass, field
from typing import Optional, Sequence

from sqlalchemy import exists, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.commerce_models import Order, OrderLine
from app.models.config_models import TestDefinition
from app.models.enums import FaultStatus, RepairPartLineStatus, TestOutcome
from app.models.inventory_models import Part, Phone, PhoneFault, TestResult
from app.models.repair_models import Repair, RepairPart
from app.models.task_models import OPEN_TASK_STATUSES, JobChannel, PhoneTicketOverride, Task
from app.services import config_service, cosmetics
from app.services import part_failures
from app.services import parts as parts_service
from app.services import phones as phones_service
from app.services import repairs as repairs_service
from app.services import tasks as tasks_service
from app.services.events import log_event

TERMINAL_STATUSES = ("COMPLETE", "SCRAPPED")
ACTIVE_REPAIR_STATUSES = ("PLANNED", "PARTS_RESERVED", "IN_PROGRESS")
OUTSTANDING_LINES = (RepairPartLineStatus.REQUIRED, RepairPartLineStatus.RESERVED)

STAGE_CATEGORY = {
    "TEST": "Testing", "REPAIR": "Repair", "VERIFY": "Quality Control", "PREP": "Preparation",
    "LIST": "Listing", "SHIP": "Dispatch", "CLOSE": "After Sale",
}


class PhoneTicketError(ValueError):
    pass


# ====================================================================== supply
@dataclass
class Unit:
    """One physical part needed by a repair line, and where it will come from."""
    kind: str  # RESERVED | IN_STOCK | ON_ORDER | DRAFTED | TO_ORDER
    part: Optional[Part] = None
    order: Optional[Order] = None
    colour: Optional[str] = None   # frames / back glass still to get: the colour it has to be


@dataclass
class Context:
    definitions: Sequence[TestDefinition]
    overrides: dict[int, set[str]]
    supply: dict[int, list[Unit]]  # RepairPart.id -> one Unit per quantity
    available: dict[int, list[Part]] = field(default_factory=dict)  # part_type_id -> AVAILABLE parts (all of them)
    failures: dict = field(default_factory=dict)  # (phone_id, part_type_id) -> part_failures.FailureInfo
    drafts: dict = field(default_factory=dict)    # phone_id -> its open DRAFT eBay Listing
    offer_terms: dict = field(default_factory=dict)  # phone_id -> listing_builder.BestOfferTerms for that draft


def _active_repair(phone: Phone) -> Optional[Repair]:
    active = [r for r in phone.repairs if r.status in ACTIVE_REPAIR_STATUSES]
    return max(active, key=lambda r: (r.created_at or dt.datetime.min.replace(tzinfo=dt.timezone.utc), r.id)) if active else None


def _outstanding_lines(repair: Optional[Repair]) -> list[RepairPart]:
    if repair is None:
        return []
    return sorted((rp for rp in repair.parts if rp.status in OUTSTANDING_LINES), key=lambda rp: rp.id)


async def _available_parts(session: AsyncSession) -> dict[int, list[Part]]:
    rows = (await session.execute(
        select(Part).where(Part.status == "AVAILABLE").order_by(Part.created_at, Part.id)
    )).scalars().all()
    out: dict[int, list[Part]] = {}
    for p in rows:
        out.setdefault(p.part_type_id, []).append(p)
    return out


async def _orders_by_part_type(session: AsyncSession) -> dict[int, list[list]]:
    """part_type_id -> [[order, units_still_to_arrive, phone_model_id], ...], placed orders
    before drafts. Each slot is for ONE exact phone model: a Pro Max battery on order is
    not a battery on order for a Pro."""
    rows = (await session.execute(
        select(OrderLine, Order).join(Order, OrderLine.order_id == Order.id)
        .where(Order.status.in_(("DRAFT", "ORDERED", "SHIPPED")), OrderLine.received_quantity < OrderLine.quantity)
        .order_by(Order.created_at, OrderLine.id)
    )).all()
    out: dict[int, list[list]] = {}
    for line, order in rows:
        out.setdefault(line.part_type_id, []).append(
            [order, line.quantity - line.received_quantity, line.phone_model_id, line.colour])
    for lst in out.values():
        lst.sort(key=lambda x: x[0].status == "DRAFT")  # stable: placed first
    return out


def _pick_stock(stock: list[Part], model_id: Optional[int], role: Optional[str], want: Optional[str]) -> Optional[Part]:
    """The in-stock part to use for a line. A frame / back glass whose colour is known and right comes first;
    one with no colour recorded still counts (it is asked for when you reserve it) rather than hiding real
    stock and pushing you to order; one that is the wrong colour never does."""
    candidates = [p for p in stock if p.fits_model(model_id)]
    if role is None:
        return candidates[0] if candidates else None
    verified = [p for p in candidates if cosmetics.norm_colour(p.colour) and cosmetics.fits_colour(p, want)]
    unverified = [p for p in candidates if not cosmetics.norm_colour(p.colour)]
    return (verified or unverified or [None])[0]


def stock_hint(rp: RepairPart, phone: Phone, available: dict[int, list[Part]]) -> str:
    """When a part shows 'needs ordering' although the shelf has that part type: say why it can't be used."""
    others = [p for p in available.get(rp.required_part_type_id, []) if not p.fits_model(phone.phone_model_id)]
    if not others:
        return ""
    untagged = sum(1 for p in others if not p.compatible_model_ids())
    elsewhere = len(others) - untagged
    bits = []
    if elsewhere:
        bits.append(f"{elsewhere} for other models")
    if untagged:
        bits.append(f"{untagged} with no model set (`/part-models`)")
    return f" ⚠️ {rp.required_part_type.name} in stock but not usable here: {', '.join(bits)}"


def allocate_supply(phones: Sequence[Phone], available: dict[int, list[Part]],
                    on_order: dict[int, list[list]]) -> dict[int, list[Unit]]:
    """Work out, for every outstanding repair line across *all* phones, whether
    its part is reserved, in stock, on order or still needs ordering.

    Phones are served oldest repair first, so two phones needing one screen
    don't both claim the single one in stock or the single one on order."""
    stock = {k: list(v) for k, v in available.items()}
    incoming = {k: [list(x) for x in v] for k, v in on_order.items()}
    lines: list[tuple[dt.datetime, int, RepairPart, Repair, Phone]] = []
    for phone in phones:
        repair = _active_repair(phone)
        for rp in _outstanding_lines(repair):
            lines.append((repair.created_at or dt.datetime.min.replace(tzinfo=dt.timezone.utc), rp.id, rp, repair, phone))
    lines.sort(key=lambda x: (x[0], x[1]))

    # Frames and back glass: the colour each repair has to use. Worked out the first time the repair's
    # lines are reached (so it sees the stock left by older repairs): a frame and back glass must match
    # each other, but need not match the phone's original colour.
    colour_plan: dict[int, dict[str, Optional[str]]] = {}

    def plan_for(repair: Repair, phone: Phone) -> dict[str, Optional[str]]:
        if repair.id in colour_plan:
            return colour_plan[repair.id]
        need = {}   # role -> an outstanding, not-yet-chosen line
        for rp in _outstanding_lines(repair):
            role = cosmetics.part_role(rp.required_part_type.name)
            if role and rp.part is None:
                need.setdefault(role, rp)
        plan: dict[str, Optional[str]] = {}
        if set(need) == {cosmetics.FRAME, cosmetics.GLASS}:
            def in_stock(role):
                rp = need[role]
                return [p for p in stock.get(rp.required_part_type_id, [])
                        if p.fits_model(phone.phone_model_id) and cosmetics.norm_colour(p.colour)]
            pair = cosmetics.pick_pair_colour(phone.colour, in_stock(cosmetics.FRAME), in_stock(cosmetics.GLASS))
            plan = {cosmetics.FRAME: pair, cosmetics.GLASS: pair}
            if pair is None:   # nothing usable in stock: whatever has to be ordered is ordered to match the phone
                plan = {cosmetics.FRAME: phone.colour or None, cosmetics.GLASS: phone.colour or None}
        else:
            for role in need:
                plan[role] = cosmetics.counterpart_colour(phone, repair, role)
        colour_plan[repair.id] = plan
        return plan

    supply: dict[int, list[Unit]] = {}
    for _, _, rp, repair, phone in lines:
        model_id = phone.phone_model_id
        role = cosmetics.part_role(rp.required_part_type.name)
        units: list[Unit] = []
        if rp.status == RepairPartLineStatus.RESERVED and rp.part is not None:
            units.append(Unit("RESERVED", part=rp.part))
            wanted = max(rp.quantity - 1, 0)
        else:
            wanted = max(rp.quantity, 1)
        want = plan_for(repair, phone).get(role) if role else None
        for _ in range(wanted):
            type_id = rp.required_part_type_id
            # Only a part tagged for THIS phone's exact model (and, for frames/back glass, the right colour)
            # or an order line for it counts.
            fit = _pick_stock(stock.get(type_id, []), model_id, role, want)
            if fit is not None:
                stock[type_id].remove(fit)
                units.append(Unit("IN_STOCK", part=fit))
                continue
            slot = next((s for s in incoming.get(type_id, [])
                         if s[1] > 0 and model_id is not None and s[2] == model_id
                         and (role is None or not cosmetics.norm_colour(want) or cosmetics.colours_match(s[3], want))), None)
            if slot is not None:
                slot[1] -= 1
                units.append(Unit("DRAFTED" if slot[0].status == "DRAFT" else "ON_ORDER", order=slot[0]))
            else:
                units.append(Unit("TO_ORDER", colour=want if role else None))
        supply[rp.id] = units
    return supply


# ====================================================================== deriving a phone's step
@dataclass
class TicketSpec:
    stage: str
    title: str
    body: str
    priority: str = "NORMAL"
    waiting_reason: Optional[str] = None  # set => nothing can be done until parts arrive
    due_days: Optional[int] = None

    def content_hash(self) -> str:
        raw = "\x1f".join([self.stage, self.title, self.body, self.priority, self.waiting_reason or ""])
        return hashlib.sha1(raw.encode()).hexdigest()


@dataclass
class Situation:
    """Everything known about where a phone is, gathered once. Both the ticket
    text and the job-channel buttons are generated from this, so they can't disagree."""
    phone: Phone
    stage: str
    untested: list = field(default_factory=list)
    failed: list = field(default_factory=list)
    recorded: int = 0
    total: int = 0
    open_faults: list = field(default_factory=list)
    repair: Optional[Repair] = None
    outstanding: list = field(default_factory=list)   # RepairPart lines still to fit
    undiagnosed: list = field(default_factory=list)  # open faults no single part fixes (incl. flagged)
    flagged: list = field(default_factory=list)      # faults whose part keeps failing: probably not the part


def _latest_results(phone: Phone) -> dict[int, TestResult]:
    latest: dict[int, TestResult] = {}
    for r in sorted(phone.test_results, key=lambda r: (r.tested_at, r.id)):
        latest[r.test_definition_id] = r
    return latest


def _test_progress(phone: Phone, definitions: Sequence[TestDefinition]):
    latest = _latest_results(phone)
    untested, failed = [], []
    for d in definitions:
        r = latest.get(d.id)
        if r is None or r.result == TestOutcome.NOT_TESTED:
            untested.append(d)
        elif r.result == TestOutcome.FAIL:
            failed.append(d)
    return untested, failed, len(definitions) - len(untested)


def _names(items, limit: int = 10) -> str:
    names = [i.name for i in items]
    extra = len(names) - limit
    return ", ".join(names[:limit]) + (f" … and {extra} more" if extra > 0 else "")


def part_label(rp: RepairPart, phone: Phone) -> str:
    """'Battery (iPhone 13 Pro)': the part and the exact model it has to be for."""
    return f"{rp.required_part_type.name} ({phone.model})"


def _where(part: Part) -> str:
    return f"shelf {part.location.code}" if part.location else "no location set"


def assess(phone: Phone, ctx: Context) -> Optional[Situation]:
    """Which step is this phone on? None = it needs nothing right now."""
    s = phone.current_status
    if s in TERMINAL_STATUSES:
        return None
    untested, failed, recorded = _test_progress(phone, ctx.definitions)
    base = dict(phone=phone, untested=untested, failed=failed, recorded=recorded, total=len(ctx.definitions))
    if s == "SHIPPED":
        return Situation(stage="CLOSE", **base)
    if s == "SOLD":
        return Situation(stage="SHIP", **base)

    open_faults = [f for f in phone.faults if f.status == FaultStatus.OPEN]
    repair = _active_repair(phone)
    base.update(open_faults=open_faults, repair=repair)

    # ---- repair branch: open faults, an active repair, or flagged as needing repair
    if open_faults or repair is not None or s in ("REPAIR_REQUIRED", "AWAITING_PARTS"):
        outstanding = _outstanding_lines(repair)
        outstanding_types = {rp.required_part_type_id for rp in outstanding}
        flagged = [f for f in open_faults
                   if f.required_part_type_id and f.required_part_type_id not in outstanding_types
                   and (ctx.failures.get((phone.id, f.required_part_type_id)) or part_failures.FailureInfo(0, "", [])).count
                   >= part_failures.DIAGNOSE_AFTER]
        undiagnosed = [f for f in open_faults if f.required_part_type_id is None] + flagged
        base.update(outstanding=outstanding, undiagnosed=undiagnosed, flagged=flagged)
        if outstanding or undiagnosed or repair is None:
            return Situation(stage="REPAIR", **base)
        return Situation(stage="VERIFY", **base)

    if (s in ("PURCHASED", "AWAITING_TEST") and "TEST" not in ctx.overrides.get(phone.id, set())
            and (untested or not cosmetics.condition_complete(phone))):
        return Situation(stage="TEST", **base)
    if s in ("PURCHASED", "AWAITING_TEST", "TESTED"):
        return Situation(stage="PREP", **base)
    if s == "READY_FOR_LISTING":
        return Situation(stage="LIST", **base)
    return None  # LISTED with nothing wrong, or an unknown status: nothing to do


def derive_ticket(phone: Phone, ctx: Context) -> Optional[TicketSpec]:
    """The one thing this phone needs next, or None if it needs nothing right now."""
    sit = assess(phone, ctx)
    return None if sit is None else spec_for(sit, ctx)


def spec_for(sit: Situation, ctx: Context) -> TicketSpec:
    pid = sit.phone.internal_id
    if sit.stage == "CLOSE":
        return TicketSpec("CLOSE", "📬 Confirm delivery and close the sale", (
            "The phone is on its way. When the buyer has it and the payout has landed:\n"
            "• Check tracking shows **delivered**\n"
            "• Match the payout to the sale (`/sales`)\n"
            "• Keep an eye on the return window\n"
            "Press **Done** when the sale is closed (marks the phone Complete)."), "LOW")
    if sit.stage == "SHIP":
        return TicketSpec("SHIP", "📦 Final check, pack and post", (
            "It has sold. Before it leaves the building:\n"
            "• Confirm payment has cleared and the address matches\n"
            "• Final power-on test, film it working (proof against disputes)\n"
            "• Erase it, sign out of Apple ID, Find My **off**, SIM removed\n"
            "• Pack securely, tracked and insured, upload tracking to eBay\n"
            "Press **Done** once it's posted (marks the phone Shipped)."), "HIGH", due_days=1)
    if sit.stage == "TEST":
        return _test_spec(sit)
    if sit.stage == "REPAIR":
        return _repair_spec(sit, ctx)
    if sit.stage == "VERIFY":
        return _verify_spec(sit)
    if sit.stage == "PREP":
        return TicketSpec("PREP", "🧼 Prep for listing", (
            "Tests are clean. Get it ready to sell:\n"
            "• Clean and disinfect, polish the screen, clear the ports\n"
            "• Check the frame and back glass grades and battery health still match (`/condition`)\n"
            "• Erase all content, confirm no Apple ID and Find My is **off**\n"
            "• Photos: front, back, sides, screen on, battery health, IMEI, every flaw\n"
            f"• Check the price (`/analyse {pid}`)\n"
            "Press **Done** when it's ready to list (marks it Ready for listing)."), "NORMAL", due_days=2)
    return TicketSpec("LIST", "🏷️ List it on eBay", (
        "Ready to sell. The bot builds the listing from the phone's record:\n"
        "• Post the photos here with `/photos` (front, back, screen on, battery health, every flaw)\n"
        "• Press **Build eBay listing**, read it through, fix anything with **Edit listing**\n"
        "• Disclose any non-genuine parts or lock status\n"
        "• Press **Publish to eBay** when you're happy: nothing goes live until you do\n"
        "This closes by itself once the listing goes live."), "NORMAL", due_days=1)


def _test_spec(sit: Situation) -> TicketSpec:
    lines = [f"**Progress:** {sit.recorded}/{sit.total} tests recorded"]
    if sit.untested:
        lines.append(f"**Still to test:** {_names(sit.untested, 12)}")
    missing = cosmetics.missing_condition(sit.phone)
    if missing:
        lines.append(f"**Still to record:** {', '.join(missing)} (graded A*, A, B, C or D per part; battery health "
                     "or 'not applicable'). `/condition` records them.")
    if sit.phone.current_status == "PURCHASED":
        lines.append(
            "**Check-in first:** photos as received, IMEI matches the advert, iCloud / Activation Lock and "
            "MDM clear, matches the listing. Raise any dispute with the seller now.")
    lines.append("A failed test raises a repair ticket by itself; a frame or back glass graded D does too. "
                 "When every test and the condition are recorded this closes.")
    return TicketSpec("TEST", "🧪 Run the tests", "\n".join(lines), "NORMAL", due_days=2)


def _repair_spec(sit: Situation, ctx: Context) -> TicketSpec:
    swap_now: list[str] = []
    waiting: list[str] = []
    waiting_names: list[str] = []
    for rp in sit.outstanding:
        name = part_label(rp, sit.phone)
        for unit in ctx.supply.get(rp.id, []):
            if unit.kind == "RESERVED":
                swap_now.append(f"• **{name}** → `{unit.part.internal_id}` · {_where(unit.part)} (reserved, ready to fit)")
            elif unit.kind == "IN_STOCK":
                swap_now.append(f"• **{name}** → `{unit.part.internal_id}` · {_where(unit.part)} (in stock)")
            elif unit.kind == "ON_ORDER":
                eta = f", due {unit.order.expected_delivery.isoformat()}" if unit.order.expected_delivery else ""
                waiting.append(f"• **{name}** → 📦 on order `{unit.order.internal_id}`{eta}")
                waiting_names.append(f"{name} (on order {unit.order.internal_id})")
            elif unit.kind == "DRAFTED":
                waiting.append(f"• **{name}** → 📝 draft order `{unit.order.internal_id}` not placed yet")
                waiting_names.append(f"{name} (order not placed)")
            else:
                col = f" in **{unit.colour}**" if unit.colour else ""
                waiting.append(f"• **{name}** → ❌ not in stock{col}, **needs ordering**{stock_hint(rp, sit.phone, ctx.available)}")
                waiting_names.append(f"{name}{' ' + unit.colour if unit.colour else ''} (needs ordering)")

    parts: list[str] = []
    if swap_now:
        parts.append("**🔧 Swap now (in stock)**\n" + "\n".join(swap_now))
    if waiting:
        parts.append("**📦 Waiting on parts**\n" + "\n".join(waiting))
    plain = [f for f in sit.undiagnosed if f not in sit.flagged]
    if plain:
        parts.append("**🔍 Needs diagnosis** (no single part fixes these)\n"
                     + "\n".join(f"• {f.description}" for f in plain))
    if sit.flagged:
        lines = []
        for f in sit.flagged:
            info = ctx.failures[(sit.phone.id, f.required_part_type_id)]
            lines.append(f"• **{info.part_type}** has failed {info.count} times here ({', '.join(info.part_ids)}). "
                         f"It's probably not the part: diagnose {f.description.lower()} (board, connector, flex).")
        parts.append("**⚠️ Probably not the part: diagnose it**\n" + "\n".join(lines))
    if sit.repair is None:
        parts.append("No repair planned yet. Plan it from the open faults."
                     if sit.open_faults else
                     "It's flagged as needing repair but no fault is logged. Log the fault, or if it's actually fine, "
                     "change its status.")
    else:
        parts.append(f"Repair `{sit.repair.internal_id}`")
    if sit.recorded and sit.untested:
        parts.append(f"⚠️ {len(sit.untested)} test(s) still not run: {_names(sit.untested, 8)}")

    if swap_now:
        title = f"🔧 Swap parts · {len(swap_now)} now" + (f", {len(waiting)} waiting" if waiting else "")
        waiting_reason = None
    elif waiting:
        title = "📦 Waiting for parts"
        waiting_reason = "Waiting for: " + ", ".join(waiting_names)
    elif sit.undiagnosed:
        title, waiting_reason = "🔍 Diagnose the fault", None
    else:
        title, waiting_reason = "🔧 Plan the repair", None
    return TicketSpec("REPAIR", title, "\n\n".join(parts), "NORMAL", waiting_reason, due_days=3)


def retest_definitions(sit: Situation) -> list[TestDefinition]:
    """The tests that failed and still need re-running (their fault is open)."""
    ids = {f.test_definition_id for f in sit.open_faults if f.test_definition_id}
    return [d for d in sit.failed if d.id in ids] or list(sit.failed)


def _verify_spec(sit: Situation) -> TicketSpec:
    retest = retest_definitions(sit)
    lines = ["**Every part is fitted.** Now prove it works:"]
    lines.append("• Re-run: " + _names(retest, 10) if retest else "• Re-tests passed ✅")
    lines += [
        "• Soak test 30+ minutes (heat, restarts, battery drain)",
        "• Check Settings → General → About → Parts and Service History for warnings",
        "• Then complete the repair, which marks the phone Ready for listing.",
    ]
    return TicketSpec("VERIFY", "✅ Re-test and close the repair", "\n".join(lines), "NORMAL", due_days=1)


# ====================================================================== loading + syncing
async def load_phones(session: AsyncSession, only_phone_id: Optional[int] = None) -> list[Phone]:
    """Phones that could need a ticket. LISTED phones with nothing wrong are
    skipped (they are the bulk of a healthy stock and need no action)."""
    open_ticket = exists().where(Task.phone_id == Phone.id, Task.status.in_(OPEN_TASK_STATUSES))
    open_fault = exists().where(PhoneFault.phone_id == Phone.id, PhoneFault.status == FaultStatus.OPEN)
    stmt = select(Phone).where(Phone.current_status.not_in(TERMINAL_STATUSES),
                               or_(Phone.current_status != "LISTED", open_fault, open_ticket))
    if only_phone_id is not None:
        stmt = stmt.where(Phone.id == only_phone_id)
    stmt = stmt.order_by(Phone.id).execution_options(populate_existing=True)  # never trust stale in-session state
    return list((await session.execute(stmt)).scalars().all())


async def _drafts_by_phone(session: AsyncSession, phone_ids: Sequence[int]) -> dict:
    from app.models.commerce_models import Listing
    if not phone_ids:
        return {}
    rows = (await session.execute(
        select(Listing).where(Listing.status == "DRAFT", Listing.phone_id.in_(list(phone_ids))).order_by(Listing.id)
    )).scalars().all()
    return {l.phone_id: l for l in rows}     # newest draft per phone wins


async def _offer_terms(session: AsyncSession, phones: Sequence[Phone], drafts: dict) -> dict:
    from app.services import listing_builder
    out = {}
    for p in phones:
        if p.id in drafts:
            try:
                out[p.id] = await listing_builder.best_offer_terms(session, drafts[p.id], p)
            except config_service.UnknownPhoneModelError:
                pass
    return out


async def load_context(session: AsyncSession, phones: Sequence[Phone]) -> Context:
    for phone in phones:  # older phones have no catalog link yet: link them if their model text is exact
        if phone.phone_model_id is None:
            try:
                await config_service.phone_model_for(session, phone)
            except config_service.UnknownPhoneModelError:
                pass  # stays unmatched: nothing will be offered for it until the model is corrected
    overrides: dict[int, set[str]] = {}
    for o in (await session.execute(select(PhoneTicketOverride))).scalars().all():
        overrides.setdefault(o.phone_id, set()).add(o.stage)
    available = await _available_parts(session)
    return Context(
        definitions=await config_service.list_test_definitions(session),
        overrides=overrides,
        supply=allocate_supply(phones, available, await _orders_by_part_type(session)),
        available=available,
        failures=await part_failures.failure_info(session),
        drafts=(drafts := await _drafts_by_phone(session, [p.id for p in phones])),
        offer_terms=await _offer_terms(session, phones, drafts),
    )


@dataclass
class SyncStats:
    opened: int = 0
    closed: int = 0
    updated: int = 0
    notes: list[str] = field(default_factory=list)
    # Handed to the job-channel panels so they are built from the same data as the tickets.
    situations: dict[int, Situation] = field(default_factory=dict)
    ctx: Optional[Context] = None

    @property
    def changed(self) -> bool:
        return bool(self.opened or self.closed or self.updated)


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


async def _close(session: AsyncSession, task: Task, note: str, actor: Optional[int] = None) -> None:
    task.status = "DONE"
    task.system_blocked = False
    task.completed_at = _now()
    task.completed_by_discord_id = actor
    line = f"{dt.date.today().isoformat()}: {note}"
    task.notes = f"{task.notes}\n{line}" if task.notes else line
    tasks_service.bump(task)
    await session.flush()
    await log_event(session, event_type="PHONE_TICKET_CLOSED", entity_type="TASK", entity_id=task.internal_id,
                    actor_discord_id=actor, new_state={"stage": task.stage}, notes=note)


async def _open(session: AsyncSession, phone: Phone, spec: TicketSpec, job: Optional[JobChannel] = None) -> Optional[Task]:
    # A phone that already has a job channel is already being worked on, so its
    # next step's ticket opens "in progress" rather than "to do".
    status = "BLOCKED" if spec.waiting_reason else ("IN_PROGRESS" if job else "TODO")
    try:
        async with session.begin_nested():
            task = await tasks_service.new_task(
                session, title=spec.title, description=spec.body, category=STAGE_CATEGORY[spec.stage],
                scope="PHONE", priority=spec.priority, phone_id=phone.id, stage=spec.stage,
                status=status, assignee_discord_id=job.created_by_discord_id if job else None,
                blocked_reason=spec.waiting_reason, system_blocked=bool(spec.waiting_reason),
                due_date=(dt.date.today() + dt.timedelta(days=spec.due_days)) if spec.due_days else None,
                content_hash=spec.content_hash(),
            )
        return task
    except IntegrityError:
        return None  # another writer opened it first: the one-ticket-per-phone index held


def _apply_spec(task: Task, spec: TicketSpec, job: Optional[JobChannel] = None) -> bool:
    """Bring an open ticket in line with its spec. Returns True if anything changed."""
    changed = False
    if task.content_hash != spec.content_hash():
        task.title, task.description, task.priority = spec.title, spec.body, spec.priority
        task.content_hash = spec.content_hash()
        changed = True
    if spec.waiting_reason:
        if task.status in ("TODO", "IN_PROGRESS") and not (task.status == "BLOCKED"):
            task.status, task.blocked_reason, task.system_blocked = "BLOCKED", spec.waiting_reason, True
            changed = True
        elif task.system_blocked and task.blocked_reason != spec.waiting_reason:
            task.blocked_reason = spec.waiting_reason
            changed = True
    elif task.system_blocked:  # parts arrived: release it
        task.status, task.blocked_reason, task.system_blocked = ("IN_PROGRESS" if job else "TODO"), None, False
        changed = True
    if changed:
        tasks_service.bump(task)
    return changed


async def sync_phone_tickets(session: AsyncSession, only_phone_id: Optional[int] = None) -> SyncStats:
    stats = SyncStats()
    phones = await load_phones(session, only_phone_id)
    # Supply must be allocated across every phone, even when we only sync one.
    all_phones = phones if only_phone_id is None else await load_phones(session)
    ctx = await load_context(session, all_phones)

    stmt = select(Task).where(Task.phone_id.is_not(None), Task.status.in_(OPEN_TASK_STATUSES))
    if only_phone_id is not None:
        stmt = stmt.where(Task.phone_id == only_phone_id)
    open_tasks: dict[int, Task] = {t.phone_id: t for t in (await session.execute(stmt)).scalars().all()}
    jobs: dict[int, JobChannel] = {j.phone_id: j for j in (await session.execute(select(JobChannel))).scalars().all()}
    stats.ctx = ctx

    seen: set[int] = set()
    for phone in phones:
        seen.add(phone.id)
        sit = assess(phone, ctx)
        spec = spec_for(sit, ctx) if sit else None
        if sit:
            stats.situations[phone.id] = sit
        task = open_tasks.get(phone.id)
        if spec is None:
            if task:
                await _close(session, task, "step finished")
                stats.closed += 1
            continue
        if task and task.stage != spec.stage:
            await _close(session, task, f"{task.stage} finished")
            stats.closed += 1
            task = None
        if task is None:
            if await _open(session, phone, spec, jobs.get(phone.id)):
                stats.opened += 1
        elif _apply_spec(task, spec, jobs.get(phone.id)):
            stats.updated += 1

    # Phones that are no longer loaded (completed, scrapped, or listed and healthy)
    for phone_id, task in open_tasks.items():
        if phone_id not in seen:
            await _close(session, task, "phone no longer needs attention")
            stats.closed += 1
    await session.flush()
    return stats


# ====================================================================== the Done button
async def preview_manual_complete(session: AsyncSession, phone: Phone, stage: str) -> str:
    """What pressing Done would do, in plain words (shown before confirming)."""
    if stage == "TEST":
        return "Skip/finish testing. It will be marked **Tested** (if no faults are open)."
    if stage == "REPAIR":
        items = await swap_now_items(session, phone)
        if not items:
            return "Nothing can be fitted yet: no reserved or in-stock parts for this repair."
        return "Mark these as **fitted and working** (reserving them first if needed):\n" + "\n".join(
            f"• {part_label(rp, phone)} → `{part.internal_id}`" for rp, part in items)
    if stage == "VERIFY":
        return "Complete the repair and mark the phone **Ready for listing**."
    if stage == "PREP":
        return "Mark the phone **Ready for listing**."
    if stage == "LIST":
        return "Mark the phone **Listed** (no eBay listing record is created, use `/list` for that)."
    if stage == "SHIP":
        return "Mark the phone **Shipped**."
    if stage == "CLOSE":
        return "Mark the phone **Complete**."
    raise PhoneTicketError(f"Unknown step {stage}.")


async def swap_now_items(session: AsyncSession, phone: Phone) -> list[tuple[RepairPart, Part]]:
    phones = await load_phones(session)
    if phone.id not in {p.id for p in phones}:
        phones.append(phone)
    ctx = await load_context(session, phones)
    out = []
    for rp in _outstanding_lines(_active_repair(phone)):
        for unit in ctx.supply.get(rp.id, []):
            if unit.kind in ("RESERVED", "IN_STOCK"):
                out.append((rp, unit.part))
    return out


async def manual_complete(session: AsyncSession, phone: Phone, stage: str, actor_discord_id: int) -> str:
    """The Done button: do the real action that finishes this step, through the
    same services the slash commands use (so history and costs stay correct)."""
    if stage == "TEST":
        existing = (await session.execute(select(PhoneTicketOverride).where(
            PhoneTicketOverride.phone_id == phone.id, PhoneTicketOverride.stage == "TEST"))).scalar_one_or_none()
        if existing is None:
            session.add(PhoneTicketOverride(phone_id=phone.id, stage="TEST", created_by_discord_id=actor_discord_id))
        if phone.current_status in ("PURCHASED", "AWAITING_TEST") and not any(
                f.status == FaultStatus.OPEN for f in phone.faults):
            await phones_service.set_status(session, phone, "TESTED", actor_discord_id, notes="Testing marked done from ticket")
        msg = "Testing marked done."
    elif stage == "REPAIR":
        items = await swap_now_items(session, phone)
        if not items:
            raise PhoneTicketError("Nothing in stock can be fitted yet. Order the missing parts first.")
        for rp, part in items:
            if part.status == "AVAILABLE":
                await parts_service.reserve_part(session, part, rp.repair, rp, actor_discord_id)
            await parts_service.install_part(session, part, phone, rp, actor_discord_id)
            await part_failures.confirm_working(session, part, phone, actor_discord_id)
        msg = f"Fitted and confirmed working: " + ", ".join(p.internal_id for _, p in items)
    elif stage == "VERIFY":
        repair = _active_repair(phone)
        if repair is None:
            raise PhoneTicketError("There is no active repair to complete.")
        try:
            await repairs_service.complete_repair(session, repair, actor_discord_id)
        except repairs_service.RepairError as e:
            raise PhoneTicketError(str(e))
        if phone.current_status not in ("SOLD", "SHIPPED", "COMPLETE"):
            await phones_service.set_status(session, phone, "READY_FOR_LISTING", actor_discord_id)
        msg = "Repair completed; phone is Ready for listing."
    elif stage == "PREP":
        await phones_service.set_status(session, phone, "READY_FOR_LISTING", actor_discord_id, notes="Prep done (ticket)")
        msg = "Marked Ready for listing."
    elif stage == "LIST":
        await phones_service.set_status(session, phone, "LISTED", actor_discord_id, notes="Marked listed (ticket)")
        msg = "Marked Listed."
    elif stage == "SHIP":
        await phones_service.set_status(session, phone, "SHIPPED", actor_discord_id, notes="Posted (ticket)")
        msg = "Marked Shipped."
    elif stage == "CLOSE":
        await phones_service.set_status(session, phone, "COMPLETE", actor_discord_id, notes="Closed (ticket)")
        msg = "Marked Complete."
    else:
        raise PhoneTicketError(f"Unknown step {stage}.")
    await session.flush()
    await sync_phone_tickets(session, only_phone_id=phone.id)
    return msg
