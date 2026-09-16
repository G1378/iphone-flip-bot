from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from app.integrations.ebay.interface import PriceStats
from app.integrations.ebay.mock import MockEbayClient
from app.models.enums import AcquisitionType, PriceConditionBucket
from app.services import config_service, pricing as pricing_service
from app.services import phones as phones_service


async def _watch_iphone_13_pro(session, actor_id, storage="128GB"):
    phone_model = await config_service.get_or_create_phone_model(session, "Apple", "iPhone 13 Pro")
    entry = await pricing_service.add_to_watchlist(session, phone_model, storage, actor_id)
    return phone_model, entry


@pytest.mark.asyncio
async def test_watchlist_add_is_idempotent(session, actor_id):
    phone_model, entry1 = await _watch_iphone_13_pro(session, actor_id)
    entry2 = await pricing_service.add_to_watchlist(session, phone_model, "128GB", actor_id)
    assert entry1.id == entry2.id

    watchlist = await pricing_service.list_watchlist(session)
    assert len(watchlist) == 1


@pytest.mark.asyncio
async def test_watchlist_remove_then_readd(session, actor_id):
    phone_model, entry = await _watch_iphone_13_pro(session, actor_id)
    removed = await pricing_service.remove_from_watchlist(session, phone_model, "128GB", actor_id)
    assert removed is True

    watchlist = await pricing_service.list_watchlist(session)
    assert watchlist == []

    # re-adding after removal should reactivate the same row, not duplicate it
    await pricing_service.add_to_watchlist(session, phone_model, "128GB", actor_id)
    watchlist = await pricing_service.list_watchlist(session)
    assert len(watchlist) == 1


@pytest.mark.asyncio
async def test_refresh_entry_stores_observation_and_falls_back_to_active_listings(session, actor_id):
    phone_model, entry = await _watch_iphone_13_pro(session, actor_id)
    ebay = MockEbayClient(configured=True)

    query = pricing_service.build_search_query(phone_model, "128GB")
    # No sold stats queued -> should fall back to active-listing estimate
    ebay.queue_active_listing_stats(query, PriceStats(
        source="ACTIVE_LISTING_ESTIMATE", sample_count=12,
        avg_price=Decimal("310.00"), median_price=Decimal("305.00"),
        min_price=Decimal("250.00"), max_price=Decimal("380.00"),
    ))

    created = await pricing_service.refresh_entry(session, entry, ebay, actor_id)
    used = next(o for o in created if o.condition_bucket == PriceConditionBucket.USED)
    assert used.source.value == "ACTIVE_LISTING_ESTIMATE"
    assert used.median_price == Decimal("305.00")

    # default_sale_price should have been auto-updated on the phone model
    await session.refresh(phone_model)
    assert phone_model.default_sale_price == Decimal("305.00")


@pytest.mark.asyncio
async def test_refresh_entry_prefers_sold_data_over_active_listings(session, actor_id):
    phone_model, entry = await _watch_iphone_13_pro(session, actor_id)
    ebay = MockEbayClient(configured=True)
    query = pricing_service.build_search_query(phone_model, "128GB")

    ebay.queue_sold_stats(query, PriceStats(
        source="SOLD", sample_count=10,
        avg_price=Decimal("290.00"), median_price=Decimal("285.00"),
        min_price=Decimal("240.00"), max_price=Decimal("340.00"),
    ))
    ebay.queue_active_listing_stats(query, PriceStats(
        source="ACTIVE_LISTING_ESTIMATE", sample_count=12,
        avg_price=Decimal("330.00"), median_price=Decimal("325.00"),
        min_price=Decimal("280.00"), max_price=Decimal("390.00"),
    ))

    created = await pricing_service.refresh_entry(session, entry, ebay, actor_id)
    used = next(o for o in created if o.condition_bucket == PriceConditionBucket.USED)
    assert used.source.value == "SOLD"
    assert used.median_price == Decimal("285.00")


