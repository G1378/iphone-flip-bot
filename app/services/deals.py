"""Buy alerts and the part finder: let eBay come to you.

The deal scanner looks at live eBay listings for the models you watch (`/pricing watch`) and
tells you about the ones you could buy and still hit your profit rules: the listing's price plus
postage is under your recommended maximum for that model (`/pricing`). Buying stays one tap on
your side; nothing is ever bought for you.

Safety rails, all aimed at the same mistake the model catalog was built to stop (buying or
ordering the wrong model):
  * the listing TITLE must name exactly the watched model: "iPhone 13" never matches
    "iPhone 13 Pro" (or "13/13 Pro" listings that cover both);
  * iCloud / activation-lock / blacklisted / MDM listings are skipped (we can't price them);
  * each listing is announced once.
"""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.integrations.ebay.interface import EbayApiError, EbayClientInterface, EbayItem
from app.models.config_models import PhoneModelConfig
from app.models.pricing_models import DealAlert
from app.services import config_service
from app.services import pricing as pricing_service

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


def classify(item: EbayItem) -> tuple[str, list[str]]:
    """('USED' | 'FAULTY' | 'RISK', notes). RISK listings are never priced or announced."""
    low = f"{item.title} {item.condition or ''}".lower()
    if any(w in low for w in RISK_WORDS):
        return "RISK", ["iCloud / blacklist / lock risk"]
    notes = []
    if any(w in low for w in NETWORK_LOCK_WORDS):
        notes.append("network locked: sells for less")
    if item.condition_id == "7000" or any(w in low for w in FAULT_WORDS):
        return "FAULTY", notes
    return "USED", notes


def all_in_cost(item: EbayItem, assumed_postage: Decimal) -> Decimal:
    ship = item.shipping_cost if item.shipping_cost is not None else assumed_postage
    return (item.price + ship).quantize(TWO, rounding=ROUND_HALF_UP)


# ---------------------------------------------------------------- scanning
@dataclass
class DealCandidate:
    alert: DealAlert
    item: EbayItem
    under_by: Decimal            # how far below your maximum (£)


@dataclass
class ScanResult:
    watched: int = 0
    checked: int = 0
    candidates: list[DealCandidate] = field(default_factory=list)
    skipped: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    def skip(self, why: str) -> None:
        self.skipped[why] = self.skipped.get(why, 0) + 1


async def scan_deals(session: AsyncSession, ebay: EbayClientInterface,
                     now: Optional[dt.datetime] = None) -> ScanResult:
    """One pass over every watched model. Returns (and records) listings worth a look."""
    now = now or dt.datetime.now(dt.timezone.utc)
    assumed = await config_service.get_setting_decimal(session, "default_shipping_in_cost", Decimal("3.50"))
    auction_hours = await config_service.get_setting_decimal(session, "deal_auction_alert_hours", Decimal("6"))
    known = set((await session.execute(select(DealAlert.ebay_item_id))).scalars().all())

    result = ScanResult()
    found: list[DealCandidate] = []
    for entry in await pricing_service.list_watchlist(session):
        result.watched += 1
        rec = await pricing_service.compute_recommendation(session, entry)
        model = entry.phone_model
        try:
            items = await ebay.search_items(pricing_service.build_search_query(model, entry.storage),
                                            category_id=PHONES_CATEGORY, limit=50)
        except EbayApiError as e:
            result.errors.append(f"{model.model}: {e}")
            continue
        for item in items:
            result.checked += 1
            if item.item_id in known:
                result.skip("already announced")
                continue
            if any(w in _tokens(item.title) for w in NOT_A_PHONE_WORDS):
                result.skip("not a phone")
                continue
            if not title_matches_model(item.title, model) or not title_has_storage(item.title, entry.storage):
                result.skip("wrong model/storage in title")
                continue
            bucket, notes = classify(item)
            if bucket == "RISK":
                result.skip("lock/blacklist risk")
                continue
            max_buy = rec.recommended_buy_faulty if bucket == "FAULTY" else rec.recommended_buy_used
            if max_buy is None or max_buy <= 0:
                result.skip("no price data yet for this model")
                continue
            if item.is_auction and not (item.ends_at and now <= item.ends_at <= now + dt.timedelta(hours=float(auction_hours))):
                result.skip("auction not ending soon")
                continue
            total = all_in_cost(item, assumed)
            if total > max_buy:
                result.skip("over your maximum")
                continue
            if item.shipping_cost is None:
                notes.append(f"postage not shown: assumed £{assumed}")
            if item.seller_feedback_pct is not None and item.seller_feedback_pct < Decimal("95"):
                notes.append(f"seller feedback only {item.seller_feedback_pct}%")
            alert = DealAlert(
                ebay_item_id=item.item_id, phone_model_id=model.id, storage=entry.storage, title=item.title[:300],
                bucket=bucket, price=item.price,
                postage=item.shipping_cost if item.shipping_cost is not None else assumed,
                max_buy=max_buy, url=item.url[:500], seller=item.seller,
                seller_feedback_pct=item.seller_feedback_pct, is_auction=item.is_auction, ends_at=item.ends_at,
                flags="; ".join(notes) or None,
            )
            found.append(DealCandidate(alert, item, (max_buy - total).quantize(TWO)))
            known.add(item.item_id)

    found.sort(key=lambda c: c.under_by, reverse=True)
    for c in found[:MAX_ALERTS_PER_SCAN]:
        session.add(c.alert)
        result.candidates.append(c)
    for c in found[MAX_ALERTS_PER_SCAN:]:
        result.skip("held back (too many at once): next scan")
    await session.flush()
    return result


def format_alert(c: DealCandidate, now: Optional[dt.datetime] = None) -> str:
    a = c.alert
    now = now or dt.datetime.now(dt.timezone.utc)
    total = (a.price + a.postage).quantize(TWO)
    how = "fixed price"
    if a.is_auction and a.ends_at:
        mins = max(0, int((a.ends_at - now).total_seconds() // 60))
        how = f"auction · ends in {mins // 60}h {mins % 60:02d}m · current bid, it may rise"
    lines = [
        f"🛒 **Deal: {a.phone_model.model}{' ' + a.storage if a.storage else ''}** · {a.bucket.lower()}",
        f"{a.title}",
        f"£{a.price} + £{a.postage} postage = **£{total}** · your max £{a.max_buy} (**£{c.under_by} under**)",
        f"{how} · seller {a.seller or '?'}" + (f" ({a.seller_feedback_pct}%)" if a.seller_feedback_pct is not None else ""),
    ]
    if a.flags:
        lines.append(f"⚠️ {a.flags}")
    lines.append(a.url)
    return "\n".join(lines)


async def recent_alerts(session: AsyncSession, limit: int = 10) -> list[DealAlert]:
    return list((await session.execute(select(DealAlert).order_by(DealAlert.id.desc()).limit(limit))).scalars().all())


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
