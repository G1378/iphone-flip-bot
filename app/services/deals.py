"""Buy-page deals and the part finder: let eBay and Vinted come to you.

The deal scanner looks at live listings for the models you watch (`/pricing watch`) and puts the
ones you could buy AND still make your target profit on onto the buy page. A deal only appears if
ALL of these hold:

  * the listing TITLE names exactly the watched model ("iPhone 13" never matches "iPhone 13 Pro");
  * it is not iCloud / activation-lock / blacklisted / MDM (we can't price those);
  * everything you would hand over (price + postage + Vinted's buyer fee) is under your maximum
    for that model: resale value, minus selling fees, postage, packaging, the profit you require
    and, if it is faulty, the estimated repair (see repair_estimate.py);
  * the resulting profit is at least your minimum ROI (`min_roi_pct`), not just `min_profit_gbp`;
  * it is not a fault we can't price (water damage, no power, Face ID, logic board);
  * it has not been announced before.

Buying stays your decision: nothing is ever bought for you. Each deal is a card with Bought / Reject
buttons (see bot/deal_cards.py and services/deal_queue.py).
"""
from __future__ import annotations

import asyncio
import datetime as dt
import random
import re
from dataclasses import dataclass, field
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal
from typing import Any, Awaitable, Callable, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.integrations.ebay.interface import EbayApiError, EbayClientInterface, EbayItem
from app.integrations.vinted.interface import (
    VintedApiError, VintedBlockedError, VintedClientInterface, VintedItem, VintedNotConfiguredError,
)
from app.models.config_models import PhoneModelConfig
from app.models.enums import DealSource, DealStatus
from app.models.pricing_models import DealAlert
from app.services import config_service
from app.services import pricing as pricing_service
from app.services import repair_estimate

PHONES_CATEGORY = "9355"          # eBay "Mobile Phones & Smartphones"
MAX_ALERTS_PER_SCAN = 10          # a flood helps nobody: the rest are found on the next scan
TWO = Decimal("0.01")

# Listings we can't value: parts-only at best, and a classic way to lose money.
RISK_WORDS = ("icloud", "activation lock", "blacklist", "black list", "blocked", "barred", "mdm", "stolen",
              "lost mode", "apple id locked", "imei issue")
FAULT_WORDS = ("faulty", "spares", "repair", "not working", "broken", "cracked", "smashed", "dead",
               "no power", "for parts", "damaged", "untested")
NETWORK_LOCK_WORDS = ("locked to", "network locked", "ee locked", "o2 locked", "vodafone locked", "three locked")
NOT_A_PHONE_WORDS = ("case", "protector", "cover", "charger", "cable", "holder", "stand", "sticker", "skin", "dummy")


def _tokens(text: str) -> list[str]:
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).split()


def title_matches_model(title: str, phone_model: PhoneModelConfig) -> bool:
    """True only if the title names exactly this model. 'iPhone 13' must not match a title that
    goes on to say Pro/Max/Plus/mini after the 13, which is a different (cheaper or dearer) phone."""
    model_tokens = [t for t in _tokens(f"{phone_model.model} {phone_model.variant or ''}") if t not in ("iphone", "apple")]
    toks, n = _tokens(title), len(model_tokens)
    if not n:
        return False
    hits = [i for i in range(len(toks) - n + 1) if toks[i:i + n] == model_tokens]
    if not hits:
        return False
    for i in hits:
        nxt = toks[i + n] if i + n < len(toks) else None
        if nxt in pricing_service.SIBLING_WORDS:
            return False
    return True


def title_has_storage(title: str, storage: Optional[str]) -> bool:
    if not storage:
        return True
    m = re.fullmatch(r"\s*(\d+)\s*(gb|tb)\s*", storage, re.I)
    if not m:
        return storage.lower() in (title or "").lower()
    toks = _tokens(title)
    num, unit = m.group(1), m.group(2).lower()
    return f"{num}{unit}" in toks or any(toks[i] == num and toks[i + 1] == unit for i in range(len(toks) - 1))


