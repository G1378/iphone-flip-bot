from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Optional

from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, Numeric, String
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
