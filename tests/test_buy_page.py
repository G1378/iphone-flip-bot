"""The buy page end to end: what qualifies, what the card says, and what the buttons do.

Numbers used throughout: a watched iPhone 13 128GB with a £300 median resale gives (default settings)
recommended_buy_used £237.32, i.e. sale costs of £42.68 and the £20 minimum profit already taken off."""
from __future__ import annotations

import datetime as dt
from contextlib import asynccontextmanager
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bot import deal_cards
from app.bot.cogs import deals as deals_cog
from discord import app_commands
from app.integrations.ebay.mock import MockEbayClient
from app.integrations.vinted.interface import VintedItem, VintedNotConfiguredError
from app.integrations.vinted.mock import MockVintedClient
from app.jobs import scheduler as sched_mod
from app.models.enums import AcquisitionType
from app.models.events import InventoryEvent
from app.models.inventory_models import Phone
from app.services import config_service, deal_queue, deals, pricing as pricing_service
from tests.test_deals import NOW, _watch, item

D = Decimal


# ------------------------------------------------------------------ helpers
def vitem(vid="V1", title="iPhone 13 128GB", price="190", fee=None, reviews=40, desc=None, seller="sam"):
    return VintedItem(item_id=vid, title=title, price=D(price), url=f"https://vinted.test/items/{vid}",
                      buyer_fee=None if fee is None else D(fee), condition="Very good", description=desc, seller=seller,
                      seller_feedback_pct=D("98.0"), seller_reviews=reviews, image_url="http://img/x.jpg")


async def _watch_second(session, actor_id, model="iPhone 12", storage="64GB", median="250"):
    return await _watch(session, actor_id, model, storage, median)


async def _scan_ebay(session, ebay, *items):
    ebay.queue_search_results("iphone 13", list(items))
    return await deals.scan_deals(session, ebay, now=NOW)


async def _scan_vinted(session, *vitems, vinted=None):
    vinted = vinted or MockVintedClient()
    vinted.queue_search_results("iphone 13", list(vitems))
    return vinted, await deals.scan_vinted(session, vinted, now=NOW)


async def _one_alert(session, actor_id, *, bucket_title="iPhone 13 128GB", price="200"):
    _, _, ebay = await _watch(session, actor_id)
    res = await _scan_ebay(session, ebay, item("E1", bucket_title, price))
    assert len(res.candidates) == 1, res.skipped
    return res.candidates[0].alert


@pytest.fixture()
def scope(engine, monkeypatch):
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

    @asynccontextmanager
    async def _scope():
        async with factory() as s:
            yield s
            await s.commit()

    monkeypatch.setattr(deal_cards, "session_scope", _scope)
    monkeypatch.setattr(sched_mod, "session_scope", _scope)
    monkeypatch.setattr(deals_cog, "session_scope", _scope)
    return _scope


def interaction(user_id=42, message=True):
    i = MagicMock()
    i.user.id = user_id
    i.response.edit_message = AsyncMock()
    i.response.send_modal = AsyncMock()
    i.response.send_message = AsyncMock()
    i.followup.send = AsyncMock()
    i.message = MagicMock() if message else None
    if message:
        i.message.edit = AsyncMock()
    return i


# ================================================================== eBay: what qualifies and why
@pytest.mark.asyncio
async def test_a_deal_carries_its_workings(session, actor_id):
    a = await _one_alert(session, actor_id)
    assert (a.source, a.status, a.bucket) == ("EBAY", "PENDING", "USED")
    assert a.resale_estimate == D("300.00") and a.sale_costs == D("42.68") and a.resale_basis == "eBay asking prices"
    assert a.total_cost == D("203.00") and a.repair_estimate == 0
    assert a.expected_profit == D("54.32") and a.roi_pct == D("26.8")        # 300 - 42.68 - 203 ; 54.32 / 203
    assert a.max_buy == D("237.32") and a.max_item_price == D("234.32")      # what to pay for the phone itself


@pytest.mark.asyncio
async def test_margin_must_meet_your_minimum_roi_not_just_the_minimum_profit(session, actor_id):
    _, _, ebay = await _watch(session, actor_id)
    await config_service.set_setting(session, "min_roi_pct", "40", value_type="number")
    res = await _scan_ebay(session, ebay, item("A", "iPhone 13 128GB", "200"), item("B", "iPhone 13 128GB", "150"))
    assert [c.alert.ebay_item_id for c in res.candidates] == ["B"]           # £200 clears min profit but is only 26.8% ROI
    assert res.skipped["margin too thin (under your minimum ROI)"] == 1


