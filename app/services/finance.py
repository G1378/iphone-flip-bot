from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from typing import Optional, Sequence

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.commerce_models import Expense, Listing, Sale, SaleProfitBreakdown
from app.models.enums import InventoryEventType, PartSourceType, RepairPartLineStatus, SaleSource, SaleStatus
from app.models.inventory_models import Phone
from app.models.repair_models import Repair, RepairPart
from app.services import config_service
from app.services.events import log_event
from app.services.ids import next_internal_id

TWO_DP = Decimal("0.01")


def q(value: Decimal) -> Decimal:
    return Decimal(value).quantize(TWO_DP, rounding=ROUND_HALF_UP)


@dataclass
class TrueCostBreakdown:
    purchase_cost: Decimal = Decimal("0")
    donor_allocated_cost: Decimal = Decimal("0")
    purchased_part_cost: Decimal = Decimal("0")
    repair_labour_cost: Decimal = Decimal("0")
    external_repair_cost: Decimal = Decimal("0")
    shipping_in_cost: Decimal = Decimal("0")
    other_expenses: Decimal = Decimal("0")

    @property
    def total(self) -> Decimal:
        return q(
            self.purchase_cost + self.donor_allocated_cost + self.purchased_part_cost
            + self.repair_labour_cost + self.external_repair_cost + self.shipping_in_cost
            + self.other_expenses
        )


async def compute_true_cost(session: AsyncSession, phone: Phone) -> TrueCostBreakdown:
    """The actual chain of costs a phone has accumulated so far:
    purchase price + every installed part's cost-at-installation snapshot
    (split by donor-allocated vs purchased) + repair labour/external costs
    + any expenses tagged directly to this phone. This is always
    reproducible because every input is itself an immutable snapshot."""
    breakdown = TrueCostBreakdown(purchase_cost=phone.purchase_price or Decimal("0"))

    repairs_stmt = (
        select(Repair)
        .where(Repair.phone_id == phone.id)
        .options(selectinload(Repair.parts).selectinload(RepairPart.part))
    )
    repairs = (await session.execute(repairs_stmt)).scalars().all()

    for repair in repairs:
        breakdown.repair_labour_cost += repair.labour_cost or Decimal("0")
        breakdown.external_repair_cost += repair.external_repair_cost or Decimal("0")
        for rp in repair.parts:
            if rp.status != RepairPartLineStatus.INSTALLED or rp.cost_at_installation is None:
                continue
            if rp.part and rp.part.source_type == PartSourceType.DONOR:
                breakdown.donor_allocated_cost += rp.cost_at_installation
            else:
                breakdown.purchased_part_cost += rp.cost_at_installation

    expenses_stmt = select(func.coalesce(func.sum(Expense.amount), 0)).where(
        Expense.phone_id == phone.id, Expense.category.has(name="Postage In")
    )
    breakdown.shipping_in_cost = Decimal(str((await session.execute(expenses_stmt)).scalar_one()))

    other_expenses_stmt = select(func.coalesce(func.sum(Expense.amount), 0)).where(
        Expense.phone_id == phone.id
    ) 
    total_phone_expenses = Decimal(str((await session.execute(other_expenses_stmt)).scalar_one()))
    breakdown.other_expenses = total_phone_expenses - breakdown.shipping_in_cost

    return breakdown


@dataclass
class EconomicAnalysis:
    breakdown: TrueCostBreakdown
    expected_sale_price: Decimal
    estimated_ebay_fees: Decimal
    estimated_postage: Decimal
    estimated_packaging: Decimal
    expected_profit: Decimal
    roi_pct: Decimal
    margin_pct: Decimal
    recommendation: str  # GOOD | MARGINAL | NOT_WORTH
    min_profit_threshold: Decimal
    min_roi_threshold: Decimal
    target_margin_threshold: Decimal


