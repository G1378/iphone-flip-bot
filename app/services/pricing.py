from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Optional, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.integrations.ebay.interface import EbayClientInterface, PriceStats
from app.models.config_models import PhoneModelConfig
from app.models.enums import InventoryEventType, PriceConditionBucket, PriceDataSource
from app.models.inventory_models import Phone
from app.models.pricing_models import PriceObservation, PriceWatchlistEntry
from app.models.repair_models import Repair
from app.services import config_service
from app.services.events import log_event

TWO_DP = Decimal("0.01")

# eBay condition IDs. "3000" (Used) and "7000" (For parts or not working)
# are stable, well-documented IDs used broadly across eBay's category
# tree. If your specific eBay category uses more granular sub-grades
# (e.g. separate "Very Good"/"Good"/"Acceptable" IDs), you can broaden
# these lists - they're passed straight through to the conditionIds
# filter on both the Browse and Marketplace Insights APIs.
USED_CONDITION_IDS = ["3000"]
FAULTY_CONDITION_IDS = ["7000"]

MIN_SOLD_SAMPLES = 3  # below this, fall back to active-listing estimate even if sold data exists


def build_search_query(phone_model: PhoneModelConfig, storage: Optional[str]) -> str:
    bits = [phone_model.manufacturer, phone_model.model, phone_model.variant, storage]
    return " ".join(b for b in bits if b)


# ----------------------------------------------------------------------
# Watchlist management
# ----------------------------------------------------------------------
async def add_to_watchlist(session: AsyncSession, phone_model: PhoneModelConfig, storage: Optional[str],
                            actor_discord_id: int) -> PriceWatchlistEntry:
    existing_stmt = select(PriceWatchlistEntry).where(
        PriceWatchlistEntry.phone_model_id == phone_model.id, PriceWatchlistEntry.storage == storage,
    )
    existing = (await session.execute(existing_stmt)).scalar_one_or_none()
    if existing:
        existing.active = True
        await session.flush()
        return existing

    entry = PriceWatchlistEntry(phone_model=phone_model, storage=storage, created_by_discord_id=actor_discord_id)
    session.add(entry)
    await session.flush()
    await log_event(
        session, event_type=InventoryEventType.PRICE_WATCHLIST_ADDED.value, entity_type="SYSTEM",
        entity_id="SYSTEM", actor_discord_id=actor_discord_id,
        new_state={"model": phone_model.display_name(), "storage": storage},
    )
    return entry


async def remove_from_watchlist(session: AsyncSession, phone_model: PhoneModelConfig, storage: Optional[str],
                                 actor_discord_id: int) -> bool:
    stmt = select(PriceWatchlistEntry).where(
        PriceWatchlistEntry.phone_model_id == phone_model.id, PriceWatchlistEntry.storage == storage,
        PriceWatchlistEntry.active.is_(True),
    )
    entry = (await session.execute(stmt)).scalar_one_or_none()
    if not entry:
        return False
    entry.active = False
    await session.flush()
    await log_event(
        session, event_type=InventoryEventType.PRICE_WATCHLIST_REMOVED.value, entity_type="SYSTEM",
        entity_id="SYSTEM", actor_discord_id=actor_discord_id,
        new_state={"model": phone_model.display_name(), "storage": storage},
    )
    return True


async def list_watchlist(session: AsyncSession) -> Sequence[PriceWatchlistEntry]:
    stmt = (
        select(PriceWatchlistEntry)
        .where(PriceWatchlistEntry.active.is_(True))
        .options(selectinload(PriceWatchlistEntry.phone_model))
        .order_by(PriceWatchlistEntry.id)
    )
    return (await session.execute(stmt)).scalars().all()


# ----------------------------------------------------------------------
# Refresh (eBay lookups -> PriceObservation rows)
# ----------------------------------------------------------------------
async def _fetch_stats_with_fallback(ebay: EbayClientInterface, query: str, condition_ids: list[str]) -> Optional[PriceStats]:
    sold = await ebay.get_sold_price_stats(query, condition_ids)
    if sold and sold.sample_count >= MIN_SOLD_SAMPLES:
        return sold
    active = await ebay.get_active_listing_price_stats(query, condition_ids)
    return active or sold  # prefer *some* sold data over nothing, even if under the sample threshold


