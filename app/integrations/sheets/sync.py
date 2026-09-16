from __future__ import annotations

import datetime as dt
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.integrations.sheets.client import SheetsClient, SheetsNotConfiguredError
from app.models.commerce_models import Expense, Listing, Order, Sale, SaleProfitBreakdown
from app.models.enums import InventoryEventType
from app.models.inventory_models import Donor, Part, Phone
from app.models.repair_models import Repair
from app.services import finance
from app.services.events import log_event

logger = logging.getLogger(__name__)

SHEET_SPECS: dict[str, list[str]] = {
    "PHONES": ["internal_id", "model", "storage", "colour", "imei", "status", "location",
               "purchase_price", "acquisition_type", "purchase_date", "updated_at"],
    "DONORS": ["internal_id", "model", "storage", "colour", "purchase_price", "status",
               "fault_description", "purchase_date", "updated_at"],
    "PARTS": ["internal_id", "part_type", "source_type", "source_donor", "cost", "grade",
              "testing_status", "location", "status", "installed_phone", "updated_at"],
    "REPAIRS": ["internal_id", "phone", "status", "labour_cost", "external_repair_cost",
                "parts_cost", "total_cost", "started_at", "completed_at"],
    "PURCHASES": ["internal_id", "type", "description", "amount", "date"],
    "ORDERS": ["internal_id", "supplier", "status", "total_cost", "order_date",
               "expected_delivery", "received_date", "tracking_number"],
    "LISTINGS": ["internal_id", "phone", "title", "price", "status", "ebay_listing_id",
                 "ebay_url", "listed_at"],
    "SALES": ["internal_id", "phone", "ebay_order_id", "sale_price", "net_profit", "roi_pct",
              "margin_pct", "sold_at"],
    "EXPENSES": ["internal_id", "category", "amount", "description", "phone", "incurred_at"],
}


def _s(v) -> str:
    return "" if v is None else str(v)


async def _rows_phones(session: AsyncSession) -> list[list[str]]:
    stmt = select(Phone).options(selectinload(Phone.location))
    phones = (await session.execute(stmt)).scalars().all()
    return [
        [p.internal_id, p.display_name(), _s(p.storage), _s(p.colour), _s(p.imei), p.current_status,
         p.location.code if p.location else "", _s(p.purchase_price), p.acquisition_type.value,
         _s(p.purchase_date), _s(p.updated_at)]
        for p in phones
    ]


async def _rows_donors(session: AsyncSession) -> list[list[str]]:
    donors = (await session.execute(select(Donor))).scalars().all()
    return [
        [d.internal_id, d.display_name(), _s(d.storage), _s(d.colour), _s(d.purchase_price),
         d.current_status, _s(d.fault_description), _s(d.purchase_date), _s(d.updated_at)]
        for d in donors
    ]


async def _rows_parts(session: AsyncSession) -> list[list[str]]:
    stmt = select(Part).options(
        selectinload(Part.part_type), selectinload(Part.location),
        selectinload(Part.source_donor), selectinload(Part.installed_phone),
    )
    parts = (await session.execute(stmt)).scalars().all()
    return [
        [p.internal_id, p.part_type.name if p.part_type else "", p.source_type.value,
         p.source_donor.internal_id if p.source_donor else "", _s(p.cost), _s(p.grade_code),
         p.testing_status.value, p.location.code if p.location else "", p.status,
         p.installed_phone.internal_id if p.installed_phone else "", _s(p.updated_at)]
        for p in parts
    ]


async def _rows_repairs(session: AsyncSession) -> list[list[str]]:
    stmt = select(Repair).options(selectinload(Repair.phone), selectinload(Repair.parts))
    repairs = (await session.execute(stmt)).scalars().all()
    return [
        [r.internal_id, r.phone.internal_id, r.status, _s(r.labour_cost), _s(r.external_repair_cost),
         _s(r.total_parts_cost()), _s(r.total_cost()), _s(r.started_at), _s(r.completed_at)]
        for r in repairs
    ]


async def _rows_orders(session: AsyncSession) -> list[list[str]]:
    stmt = select(Order).options(selectinload(Order.lines))
    orders = (await session.execute(stmt)).scalars().all()
    return [
        [o.internal_id, _s(o.supplier), o.status, _s(o.total_cost()), _s(o.order_date),
         _s(o.expected_delivery), _s(o.received_date), _s(o.tracking_number)]
        for o in orders
    ]