@pytest.mark.asyncio
async def test_asking_price_haircut_lowers_the_resale_value_and_so_the_maximum(session, actor_id):
    pm, entry, _ = await _watch(session, actor_id)
    assert (await pricing_service.compute_recommendation(session, entry)).resale_price == D("300.00")   # default: untouched
    await config_service.set_setting(session, "asking_price_haircut_pct", "10", value_type="number")
    rec = await pricing_service.compute_recommendation(session, entry)
    assert rec.resale_price == D("270.00") and rec.resale_is_estimate
    assert rec.recommended_buy_used < D("237.32")


@pytest.mark.asyncio
async def test_a_named_fault_is_priced_as_that_repair_and_comes_off_the_maximum(session, actor_id):
    _, _, ebay = await _watch(session, actor_id)
    res = await _scan_ebay(session, ebay, item("F1", "iPhone 13 128GB cracked screen", "120"))
    a = res.candidates[0].alert
    assert a.bucket == "FAULTY" and a.repair_estimate == D("60.50")
    assert "screen" in a.repair_basis and a.max_buy == D("176.82")            # 237.32 - 60.50
    assert a.expected_profit == D("73.82")                                     # 300 - 42.68 sale costs - 123 all in - 60.50 repair
    assert any("repair £60.50" in f for f in a.flags.split("; "))


@pytest.mark.asyncio
async def test_a_working_phone_that_admits_a_worn_battery_is_priced_as_a_repair(session, actor_id):
    _, _, ebay = await _watch(session, actor_id)
    res = await _scan_ebay(session, ebay, item("B1", "iPhone 13 128GB battery health 74%", "150"))
    a = res.candidates[0].alert
    assert a.bucket == "FAULTY" and a.repair_estimate == D("22.00")           # £20 battery + 10%
    assert "battery" in a.repair_basis


@pytest.mark.asyncio
async def test_faults_we_cannot_price_never_reach_the_buy_page(session, actor_id):
    _, _, ebay = await _watch(session, actor_id)
    res = await _scan_ebay(session, ebay, item("W", "iPhone 13 128GB water damaged", "50"),
                           item("N", "iPhone 13 128GB won't turn on", "50"), item("FID", "iPhone 13 128GB face id not working", "50"))
    assert not res.candidates
    assert res.skipped["can't price the repair (water / no power / Face ID / board fault)"] == 3


@pytest.mark.asyncio
async def test_a_denial_is_not_read_as_a_fault(session, actor_id):
    _, _, ebay = await _watch(session, actor_id)
    res = await _scan_ebay(session, ebay, item("OK1", "iPhone 13 128GB no cracks no scratches", "150"))
    assert res.candidates[0].alert.bucket == "USED"


@pytest.mark.asyncio
async def test_a_price_far_below_market_is_flagged_not_hidden(session, actor_id):
    _, _, ebay = await _watch(session, actor_id)
    res = await _scan_ebay(session, ebay, item("CHEAP", "iPhone 13 128GB", "100"))
    assert "scam, lock or hidden damage" in res.candidates[0].alert.flags


# ================================================================== Vinted
@pytest.mark.asyncio
async def test_vinted_buyer_protection_is_part_of_what_you_pay(session, actor_id):
    await _watch(session, actor_id)
    _, res = await _scan_vinted(session, vitem(price="190", desc="Great phone"))
    a = res.candidates[0].alert
    assert (a.source, a.ebay_item_id) == ("VINTED", "vinted:V1")
    assert a.buyer_fee == D("10.20") and a.postage == D("3.50")               # 5% + £0.70 on £190; assumed postage
    assert a.total_cost == D("203.70") and a.expected_profit == D("53.62")
    assert "estimated" in a.flags and "postage assumed" in a.flags
    assert a.max_item_price == D("222.01")                                     # (237.32 - 3.50 - 0.70) / 1.05, rounded down


@pytest.mark.asyncio
async def test_a_fee_stated_by_vinted_is_used_as_is(session, actor_id):
    await _watch(session, actor_id)
    _, res = await _scan_vinted(session, vitem(price="190", fee="8.00", desc="Fine"))
    a = res.candidates[0].alert
    assert a.buyer_fee == D("8.00") and a.total_cost == D("201.50") and "estimated" not in a.flags


@pytest.mark.asyncio
async def test_the_description_is_read_before_a_title_only_deal_is_trusted(session, actor_id):
    await _watch(session, actor_id)
    v = MockVintedClient()
    v.queue_description("V1", "Cracked screen, battery health 80%")
    v, res = await _scan_vinted(session, vitem(price="190"), vinted=v)
    assert v.description_lookups == ["V1"] and not res.candidates
    assert res.skipped["over your maximum"] == 1                               # now a £60.50 repair: max is only £176.82


