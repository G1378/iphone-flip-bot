from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from app.models.enums import AcquisitionType, PartSourceType, TestOutcome
from app.services import config_service, finance, parts, phones, repairs


async def _make_repaired_phone(session, actor_id, today, purchase_price="250.00", part_cost="70.00"):
    data = phones.BuyPhoneInput(
        manufacturer="Apple", model="iPhone 13 Pro", variant=None, storage="128GB", colour="Graphite",
        imei="999888777", serial_number="SN999", carrier="Unlocked",
        purchase_price=Decimal(purchase_price), purchase_date=today, seller_source="Auction",
        acquisition_type=AcquisitionType.REPAIR, notes=None, location_code=None,
    )
    phone = await phones.buy_phone(session, data, actor_id)
    screen_type = await config_service.get_or_create_part_type(session, "Screen")
    await phones.add_fault(session, phone, "Cracked screen", actor_id, required_part_type_id=screen_type.id)

    repair = await repairs.create_repair_for_open_faults(session, phone, actor_id)
    rp = repair.parts[0]

    part_data = parts.CreatePartInput(
        part_type_name="Screen", source_type=PartSourceType.DONOR, cost=Decimal(part_cost),
        testing_status=TestOutcome.PASS,
    )
    part = await parts.create_part(session, part_data, actor_id)

    await parts.reserve_part(session, part, repair, rp, actor_id)
    await parts.install_part(session, part, phone, rp, actor_id)
    await repairs.complete_repair(session, repair, actor_id)

    return phone


@pytest.mark.asyncio
async def test_true_cost_breakdown_includes_purchase_and_installed_part(session, actor_id, today):
    phone = await _make_repaired_phone(session, actor_id, today, purchase_price="250.00", part_cost="70.00")
    breakdown = await finance.compute_true_cost(session, phone)

    assert breakdown.purchase_cost == Decimal("250.00")
    assert breakdown.donor_allocated_cost == Decimal("70.00")
    assert breakdown.purchased_part_cost == Decimal("0")
    assert breakdown.total == Decimal("320.00")


@pytest.mark.asyncio
async def test_analyse_phone_recommends_good_when_profitable(session, actor_id, today):
    phone = await _make_repaired_phone(session, actor_id, today, purchase_price="150.00", part_cost="40.00")
    analysis = await finance.analyse_phone(session, phone, expected_sale_price=Decimal("399.00"))

    assert analysis.breakdown.total == Decimal("190.00")
    assert analysis.expected_profit > 0
    assert analysis.recommendation == "GOOD"


@pytest.mark.asyncio
async def test_analyse_phone_recommends_not_worth_when_loss_making(session, actor_id, today):
    phone = await _make_repaired_phone(session, actor_id, today, purchase_price="380.00", part_cost="70.00")
    analysis = await finance.analyse_phone(session, phone, expected_sale_price=Decimal("399.00"))

    assert analysis.expected_profit < 0
    assert analysis.recommendation == "NOT_WORTH"


@pytest.mark.asyncio
async def test_record_sale_creates_reproducible_profit_snapshot(session, actor_id, today):
    phone = await _make_repaired_phone(session, actor_id, today, purchase_price="250.00", part_cost="70.00")
    sold_at = dt.datetime(2026, 9, 10, tzinfo=dt.timezone.utc)

    sale = await finance.record_sale(
        session, phone, sale_price=Decimal("399.00"), sold_at=sold_at, actor_discord_id=actor_id,
        ebay_order_id="EBAY-ORDER-1", ebay_fees=Decimal("55.00"), postage_cost=Decimal("6.00"),
        packaging_cost=Decimal("1.00"),
    )

    breakdown = await finance.get_sale_breakdown(session, sale)
    assert breakdown is not None
    assert breakdown.purchase_cost == Decimal("250.00")
    assert breakdown.donor_allocated_cost == Decimal("70.00")
    assert breakdown.sale_revenue == Decimal("399.00")
    assert breakdown.ebay_fees == Decimal("55.00")
    # total_cost = 250 + 70 + 6 (postage) + 1 (packaging) + 55 (fees) = 382
    assert breakdown.total_cost == Decimal("382.00")
    assert breakdown.net_profit == Decimal("17.00")

    await session.refresh(phone)
    assert phone.current_status == "SOLD"


@pytest.mark.asyncio
async def test_record_sale_is_idempotent_on_ebay_order_id(session, actor_id, today):
    """Spec: duplicate eBay order handling - re-syncing the same order must
    not create a second Sale row or double-count profit."""
    phone = await _make_repaired_phone(session, actor_id, today)
    sold_at = dt.datetime(2026, 9, 10, tzinfo=dt.timezone.utc)

    sale1 = await finance.record_sale(
        session, phone, sale_price=Decimal("399.00"), sold_at=sold_at, actor_discord_id=actor_id,
        ebay_order_id="EBAY-DUP-1", ebay_fees=Decimal("55.00"),
    )
    sale2 = await finance.record_sale(
        session, phone, sale_price=Decimal("399.00"), sold_at=sold_at, actor_discord_id=actor_id,
        ebay_order_id="EBAY-DUP-1", ebay_fees=Decimal("55.00"),
    )

    assert sale1.id == sale2.id

    from sqlalchemy import func, select
    from app.models.commerce_models import Sale
    count = (await session.execute(
        select(func.count(Sale.id)).where(Sale.ebay_order_id == "EBAY-DUP-1")
    )).scalar_one()
    assert count == 1


@pytest.mark.asyncio
async def test_period_report_aggregates_sales_in_range(session, actor_id, today):
    phone1 = await _make_repaired_phone(session, actor_id, today, purchase_price="200.00", part_cost="50.00")
    phone2 = await _make_repaired_phone(session, actor_id, today, purchase_price="200.00", part_cost="50.00")

    await finance.record_sale(
        session, phone1, sale_price=Decimal("350.00"), sold_at=dt.datetime(2026, 9, 5, tzinfo=dt.timezone.utc),
        actor_discord_id=actor_id, ebay_order_id="ORDER-A", ebay_fees=Decimal("40.00"),
    )
    await finance.record_sale(
        session, phone2, sale_price=Decimal("360.00"), sold_at=dt.datetime(2026, 9, 20, tzinfo=dt.timezone.utc),
        actor_discord_id=actor_id, ebay_order_id="ORDER-B", ebay_fees=Decimal("40.00"),
    )

    report = await finance.period_report(session, dt.date(2026, 9, 1), dt.date(2026, 9, 10))
    assert report.phones_sold == 1
    assert report.revenue == Decimal("350.00")

    full_month = await finance.period_report(session, dt.date(2026, 9, 1), dt.date(2026, 9, 30))
    assert full_month.phones_sold == 2
    assert full_month.revenue == Decimal("710.00")