# "no iCloud lock", "not blacklisted", "iCloud free", "clean IMEI": sellers saying it is FINE. Without this,
# every honest Vinted description would be thrown out as a lock risk.
_CLEAN_CLAIM = re.compile(
    r"\b(?:no|not|without|isn'?t|is\s+not|free\s+of|removed)\s+(?:\w+\s+){0,2}?"
    r"(?:icloud|activation\s+lock|blacklist(?:ed)?|black\s+list(?:ed)?|blocked|barred|mdm|lost\s+mode|stolen|apple\s+id\s+locked)\b"
    r"|\b(?:icloud|activation\s+lock|mdm)\s+(?:free|clear|clean|removed|off|unlocked)\b|\bun(?:blocked|barred)\b|\b(?:clean|clear)\s+imei\b")


def _neutralise(text: str) -> tuple[str, bool]:
    """Lower-case the text and cut out denials ("no cracks", "no iCloud lock"). Also says whether the
    seller claimed the phone is clear of locks, which is worth a note but never proof."""
    low = (text or "").lower().replace("’", "'")
    claimed_clear = bool(_CLEAN_CLAIM.search(low))
    low = _CLEAN_CLAIM.sub(" ", low)
    low = repair_estimate._NEGATED_FAULT.sub(" ", low)
    return low, claimed_clear


def classify(item: Any, description: Optional[str] = None) -> tuple[str, list[str]]:
    """('USED' | 'FAULTY' | 'RISK', notes). RISK listings are never priced or announced.
    Works on anything with .title/.condition/.condition_id (an EbayItem or a MarketListing)."""
    desc = description if description is not None else getattr(item, "description", None)
    low, claimed_clear = _neutralise(f"{item.title} {item.condition or ''} {desc or ''}")
    if any(w in low for w in RISK_WORDS):
        return "RISK", ["iCloud / blacklist / lock risk"]
    notes = []
    if any(w in low for w in NETWORK_LOCK_WORDS):
        notes.append("network locked: sells for less")
    if claimed_clear:
        notes.append("seller says no iCloud/lock/blacklist: still verify (Find My, Settings) before paying")
    if getattr(item, "condition_id", None) == "7000" or any(w in low for w in FAULT_WORDS):
        return "FAULTY", notes
    return "USED", notes


def all_in_cost(item: EbayItem, assumed_postage: Decimal) -> Decimal:
    ship = item.shipping_cost if item.shipping_cost is not None else assumed_postage
    return (item.price + ship).quantize(TWO, rounding=ROUND_HALF_UP)


# ---------------------------------------------------------------- one listing, whatever the source
VINTED_PREFIX = "vinted:"


@dataclass
class MarketListing:
    """A listing from any marketplace, in the one shape the decision code understands."""
    source: str                               # DealSource value
    listing_id: str                           # the key stored in DealAlert.ebay_item_id
    title: str
    price: Decimal
    url: str
    shipping: Optional[Decimal] = None        # None = the site didn't say
    buyer_fee: Optional[Decimal] = None       # Vinted Buyer Protection, when stated
    description: Optional[str] = None
    condition: Optional[str] = None
    condition_id: Optional[str] = None
    seller: Optional[str] = None
    seller_feedback_pct: Optional[Decimal] = None
    seller_reviews: Optional[int] = None
    image_url: Optional[str] = None
    is_auction: bool = False
    ends_at: Optional[dt.datetime] = None
    raw: Any = None

    @property
    def text(self) -> str:
        return f"{self.title} {self.condition or ''} {self.description or ''}"


def listing_from_ebay(item: EbayItem) -> MarketListing:
    return MarketListing(
        source=DealSource.EBAY.value, listing_id=item.item_id, title=item.title, price=item.price, url=item.url,
        shipping=item.shipping_cost, condition=item.condition, condition_id=item.condition_id, seller=item.seller,
        seller_feedback_pct=item.seller_feedback_pct, image_url=item.image_url, is_auction=item.is_auction,
        ends_at=item.ends_at, raw=item,
        description=getattr(item, "description", None),
    )