@pytest.mark.asyncio
async def test_a_lock_admitted_in_the_description_is_skipped(session, actor_id):
    await _watch(session, actor_id)
    v = MockVintedClient()
    v.queue_description("V1", "Sadly iCloud locked, selling for spares")
    _, res = await _scan_vinted(session, vitem(), vinted=v)
    assert not res.candidates and res.skipped["lock/blacklist risk"] == 1


@pytest.mark.asyncio
async def test_a_seller_saying_it_is_clear_passes_but_is_told_to_verify(session, actor_id):
    await _watch(session, actor_id)
    v = MockVintedClient()
    v.queue_description("V1", "No iCloud lock, no cracks, boxed with charger")
    _, res = await _scan_vinted(session, vitem(), vinted=v)
    a = res.candidates[0].alert
    assert a.bucket == "USED" and "seller says no iCloud" in a.flags


@pytest.mark.asyncio
async def test_an_unreadable_description_is_said_out_loud(session, actor_id):
    await _watch(session, actor_id)
    _, res = await _scan_vinted(session, vitem())                              # mock has no description for it
    assert "description not read" in res.candidates[0].alert.flags


@pytest.mark.asyncio
async def test_listing_page_fetches_are_capped_per_scan(session, actor_id, monkeypatch):
    await _watch(session, actor_id)
    monkeypatch.setattr(deals.settings, "vinted_max_detail_lookups_per_scan", 0)
    v, res = await _scan_vinted(session, vitem())
    assert v.description_lookups == [] and "description not read" in res.candidates[0].alert.flags


@pytest.mark.asyncio
async def test_no_request_is_spent_on_a_model_we_cannot_value(session, actor_id):
    pm = await config_service.resolve_phone_model(session, "iPhone 14")
    await pricing_service.add_to_watchlist(session, pm, "128GB", actor_id)    # watched, but never priced
    v, res = await _scan_vinted(session)
    assert v.searches == [] and res.skipped["no price data yet for this model"] == 1


@pytest.mark.asyncio
async def test_vinted_is_asked_only_for_what_could_ever_qualify(session, actor_id):
    await _watch(session, actor_id)
    v, _ = await _scan_vinted(session)
    assert v.searches[0]["query"] == "iPhone 13 128GB" and v.searches[0]["max_price"] == D("237.32")


@pytest.mark.asyncio
async def test_siblings_and_few_review_sellers(session, actor_id):
    await _watch(session, actor_id)
    v = MockVintedClient()
    v.queue_description("V2", "Lovely")
    _, res = await _scan_vinted(session, vitem("V1", "iPhone 13 Pro 128GB"), vitem("V2", reviews=1), vinted=v)
    assert [c.alert.ebay_item_id for c in res.candidates] == ["vinted:V2"]
    assert res.skipped["wrong model/storage in title"] == 1 and "only 1 review" in res.candidates[0].alert.flags


@pytest.mark.asyncio
async def test_when_vinted_blocks_the_scan_stops_and_keeps_what_it_found(session, actor_id):
    await _watch(session, actor_id)
    await _watch_second(session, actor_id)
    v = MockVintedClient()
    v.block_after_searches = 1
    v.queue_search_results("iphone 13", [vitem("A", desc=None)])
    v.queue_search_results("iphone 12", [vitem("B", "iPhone 12 64GB", "150")])
    res = await deals.scan_vinted(session, v, now=NOW)
    assert res.blocked and res.errors and len(v.searches) == 1                 # it did not push on to the second model
    assert len(res.candidates) <= 1


@pytest.mark.asyncio
async def test_scanning_vinted_when_it_is_off_is_an_error_not_a_silent_nothing(session):
    with pytest.raises(VintedNotConfiguredError):
        await deals.scan_vinted(session, MockVintedClient(configured=False))


@pytest.mark.asyncio
async def test_a_listing_is_never_announced_twice_across_scans(session, actor_id):
    await _watch(session, actor_id)
    v = MockVintedClient()
    v.queue_description("V1", "Fine")
    v, first = await _scan_vinted(session, vitem(), vinted=v)
    _, second = await _scan_vinted(session, vitem(), vinted=v)
    assert len(first.candidates) == 1 and not second.candidates and second.skipped["already announced"] == 1