@pytest.mark.asyncio
async def test_refresh_entry_falls_back_when_sold_samples_below_threshold(session, actor_id):
    """Even if Marketplace Insights returns *some* sold data, too few
    samples (< MIN_SOLD_SAMPLES) should still prefer the active-listing
    estimate, which has a healthier sample size."""
    phone_model, entry = await _watch_iphone_13_pro(session, actor_id)
    ebay = MockEbayClient(configured=True)
    query = pricing_service.build_search_query(phone_model, "128GB")

    ebay.queue_sold_stats(query, PriceStats(
        source="SOLD", sample_count=1,  # below MIN_SOLD_SAMPLES
        avg_price=Decimal("500.00"), median_price=Decimal("500.00"),
        min_price=Decimal("500.00"), max_price=Decimal("500.00"),
    ))
    ebay.queue_active_listing_stats(query, PriceStats(
        source="ACTIVE_LISTING_ESTIMATE", sample_count=20,
        avg_price=Decimal("300.00"), median_price=Decimal("300.00"),
        min_price=Decimal("260.00"), max_price=Decimal("350.00"),
    ))

    created = await pricing_service.refresh_entry(session, entry, ebay, actor_id)
    used = next(o for o in created if o.condition_bucket == PriceConditionBucket.USED)
    assert used.source.value == "ACTIVE_LISTING_ESTIMATE"


@pytest.mark.asyncio
async def test_refresh_entry_no_data_creates_no_observation(session, actor_id):
    phone_model, entry = await _watch_iphone_13_pro(session, actor_id)
    ebay = MockEbayClient(configured=True)  # nothing queued at all
    created = await pricing_service.refresh_entry(session, entry, ebay, actor_id)
    assert created == []


@pytest.mark.asyncio
async def test_recommendation_uses_default_repair_cost_assumption_when_no_history(session, actor_id, today):
    phone_model, entry = await _watch_iphone_13_pro(session, actor_id)
    ebay = MockEbayClient(configured=True)
    query = pricing_service.build_search_query(phone_model, "128GB")

    ebay.queue_active_listing_stats(query, PriceStats(
        source="ACTIVE_LISTING_ESTIMATE", sample_count=15,
        avg_price=Decimal("400.00"), median_price=Decimal("400.00"),
        min_price=Decimal("350.00"), max_price=Decimal("450.00"),
    ))
    await pricing_service.refresh_entry(session, entry, ebay, actor_id)

    rec = await pricing_service.compute_recommendation(session, entry)
    assert rec.repair_cost_is_historical is False
    assert rec.repair_cost_assumption == Decimal("60.00")  # the seeded default

    # sanity check the actual arithmetic:
    # fees = 400 * 12.8% + 0.30 = 51.50 (rounded)
    # buy_used = 400 - 51.50 - 6.00 (postage) - 1.00 (packaging) - 20.00 (min profit)
    fee_pct = await config_service.get_setting_decimal(session, "ebay_fee_pct_assumption", Decimal("12.8"))
    fee_fixed = await config_service.get_setting_decimal(session, "ebay_fixed_fee_assumption", Decimal("0.30"))
    postage = await config_service.get_setting_decimal(session, "default_postage_cost", Decimal("6.00"))
    packaging = await config_service.get_setting_decimal(session, "default_packaging_cost", Decimal("1.00"))
    min_profit = await config_service.get_setting_decimal(session, "min_profit_gbp", Decimal("20.00"))
    expected_fees = (Decimal("400.00") * fee_pct / 100 + fee_fixed).quantize(Decimal("0.01"))
    expected_buy_used = (Decimal("400.00") - expected_fees - postage - packaging - min_profit).quantize(Decimal("0.01"))
    expected_buy_faulty = (expected_buy_used - Decimal("60.00")).quantize(Decimal("0.01"))

    assert rec.recommended_buy_used == expected_buy_used
    assert rec.recommended_buy_faulty == expected_buy_faulty
    assert rec.recommended_buy_faulty < rec.recommended_buy_used  # faulty must always recommend paying less


