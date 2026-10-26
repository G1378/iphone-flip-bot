"""Best Offer (auto-decline below your profit floor) and a sturdier order sync."""
from __future__ import annotations

import datetime as dt
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.integrations.ebay.client import EbayClient, _parse_order
from app.integrations.ebay.mock import MockEbayClient
from app.jobs.ebay_sync_job import sync_ebay_orders
from app.models.commerce_models import Sale
from app.services import config_service, finance, listing_builder, listings, phones
from tests.test_ebay_listing_flow import _configure_ebay_account, _live_listing, _ready_phone, _panel
from tests.test_ebay_sync import _make_listed_phone

pytestmark = pytest.mark.asyncio

SINCE = dt.datetime(2026, 9, 1, tzinfo=dt.timezone.utc)
WHEN = dt.datetime(2026, 9, 10, tzinfo=dt.timezone.utc)


# ------------------------------------------------------------------ the profit floor
async def test_minimum_sale_price_agrees_with_analyse(session, actor_id):
    phone = await _ready_phone(session, actor_id)
    floor = await finance.minimum_sale_price(session, phone)
    assert (await finance.analyse_phone(session, phone, expected_sale_price=floor)).recommendation == "GOOD"
    assert (await finance.analyse_phone(session, phone, expected_sale_price=floor - 1)).recommendation != "GOOD"


async def test_the_floor_follows_the_settings(session, actor_id):
    phone = await _ready_phone(session, actor_id)
    base = await finance.minimum_sale_price(session, phone)
    await config_service.set_setting(session, "min_profit_gbp", "80")       # /config business ...
    raised = await finance.minimum_sale_price(session, phone)
    assert raised > base, "changing the setting must actually change the floor"
    assert (await finance.analyse_phone(session, phone, expected_sale_price=raised)).recommendation == "GOOD"

    from app.models.config_models import PricingRule      # a rule for this exact model still wins
    model = await config_service.phone_model_for(session, phone)
    session.add(PricingRule(phone_model_id=model.id, min_profit_gbp=Decimal("30"), min_roi_pct=Decimal("0")))
    await session.flush()
    assert await finance.minimum_sale_price(session, phone) < raised
    assert await config_service.pricing_thresholds(session, model.id) == (Decimal("30"), Decimal("0"), Decimal("25.00"))


# ------------------------------------------------------------------ Best Offer terms
async def _draft(session, actor_id, price):
    phone = await _ready_phone(session, actor_id)
    draft, _ = await listings.build_or_refresh_draft(session, phone, actor_id)
    draft.price = Decimal(price)
    return phone, draft


async def test_best_offer_is_on_and_declines_offers_that_lose_you_money(session, actor_id):
    phone, draft = await _draft(session, actor_id, "300")
    terms = await listing_builder.best_offer_terms(session, draft, phone)
    assert terms.enabled and terms.auto_decline == await finance.minimum_sale_price(session, phone)
    assert terms.auto_accept is None, "never accepts by itself unless you ask it to"
    assert "declined automatically" in terms.summary()


async def test_best_offer_auto_accept_is_opt_in_and_sits_between_floor_and_price(session, actor_id):
    phone, draft = await _draft(session, actor_id, "300")
    await config_service.set_setting(session, "best_offer_auto_accept_pct", "95")
    terms = await listing_builder.best_offer_terms(session, draft, phone)
    assert terms.auto_accept == Decimal("285")
    await config_service.set_setting(session, "best_offer_auto_accept_pct", "50")      # would be under the floor
    assert (await listing_builder.best_offer_terms(session, draft, phone)).auto_accept is None


async def test_best_offer_is_off_when_the_price_leaves_no_room_or_you_turn_it_off(session, actor_id):
    phone, draft = await _draft(session, actor_id, "150")
    terms = await listing_builder.best_offer_terms(session, draft, phone)
    assert not terms.enabled and "no room" in terms.note
    draft.price = Decimal("300")
    await config_service.set_setting(session, "best_offer_enabled", "false")
    assert not (await listing_builder.best_offer_terms(session, draft, phone)).enabled


async def test_publish_sends_best_offer_terms_to_ebay(session, actor_id):
    ebay = MockEbayClient()
    live = await _live_listing(session, actor_id, ebay)       # priced £299
    sent = ebay.listings[live.ebay_listing_id]
    assert sent["best_offer_enabled"] is True
    assert sent["best_offer_auto_decline"] == await finance.minimum_sale_price(session, live.phone)


async def test_the_panel_tells_you_what_best_offer_will_do(session, actor_id):
    phone, draft = await _draft(session, actor_id, "300")
    panel = await _panel(session, phone)
    assert "Best Offer on. Offers below £" in panel.body


# ------------------------------------------------------------------ reading eBay's order format
def _raw(**over):
    raw = {"orderId": "O1", "creationDate": "2026-09-10T10:00:00.000Z",
           "lineItems": [{"sku": "LS-000001", "quantity": 1, "lineItemCost": {"value": "280.00"}}],
           "pricingSummary": {"total": {"value": "286.00"}, "deliveryCost": {"value": "6.00"}},
           "buyer": {"username": "bob"}, "orderFulfillmentStatus": "NOT_STARTED", "orderPaymentStatus": "PAID"}
    raw.update(over)
    return raw