# ================================================================== the queue: Bought / Reject / Undo / Expire
@pytest.mark.asyncio
async def test_bought_creates_the_phone_with_the_real_cost_and_source(session, actor_id):
    a = await _one_alert(session, actor_id)
    alert, phone = await deal_queue.mark_bought(session, a.id, total_paid=D("205.50"), actor_discord_id=7,
                                                colour="Blue", storage="128gb", notes="boxed", today=dt.date(2026, 10, 26))
    assert alert.status == "BOUGHT" and alert.phone_id == phone.id and alert.decided_by_discord_id == 7
    assert (phone.model, phone.storage, phone.colour) == ("iPhone 13", "128GB", "Blue")
    assert phone.purchase_price == D("205.50") and phone.seller_source == "eBay"
    assert phone.acquisition_type == AcquisitionType.RESALE
    assert a.url in phone.notes and "boxed" in phone.notes and "expected profit £54.32" in phone.notes
    ev = (await session.execute(select(InventoryEvent).where(InventoryEvent.event_type == "DEAL_BOUGHT"))).scalar_one()
    assert ev.entity_id == phone.internal_id and ev.new_state["deal_id"] == a.id


@pytest.mark.asyncio
async def test_a_faulty_deal_becomes_a_repair_phone_and_a_vinted_one_says_vinted(session, actor_id):
    await _watch(session, actor_id)
    v = MockVintedClient()
    v.queue_description("V1", "Cracked screen")
    _, res = await _scan_vinted(session, vitem(price="100"), vinted=v)
    alert, phone = await deal_queue.mark_bought(session, res.candidates[0].alert.id, total_paid=D("114"), actor_discord_id=7)
    assert phone.acquisition_type == AcquisitionType.REPAIR and phone.seller_source == "Vinted"
    assert "Repair estimate £60.50" in phone.notes


