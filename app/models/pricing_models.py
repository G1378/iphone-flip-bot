from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Optional

from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin
from app.models.enums import PriceConditionBucket, PriceDataSource


class PriceWatchlistEntry(Base, TimestampMixin):
    """A (phone model, storage) pair the business wants live eBay pricing
    tracked for. `storage=NULL` means "track this model without splitting
    by storage" (broader search query, more comps, less precise)."""

    __tablename__ = "price_watchlist_entries"

    id: Mapped[int] = mapped_column(primary_key=True)
    phone_model_id: Mapped[int] = mapped_column(ForeignKey("phone_models.id"), nullable=False)
    storage: Mapped[Optional[str]] = mapped_column(String(20))
    active: Mapped[bool] = mapped_column(default=True)
    created_by_discord_id: Mapped[Optional[int]] = mapped_column(BigInteger)

    phone_model: Mapped["PhoneModelConfig"] = relationship(lazy="selectin")


class PriceObservation(Base, TimestampMixin):
    """One pricing snapshot for a (model, storage, condition bucket),
    captured at `observed_at`. New rows are added on every refresh rather
    than overwriting the previous one, so pricing history/trends stay
    queryable and past /analyse recommendations remain reproducible -
    same immutable-snapshot approach used for cost allocations and sale
    profit breakdowns elsewhere in this app.
    """

    __tablename__ = "price_observations"

    id: Mapped[int] = mapped_column(primary_key=True)
    phone_model_id: Mapped[int] = mapped_column(ForeignKey("phone_models.id"), nullable=False, index=True)
    storage: Mapped[Optional[str]] = mapped_column(String(20), index=True)
    condition_bucket: Mapped[PriceConditionBucket] = mapped_column(nullable=False, index=True)
    source: Mapped[PriceDataSource] = mapped_column(nullable=False)

    sample_count: Mapped[int] = mapped_column(Integer, nullable=False)
    avg_price: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    median_price: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    min_price: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    max_price: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(6), default="GBP")

    observed_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)

    phone_model: Mapped["PhoneModelConfig"] = relationship(lazy="selectin")


class DealAlert(Base, TimestampMixin):
    """A listing on the buy page: one row per listing, so the same one is never announced twice.

    Despite the column name, `ebay_item_id` is the listing key for EVERY source (the name is kept so
    nothing that already reads it breaks): eBay item ids as eBay gives them, Vinted ones as
    "vinted:<id>". `source` says which marketplace it came from.

    The card in Discord is a view of this row: `status` is PENDING until someone presses Bought or
    Reject, and `message_channel_id` / `message_id` point at the card so it can be updated after.
    """

    __tablename__ = "deal_alerts"

    id: Mapped[int] = mapped_column(primary_key=True)
    ebay_item_id: Mapped[str] = mapped_column(String(60), unique=True, index=True, nullable=False)
    source: Mapped[str] = mapped_column(String(10), default="EBAY", server_default="EBAY", nullable=False)
    status: Mapped[str] = mapped_column(String(12), default="PENDING", server_default="PENDING", index=True, nullable=False)
    phone_model_id: Mapped[int] = mapped_column(ForeignKey("phone_models.id"), nullable=False)
    storage: Mapped[Optional[str]] = mapped_column(String(20))
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    bucket: Mapped[str] = mapped_column(String(20), nullable=False)          # USED | FAULTY
    price: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)    # item price when announced
    postage: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)  # postage (or the assumed amount)
    buyer_fee: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=0, server_default="0", nullable=False)  # Vinted buyer protection
    max_buy: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)  # your recommended maximum, all in, for THIS listing
    max_item_price: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 2))  # most to pay for the phone itself (what to offer)
    url: Mapped[str] = mapped_column(String(500), nullable=False)
    image_url: Mapped[Optional[str]] = mapped_column(String(500))
    seller: Mapped[Optional[str]] = mapped_column(String(100))
    seller_feedback_pct: Mapped[Optional[Decimal]] = mapped_column(Numeric(5, 1))
    seller_reviews: Mapped[Optional[int]] = mapped_column(Integer)
    is_auction: Mapped[bool] = mapped_column(default=False)
    ends_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
    flags: Mapped[Optional[str]] = mapped_column(String(600))                # e.g. "iCloud/lock risk"

    # The workings, frozen at the moment of the alert (prices move; the decision should be reproducible).
    resale_estimate: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 2))
    resale_basis: Mapped[Optional[str]] = mapped_column(String(80))          # "recent sales" | "eBay asking prices"
    sale_costs: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 2))    # fees + postage out + packaging
    repair_estimate: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=0, server_default="0", nullable=False)
    repair_basis: Mapped[Optional[str]] = mapped_column(String(400))
    expected_profit: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 2))
    roi_pct: Mapped[Optional[Decimal]] = mapped_column(Numeric(7, 1))

    # What happened to it.
    decision_note: Mapped[Optional[str]] = mapped_column(Text)
    decided_by_discord_id: Mapped[Optional[int]] = mapped_column(BigInteger)
    decided_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
    phone_id: Mapped[Optional[int]] = mapped_column(ForeignKey("phones.id"))   # set when bought: the inventory record
    message_channel_id: Mapped[Optional[int]] = mapped_column(BigInteger)
    message_id: Mapped[Optional[int]] = mapped_column(BigInteger)

    phone_model: Mapped["PhoneModelConfig"] = relationship(lazy="selectin")

    @property
    def total_cost(self) -> Decimal:
        """Everything you'd hand over to get the phone to your door."""
        return (self.price + self.postage + (self.buyer_fee or 0)).quantize(Decimal("0.01"))