def listing_from_vinted(item: VintedItem) -> MarketListing:
    return MarketListing(
        source=DealSource.VINTED.value, listing_id=f"{VINTED_PREFIX}{item.item_id}", title=item.title, price=item.price,
        url=item.url, buyer_fee=item.buyer_fee, description=item.description, condition=item.condition, seller=item.seller,
        seller_feedback_pct=item.seller_feedback_pct, seller_reviews=item.seller_reviews, image_url=item.image_url, raw=item,
    )


# ---------------------------------------------------------------- the economics of one listing
@dataclass
class Economics:
    resale: Decimal
    sale_costs: Decimal
    repair: Decimal
    max_buy: Decimal            # most you could pay all in (price + postage + fee), for THIS listing
    total: Decimal              # what it would actually cost you all in
    profit: Decimal             # what you'd make after selling costs and repair
    roi_pct: Decimal            # profit / (all-in + repair)
    max_item_price: Decimal     # the most to pay for the phone itself: what to offer


@dataclass
class _Ctx:
    now: dt.datetime
    assumed_postage: Decimal
    auction_hours: Decimal
    min_roi: Decimal
    suspicious_pct: Decimal
    fee_pct: Decimal
    fee_fixed: Decimal
    repair: repair_estimate.RepairSettings
    detail_left: int = 0        # Vinted listing-page fetches still allowed this scan


async def _load_ctx(session: AsyncSession, now: dt.datetime, detail_lookups: int = 0) -> _Ctx:
    get = config_service.get_setting_decimal
    return _Ctx(
        now=now,
        assumed_postage=await get(session, "default_shipping_in_cost", Decimal("3.50")),
        auction_hours=await get(session, "deal_auction_alert_hours", Decimal("6")),
        min_roi=await get(session, "min_roi_pct", Decimal("15")),
        suspicious_pct=await get(session, "deal_suspicious_pct", Decimal("50")),
        fee_pct=await get(session, "vinted_buyer_fee_pct", Decimal("5")),
        fee_fixed=await get(session, "vinted_buyer_fee_fixed", Decimal("0.70")),
        repair=await repair_estimate.load_settings(session),
        detail_left=detail_lookups,
    )


def _costs(listing: MarketListing, ctx: _Ctx) -> tuple[Decimal, Decimal, Decimal, list[str]]:
    """(all-in total, postage, buyer fee, notes): what you would hand over for this listing."""
    notes: list[str] = []
    postage = listing.shipping
    if postage is None:
        postage = ctx.assumed_postage
        notes.append(f"postage not shown: assumed £{ctx.assumed_postage}" if listing.source == DealSource.EBAY.value
                     else f"postage assumed £{ctx.assumed_postage}: Vinted shows the real one at checkout")
    fee = Decimal("0")
    if listing.source == DealSource.VINTED.value:
        if listing.buyer_fee is not None:
            fee = listing.buyer_fee
        else:
            fee = (listing.price * ctx.fee_pct / Decimal("100") + ctx.fee_fixed).quantize(TWO, rounding=ROUND_HALF_UP)
            notes.append(f"Vinted Buyer Protection estimated at {ctx.fee_pct.normalize():f}% + £{ctx.fee_fixed}: check at checkout")
    total = (listing.price + postage + fee).quantize(TWO, rounding=ROUND_HALF_UP)
    return total, postage, fee, notes


def _max_item_price(listing: MarketListing, max_buy: Decimal, postage: Decimal, ctx: _Ctx) -> Decimal:
    """The most to offer for the phone itself so that item + postage (+ Vinted's fee on it) stays within max_buy."""
    if listing.source == DealSource.VINTED.value:
        room = (max_buy - postage - ctx.fee_fixed) / (Decimal("1") + ctx.fee_pct / Decimal("100"))
    else:
        room = max_buy - postage
    return max(Decimal("0"), room).quantize(TWO, rounding=ROUND_DOWN)


# ---------------------------------------------------------------- scanning
_SCAN_LOCKS: dict[str, asyncio.Lock] = {}