@pytest.mark.asyncio
async def test_pressing_bought_twice_creates_one_phone(session, actor_id):
    a = await _one_alert(session, actor_id)
    await deal_queue.mark_bought(session, a.id, total_paid=D("200"), actor_discord_id=7)
    with pytest.raises(deal_queue.DealStateError, match="already bought"):
        await deal_queue.mark_bought(session, a.id, total_paid=D("200"), actor_discord_id=8)
    assert await session.scalar(select(func.count()).select_from(Phone)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("paid", ["0", "-5"])
async def test_a_nonsense_price_is_refused_and_nothing_is_created(session, actor_id, paid):
    a = await _one_alert(session, actor_id)
    with pytest.raises(deal_queue.DealStateError):
        await deal_queue.mark_bought(session, a.id, total_paid=D(paid), actor_discord_id=7)
    assert await session.scalar(select(func.count()).select_from(Phone)) == 0
    assert (await deal_queue.get_alert(session, a.id)).status == "PENDING"


@pytest.mark.asyncio
async def test_reject_undo_and_the_states_that_forbid_them(session, actor_id):
    a = await _one_alert(session, actor_id)
    r = await deal_queue.mark_rejected(session, a.id, actor_discord_id=7, reason="seller looks dodgy")
    assert r.status == "REJECTED" and r.decision_note == "seller looks dodgy"
    with pytest.raises(deal_queue.DealStateError):
        await deal_queue.mark_rejected(session, a.id, actor_discord_id=7)
    with pytest.raises(deal_queue.DealStateError):
        await deal_queue.mark_bought(session, a.id, total_paid=D("100"), actor_discord_id=7)
    back = await deal_queue.restore_rejected(session, a.id, actor_discord_id=7)
    assert back.status == "PENDING" and back.decision_note is None and back.decided_by_discord_id is None
    with pytest.raises(deal_queue.DealStateError, match="nothing to undo"):
        await deal_queue.restore_rejected(session, a.id, actor_discord_id=7)


@pytest.mark.asyncio
async def test_you_cannot_undo_a_reject_once_the_auction_has_ended(session, actor_id):
    _, _, ebay = await _watch(session, actor_id)
    res = await _scan_ebay(session, ebay, item("AU", "iPhone 13 128GB", "150", options=("AUCTION",), ends=NOW + dt.timedelta(hours=2)))
    a = res.candidates[0].alert
    await deal_queue.mark_rejected(session, a.id, actor_discord_id=7)
    with pytest.raises(deal_queue.DealStateError, match="ended"):
        await deal_queue.restore_rejected(session, a.id, actor_discord_id=7, now=NOW + dt.timedelta(hours=3))


@pytest.mark.asyncio
async def test_stale_cards_expire_but_fresh_ones_do_not(session, actor_id):
    _, _, ebay = await _watch(session, actor_id)
    res = await _scan_ebay(session, ebay, item("OLD", "iPhone 13 128GB", "150"), item("NEW", "iPhone 13 128GB", "151"),
                           item("AU", "iPhone 13 128GB", "152", options=("AUCTION",), ends=NOW + dt.timedelta(hours=1)))
    by = {c.alert.ebay_item_id: c.alert for c in res.candidates}
    by["OLD"].created_at = NOW - dt.timedelta(hours=60)
    by["NEW"].created_at = NOW - dt.timedelta(hours=1)
    by["AU"].created_at = NOW - dt.timedelta(hours=1)
    await session.flush()
    expired = await deal_queue.expire_stale(session, now=NOW + dt.timedelta(hours=2), ttl_hours=D("48"))
    assert sorted(a.ebay_item_id for a in expired) == ["AU", "OLD"]
    assert by["OLD"].decision_note == "not acted on in 48h" and by["AU"].decision_note == "auction ended"
    assert by["NEW"].status == "PENDING"


@pytest.mark.asyncio
async def test_counts_and_unposted_cards(session, actor_id):
    _, _, ebay = await _watch(session, actor_id)
    res = await _scan_ebay(session, ebay, item("A", "iPhone 13 128GB", "150"), item("B", "iPhone 13 128GB", "151"))
    ids = [c.alert.id for c in res.candidates]
    await deal_queue.mark_rejected(session, ids[0], actor_discord_id=7)
    c = await deal_queue.counts(session)
    assert (c.pending, c.rejected, c.bought) == (1, 1, 0) and c.pending_profit > 0
    assert [a.id for a in await deal_queue.unposted_pending(session)] == [ids[1]]
    await deal_queue.record_message(session, ids[1], 555, 777)
    assert await deal_queue.unposted_pending(session) == []


# ================================================================== the card
@pytest.mark.asyncio
async def test_the_card_leads_with_the_numbers_and_fits_discords_limits(session, actor_id):
    _, _, ebay = await _watch(session, actor_id)
    res = await _scan_ebay(session, ebay, item("F1", "iPhone 13 128GB cracked screen " + "x" * 400, "120"))
    a = res.candidates[0].alert
    a.flags = "; ".join(f"flag number {i} " + "y" * 60 for i in range(30))[:600]
    a.created_at = NOW
    e = deal_cards.deal_embed(a, NOW)
    names = [f.name for f in e.fields]
    assert names[:4] == ["You'd pay", "Sells for", "Profit", "Pay up to"] and "Repair estimate" in names
    assert len(e) <= 6000 and len(e.title) <= 256 and all(len(f.value) <= 1024 for f in e.fields)
    assert e.url == a.url


@pytest.mark.asyncio
async def test_a_working_phones_card_has_no_repair_line_and_decided_cards_say_so(session, actor_id):
    a = await _one_alert(session, actor_id)
    a.created_at = NOW
    assert "Repair estimate" not in [f.name for f in deal_cards.deal_embed(a, NOW).fields]
    a.status, a.decided_by_discord_id = "BOUGHT", 7
    e = deal_cards.deal_embed(a, NOW)
    assert e.title.startswith("✅ BOUGHT") and "<@7>" in e.description
    a.status = "REJECTED"
    assert deal_cards.deal_embed(a, NOW).title.startswith("❌ REJECTED")


@pytest.mark.asyncio
async def test_each_state_offers_the_right_buttons(session, actor_id):
    a = await _one_alert(session, actor_id)

    def ids(view):
        return [getattr(c, "custom_id", None) or c.url for c in view.children]

    assert ids(deal_cards.deal_view(a)) == [f"deal:bought:{a.id}", f"deal:reject:{a.id}", a.url]
    a.status = "REJECTED"
    assert ids(deal_cards.deal_view(a)) == [f"deal:undo:{a.id}", a.url]
    a.status = "BOUGHT"
    assert ids(deal_cards.deal_view(a)) == [a.url]


def test_button_ids_round_trip_through_the_template():
    m = deal_cards.DealButton.__discord_ui_compiled_template__.fullmatch("deal:bought:123")
    assert m and (m["act"], m["id"]) == ("bought", "123")
    assert not deal_cards.DealButton.__discord_ui_compiled_template__.fullmatch("deal:bought:abc")


# ================================================================== the buttons
@pytest.mark.asyncio
async def test_reject_is_one_tap_and_greys_the_card_out(session, scope, actor_id):
    a = await _one_alert(session, actor_id)
    await session.commit()
    i = interaction(user_id=9)
    await deal_cards.DealButton("reject", a.id).callback(i)
    kwargs = i.response.edit_message.await_args.kwargs
    assert kwargs["embed"].title.startswith("❌ REJECTED")
    assert [getattr(c, "custom_id", "") for c in kwargs["view"].children][0] == f"deal:undo:{a.id}"
    async with scope() as s:
        assert (await deal_queue.get_alert(s, a.id)).decided_by_discord_id == 9


@pytest.mark.asyncio
async def test_a_second_press_is_told_why_and_the_stale_card_is_refreshed(session, scope, actor_id):
    a = await _one_alert(session, actor_id)
    await session.commit()
    await deal_cards.DealButton("reject", a.id).callback(interaction())
    i = interaction()
    await deal_cards.DealButton("reject", a.id).callback(i)
    assert i.response.send_message.await_args.kwargs["ephemeral"] is True
    i.message.edit.assert_awaited_once()                                       # the card shown to this person is brought up to date
    i.response.edit_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_undo_puts_the_buttons_back(session, scope, actor_id):
    a = await _one_alert(session, actor_id)
    await session.commit()
    await deal_cards.DealButton("reject", a.id).callback(interaction())
    i = interaction()
    await deal_cards.DealButton("undo", a.id).callback(i)
    view = i.response.edit_message.await_args.kwargs["view"]
    assert [getattr(c, "custom_id", "") for c in view.children][:2] == [f"deal:bought:{a.id}", f"deal:reject:{a.id}"]


@pytest.mark.asyncio
async def test_bought_opens_a_form_prefilled_with_the_all_in_price(session, scope, actor_id):
    a = await _one_alert(session, actor_id)
    await session.commit()
    i = interaction()
    await deal_cards.DealButton("bought", a.id).callback(i)
    modal = i.response.send_modal.await_args.args[0]
    assert isinstance(modal, deal_cards.BoughtModal)
    assert modal.paid.default == "203.00" and modal.storage.default == "128GB"
    assert len(modal.children) == 5 and all(len(c.label) <= 45 for c in modal.children)


@pytest.mark.asyncio
async def test_submitting_the_form_records_the_phone_and_updates_the_card(session, scope, actor_id):
    a = await _one_alert(session, actor_id)
    await session.commit()
    modal = deal_cards.BoughtModal(a)
    for box, text in (("paid", "£205.50"), ("colour", "Blue"), ("imei", "356789012345678"), ("notes", "boxed")):
        getattr(modal, box)._value = text
    i = interaction(user_id=5)
    await modal.on_submit(i)
    assert i.response.edit_message.await_args.kwargs["embed"].title.startswith("✅ BOUGHT")
    assert "Added to inventory" in i.followup.send.await_args.kwargs["embed"].title
    async with scope() as s:
        phone = (await s.execute(select(Phone))).scalar_one()
        assert (phone.purchase_price, phone.imei, phone.colour) == (D("205.50"), "356789012345678", "Blue")
        assert (await deal_queue.get_alert(s, a.id)).status == "BOUGHT"


@pytest.mark.asyncio
async def test_a_bad_price_in_the_form_creates_nothing(session, scope, actor_id):
    a = await _one_alert(session, actor_id)
    await session.commit()
    modal = deal_cards.BoughtModal(a)
    modal.paid._value = "two hundred"
    i = interaction()
    await modal.on_submit(i)
    assert i.response.send_message.await_args.kwargs["ephemeral"] is True
    async with scope() as s:
        assert await s.scalar(select(func.count()).select_from(Phone)) == 0
        assert (await deal_queue.get_alert(s, a.id)).status == "PENDING"


@pytest.mark.asyncio
async def test_strangers_cannot_press_the_buttons(monkeypatch):
    monkeypatch.setattr(deal_cards, "_is_authorized", AsyncMock(return_value=False))
    i = interaction()
    assert await deal_cards.DealButton("bought", 1).interaction_check(i) is False
    assert i.response.send_message.await_args.kwargs["ephemeral"] is True
    monkeypatch.setattr(deal_cards, "_is_authorized", AsyncMock(return_value=True))
    assert await deal_cards.DealButton("bought", 1).interaction_check(interaction()) is True


# ================================================================== posting cards
class FakeChannel:
    def __init__(self, cid):
        self.id = cid
        self.sent = []

    async def send(self, **kw):
        self.sent.append(kw)
        return MagicMock(id=9000 + len(self.sent))


def fake_bot(*channels):
    bot = MagicMock()
    by_id = {c.id: c for c in channels}
    bot.get_channel = lambda cid: by_id.get(cid)
    bot.fetch_channel = AsyncMock(side_effect=lambda cid: by_id[cid])
    return bot


@pytest.mark.asyncio
async def test_cards_go_to_the_buy_page_once_each(session, scope, actor_id):
    a = await _one_alert(session, actor_id)
    await config_service.add_channel(session, 1, 111, "buy-page")
    await session.commit()
    page = FakeChannel(111)
    bot = fake_bot(page)
    assert await deal_cards.post_deal_cards(bot, [a.id]) == 1
    assert await deal_cards.post_deal_cards(bot, [a.id]) == 0                  # already has a card: never posted twice
    assert len(page.sent) == 1 and page.sent[0]["embed"].url == a.url
    async with scope() as s:
        row = await deal_queue.get_alert(s, a.id)
        assert (row.message_channel_id, row.message_id) == (111, 9001)


@pytest.mark.asyncio
async def test_without_a_buy_page_the_command_channel_is_the_fallback_and_with_nowhere_nothing_is_lost(session, scope, actor_id):
    a = await _one_alert(session, actor_id)
    await session.commit()
    here = FakeChannel(222)
    assert await deal_cards.post_deal_cards(fake_bot(), [a.id], fallback_channel=here) == 1
    b = FakeChannel(333)
    assert await deal_cards.post_deal_cards(fake_bot(b), []) == 0
    async with scope() as s:
        c = await deal_queue.counts(s)
    assert c.pending == 1                                                       # still pending: /buy-page can post it later


@pytest.mark.asyncio
async def test_a_card_that_never_got_posted_stays_findable(session, scope, actor_id):
    a = await _one_alert(session, actor_id)
    await session.commit()
    assert await deal_cards.post_deal_cards(fake_bot(), [a.id]) == 0            # nowhere to post
    async with scope() as s:
        assert [x.id for x in await deal_queue.unposted_pending(s)] == [a.id]


@pytest.mark.asyncio
async def test_refreshing_a_card_edits_the_message_in_place(session, actor_id):
    a = await _one_alert(session, actor_id)
    a.message_channel_id, a.message_id, a.status, a.created_at = 111, 5, "EXPIRED", NOW
    msg = MagicMock()
    msg.edit = AsyncMock()
    chan = MagicMock(id=111)
    chan.get_partial_message = lambda mid: msg
    await deal_cards.refresh_card(MagicMock(get_channel=lambda cid: chan), a)
    assert msg.edit.await_args.kwargs["embed"].title.startswith("⌛ EXPIRED")


# ================================================================== the scheduler
def make_scheduler(vinted, post=None, refresh=None, notify=None):
    return sched_mod.JobScheduler(MockEbayClient(), MagicMock(), notify or AsyncMock(), vinted_client=vinted,
                                  deal_post_fn=post, deal_refresh_fn=refresh)


@pytest.mark.asyncio
async def test_a_vinted_tick_posts_the_cards_it_found(session, scope, actor_id):
    await _watch(session, actor_id)
    await session.commit()
    v = MockVintedClient()
    v.queue_search_results("iphone 13", [vitem()])
    v.queue_description("V1", "Fine, no issues")
    post = AsyncMock(return_value=1)
    s = make_scheduler(v, post=post)
    await s._vinted_tick()
    assert s.state.vinted.last_success is True and "1 deal(s)" in s.state.vinted.last_summary
    ids = post.await_args.args[0]
    assert len(ids) == 1


@pytest.mark.asyncio
async def test_a_block_is_announced_once_and_recovery_once(session, scope, actor_id):
    await _watch(session, actor_id)
    await session.commit()
    v = MockVintedClient()
    v.block_after_searches = 0
    notify = AsyncMock()
    s = make_scheduler(v, post=AsyncMock(), notify=notify)
    await s._vinted_tick()
    await s._vinted_tick()
    assert notify.await_count == 1 and "blocking" in notify.await_args.args[0]
    assert s.state.vinted.last_success is False and "BLOCKED" in s.state.vinted.last_summary
    v.block_after_searches = None
    await s._vinted_tick()
    assert notify.await_count == 2 and "working again" in notify.await_args.args[0]


@pytest.mark.asyncio
async def test_the_expiry_tick_greys_out_cards_that_have_lapsed(session, scope, actor_id):
    a = await _one_alert(session, actor_id)
    a.created_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=100)
    await session.commit()
    refresh = AsyncMock()
    await make_scheduler(MockVintedClient(), refresh=refresh)._expiry_tick()
    assert refresh.await_args.args[0] == [a.id]
    async with scope() as s:
        assert (await deal_queue.get_alert(s, a.id)).status == "EXPIRED"


