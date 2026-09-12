from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal
from typing import Optional

from sqlalchemy import BigInteger, DateTime, ForeignKey, Numeric, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin
from app.models.enums import AllocationMethod, RepairPartLineStatus


class Repair(Base, TimestampMixin):
    """A repair job against a phone. A phone may have multiple repairs over time."""

    __tablename__ = "repairs"

    id: Mapped[int] = mapped_column(primary_key=True)
    internal_id: Mapped[str] = mapped_column(String(20), unique=True, nullable=False, index=True)
    phone_id: Mapped[int] = mapped_column(ForeignKey("phones.id"), nullable=False, index=True)

    status: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    labour_cost: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=0)
    external_repair_cost: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=0)
    notes: Mapped[Optional[str]] = mapped_column(Text)

    started_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))

    phone: Mapped["Phone"] = relationship(lazy="selectin", back_populates="repairs")
    parts: Mapped[list["RepairPart"]] = relationship(lazy="selectin", back_populates="repair", cascade="all, delete-orphan")
    faults: Mapped[list["PhoneFault"]] = relationship(lazy="selectin")

    def total_parts_cost(self) -> Decimal:
        total = Decimal("0")
        for rp in self.parts:
            if rp.status.value in ("INSTALLED",) and rp.cost_at_installation is not None:
                total += rp.cost_at_installation
        return total

    def total_cost(self) -> Decimal:
        return self.total_parts_cost() + (self.labour_cost or 0) + (self.external_repair_cost or 0)


class RepairPart(Base, TimestampMixin):
    """A required/assigned/installed part line item within a repair."""

    __tablename__ = "repair_parts"

    id: Mapped[int] = mapped_column(primary_key=True)
    repair_id: Mapped[int] = mapped_column(ForeignKey("repairs.id"), nullable=False)
    required_part_type_id: Mapped[int] = mapped_column(ForeignKey("part_types.id"), nullable=False)
    quantity: Mapped[int] = mapped_column(default=1)

    part_id: Mapped[Optional[int]] = mapped_column(ForeignKey("parts.id"))
    status: Mapped[RepairPartLineStatus] = mapped_column(default=RepairPartLineStatus.REQUIRED)

    # Snapshot of the part's cost at the moment it was installed - this is
    # what feeds the phone's true-cost calculation and must never change
    # retroactively even if the part's `cost` field is later reallocated.
    cost_at_installation: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 2))

    reserved_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
    installed_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
    removed_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))

    repair: Mapped["Repair"] = relationship(lazy="selectin", back_populates="parts")
    required_part_type: Mapped["PartTypeConfig"] = relationship(lazy="selectin")
    part: Mapped[Optional["Part"]] = relationship(lazy="selectin", foreign_keys=[part_id])


class CostAllocation(Base, TimestampMixin):
    """Historical record of how much of a donor's cost was allocated to a
    recovered part. Never edited or deleted - re-allocation inserts a new
    row and marks the previous one as no longer current, preserving a full
    audit trail (see spec section 11)."""

    __tablename__ = "cost_allocations"

    id: Mapped[int] = mapped_column(primary_key=True)
    batch_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), default=uuid.uuid4, index=True)
    donor_id: Mapped[int] = mapped_column(ForeignKey("donors.id"), nullable=False)
    part_id: Mapped[int] = mapped_column(ForeignKey("parts.id"), nullable=False)

    method: Mapped[AllocationMethod] = mapped_column(nullable=False)
    donor_total_cost_snapshot: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    allocated_amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)

    is_current: Mapped[bool] = mapped_column(default=True)
    created_by_discord_id: Mapped[Optional[int]] = mapped_column(BigInteger)
    notes: Mapped[Optional[str]] = mapped_column(Text)

    donor: Mapped["Donor"] = relationship(lazy="selectin", back_populates="cost_allocations")
    part: Mapped["Part"] = relationship(lazy="selectin", back_populates="cost_allocations")
