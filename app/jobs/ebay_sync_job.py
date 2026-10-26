from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.integrations.ebay.interface import EbayApiError, EbayClientInterface, EbayNotConfiguredError, EbayOrder
from app.models.commerce_models import Listing
from app.models.enums import SaleSource
from app.services import config_service, finance

logger = logging.getLogger(__name__)


@dataclass
class SyncedOrderResult:
    ebay_order_id: str
    phone_internal_id: Optional[str]
    sale_internal_id: Optional[str]
    net_profit: Optional[Decimal]
    matched: bool
    already_recorded: bool
    note: Optional[str] = None        # something a person should know (fees estimated, cancelled, unpaid)


async def _match_listing(session: AsyncSession, ebay_order: EbayOrder) -> Optional[Listing]:
    """An eBay order's line item SKU is the internal Listing ID we set when
    publishing (see services.listings.publish -> sku=listing.internal_id)."""
    if not ebay_order.line_items:
        return None
    sku = ebay_order.line_items[0].sku
    stmt = select(Listing).where(Listing.internal_id == sku).options(selectinload(Listing.phone))
    return (await session.execute(stmt)).scalar_one_or_none()


async def sync_ebay_orders(
    session: AsyncSession, ebay: EbayClientInterface, since: dt.datetime,
    actor_discord_id: Optional[int] = None,
) -> list[SyncedOrderResult]:
    """Pull eBay orders created since `since`, match each to a local
    Listing/Phone, and record the sale. Idempotent: calling this
    repeatedly for orders already recorded (by ebay_order_id) is always
    safe and never double-counts profit (see services.finance.record_sale).
    """
    if not ebay.configured:
        raise EbayNotConfiguredError("eBay integration is not configured - background sync skipped.")

    orders = await ebay.get_orders_since(since)
    default_postage = await config_service.get_setting_decimal(session, "default_postage_cost", Decimal("2.98"))
    default_packaging = await config_service.get_setting_decimal(session, "default_packaging_cost", Decimal("1.00"))

    results: list[SyncedOrderResult] = []
    fee_pct = await config_service.get_setting_decimal(session, "ebay_fee_pct_assumption", Decimal("12.8"))
    fee_fixed = await config_service.get_setting_decimal(session, "ebay_fixed_fee_assumption", Decimal("0.30"))

    for order in orders:
        # Already recorded? (idempotency check up front, avoids unnecessary lookups)
        from app.models.commerce_models import Sale
        existing = (
            await session.execute(select(Sale).where(Sale.ebay_order_id == order.order_id))
        ).scalar_one_or_none()
        cancelled = (order.cancel_state or "").upper() in ("CANCELED", "CANCELLED")
        if existing:
            results.append(SyncedOrderResult(
                ebay_order_id=order.order_id, phone_internal_id=None, sale_internal_id=existing.internal_id,
                net_profit=None, matched=True, already_recorded=True,
                note="eBay says this order was cancelled: check the sale" if cancelled else None,
            ))
            continue
        if cancelled:
            results.append(SyncedOrderResult(
                ebay_order_id=order.order_id, phone_internal_id=None, sale_internal_id=None, net_profit=None,
                matched=False, already_recorded=False, note="Cancelled on eBay: not recorded as a sale",
            ))
            continue
        if (order.payment_status or "PAID").upper() != "PAID":
            # Not paid yet (or the payment failed): wait. It's picked up on a later run once paid.
            results.append(SyncedOrderResult(
                ebay_order_id=order.order_id, phone_internal_id=None, sale_internal_id=None, net_profit=None,
                matched=False, already_recorded=False, note=f"Payment {order.payment_status}: waiting",
            ))
            continue

        listing = await _match_listing(session, order)
        if listing is None:
            logger.warning("eBay order %s did not match any known listing SKU - skipping", order.order_id)
            results.append(SyncedOrderResult(
                ebay_order_id=order.order_id, phone_internal_id=None, sale_internal_id=None,
                net_profit=None, matched=False, already_recorded=False,
            ))
            continue

        fees, note = order.total_fees, None
        if fees <= 0:   # eBay didn't report fees: never record a £0 fee, which would overstate profit
            fees = (order.total_amount * fee_pct / Decimal("100") + fee_fixed).quantize(Decimal("0.01"))
            note = f"eBay fees estimated at {fee_pct}% + £{fee_fixed} (eBay didn't report them)"
        sale = await finance.record_sale(
            session,
            listing.phone,
            sale_price=order.total_amount,
            sold_at=order.created_at,
            actor_discord_id=actor_discord_id,
            listing=listing,
            ebay_order_id=order.order_id,
            ebay_fees=fees,
            postage_cost=default_postage,
            packaging_cost=default_packaging,
            source=SaleSource.EBAY_SYNC,
            notes=note,
        )
        breakdown = await finance.get_sale_breakdown(session, sale)
        results.append(SyncedOrderResult(
            ebay_order_id=order.order_id, phone_internal_id=listing.phone.internal_id,
            sale_internal_id=sale.internal_id, net_profit=breakdown.net_profit if breakdown else None,
            matched=True, already_recorded=False, note=note,
        ))
    return results