async def analyse_phone(
    session: AsyncSession, phone: Phone, expected_sale_price: Optional[Decimal] = None,
) -> EconomicAnalysis:
    breakdown = await compute_true_cost(session, phone)

    phone_model = await config_service.get_or_create_phone_model(session, phone.manufacturer, phone.model, phone.variant)
    rule = await config_service.get_pricing_rule(session, phone_model.id)

    if expected_sale_price is None:
        expected_sale_price = rule.default_sale_price if rule and rule.default_sale_price else None
        if expected_sale_price is None:
            from app.services import pricing as pricing_service
            expected_sale_price = await pricing_service.get_current_estimated_sale_price(session, phone)
        expected_sale_price = expected_sale_price or Decimal("0")

    fee_pct = await config_service.get_setting_decimal(session, "ebay_fee_pct_assumption", Decimal("12.8"))
    fee_fixed = await config_service.get_setting_decimal(session, "ebay_fixed_fee_assumption", Decimal("0.30"))
    default_postage = await config_service.get_setting_decimal(session, "default_postage_cost", Decimal("6.00"))
    default_packaging = await config_service.get_setting_decimal(session, "default_packaging_cost", Decimal("1.00"))

    min_profit = (rule.min_profit_gbp if rule and rule.min_profit_gbp is not None else None) \
        or await config_service.get_setting_decimal(session, "min_profit_gbp", Decimal("20.00"))
    min_roi = (rule.min_roi_pct if rule and rule.min_roi_pct is not None else None) \
        or await config_service.get_setting_decimal(session, "min_roi_pct", Decimal("15.00"))
    target_margin = (rule.target_margin_pct if rule and rule.target_margin_pct is not None else None) \
        or await config_service.get_setting_decimal(session, "target_margin_pct", Decimal("25.00"))

    estimated_ebay_fees = q(expected_sale_price * (fee_pct / Decimal("100")) + fee_fixed)
    total_cost = breakdown.total
    expected_profit = q(expected_sale_price - total_cost - estimated_ebay_fees - default_postage - default_packaging)

    roi_pct = q((expected_profit / total_cost) * 100) if total_cost > 0 else Decimal("0")
    margin_pct = q((expected_profit / expected_sale_price) * 100) if expected_sale_price > 0 else Decimal("0")

    if expected_profit >= min_profit and roi_pct >= min_roi:
        recommendation = "GOOD"
    elif expected_profit > 0:
        recommendation = "MARGINAL"
    else:
        recommendation = "NOT_WORTH"

    return EconomicAnalysis(
        breakdown=breakdown,
        expected_sale_price=expected_sale_price,
        estimated_ebay_fees=estimated_ebay_fees,
        estimated_postage=default_postage,
        estimated_packaging=default_packaging,
        expected_profit=expected_profit,
        roi_pct=roi_pct,
        margin_pct=margin_pct,
        recommendation=recommendation,
        min_profit_threshold=min_profit,
        min_roi_threshold=min_roi,
        target_margin_threshold=target_margin,
    )


async def record_sale(
    session: AsyncSession,
    phone: Phone,
    sale_price: Decimal,
    sold_at: dt.datetime,
    actor_discord_id: Optional[int],
    listing: Optional[Listing] = None,
    ebay_order_id: Optional[str] = None,
    ebay_fees: Decimal = Decimal("0"),
    postage_cost: Optional[Decimal] = None,
    packaging_cost: Optional[Decimal] = None,
    other_fees: Decimal = Decimal("0"),
    source: SaleSource = SaleSource.MANUAL,
    notes: Optional[str] = None,
) -> Sale:
    """Idempotent on ebay_order_id: if a Sale already exists for this
    eBay order, return it unchanged rather than creating a duplicate
    (spec: 'duplicate eBay order handling')."""
    if ebay_order_id:
        existing_stmt = select(Sale).where(Sale.ebay_order_id == ebay_order_id)
        existing = (await session.execute(existing_stmt)).scalar_one_or_none()
        if existing:
            return existing

    if postage_cost is None:
        postage_cost = await config_service.get_setting_decimal(session, "default_postage_cost", Decimal("6.00"))
    if packaging_cost is None:
        packaging_cost = await config_service.get_setting_decimal(session, "default_packaging_cost", Decimal("1.00"))

    internal_id = await next_internal_id(session, "sale")
    sale = Sale(
        internal_id=internal_id,
        phone_id=phone.id,
        listing_id=listing.id if listing else None,
        ebay_order_id=ebay_order_id,
        source=source,
        status=SaleStatus.PENDING_SHIPMENT,
        sale_price=sale_price,
        ebay_fees=ebay_fees,
        postage_cost=postage_cost,
        packaging_cost=packaging_cost,
        other_fees=other_fees,
        sold_at=sold_at,
        notes=notes,
    )
    session.add(sale)
    await session.flush()

    breakdown = await compute_true_cost(session, phone)
    total_cost = q(breakdown.total + postage_cost + packaging_cost + ebay_fees + other_fees)
    gross_profit = q(sale_price - breakdown.total)
    net_profit = q(sale_price - total_cost)
    margin_pct = q((net_profit / sale_price) * 100) if sale_price > 0 else Decimal("0")
    roi_pct = q((net_profit / total_cost) * 100) if total_cost > 0 else Decimal("0")

    snapshot = SaleProfitBreakdown(
        sale_id=sale.id,
        purchase_cost=breakdown.purchase_cost,
        donor_allocated_cost=breakdown.donor_allocated_cost,
        purchased_part_cost=breakdown.purchased_part_cost,
        repair_labour_cost=breakdown.repair_labour_cost,
        external_repair_cost=breakdown.external_repair_cost,
        shipping_in_cost=breakdown.shipping_in_cost,
        shipping_out_cost=postage_cost,
        packaging_cost=packaging_cost,
        ebay_fees=ebay_fees,
        other_fees=other_fees,
        other_expenses=breakdown.other_expenses,
        sale_revenue=sale_price,
        total_cost=total_cost,
        gross_profit=gross_profit,
        net_profit=net_profit,
        margin_pct=margin_pct,
        roi_pct=roi_pct,
    )
    session.add(snapshot)

    phone.current_status = "SOLD"
    if listing:
        listing.status = "SOLD"

    await session.flush()

    await log_event(
        session,
        event_type=InventoryEventType.PHONE_SOLD.value,
        entity_type="PHONE",
        entity_id=phone.internal_id,
        actor_discord_id=actor_discord_id,
        related_entity_type="SALE",
        related_entity_id=sale.internal_id,
        new_state={"sale_price": str(sale_price), "net_profit": str(net_profit), "ebay_order_id": ebay_order_id},
    )
    return sale