async def _rows_listings(session: AsyncSession) -> list[list[str]]:
    stmt = select(Listing).options(selectinload(Listing.phone))
    listings = (await session.execute(stmt)).scalars().all()
    return [
        [l.internal_id, l.phone.internal_id, l.title, _s(l.price), l.status,
         _s(l.ebay_listing_id), _s(l.ebay_url), _s(l.listed_at)]
        for l in listings
    ]


async def _rows_sales(session: AsyncSession) -> list[list[str]]:
    stmt = select(Sale).options(selectinload(Sale.phone), selectinload(Sale.profit_breakdown))
    sales = (await session.execute(stmt)).scalars().all()
    rows = []
    for s in sales:
        bd = s.profit_breakdown
        rows.append([
            s.internal_id, s.phone.internal_id, _s(s.ebay_order_id), _s(s.sale_price),
            _s(bd.net_profit) if bd else "", _s(bd.roi_pct) if bd else "", _s(bd.margin_pct) if bd else "",
            _s(s.sold_at),
        ])
    return rows


async def _rows_expenses(session: AsyncSession) -> list[list[str]]:
    stmt = select(Expense).options(selectinload(Expense.category), selectinload(Expense.phone))
    expenses = (await session.execute(stmt)).scalars().all()
    return [
        [e.internal_id, e.category.name if e.category else "", _s(e.amount), _s(e.description),
         e.phone.internal_id if e.phone else "", _s(e.incurred_at)]
        for e in expenses
    ]


async def _rows_purchases(session: AsyncSession) -> list[list[str]]:
    """Aggregate view of every acquisition cost - phones bought for resale/
    repair and donors bought for parts - in one ledger-style sheet."""
    phones = (await session.execute(select(Phone))).scalars().all()
    donors = (await session.execute(select(Donor))).scalars().all()
    rows = [
        [p.internal_id, f"PHONE ({p.acquisition_type.value})", p.display_name(), _s(p.purchase_price), _s(p.purchase_date)]
        for p in phones
    ]
    rows += [
        [d.internal_id, "DONOR", d.display_name(), _s(d.purchase_price), _s(d.purchase_date)]
        for d in donors
    ]
    return rows


ROW_BUILDERS = {
    "PHONES": _rows_phones,
    "DONORS": _rows_donors,
    "PARTS": _rows_parts,
    "REPAIRS": _rows_repairs,
    "PURCHASES": _rows_purchases,
    "ORDERS": _rows_orders,
    "LISTINGS": _rows_listings,
    "SALES": _rows_sales,
    "EXPENSES": _rows_expenses,
}


async def sync_all(session: AsyncSession, client: SheetsClient, actor_discord_id: int = 0) -> dict[str, tuple[int, int]]:
    """Push every entity table to its sheet tab, idempotently. Returns
    {sheet_name: (updated, appended)}. Raises SheetsNotConfiguredError if
    Sheets isn't set up - callers should catch this and report cleanly."""
    if not client.configured:
        raise SheetsNotConfiguredError("Google Sheets is not configured.")

    results: dict[str, tuple[int, int]] = {}
    for name, headers in SHEET_SPECS.items():
        ws = client.get_or_create_worksheet(name, headers)
        rows = await ROW_BUILDERS[name](session)
        results[name] = client.upsert_rows(ws, rows)

    await _sync_dashboard(session, client)

    await log_event(
        session,
        event_type=InventoryEventType.SHEETS_SYNCED.value,
        entity_type="SYSTEM",
        entity_id="SYSTEM",
        actor_discord_id=actor_discord_id or None,
        new_state={k: {"updated": v[0], "appended": v[1]} for k, v in results.items()},
    )
    return results


async def _sync_dashboard(session: AsyncSession, client: SheetsClient) -> None:
    ws = client.get_or_create_worksheet("DASHBOARD", ["metric", "value", "as_of"])
    stock = await finance.stock_value(session)
    today = dt.date.today()
    month_start = today.replace(day=1)
    month_report = await finance.period_report(session, month_start, today)

    rows = [
        ["stock_value_phones", str(stock["phones"]), str(today)],
        ["stock_value_parts", str(stock["parts"]), str(today)],
        ["stock_value_donors_awaiting_teardown", str(stock["donors_awaiting_teardown"]), str(today)],
        ["stock_value_total", str(stock["total"]), str(today)],
        ["month_to_date_revenue", str(month_report.revenue), str(today)],
        ["month_to_date_net_profit", str(month_report.net_profit), str(today)],
        ["month_to_date_phones_sold", str(month_report.phones_sold), str(today)],
        ["month_to_date_roi_pct", str(month_report.roi_pct), str(today)],
    ]
    client.upsert_rows(ws, rows, id_col_index=0)