def scan_lock(source: str) -> asyncio.Lock:
    """Hold this around `async with session_scope(): scan_...()` (scheduler AND /deal-scan do). Two overlapping
    scans of one source would each see a listing as new and both try to insert it. The lock must wrap the session
    so it is only released after the first scan has COMMITTED."""
    return _SCAN_LOCKS.setdefault(source, asyncio.Lock())


@dataclass
class DealCandidate:
    alert: DealAlert
    item: Any                    # the EbayItem / VintedItem it came from
    under_by: Decimal            # how far below your maximum (£)


@dataclass
class ScanResult:
    watched: int = 0
    checked: int = 0
    candidates: list[DealCandidate] = field(default_factory=list)
    skipped: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    blocked: bool = False        # the source refused us: the scan stopped early (Vinted)

    def skip(self, why: str) -> None:
        self.skipped[why] = self.skipped.get(why, 0) + 1


@dataclass
class _Verdict:
    skip: Optional[str] = None
    bucket: str = "USED"
    notes: list[str] = field(default_factory=list)
    postage: Decimal = Decimal("0")
    buyer_fee: Decimal = Decimal("0")
    econ: Optional[Economics] = None
    estimate: Optional[repair_estimate.RepairEstimate] = None


async def _judge(session: AsyncSession, ctx: _Ctx, entry, rec, listing: MarketListing) -> _Verdict:
    """Should this listing go on the buy page? Pure decision: no side effects."""
    model = entry.phone_model
    if any(w in _tokens(listing.title) for w in NOT_A_PHONE_WORDS):
        return _Verdict(skip="not a phone")
    if not title_matches_model(listing.title, model) or not title_has_storage(listing.title, entry.storage):
        return _Verdict(skip="wrong model/storage in title")
    bucket, notes = classify(listing)
    if bucket == "RISK":
        return _Verdict(skip="lock/blacklist risk")

    text = listing.text
    faults = repair_estimate.scan_faults(text)
    if faults.board_reason:
        return _Verdict(skip="can't price the repair (water / no power / Face ID / board fault)")
    if bucket == "USED" and faults.parts:
        # "Good condition, battery health 74%": the listing admits a fault, so price it as a repair.
        bucket = "FAULTY"
        notes.append("listing mentions: " + ", ".join(p.lower() for p in faults.parts))

    if rec.resale_price is None or rec.recommended_buy_used is None:
        return _Verdict(skip="no price data yet for this model")
    estimate = None
    repair_cost = Decimal("0")
    if bucket == "FAULTY":
        estimate = await repair_estimate.estimate_repair(session, model, text, rec.repair_cost_assumption, ctx.repair)
        repair_cost = estimate.cost
    max_buy = (rec.recommended_buy_used - repair_cost).quantize(TWO, rounding=ROUND_HALF_UP)
    if max_buy <= 0:
        return _Verdict(skip="not worth buying at today's prices")

    if listing.is_auction and not (listing.ends_at and ctx.now <= listing.ends_at <= ctx.now + dt.timedelta(hours=float(ctx.auction_hours))):
        return _Verdict(skip="auction not ending soon")

    total, postage, fee, cost_notes = _costs(listing, ctx)
    if total > max_buy:
        return _Verdict(skip="over your maximum")

    sale_costs = rec.sale_costs or Decimal("0")
    profit = (rec.resale_price - sale_costs - total - repair_cost).quantize(TWO, rounding=ROUND_HALF_UP)
    invested = total + repair_cost
    roi = (profit / invested * Decimal("100")).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP) if invested > 0 else Decimal("0")
    if roi < ctx.min_roi:
        return _Verdict(skip="margin too thin (under your minimum ROI)")

    notes += cost_notes
    if bucket == "FAULTY" and estimate is not None:
        notes.append(f"repair £{estimate.cost}: {estimate.basis}")
    if listing.seller_feedback_pct is not None and listing.seller_feedback_pct < Decimal("95"):
        notes.append(f"seller feedback only {listing.seller_feedback_pct}%")
    if listing.source == DealSource.VINTED.value and listing.seller_reviews is not None and listing.seller_reviews < 3:
        notes.append(f"seller has only {listing.seller_reviews} review{'s' if listing.seller_reviews != 1 else ''}")
    if bucket == "USED" and rec.resale_price > 0 and total < rec.resale_price * ctx.suspicious_pct / Decimal("100"):
        notes.append(f"priced under {ctx.suspicious_pct.normalize():f}% of what it sells for: scam, lock or hidden damage until proven otherwise")
    if listing.is_auction:
        notes.append("auction: the bid may rise")
    if rec.resale_is_estimate:
        notes.append("resale value is from asking prices, not sold prices")

    econ = Economics(resale=rec.resale_price, sale_costs=sale_costs, repair=repair_cost, max_buy=max_buy, total=total,
                     profit=profit, roi_pct=roi, max_item_price=_max_item_price(listing, max_buy, postage, ctx))
    return _Verdict(bucket=bucket, notes=notes, postage=postage, buyer_fee=fee, econ=econ, estimate=estimate)