async def get_sale_breakdown(session: AsyncSession, sale: Sale) -> Optional[SaleProfitBreakdown]:
    return (
        await session.execute(select(SaleProfitBreakdown).where(SaleProfitBreakdown.sale_id == sale.id))
    ).scalar_one_or_none()


# ----------------------------------------------------------------------
# Reporting
# ----------------------------------------------------------------------
@dataclass
class PeriodReport:
    start: dt.date
    end: dt.date
    revenue: Decimal = Decimal("0")
    total_cost: Decimal = Decimal("0")
    net_profit: Decimal = Decimal("0")
    phones_sold: int = 0
    phones_purchased: int = 0
    avg_profit_per_phone: Decimal = Decimal("0")
    roi_pct: Decimal = Decimal("0")


async def period_report(session: AsyncSession, start: dt.date, end: dt.date) -> PeriodReport:
    sales_stmt = (
        select(SaleProfitBreakdown)
        .join(Sale, SaleProfitBreakdown.sale_id == Sale.id)
        .where(func.date(Sale.sold_at) >= start, func.date(Sale.sold_at) <= end)
    )
    breakdowns = (await session.execute(sales_stmt)).scalars().all()

    report = PeriodReport(start=start, end=end)
    report.phones_sold = len(breakdowns)
    report.revenue = q(sum((b.sale_revenue for b in breakdowns), Decimal("0")))
    report.total_cost = q(sum((b.total_cost for b in breakdowns), Decimal("0")))
    report.net_profit = q(sum((b.net_profit for b in breakdowns), Decimal("0")))
    report.avg_profit_per_phone = q(report.net_profit / report.phones_sold) if report.phones_sold else Decimal("0")
    report.roi_pct = q((report.net_profit / report.total_cost) * 100) if report.total_cost > 0 else Decimal("0")

    purchased_stmt = select(func.count(Phone.id)).where(
        Phone.purchase_date >= start, Phone.purchase_date <= end
    )
    report.phones_purchased = (await session.execute(purchased_stmt)).scalar_one()

    return report


async def stock_value(session: AsyncSession) -> dict[str, Decimal]:
    from app.models.inventory_models import Donor, Part

    phones_stmt = select(func.coalesce(func.sum(Phone.purchase_price), 0)).where(
        ~Phone.current_status.in_(["SOLD", "SHIPPED", "COMPLETE", "SCRAPPED"])
    )
    phones_value = Decimal(str((await session.execute(phones_stmt)).scalar_one()))

    parts_stmt = select(func.coalesce(func.sum(Part.cost), 0)).where(Part.status.in_(["AVAILABLE", "RESERVED"]))
    parts_value = Decimal(str((await session.execute(parts_stmt)).scalar_one()))

    donors_stmt = select(func.coalesce(func.sum(Donor.purchase_price), 0)).where(Donor.current_status == "AWAITING_TEARDOWN")
    donors_value = Decimal(str((await session.execute(donors_stmt)).scalar_one()))

    return {
        "phones": q(phones_value),
        "parts": q(parts_value),
        "donors_awaiting_teardown": q(donors_value),
        "total": q(phones_value + parts_value + donors_value),
    }