def test_order_parsing_reads_fees_postage_payment_and_cancellation():
    o = _parse_order(_raw(totalMarketplaceFee={"value": "36.80", "currency": "GBP"},
                          cancelStatus={"cancelState": "NONE_REQUESTED"}))
    assert (o.total_fees, o.delivery_cost, o.total_amount) == (Decimal("36.80"), Decimal("6.00"), Decimal("286.00"))
    assert (o.payment_status, o.cancel_state, o.buyer_username) == ("PAID", "NONE_REQUESTED", "bob")


def test_order_parsing_falls_back_and_reports_zero_when_ebay_gives_no_fee():
    old = _parse_order(_raw(paymentSummary={"payments": [{"marketplaceFees": [{"value": "10"}, {"value": "2.5"}]}]}))
    assert old.total_fees == Decimal("12.5")
    assert _parse_order(_raw()).total_fees == Decimal("0")


async def test_every_page_of_orders_is_fetched(monkeypatch):
    client = EbayClient()
    client.configured = True
    pages = [{"total": 5, "orders": [_raw(orderId=f"O{i}") for i in range(2)]},
             {"total": 5, "orders": [_raw(orderId=f"O{i}") for i in range(2, 4)]},
             {"total": 5, "orders": [_raw(orderId="O4")]}]
    calls = []

    async def fake_request(method, path, **kw):
        calls.append(kw["params"]["offset"])
        return SimpleNamespace(status_code=200, json=lambda: pages[len(calls) - 1], text="")
    monkeypatch.setattr(client, "_request", fake_request)
    orders = await client.get_orders_since(SINCE)
    assert [o.order_id for o in orders] == ["O0", "O1", "O2", "O3", "O4"] and calls == ["0", "2", "4"]


# ------------------------------------------------------------------ the sync
async def _listed(session, actor_id, today):
    phone, listing = await _make_listed_phone(session, actor_id, today)
    ebay = MockEbayClient()
    published = await listings.publish(session, listing, ebay, actor_id)
    return phone, published, ebay


async def _sale(session, order_id):
    return (await session.execute(select(Sale).where(Sale.ebay_order_id == order_id))).scalar_one_or_none()


async def test_missing_fees_are_estimated_not_recorded_as_zero(session, actor_id, today):
    phone, published, ebay = await _listed(session, actor_id, today)
    ebay.queue_sale(order_id="O-NOFEE", sku=published.internal_id, price=Decimal("280.00"), fees=Decimal("0"), created_at=WHEN)
    [r] = await sync_ebay_orders(session, ebay, since=SINCE)
    sale = await _sale(session, "O-NOFEE")
    assert sale.ebay_fees == Decimal("36.14")                       # 12.8% of 280 + 0.30
    assert "estimated" in r.note and "estimated" in sale.notes



async def test_real_fees_are_used_as_reported(session, actor_id, today):
    phone, published, ebay = await _listed(session, actor_id, today)
    ebay.queue_sale(order_id="O-FEE", sku=published.internal_id, price=Decimal("280.00"), fees=Decimal("31.00"), created_at=WHEN)
    [r] = await sync_ebay_orders(session, ebay, since=SINCE)
    assert (await _sale(session, "O-FEE")).ebay_fees == Decimal("31.00") and r.note is None


async def test_an_unpaid_order_waits_and_is_recorded_once_paid(session, actor_id, today):
    phone, published, ebay = await _listed(session, actor_id, today)
    ebay.queue_sale(order_id="O-WAIT", sku=published.internal_id, price=Decimal("280"), fees=Decimal("30"),
                    created_at=WHEN, payment_status="PENDING")
    [r] = await sync_ebay_orders(session, ebay, since=SINCE)
    assert not r.matched and "waiting" in r.note and await _sale(session, "O-WAIT") is None
    await session.refresh(phone)
    assert phone.current_status == "LISTED"

    ebay.pending_orders[0].payment_status = "PAID"
    await sync_ebay_orders(session, ebay, since=SINCE)
    assert await _sale(session, "O-WAIT") is not None
    await session.refresh(phone)
    assert phone.current_status == "SOLD"


async def test_a_cancelled_order_is_not_recorded_and_a_later_cancel_is_flagged(session, actor_id, today):
    phone, published, ebay = await _listed(session, actor_id, today)
    ebay.queue_sale(order_id="O-CAN", sku=published.internal_id, price=Decimal("280"), fees=Decimal("30"),
                    created_at=WHEN, cancel_state="CANCELED")
    [r] = await sync_ebay_orders(session, ebay, since=SINCE)
    assert "Cancelled on eBay" in r.note and await _sale(session, "O-CAN") is None

    ebay.pending_orders.clear()
    ebay.queue_sale(order_id="O-LATER", sku=published.internal_id, price=Decimal("280"), fees=Decimal("30"), created_at=WHEN)
    await sync_ebay_orders(session, ebay, since=SINCE)
    ebay.pending_orders[0].cancel_state = "CANCELED"
    [again] = await sync_ebay_orders(session, ebay, since=SINCE)
    assert again.already_recorded and "was cancelled" in again.note
