from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from app.integrations.ebay.interface import EbayNotConfiguredError
from app.integrations.ebay.mock import MockEbayClient
from app.jobs.ebay_sync_job import sync_ebay_orders
from app.models.enums import AcquisitionType
from app.services import finance, listings, phones


async def _make_listed_phone(session, actor_id, today, ebay_sku_will_match=True):
    data = phones.BuyPhoneInput(
        manufacturer="Apple", model="iPhone 12", variant=None, storage="64GB", colour="Black",
        imei="111222333", serial_number="SN111", carrier="Unlocked",
        purchase_price=Decimal("150.00"), purchase_date=today, seller_source="Trade-in",
        acquisition_type=AcquisitionType.RESALE, notes=None, location_code=None,
    )
    phone = await phones.buy_phone(session, data, actor_id)

    draft = listings.DraftListingInput(
        title="iPhone 12 64GB Black - Unlocked - Excellent", description="Tested and working.",
        condition_text="Excellent", price=Decimal("280.00"),
    )
    listing = await listings.create_draft(session, phone, draft, actor_id)
    return phone, listing


@pytest.mark.asyncio
async def test_publish_listing_via_mock_ebay_client(session, actor_id, today):
    phone, listing = await _make_listed_phone(session, actor_id, today)
    ebay = MockEbayClient(configured=True)

    published = await listings.publish(session, listing, ebay, actor_id)
    assert published.status == "ACTIVE"
    assert published.ebay_listing_id is not None

    await session.refresh(phone)
    assert phone.current_status == "LISTED"


@pytest.mark.asyncio
async def test_publish_raises_clean_error_when_ebay_not_configured(session, actor_id, today):
    phone, listing = await _make_listed_phone(session, actor_id, today)
    ebay = MockEbayClient(configured=False)

    with pytest.raises(EbayNotConfiguredError):
        await listings.publish(session, listing, ebay, actor_id)

    # nothing should have changed - listing stays DRAFT, phone stays PURCHASED
    assert listing.status == "DRAFT"
    await session.refresh(phone)
    assert phone.current_status == "PURCHASED"


@pytest.mark.asyncio
async def test_ebay_sync_job_marks_phone_sold_and_computes_profit(session, actor_id, today):
    phone, listing = await _make_listed_phone(session, actor_id, today)
    ebay = MockEbayClient(configured=True)
    published = await listings.publish(session, listing, ebay, actor_id)

    ebay.queue_sale(
        order_id="EBAY-ORD-100", sku=published.internal_id, price=Decimal("280.00"), fees=Decimal("36.00"),
        created_at=dt.datetime(2026, 9, 10, tzinfo=dt.timezone.utc),
    )

    results = await sync_ebay_orders(session, ebay, since=dt.datetime(2026, 9, 1, tzinfo=dt.timezone.utc))
    assert len(results) == 1

    await session.refresh(phone)
    assert phone.current_status == "SOLD"

    from sqlalchemy import select
    from app.models.commerce_models import Sale
    sale = (await session.execute(select(Sale).where(Sale.ebay_order_id == "EBAY-ORD-100"))).scalar_one()
    breakdown = await finance.get_sale_breakdown(session, sale)
    assert breakdown.sale_revenue == Decimal("280.00")
    assert breakdown.ebay_fees == Decimal("36.00")


@pytest.mark.asyncio
async def test_ebay_sync_job_is_idempotent_on_repeated_runs(session, actor_id, today):
    """Running the sync job twice for the same order must not create a
    second Sale or double-mark the phone sold."""
    phone, listing = await _make_listed_phone(session, actor_id, today)
    ebay = MockEbayClient(configured=True)
    published = await listings.publish(session, listing, ebay, actor_id)

    ebay.queue_sale(
        order_id="EBAY-ORD-DUP", sku=published.internal_id, price=Decimal("280.00"), fees=Decimal("36.00"),
        created_at=dt.datetime(2026, 9, 10, tzinfo=dt.timezone.utc),
    )
    since = dt.datetime(2026, 9, 1, tzinfo=dt.timezone.utc)

    first_run = await sync_ebay_orders(session, ebay, since=since)
    second_run = await sync_ebay_orders(session, ebay, since=since)

    assert len(first_run) == 1
    assert len(second_run) == 1  # still "seen", but not duplicated

    from sqlalchemy import func, select
    from app.models.commerce_models import Sale
    count = (await session.execute(
        select(func.count(Sale.id)).where(Sale.ebay_order_id == "EBAY-ORD-DUP")
    )).scalar_one()
    assert count == 1