@pytest.mark.asyncio
async def test_recommendation_uses_historical_repair_cost_when_available(session, actor_id, today):
    """When the business has actually completed repairs for this model
    before, use the real historical average instead of the generic
    fallback assumption."""
    from app.services import repairs as repairs_service
    from app.services import parts as parts_service
    from app.models.enums import PartSourceType, TestOutcome

    # Build and complete one real repair for an iPhone 13 Pro so there's history.
    data = phones_service.BuyPhoneInput(
        manufacturer="Apple", model="iPhone 13 Pro", variant=None, storage="128GB", colour="Graphite",
        imei="1", serial_number="1", carrier="Unlocked", purchase_price=Decimal("200.00"),
        purchase_date=today, seller_source="x", acquisition_type=AcquisitionType.REPAIR, notes=None, location_code=None,
    )
    phone = await phones_service.buy_phone(session, data, actor_id)
    screen_type = await config_service.get_or_create_part_type(session, "Screen")
    await phones_service.add_fault(session, phone, "Cracked screen", actor_id, required_part_type_id=screen_type.id)
    repair = await repairs_service.create_repair_for_open_faults(session, phone, actor_id)
    rp = repair.parts[0]
    part_data = parts_service.CreatePartInput(
        part_type_name="Screen", source_type=PartSourceType.PURCHASED, cost=Decimal("45.00"),
        testing_status=TestOutcome.PASS,
    )
    part = await parts_service.create_part(session, part_data, actor_id)
    await parts_service.reserve_part(session, part, repair, rp, actor_id)
    await parts_service.install_part(session, part, phone, rp, actor_id)
    await repairs_service.complete_repair(session, repair, actor_id)

    phone_model, entry = await _watch_iphone_13_pro(session, actor_id)
    ebay = MockEbayClient(configured=True)
    query = pricing_service.build_search_query(phone_model, "128GB")
    ebay.queue_active_listing_stats(query, PriceStats(
        source="ACTIVE_LISTING_ESTIMATE", sample_count=15,
        avg_price=Decimal("400.00"), median_price=Decimal("400.00"),
        min_price=Decimal("350.00"), max_price=Decimal("450.00"),
    ))
    await pricing_service.refresh_entry(session, entry, ebay, actor_id)

    rec = await pricing_service.compute_recommendation(session, entry)
    assert rec.repair_cost_is_historical is True
    assert rec.repair_cost_assumption == Decimal("45.00")  # the one completed repair's total cost


@pytest.mark.asyncio
async def test_get_current_estimated_sale_price_prefers_storage_specific_observation(session, actor_id, today):
    phone_model, entry_128 = await _watch_iphone_13_pro(session, actor_id, storage="128GB")
    _, entry_256 = await _watch_iphone_13_pro(session, actor_id, storage="256GB")
    ebay = MockEbayClient(configured=True)

    query_128 = pricing_service.build_search_query(phone_model, "128GB")
    query_256 = pricing_service.build_search_query(phone_model, "256GB")
    ebay.queue_active_listing_stats(query_128, PriceStats(
        source="ACTIVE_LISTING_ESTIMATE", sample_count=10,
        avg_price=Decimal("300.00"), median_price=Decimal("300.00"), min_price=Decimal("270"), max_price=Decimal("340"),
    ))
    ebay.queue_active_listing_stats(query_256, PriceStats(
        source="ACTIVE_LISTING_ESTIMATE", sample_count=10,
        avg_price=Decimal("380.00"), median_price=Decimal("380.00"), min_price=Decimal("350"), max_price=Decimal("420"),
    ))
    await pricing_service.refresh_entry(session, entry_128, ebay, actor_id)
    await pricing_service.refresh_entry(session, entry_256, ebay, actor_id)

    data = phones_service.BuyPhoneInput(
        manufacturer="Apple", model="iPhone 13 Pro", variant=None, storage="256GB", colour="Silver",
        imei="2", serial_number="2", carrier="Unlocked", purchase_price=Decimal("250.00"),
        purchase_date=today, seller_source="x", acquisition_type=AcquisitionType.RESALE, notes=None, location_code=None,
    )
    phone_256 = await phones_service.buy_phone(session, data, actor_id)

    price = await pricing_service.get_current_estimated_sale_price(session, phone_256)
    assert price == Decimal("380.00")  # must pick the 256GB observation, not 128GB's