@pytest.mark.asyncio
async def test_without_a_card_poster_deals_still_arrive_as_text(session, scope, actor_id):
    _, _, ebay = await _watch(session, actor_id)
    await session.commit()
    ebay.queue_search_results("iphone 13", [item("E1", "iPhone 13 128GB", "200")])
    notify = AsyncMock()
    s = sched_mod.JobScheduler(ebay, MagicMock(), notify)
    await s._deals_tick()
    assert "£237.32" in notify.await_args.args[0]


# ================================================================== the slash commands
def make_cog(ebay=None, vinted=None, scheduler=None):
    bot = MagicMock()
    bot.ebay_client, bot.vinted_client, bot.job_scheduler = ebay or MockEbayClient(), vinted or MockVintedClient(configured=False, reason="Vinted scanning is off (VINTED_ENABLED=false)."), scheduler
    return deals_cog.DealsCog(bot)


def slash(channel=None):
    i = interaction()
    i.response.defer = AsyncMock()
    i.channel = channel or FakeChannel(1)
    return i


def said(i) -> str:
    call = i.followup.send.await_args or i.response.send_message.await_args
    return (call.args[0] if call.args else "") + " ".join(str(f.value) for f in getattr(call.kwargs.get("embed"), "fields", []) or [])


@pytest.mark.asyncio
async def test_deal_scan_looks_everywhere_and_posts_the_cards(session, scope, actor_id):
    _, _, ebay = await _watch(session, actor_id)
    await session.commit()
    ebay.queue_search_results("iphone 13", [item("E1", "iPhone 13 128GB", "200")])
    v = MockVintedClient()
    v.queue_search_results("iphone 13", [vitem()])
    v.queue_description("V1", "Fine")
    here = FakeChannel(1)
    i = slash(here)
    await deals_cog.DealsCog.deal_scan.callback(make_cog(ebay, v), i, None)
    text = said(i)
    assert "**eBay**" in text and "**Vinted**" in text and "2** new card(s)" in text
    assert len(here.sent) == 2


