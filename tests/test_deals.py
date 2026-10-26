"""Buy alerts and the eBay part finder.

The same hard rule as for stock applies: 'iPhone 13' is never 'iPhone 13 Pro'."""
from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from app.integrations.ebay.interface import EbayApiError, EbayItem, PriceStats
from app.integrations.ebay.mock import MockEbayClient
from app.models.pricing_models import DealAlert
from app.services import config_service, deals, job_actions, pricing as pricing_service
from app.services.job_actions import JobActionError
from tests.test_job_panels import _acts, _panel
from tests.test_model_specific_parts import _fail_display, _phone

pytestmark = pytest.mark.asyncio

NOW = dt.datetime(2026, 10, 26, 12, 0, tzinfo=dt.timezone.utc)


def item(item_id, title, price, ship="3.00", condition_id=None, condition=None, options=("FIXED_PRICE",),
         fb="99.5", ends=None, seller="bob"):
    return EbayItem(item_id=item_id, title=title, price=Decimal(price), url=f"https://ebay.test/itm/{item_id}",
                    shipping_cost=None if ship is None else Decimal(ship), condition=condition, condition_id=condition_id,
                    buying_options=list(options), seller=seller,
                    seller_feedback_pct=None if fb is None else Decimal(fb), ends_at=ends)


async def _watch(session, actor_id, model="iPhone 13", storage="128GB", median="300"):
    pm = await config_service.resolve_phone_model(session, model)
    entry = await pricing_service.add_to_watchlist(session, pm, storage, actor_id)
    ebay = MockEbayClient()
    ebay.queue_active_listing_stats(pricing_service.build_search_query(pm, storage), PriceStats(
        source="ACTIVE_LISTING_ESTIMATE", sample_count=10, avg_price=Decimal(median), median_price=Decimal(median),
        min_price=Decimal("200"), max_price=Decimal("400")))
    await pricing_service.refresh_entry(session, entry, ebay, actor_id)
    return pm, entry, ebay


# ------------------------------------------------------------------ title rules
@pytest.mark.parametrize("model,title,ok", [
    ("iPhone 13", "Apple iPhone 13 128GB Blue Unlocked", True),
    ("iPhone 13", "iPhone 13 Pro 128GB", False),
    ("iPhone 13", "iPhone 13 Pro Max 256GB", False),
    ("iPhone 13", "iPhone 13 mini 128GB", False),
    ("iPhone 13", "iPhone 13/13 Pro 128GB spares", False),            # covers two models: ambiguous
    ("iPhone 13", "IPHONE-13 128gb", True),
    ("iPhone 13 Pro", "iPhone 13 Pro 128GB", True),
    ("iPhone 13 Pro", "iPhone 13 Pro Max 128GB", False),
    ("iPhone 13 Pro", "iPhone 13 128GB", False),
    ("iPhone 14", "iPhone 14 Plus 128GB", False),
    ("iPhone XR", "Apple iPhone XR 64GB", True),
    ("iPhone XS", "Apple iPhone XS Max 64GB", False),
    ("iPhone 12", "iPhone 11 128GB", False),
])
async def test_a_title_must_name_exactly_the_watched_model(session, model, title, ok):
    pm = await config_service.resolve_phone_model(session, model)
    assert deals.title_matches_model(title, pm) is ok


def test_storage_in_a_title():
    assert deals.title_has_storage("iPhone 13 128GB blue", "128GB")
    assert deals.title_has_storage("iPhone 13 128 GB blue", "128GB")
    assert not deals.title_has_storage("iPhone 13 256GB blue", "128GB")
    assert not deals.title_has_storage("iPhone 13 1128GB", "128GB")
    assert deals.title_has_storage("anything", None)


def test_listings_are_sorted_into_used_faulty_and_risky():
    assert deals.classify(item("1", "iPhone 13 128GB good condition", "100"))[0] == "USED"
    assert deals.classify(item("2", "iPhone 13 cracked screen spares", "100"))[0] == "FAULTY"
    assert deals.classify(item("3", "iPhone 13", "100", condition_id="7000"))[0] == "FAULTY"
    assert deals.classify(item("4", "iPhone 13 iCloud locked", "100"))[0] == "RISK"
    assert deals.classify(item("5", "iPhone 13 blacklisted IMEI", "100"))[0] == "RISK"
    bucket, notes = deals.classify(item("6", "iPhone 13 locked to EE", "100"))
    assert bucket == "USED" and "network locked" in notes[0]


