from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Optional

from sqlalchemy import BigInteger, Boolean, Date, DateTime, ForeignKey, Integer, Numeric, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class PartFailure(Base):
    """A part that was fitted (or about to be) and did not work.

    Parts are not tested before fitting: they are fitted and either work or
    they don't. One row per failure, with what was done to the part, the
    supplier it came from (for quality stats), the business loss, and, for
    returns, whether the refund has arrived.
    """

    __tablename__ = "part_failures"

    id: Mapped[int] = mapped_column(primary_key=True)
    part_id: Mapped[int] = mapped_column(ForeignKey("parts.id"), nullable=False, index=True)
    part_type_id: Mapped[int] = mapped_column(ForeignKey("part_types.id"), nullable=False)
    phone_id: Mapped[Optional[int]] = mapped_column(ForeignKey("phones.id", ondelete="SET NULL"), index=True)
    repair_part_id: Mapped[Optional[int]] = mapped_column(ForeignKey("repair_parts.id", ondelete="SET NULL"))

    order_id: Mapped[Optional[int]] = mapped_column(ForeignKey("orders.id", ondelete="SET NULL"))
    supplier: Mapped[Optional[str]] = mapped_column(String(120), index=True)  # kept even if the order is later deleted

    action: Mapped[str] = mapped_column(String(10), nullable=False)  # SCRAP | RETURN | FAULTY
    reason: Mapped[Optional[str]] = mapped_column(Text)
    was_installed: Mapped[bool] = mapped_column(Boolean, default=False)

    # Written off as a business loss (never charged to the phone).
    loss_amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=0)
    expense_id: Mapped[Optional[int]] = mapped_column(ForeignKey("expenses.id", ondelete="SET NULL"))

    # Returns only: AWAITING_REFUND -> REFUNDED | NO_REFUND
    return_status: Mapped[Optional[str]] = mapped_column(String(20))
    refund_amount: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 2))
    refunded_at: Mapped[Optional[dt.date]] = mapped_column(Date)
    task_id: Mapped[Optional[int]] = mapped_column(ForeignKey("tasks.id", ondelete="SET NULL"))  # the "chase the refund" ticket

    created_by_discord_id: Mapped[Optional[int]] = mapped_column(BigInteger)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