@pytest.mark.asyncio
async def test_asking_for_vinted_while_it_is_off_says_why_instead_of_nothing(session, scope, actor_id):
    await _watch(session, actor_id)
    await session.commit()
    i = slash()
    await deals_cog.DealsCog.deal_scan.callback(make_cog(), i, app_commands.Choice(name="Vinted only", value="vinted"))
    assert "VINTED_ENABLED" in said(i) and "aren't watching" not in said(i)


@pytest.mark.asyncio
async def test_deal_scan_with_nothing_watched_points_at_the_next_step(session, scope):
    i = slash()
    await deals_cog.DealsCog.deal_scan.callback(make_cog(), i, None)
    assert "/pricing watch" in i.followup.send.await_args.kwargs["embed"].description


@pytest.mark.asyncio
async def test_buy_page_command_posts_what_is_waiting(session, scope, actor_id):
    await _one_alert(session, actor_id)
    await session.commit()
    here = FakeChannel(1)
    i = slash(here)
    await deals_cog.DealsCog.buy_page.callback(make_cog(), i)
    assert len(here.sent) == 1 and "Posted **1** card(s)" in said(i) and "1** deal(s) are waiting" in said(i)


@pytest.mark.asyncio
async def test_deals_lists_by_status_and_shows_the_counts(session, scope, actor_id):
    a = await _one_alert(session, actor_id)
    await session.commit()
    i = slash()
    await deals_cog.DealsCog.deals.callback(make_cog(), i, None)
    text = said(i)
    assert "**1** waiting" in text and f"#{a.id}" in text and "£203.00 all in" in text and "+£54.32" in text
    i = slash()
    await deals_cog.DealsCog.deals.callback(make_cog(), i, app_commands.Choice(name="Bought", value="BOUGHT"))
    assert "Nothing bought" in said(i)


@pytest.mark.asyncio
async def test_deal_status_reports_each_source_and_the_queue(session, scope, actor_id):
    await _one_alert(session, actor_id)
    await session.commit()
    s = make_scheduler(MockVintedClient(configured=False))
    i = slash()
    await deals_cog.DealsCog.deal_status.callback(make_cog(scheduler=s), i)
    names = [f.name for f in i.response.send_message.await_args.kwargs["embed"].fields]
    assert names == ["eBay scan", "Vinted", "Buy page"] and "Off:" in said(i) and "1 waiting" in said(i)