# ------------------------------------------------------------------ the scan
async def test_a_good_listing_is_announced_with_how_far_under_your_max_it_is(session, actor_id):
    pm, entry, ebay = await _watch(session, actor_id)
    rec = await pricing_service.compute_recommendation(session, entry)
    assert rec.recommended_buy_used == Decimal("237.32") and rec.recommended_buy_faulty == Decimal("177.32")
    ebay.queue_search_results("iphone 13", [item("A1", "Apple iPhone 13 128GB Blue Unlocked", "200", ship="3.00")])

    result = await deals.scan_deals(session, ebay, now=NOW)
    [c] = result.candidates
    assert (c.alert.bucket, c.under_by) == ("USED", Decimal("34.32"))
    text = deals.format_alert(c, now=NOW)
    assert "£203.00" in text and "£237.32" in text and "£34.32 under" in text and "https://ebay.test/itm/A1" in text
    assert ebay.searches[0]["category_id"] == "9355" and "-pro" in ebay.searches[0]["query"]


async def test_the_scanner_only_ever_announces_the_exact_model_and_under_your_max(session, actor_id):
    pm, entry, ebay = await _watch(session, actor_id)
    ebay.queue_search_results("iphone 13", [
        item("OK", "iPhone 13 128GB Blue", "200"),
        item("PRO", "iPhone 13 Pro 128GB Blue", "150"),
        item("BOTH", "iPhone 13 / 13 Pro 128GB spares", "100"),
        item("DEAR", "iPhone 13 128GB Blue", "260"),
        item("S256", "iPhone 13 256GB Blue", "150"),
        item("CASE", "iPhone 13 128GB case cover", "5"),
        item("ICLOUD", "iPhone 13 128GB iCloud locked", "50"),
    ])
    result = await deals.scan_deals(session, ebay, now=NOW)
    assert [c.alert.ebay_item_id for c in result.candidates] == ["OK"]
    assert result.skipped == {"wrong model/storage in title": 3, "over your maximum": 1, "not a phone": 1,
                              "lock/blacklist risk": 1}


async def test_faulty_phones_are_judged_against_the_repair_adjusted_maximum(session, actor_id):
    pm, entry, ebay = await _watch(session, actor_id)
    ebay.queue_search_results("iphone 13", [
        item("F1", "iPhone 13 128GB cracked screen for repair", "150", ship=None),      # 153.50 <= 177.32
        item("F2", "iPhone 13 128GB cracked screen for repair", "175", ship=None),      # 178.50 > 177.32
    ])
    result = await deals.scan_deals(session, ebay, now=NOW)
    [c] = result.candidates
    assert c.alert.ebay_item_id == "F1" and c.alert.bucket == "FAULTY" and c.alert.postage == Decimal("3.50")
    assert "postage not shown" in c.alert.flags


async def test_auctions_only_appear_when_they_are_about_to_end(session, actor_id):
    pm, entry, ebay = await _watch(session, actor_id)
    ebay.queue_search_results("iphone 13", [
        item("SOON", "iPhone 13 128GB", "150", options=("AUCTION",), ends=NOW + dt.timedelta(hours=2)),
        item("LATER", "iPhone 13 128GB", "50", options=("AUCTION",), ends=NOW + dt.timedelta(days=3)),
    ])
    result = await deals.scan_deals(session, ebay, now=NOW)
    assert [c.alert.ebay_item_id for c in result.candidates] == ["SOON"]
    assert "auction · ends in 2h 00m" in deals.format_alert(result.candidates[0], now=NOW)


async def test_a_listing_is_announced_once(session, actor_id):
    pm, entry, ebay = await _watch(session, actor_id)
    ebay.queue_search_results("iphone 13", [item("ONCE", "iPhone 13 128GB", "200")])
    assert len((await deals.scan_deals(session, ebay, now=NOW)).candidates) == 1
    again = await deals.scan_deals(session, ebay, now=NOW)
    assert again.candidates == [] and again.skipped == {"already announced": 1}


async def test_a_flood_is_capped_and_the_rest_come_next_time(session, actor_id):
    pm, entry, ebay = await _watch(session, actor_id)
    ebay.queue_search_results("iphone 13", [item(f"N{i}", "iPhone 13 128GB", str(150 + i)) for i in range(12)])
    first = await deals.scan_deals(session, ebay, now=NOW)
    assert len(first.candidates) == deals.MAX_ALERTS_PER_SCAN
    assert first.candidates[0].under_by >= first.candidates[-1].under_by          # best deals first
    second = await deals.scan_deals(session, ebay, now=NOW)
    assert len(second.candidates) == 2