def _make_alert(listing: MarketListing, entry, rec, v: _Verdict) -> DealAlert:
    e = v.econ
    return DealAlert(
        ebay_item_id=listing.listing_id, source=listing.source, status=DealStatus.PENDING.value,
        phone_model=entry.phone_model,   # set the object, not just the id: reading it later must never need a lazy load
        phone_model_id=entry.phone_model.id, storage=entry.storage, title=listing.title[:300], bucket=v.bucket,
        price=listing.price, postage=v.postage, buyer_fee=v.buyer_fee, max_buy=e.max_buy, max_item_price=e.max_item_price,
        url=listing.url[:500], image_url=(listing.image_url or None) and listing.image_url[:500], seller=listing.seller,
        seller_feedback_pct=listing.seller_feedback_pct, seller_reviews=listing.seller_reviews,
        is_auction=listing.is_auction, ends_at=listing.ends_at, flags="; ".join(v.notes)[:600] or None,
        resale_estimate=e.resale, resale_basis="eBay asking prices" if rec.resale_is_estimate else "recent eBay sales",
        sale_costs=e.sale_costs, repair_estimate=e.repair, repair_basis=(v.estimate.basis[:400] if v.estimate else None),
        expected_profit=e.profit, roi_pct=e.roi_pct,
    )


async def _scan(
    session: AsyncSession, ctx: _Ctx, *,
    search: Callable[[Any, Any], Awaitable[list]],
    to_listing: Callable[[Any], MarketListing],
    describe: Optional[Callable[[MarketListing], Awaitable[Optional[str]]]] = None,
    soft_errors: tuple = (EbayApiError,),
    hard_errors: tuple = (),
    shuffle: bool = False,
    skip_entries_without_price: bool = False,
) -> ScanResult:
    known = set((await session.execute(select(DealAlert.ebay_item_id))).scalars().all())
    result = ScanResult()
    found: list[DealCandidate] = []
    entries = list(await pricing_service.list_watchlist(session))
    if shuffle:
        random.shuffle(entries)          # a different order each scan: no fixed pattern for the site to learn

    for entry in entries:
        result.watched += 1
        model = entry.phone_model
        rec = await pricing_service.compute_recommendation(session, entry)
        if skip_entries_without_price and (rec.recommended_buy_used is None or rec.recommended_buy_used <= 0):
            result.skip("no price data yet for this model")      # don't spend a request on a model we can't judge
            continue
        try:
            raws = await search(entry, rec)
            for raw in raws:
                listing = to_listing(raw)
                result.checked += 1
                if listing.listing_id in known:
                    result.skip("already announced")
                    continue
                v = await _judge(session, ctx, entry, rec, listing)
                if v.skip is None and describe is not None and listing.description is None and ctx.detail_left > 0:
                    # Looks like a deal on the title alone. Read the seller's description before we trust that:
                    # it can only make the deal worse (a fault, a lock), never better.
                    ctx.detail_left -= 1
                    listing.description = await describe(listing)
                    if listing.description:
                        v = await _judge(session, ctx, entry, rec, listing)
                if v.skip is not None:
                    result.skip(v.skip)
                    continue
                if describe is not None and not listing.description:
                    v.notes.append("description not read: check faults, battery and iCloud in the listing before buying")
                found.append(DealCandidate(_make_alert(listing, entry, rec, v), listing.raw, (v.econ.max_buy - v.econ.total).quantize(TWO)))
                known.add(listing.listing_id)
        except hard_errors as e:
            result.errors.append(f"{model.model}: {e}")
            result.blocked = True
            break
        except soft_errors as e:
            result.errors.append(f"{model.model}: {e}")
            continue

    found.sort(key=lambda c: c.under_by, reverse=True)
    for c in found[:MAX_ALERTS_PER_SCAN]:
        session.add(c.alert)
        result.candidates.append(c)
    for c in found[MAX_ALERTS_PER_SCAN:]:
        result.skip("held back (too many at once): next scan")
    await session.flush()
    return result


