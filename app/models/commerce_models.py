from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Optional

from sqlalchemy import BigInteger, DateTime, ForeignKey, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin
from app.models.enums import SaleSource, SaleStatus


class Order(Base, TimestampMixin):
    """A purchase order for replacement parts. Internal ID: PO-000001."""

    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(primary_key=True)
    internal_id: Mapped[str] = mapped_column(String(20), unique=True, nullable=False, index=True)

    supplier: Mapped[Optional[str]] = mapped_column(String(150))
    status: Mapped[str] = mapped_column(String(30), nullable=False, index=True)

    shipping_cost: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=0)
    tax: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=0)

    order_date: Mapped[Optional[dt.date]] = mapped_column()
    expected_delivery: Mapped[Optional[dt.date]] = mapped_column()
    received_date: Mapped[Optional[dt.date]] = mapped_column()

    tracking_number: Mapped[Optional[str]] = mapped_column(String(100))
    notes: Mapped[Optional[str]] = mapped_column(Text)

    lines: Mapped[list["OrderLine"]] = relationship(lazy="selectin", back_populates="order", cascade="all, delete-orphan")

    def total_cost(self) -> Decimal:
        lines_total = sum((l.unit_cost * l.quantity for l in self.lines), Decimal("0"))
        return lines_total + (self.shipping_cost or 0) + (self.tax or 0)


class OrderLine(Base, TimestampMixin):
    __tablename__ = "order_lines"

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), nullable=False)
    part_type_id: Mapped[int] = mapped_column(ForeignKey("part_types.id"), nullable=False)
    phone_model_id: Mapped[Optional[int]] = mapped_column(ForeignKey("phone_models.id"))

    quantity: Mapped[int] = mapped_column(default=1)
    unit_cost: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=0)
    received_quantity: Mapped[int] = mapped_column(default=0)
    notes: Mapped[Optional[str]] = mapped_column(String(200))

    order: Mapped["Order"] = relationship(lazy="selectin", back_populates="lines")
    part_type: Mapped["PartTypeConfig"] = relationship(lazy="selectin")
    phone_model: Mapped[Optional["PhoneModelConfig"]] = relationship(lazy="selectin")


class Listing(Base, TimestampMixin):
    """An eBay listing draft/active/ended record. Internal ID: LS-000001."""

    __tablename__ = "listings"

    id: Mapped[int] = mapped_column(primary_key=True)
    internal_id: Mapped[str] = mapped_column(String(20), unique=True, nullable=False, index=True)
    phone_id: Mapped[int] = mapped_column(ForeignKey("phones.id"), nullable=False, index=True)

    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    condition_text: Mapped[Optional[str]] = mapped_column(String(100))
    category_id: Mapped[Optional[str]] = mapped_column(String(50))
    price: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(6), default="GBP")
    quantity: Mapped[int] = mapped_column(default=1)
    shipping_policy_id: Mapped[Optional[str]] = mapped_column(String(80))
    payment_policy_id: Mapped[Optional[str]] = mapped_column(String(80))
    return_policy_id: Mapped[Optional[str]] = mapped_column(String(80))

    status: Mapped[str] = mapped_column(String(30), nullable=False, index=True)  # DRAFT/ACTIVE/ENDED/SOLD/CANCELLED

    ebay_listing_id: Mapped[Optional[str]] = mapped_column(String(60), unique=True, index=True)
    ebay_offer_id: Mapped[Optional[str]] = mapped_column(String(60))
    ebay_sku: Mapped[Optional[str]] = mapped_column(String(60), unique=True)
    ebay_url: Mapped[Optional[str]] = mapped_column(String(300))

    listed_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
    created_by_discord_id: Mapped[Optional[int]] = mapped_column(BigInteger)

    phone: Mapped["Phone"] = relationship(lazy="selectin")


class Sale(Base, TimestampMixin):
    """A completed (or in-progress) sale. Internal ID: SL-000001."""

    __tablename__ = "sales"

    id: Mapped[int] = mapped_column(primary_key=True)
    internal_id: Mapped[str] = mapped_column(String(20), unique=True, nullable=False, index=True)
    phone_id: Mapped[int] = mapped_column(ForeignKey("phones.id"), nullable=False, index=True)
    listing_id: Mapped[Optional[int]] = mapped_column(ForeignKey("listings.id"))

    ebay_order_id: Mapped[Optional[str]] = mapped_column(String(60), unique=True, index=True)
    source: Mapped[SaleSource] = mapped_column(default=SaleSource.MANUAL)
    status: Mapped[SaleStatus] = mapped_column(default=SaleStatus.PENDING_SHIPMENT)

    sale_price: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    ebay_fees: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=0)
    postage_cost: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=0)
    packaging_cost: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=0)
    other_fees: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=0)

    sold_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    shipped_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))

    buyer_reference: Mapped[Optional[str]] = mapped_column(String(120))
    notes: Mapped[Optional[str]] = mapped_column(Text)

    phone: Mapped["Phone"] = relationship(lazy="selectin")
    listing: Mapped[Optional["Listing"]] = relationship(lazy="selectin")
    profit_breakdown: Mapped[Optional["SaleProfitBreakdown"]] = relationship(lazy="selectin", 
        back_populates="sale", uselist=False, cascade="all, delete-orphan"
    )


class SaleProfitBreakdown(Base, TimestampMixin):
    """Persisted, reproducible snapshot of every cost component that made up
    a sale's profit at the time it was calculated (spec section 20).

    We still *can* recompute this from first principles at any time (see
    services.finance.compute_phone_cost_breakdown) but we snapshot it at
    the point of sale so historical reporting never silently changes if
    unrelated inventory data is edited later.
    """

    __tablename__ = "sale_profit_breakdowns"

    id: Mapped[int] = mapped_column(primary_key=True)
    sale_id: Mapped[int] = mapped_column(ForeignKey("sales.id"), unique=True, nullable=False)

    purchase_cost: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=0)
    donor_allocated_cost: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=0)
    purchased_part_cost: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=0)
    repair_labour_cost: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=0)
    external_repair_cost: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=0)
    shipping_in_cost: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=0)
    shipping_out_cost: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=0)
    packaging_cost: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=0)
    ebay_fees: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=0)
    other_fees: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=0)
    other_expenses: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=0)
    sale_revenue: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=0)

    total_cost: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=0)
    gross_profit: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=0)
    net_profit: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=0)
    margin_pct: Mapped[Decimal] = mapped_column(Numeric(6, 2), default=0)
    roi_pct: Mapped[Decimal] = mapped_column(Numeric(6, 2), default=0)

    sale: Mapped["Sale"] = relationship(lazy="selectin", back_populates="profit_breakdown")


class Expense(Base, TimestampMixin):
    __tablename__ = "expenses"

    id: Mapped[int] = mapped_column(primary_key=True)
    internal_id: Mapped[str] = mapped_column(String(20), unique=True, nullable=False, index=True)
    category_id: Mapped[int] = mapped_column(ForeignKey("expense_categories.id"), nullable=False)

    amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(String(300))

    phone_id: Mapped[Optional[int]] = mapped_column(ForeignKey("phones.id"))
    donor_id: Mapped[Optional[int]] = mapped_column(ForeignKey("donors.id"))

    incurred_at: Mapped[dt.date] = mapped_column(nullable=False)
    created_by_discord_id: Mapped[Optional[int]] = mapped_column(BigInteger)

    category: Mapped["ExpenseCategory"] = relationship(lazy="selectin")
    phone: Mapped[Optional["Phone"]] = relationship(lazy="selectin")
    donor: Mapped[Optional["Donor"]] = relationship(lazy="selectin")