async def test_models_with_no_price_data_are_not_guessed_at(session, actor_id):
    pm = await config_service.resolve_phone_model(session, "iPhone 14")
    await pricing_service.add_to_watchlist(session, pm, None, actor_id)
    ebay = MockEbayClient()
    ebay.queue_search_results("iphone 14", [item("X", "iPhone 14 128GB", "100")])
    result = await deals.scan_deals(session, ebay, now=NOW)
    assert result.candidates == [] and result.skipped == {"no price data yet for this model": 1}


async def test_an_ebay_error_for_one_model_does_not_stop_the_others(session, actor_id, monkeypatch):
    pm, entry, ebay = await _watch(session, actor_id)
    pm14 = await config_service.resolve_phone_model(session, "iPhone 14")
    await pricing_service.add_to_watchlist(session, pm14, None, actor_id)
    ebay.queue_search_results("iphone 13", [item("OK", "iPhone 13 128GB", "200")])
    real = ebay.search_items

    async def flaky(query, **kw):
        if "14" in query:
            raise EbayApiError("rate limited")
        return await real(query, **kw)
    monkeypatch.setattr(ebay, "search_items", flaky)
    result = await deals.scan_deals(session, ebay, now=NOW)
    assert [c.alert.ebay_item_id for c in result.candidates] == ["OK"] and result.errors == ["iPhone 14: rate limited"]


# ------------------------------------------------------------------ the part finder
async def test_part_finder_returns_only_parts_that_name_exactly_this_model(session):
    pm = await config_service.resolve_phone_model(session, "iPhone 13")
    ebay = MockEbayClient()
    ebay.queue_search_results("iphone 13 screen", [
        item("P1", "iPhone 13 LCD Screen Replacement Display", "30", ship="3.00"),
        item("P2", "iPhone 13 Pro OLED Screen Replacement", "25", ship="3.00"),         # a different model
        item("P3", "iPhone 13 screen protector tempered glass", "2", ship="1.00"),       # not a part
        item("P4", "iPhone 13 Battery Replacement", "12", ship="2.00"),                  # wrong part
        item("P5", "iPhone 13 Screen Display", "20", ship=None, fb="88.0"),              # cheap but poor seller
        item("P6", "iPhone 13 Screen Display", "15", options=("AUCTION",)),              # auction: skipped
    ])
    offers = await deals.find_parts_on_ebay(session, ebay, "Screen", pm)
    assert [o.item.item_id for o in offers] == ["P1", "P5"], "good sellers first, then by price"
    assert offers[0].total == Decimal("33.00") and any("feedback" in n for n in offers[1].notes)
    assert "-pro" in ebay.searches[0]["query"]
    text = deals.format_part_offers("Screen", pm, offers)
    assert "Screen for iPhone 13" in text and "https://ebay.test/itm/P1" in text


async def test_job_channel_offers_find_on_ebay_only_when_a_part_must_be_ordered(session, actor_id):
    from tests.test_tasks import _stock
    phone = await _phone(session, actor_id, "iPhone 13")
    await _fail_display(session, phone, actor_id)
    assert "findebay" in _acts(await _panel(session, phone))
    await _stock(session, actor_id, "Screen", model="iPhone 13")                       # now it's in stock
    assert "findebay" not in _acts(await _panel(session, phone))


async def test_find_on_ebay_searches_for_the_phones_exact_model(session, actor_id):
    phone = await _phone(session, actor_id, "iPhone 13 Pro")
    await _fail_display(session, phone, actor_id)
    ebay = MockEbayClient()
    ebay.queue_search_results("iphone 13 pro screen", [item("P1", "iPhone 13 Pro OLED Screen", "60"),
                                                       item("P2", "iPhone 13 Pro Max OLED Screen", "70")])
    text = await job_actions.find_ebay_parts(session, phone.id, ebay)
    assert "P1" in text and "P2" not in text and "Screen for iPhone 13 Pro" in text
    assert "-pro" not in ebay.searches[0]["query"] and "-max" in ebay.searches[0]["query"]


async def test_find_on_ebay_says_so_when_nothing_needs_ordering(session, actor_id):
    phone = await _phone(session, actor_id, "iPhone 13")
    with pytest.raises(JobActionError, match="Nothing here needs ordering"):
        await job_actions.find_ebay_parts(session, phone.id, MockEbayClient())