async def scan_deals(session: AsyncSession, ebay: EbayClientInterface,
                     now: Optional[dt.datetime] = None) -> ScanResult:
    """One pass over every watched model on eBay. Returns (and records) listings worth a look."""
    ctx = await _load_ctx(session, now or dt.datetime.now(dt.timezone.utc))

    async def search(entry, rec):
        return await ebay.search_items(pricing_service.build_search_query(entry.phone_model, entry.storage),
                                       category_id=PHONES_CATEGORY, limit=50)

    return await _scan(session, ctx, search=search, to_listing=listing_from_ebay)


def vinted_query(model: PhoneModelConfig, storage: Optional[str]) -> str:
    """Vinted search is plain keywords (no minus-terms), so siblings are weeded out by the title check afterwards."""
    return " ".join(b for b in (model.model, storage) if b)


async def scan_vinted(session: AsyncSession, vinted: VintedClientInterface,
                      now: Optional[dt.datetime] = None) -> ScanResult:
    """One pass over every watched model on Vinted. Slow on purpose: see integrations/vinted/client.py.
    Stops (result.blocked) the moment Vinted says no, rather than pushing on."""
    if not vinted.configured:
        raise VintedNotConfiguredError("Vinted scanning is not enabled.")
    ctx = await _load_ctx(session, now or dt.datetime.now(dt.timezone.utc), settings.vinted_max_detail_lookups_per_scan)

    async def search(entry, rec):
        # No all-in price can be below the item price, so nothing above the working-phone maximum can ever qualify.
        return await vinted.search_items(vinted_query(entry.phone_model, entry.storage), max_price=rec.recommended_buy_used,
                                         limit=settings.vinted_results_per_query)

    async def describe(listing: MarketListing) -> Optional[str]:
        return await vinted.get_description(listing.listing_id[len(VINTED_PREFIX):])

    return await _scan(session, ctx, search=search, to_listing=listing_from_vinted, describe=describe,
                       soft_errors=(VintedApiError,), hard_errors=(VintedBlockedError,), shuffle=True,
                       skip_entries_without_price=True)