async def refresh_entry(session: AsyncSession, entry: PriceWatchlistEntry, ebay: EbayClientInterface,
                         actor_discord_id: Optional[int] = None) -> list[PriceObservation]:
    """Fetch current pricing for one watchlist entry (both USED and FAULTY
    condition buckets) and store a new PriceObservation row for each."""
    query = build_search_query(entry.phone_model, entry.storage)
    observed_at = dt.datetime.now(dt.timezone.utc)
    created: list[PriceObservation] = []

    for bucket, condition_ids in ((PriceConditionBucket.USED, USED_CONDITION_IDS),
                                   (PriceConditionBucket.FAULTY, FAULTY_CONDITION_IDS)):
        stats = await _fetch_stats_with_fallback(ebay, query, condition_ids)
        if stats is None:
            continue
        obs = PriceObservation(
            phone_model_id=entry.phone_model_id,
            storage=entry.storage,
            condition_bucket=bucket,
            source=PriceDataSource(stats.source),
            sample_count=stats.sample_count,
            avg_price=stats.avg_price,
            median_price=stats.median_price,
            min_price=stats.min_price,
            max_price=stats.max_price,
            currency=stats.currency,
            observed_at=observed_at,
        )
        session.add(obs)
        created.append(obs)

    await session.flush()
    if created:
        await log_event(
            session, event_type=InventoryEventType.PRICE_OBSERVED.value, entity_type="SYSTEM", entity_id="SYSTEM",
            actor_discord_id=actor_discord_id,
            new_state={"query": query, "buckets": [o.condition_bucket.value for o in created]},
        )

    # Per the confirmed default: keep /analyse and /list pricing fresh
    # automatically from the latest USED observation for this model. Fetch
    # via session.get() (identity-map safe, explicit) rather than the
    # entry.phone_model relationship attribute, and flush the change here
    # rather than leaving it for the caller to remember to commit.
    used_obs = next((o for o in created if o.condition_bucket == PriceConditionBucket.USED), None)
    if used_obs:
        phone_model = await session.get(PhoneModelConfig, entry.phone_model_id)
        if phone_model:
            phone_model.default_sale_price = used_obs.median_price
            await session.flush()

    return created


async def refresh_all_watched(session: AsyncSession, ebay: EbayClientInterface,
                               actor_discord_id: Optional[int] = None) -> dict[str, int]:
    entries = await list_watchlist(session)
    observed, skipped = 0, 0
    for entry in entries:
        results = await refresh_entry(session, entry, ebay, actor_discord_id)
        if results:
            observed += 1
        else:
            skipped += 1
    return {"watched": len(entries), "observed": observed, "no_data": skipped}