def format_alert(c: DealCandidate, now: Optional[dt.datetime] = None) -> str:
    """Plain-text version of a deal (used when there is no buy-page channel to post a card in)."""
    a = c.alert
    now = now or dt.datetime.now(dt.timezone.utc)
    total = a.total_cost
    how = "fixed price"
    if a.is_auction and a.ends_at:
        mins = max(0, int((a.ends_at - now).total_seconds() // 60))
        how = f"auction · ends in {mins // 60}h {mins % 60:02d}m · current bid, it may rise"
    fee = f" + £{a.buyer_fee} buyer fee" if a.buyer_fee else ""
    lines = [
        f"🛒 **Deal: {a.phone_model.model}{' ' + a.storage if a.storage else ''}** · {a.bucket.lower()} · {a.source.title()}",
        f"{a.title}",
        f"£{a.price} + £{a.postage} postage{fee} = **£{total}** · your max £{a.max_buy} (**£{c.under_by} under**)",
        f"{how} · seller {a.seller or '?'}" + (f" ({a.seller_feedback_pct}%)" if a.seller_feedback_pct is not None else ""),
    ]
    if a.expected_profit is not None:
        lines.append(f"expected profit **£{a.expected_profit}** · ROI {a.roi_pct}%")
    if a.flags:
        lines.append(f"⚠️ {a.flags}")
    lines.append(a.url)
    return "\n".join(lines)


async def recent_alerts(session: AsyncSession, limit: int = 10, status: Optional[str] = None) -> list[DealAlert]:
    stmt = select(DealAlert).order_by(DealAlert.id.desc()).limit(limit)
    if status:
        stmt = stmt.where(DealAlert.status == status)
    return list((await session.execute(stmt)).scalars().all())


# ---------------------------------------------------------------- part finder
PART_WORDS = {
    "screen": ("screen", "lcd", "display", "oled", "digitiser", "digitizer"),
    "battery": ("battery",),
    "charging port": ("charging", "charger port", "dock", "port"),
    "back glass": ("back glass", "rear glass", "back cover", "housing"),
    "rear camera": ("rear camera", "back camera", "main camera"),
    "front camera": ("front camera", "selfie camera"),
    "speaker": ("speaker", "earpiece", "loudspeaker"),
}
NOT_A_PART_WORDS = ("case", "protector", "tempered", "film", "wallet", "stand")


@dataclass
class PartOffer:
    item: EbayItem
    total: Decimal
    notes: list[str]


def _part_keywords(part_type_name: str) -> tuple[str, ...]:
    key = part_type_name.strip().lower()
    for name, words in PART_WORDS.items():
        if name in key or key in name:
            return words
    return tuple(_tokens(key)) or (key,)


async def find_parts_on_ebay(session: AsyncSession, ebay: EbayClientInterface, part_type_name: str,
                              phone_model: PhoneModelConfig, limit: int = 5) -> list[PartOffer]:
    """Cheapest listings for this part for exactly this model. Only titles that name the model
    (and not a sibling) and the part are kept, since the same hard rule applies as for stock:
    a Pro Max part is not a base-model part."""
    assumed = await config_service.get_setting_decimal(session, "default_shipping_in_cost", Decimal("3.50"))
    query = " ".join([phone_model.model, part_type_name] + pricing_service.sibling_exclusions(phone_model))
    items = await ebay.search_items(query, limit=100, newest_first=False)
    words = _part_keywords(part_type_name)
    offers: list[PartOffer] = []
    for item in items:
        low = item.title.lower()
        toks = _tokens(item.title)
        if not title_matches_model(item.title, phone_model) or not any(w in low for w in words):
            continue
        if any(w in toks for w in NOT_A_PART_WORDS) or item.is_auction:
            continue
        notes = []
        if item.seller_feedback_pct is not None and item.seller_feedback_pct < Decimal("95"):
            notes.append(f"seller feedback {item.seller_feedback_pct}%")
        if item.shipping_cost is None:
            notes.append(f"postage not shown (assumed £{assumed})")
        offers.append(PartOffer(item, all_in_cost(item, assumed), notes))
    offers.sort(key=lambda o: (bool(o.notes and any("feedback" in n for n in o.notes)), o.total))
    return offers[:limit]


def format_part_offers(part_type_name: str, phone_model: PhoneModelConfig, offers: list[PartOffer]) -> str:
    head = f"**{part_type_name} for {phone_model.model}** on eBay (the title says this model; check the photos and compatibility):"
    if not offers:
        return head + "\nNothing found that names exactly this model."
    rows = []
    for o in offers:
        i = o.item
        fb = f" · {i.seller_feedback_pct}%" if i.seller_feedback_pct is not None else ""
        rows.append(f"• **£{o.total}** all in · {i.title[:70]}{fb}" + (f" ⚠️ {'; '.join(o.notes)}" if o.notes else "") + f"\n  {i.url}")
    return head + "\n" + "\n".join(rows)