# ----------------------------------------------------------------------
# Reading observations + recommendations
# ----------------------------------------------------------------------
async def latest_observation(session: AsyncSession, phone_model_id: int, storage: Optional[str],
                              bucket: PriceConditionBucket) -> Optional[PriceObservation]:
    stmt = (
        select(PriceObservation)
        .where(
            PriceObservation.phone_model_id == phone_model_id,
            PriceObservation.storage == storage,
            PriceObservation.condition_bucket == bucket,
        )
        .order_by(PriceObservation.observed_at.desc())
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def average_repair_cost_for_model(session: AsyncSession, phone_model_id: int) -> Optional[Decimal]:
    """Historical average total repair cost (labour + external + installed
    parts) for completed repairs on phones matching this model. Returns
    None if there's no history yet, so callers can fall back to the
    configurable default_repair_cost_assumption business setting."""
    phone_model = await session.get(PhoneModelConfig, phone_model_id)
    if not phone_model:
        return None

    stmt = (
        select(Repair)
        .join(Phone, Repair.phone_id == Phone.id)
        .where(
            Repair.status == "COMPLETE",
            Phone.manufacturer == phone_model.manufacturer,
            Phone.model == phone_model.model,
            Phone.variant == phone_model.variant,
        )
        .options(selectinload(Repair.parts))
    )
    repairs = (await session.execute(stmt)).scalars().all()
    if not repairs:
        return None
    total = sum((r.total_cost() for r in repairs), Decimal("0"))
    return (total / len(repairs)).quantize(TWO_DP, rounding=ROUND_HALF_UP)


@dataclass
class PriceRecommendation:
    phone_model: PhoneModelConfig
    storage: Optional[str]
    used_observation: Optional[PriceObservation]
    faulty_observation: Optional[PriceObservation]
    repair_cost_assumption: Decimal
    repair_cost_is_historical: bool
    recommended_buy_used: Optional[Decimal]
    recommended_buy_faulty: Optional[Decimal]


async def compute_recommendation(session: AsyncSession, entry: PriceWatchlistEntry) -> PriceRecommendation:
    used_obs = await latest_observation(session, entry.phone_model_id, entry.storage, PriceConditionBucket.USED)
    faulty_obs = await latest_observation(session, entry.phone_model_id, entry.storage, PriceConditionBucket.FAULTY)

    fee_pct = await config_service.get_setting_decimal(session, "ebay_fee_pct_assumption", Decimal("12.8"))
    fee_fixed = await config_service.get_setting_decimal(session, "ebay_fixed_fee_assumption", Decimal("0.30"))
    postage = await config_service.get_setting_decimal(session, "default_postage_cost", Decimal("6.00"))
    packaging = await config_service.get_setting_decimal(session, "default_packaging_cost", Decimal("1.00"))
    min_profit = await config_service.get_setting_decimal(session, "min_profit_gbp", Decimal("20.00"))

    historical_repair_cost = await average_repair_cost_for_model(session, entry.phone_model_id)
    default_repair_cost = await config_service.get_setting_decimal(session, "default_repair_cost_assumption", Decimal("60.00"))
    repair_cost = historical_repair_cost if historical_repair_cost is not None else default_repair_cost

    recommended_used = None
    recommended_faulty = None
    if used_obs:
        resale_price = used_obs.median_price
        estimated_fees = (resale_price * fee_pct / Decimal("100") + fee_fixed).quantize(TWO_DP, rounding=ROUND_HALF_UP)
        recommended_used = (resale_price - estimated_fees - postage - packaging - min_profit).quantize(TWO_DP, rounding=ROUND_HALF_UP)
        # Faulty buy price = expected resale-after-repair minus repair cost
        # minus the same sale-side costs minus target profit (per confirmed
        # design: repair-economics based, not benchmarked off 'for parts'
        # sold comps directly).
        recommended_faulty = (recommended_used - repair_cost).quantize(TWO_DP, rounding=ROUND_HALF_UP)

    return PriceRecommendation(
        phone_model=entry.phone_model,
        storage=entry.storage,
        used_observation=used_obs,
        faulty_observation=faulty_obs,
        repair_cost_assumption=repair_cost,
        repair_cost_is_historical=historical_repair_cost is not None,
        recommended_buy_used=recommended_used,
        recommended_buy_faulty=recommended_faulty,
    )


async def get_current_estimated_sale_price(session: AsyncSession, phone: Phone) -> Optional[Decimal]:
    """Best available live sale-price estimate for a specific phone,
    checking storage-specific pricing data first before falling back to
    the model's general default_sale_price. Used by /analyse and /list so
    they benefit from live watchlist pricing automatically."""
    stmt = select(PhoneModelConfig).where(
        PhoneModelConfig.manufacturer == phone.manufacturer,
        PhoneModelConfig.model == phone.model,
        PhoneModelConfig.variant == phone.variant,
    )
    phone_model = (await session.execute(stmt)).scalar_one_or_none()
    if not phone_model:
        return None

    obs = await latest_observation(session, phone_model.id, phone.storage, PriceConditionBucket.USED)
    if obs:
        return obs.median_price
    return phone_model.default_sale_price
