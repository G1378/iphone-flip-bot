from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Optional

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Numeric,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin
from app.models.enums import AcquisitionType, FaultStatus, LockStatus, PartSourceType, TestOutcome


class Phone(Base, TimestampMixin):
    """A complete phone unit (resale or repair-and-resell). Internal ID: IP-000001."""

    __tablename__ = "phones"

    id: Mapped[int] = mapped_column(primary_key=True)
    internal_id: Mapped[str] = mapped_column(String(20), unique=True, nullable=False, index=True)

    manufacturer: Mapped[str] = mapped_column(String(50), default="Apple")
    model: Mapped[str] = mapped_column(String(80), nullable=False)
    variant: Mapped[Optional[str]] = mapped_column(String(50))
    # The exact catalog model ("iPhone 13 Pro" is not "iPhone 13"). Parts are matched
    # to a phone through this id, never through the free-text `model` above.
    phone_model_id: Mapped[Optional[int]] = mapped_column(ForeignKey("phone_models.id"), index=True)
    storage: Mapped[Optional[str]] = mapped_column(String(20))   # "128GB"
    colour: Mapped[Optional[str]] = mapped_column(String(40))

    imei: Mapped[Optional[str]] = mapped_column(String(20), index=True)
    serial_number: Mapped[Optional[str]] = mapped_column(String(40), index=True)

    carrier: Mapped[Optional[str]] = mapped_column(String(60))  # network/carrier status
    lock_status: Mapped[LockStatus] = mapped_column(default=LockStatus.UNKNOWN)

    battery_health: Mapped[Optional[int]] = mapped_column()  # percentage
    # eBay-hosted photo URLs, in the order they'll show on a listing (first = main image).
    photo_urls: Mapped[Optional[list]] = mapped_column(JSON)
    cosmetic_condition: Mapped[Optional[str]] = mapped_column(String(100))
    functional_condition: Mapped[Optional[str]] = mapped_column(String(100))

    purchase_price: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=0)
    purchase_date: Mapped[Optional[dt.date]] = mapped_column(Date)
    seller_source: Mapped[Optional[str]] = mapped_column(String(150))
    acquisition_type: Mapped[AcquisitionType] = mapped_column(default=AcquisitionType.RESALE)

    current_status: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    location_id: Mapped[Optional[int]] = mapped_column(ForeignKey("locations.id"))

    notes: Mapped[Optional[str]] = mapped_column(Text)

    location: Mapped[Optional["Location"]] = relationship(lazy="selectin")
    catalog_model: Mapped[Optional["PhoneModelConfig"]] = relationship(lazy="selectin", foreign_keys=[phone_model_id])
    faults: Mapped[list["PhoneFault"]] = relationship(lazy="selectin", back_populates="phone", cascade="all, delete-orphan")
    test_results: Mapped[list["TestResult"]] = relationship(lazy="selectin", back_populates="phone", cascade="all, delete-orphan")
    repairs: Mapped[list["Repair"]] = relationship(lazy="selectin", back_populates="phone")
    installed_parts: Mapped[list["Part"]] = relationship(lazy="selectin", back_populates="installed_phone", foreign_keys="Part.installed_phone_id")

    def display_name(self) -> str:
        bits = [self.manufacturer, self.model, self.variant, self.storage, self.colour]
        return " ".join(b for b in bits if b)


class Donor(Base, TimestampMixin):
    """A phone purchased specifically to harvest parts. Internal ID: DF-000001.

    Donors are first-class historical provenance records: they are never
    deleted, even after teardown - recovered parts continue to reference
    the donor they came from.
    """

    __tablename__ = "donors"

    id: Mapped[int] = mapped_column(primary_key=True)
    internal_id: Mapped[str] = mapped_column(String(20), unique=True, nullable=False, index=True)

    manufacturer: Mapped[str] = mapped_column(String(50), default="Apple")
    model: Mapped[str] = mapped_column(String(80), nullable=False)
    variant: Mapped[Optional[str]] = mapped_column(String(50))
    phone_model_id: Mapped[Optional[int]] = mapped_column(ForeignKey("phone_models.id"), index=True)
    storage: Mapped[Optional[str]] = mapped_column(String(20))
    colour: Mapped[Optional[str]] = mapped_column(String(40))
    imei: Mapped[Optional[str]] = mapped_column(String(20))
    serial_number: Mapped[Optional[str]] = mapped_column(String(40))

    purchase_price: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=0)
    purchase_date: Mapped[Optional[dt.date]] = mapped_column(Date)
    seller_source: Mapped[Optional[str]] = mapped_column(String(150))

    fault_description: Mapped[Optional[str]] = mapped_column(Text)
    current_status: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    location_id: Mapped[Optional[int]] = mapped_column(ForeignKey("locations.id"))

    torn_down_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
    notes: Mapped[Optional[str]] = mapped_column(Text)

    location: Mapped[Optional["Location"]] = relationship(lazy="selectin")
    catalog_model: Mapped[Optional["PhoneModelConfig"]] = relationship(lazy="selectin", foreign_keys=[phone_model_id])
    parts_recovered: Mapped[list["Part"]] = relationship(lazy="selectin", back_populates="source_donor")
    cost_allocations: Mapped[list["CostAllocation"]] = relationship(lazy="selectin", back_populates="donor")

    def display_name(self) -> str:
        bits = [self.manufacturer, self.model, self.variant, self.storage, self.colour]
        return " ".join(b for b in bits if b)


class PartCompatibleModel(Base):
    """Which phone models a part is compatible with (many-to-many)."""

    __tablename__ = "part_compatible_models"

    part_id: Mapped[int] = mapped_column(ForeignKey("parts.id"), primary_key=True)
    phone_model_id: Mapped[int] = mapped_column(ForeignKey("phone_models.id"), primary_key=True)

    phone_model: Mapped["PhoneModelConfig"] = relationship(lazy="selectin")


class Part(Base, TimestampMixin):
    """A single physical component with full provenance. Internal ID: PT-000001."""

    __tablename__ = "parts"

    id: Mapped[int] = mapped_column(primary_key=True)
    internal_id: Mapped[str] = mapped_column(String(20), unique=True, nullable=False, index=True)

    part_type_id: Mapped[int] = mapped_column(ForeignKey("part_types.id"), nullable=False)

    source_type: Mapped[PartSourceType] = mapped_column(nullable=False)
    source_donor_id: Mapped[Optional[int]] = mapped_column(ForeignKey("donors.id"))
    source_phone_id: Mapped[Optional[int]] = mapped_column(ForeignKey("phones.id"))
    source_order_line_id: Mapped[Optional[int]] = mapped_column(ForeignKey("order_lines.id"))

    # Current effective cost basis. The authoritative *historical* trail
    # lives in CostAllocation (for donor parts) - this column is a
    # convenience cache of the current allocation, kept in sync by the
    # allocation service, never edited directly for donor-sourced parts.
    cost: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=0)

    condition: Mapped[Optional[str]] = mapped_column(String(100))
    grade_code: Mapped[Optional[str]] = mapped_column(String(10))
    testing_status: Mapped[TestOutcome] = mapped_column(default=TestOutcome.NOT_TESTED)  # legacy: parts are no longer pre-tested

    # Set when someone confirms the part worked after fitting it. Parts are never
    # tested beforehand: they're fitted and either work or they don't.
    confirmed_working_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
    confirmed_working_in: Mapped[Optional[str]] = mapped_column(String(20))  # e.g. "IP-000004"

    location_id: Mapped[Optional[int]] = mapped_column(ForeignKey("locations.id"))
    status: Mapped[str] = mapped_column(String(30), nullable=False, index=True)

    installed_phone_id: Mapped[Optional[int]] = mapped_column(ForeignKey("phones.id"))
    reserved_for_repair_id: Mapped[Optional[int]] = mapped_column(ForeignKey("repairs.id"))

    purchase_date: Mapped[Optional[dt.date]] = mapped_column(Date)
    notes: Mapped[Optional[str]] = mapped_column(Text)

    part_type: Mapped["PartTypeConfig"] = relationship(lazy="selectin")
    source_donor: Mapped[Optional["Donor"]] = relationship(lazy="selectin", back_populates="parts_recovered")
    location: Mapped[Optional["Location"]] = relationship(lazy="selectin")
    installed_phone: Mapped[Optional["Phone"]] = relationship(lazy="selectin", back_populates="installed_parts", foreign_keys=[installed_phone_id])
    compatible_model_links: Mapped[list["PartCompatibleModel"]] = relationship(lazy="selectin", cascade="all, delete-orphan")
    cost_allocations: Mapped[list["CostAllocation"]] = relationship(lazy="selectin", back_populates="part")

    def compatible_model_ids(self) -> set[int]:
        return {link.phone_model_id for link in self.compatible_model_links}

    def fits_model(self, phone_model_id: Optional[int]) -> bool:
        """True only if this part is explicitly tagged for that exact model.

        A part with no tags fits NOTHING (there is no "universal" part), and a
        phone with no catalog model can't be matched to anything.
        """
        return phone_model_id is not None and phone_model_id in self.compatible_model_ids()

    def model_label(self) -> str:
        names = sorted(l.phone_model.model for l in self.compatible_model_links if l.phone_model)
        return " / ".join(names) if names else "NO MODEL SET"

    def display_name(self) -> str:
        """e.g. 'Battery (iPhone 13 Pro)'."""
        return f"{self.part_type.name if self.part_type else 'Part'} ({self.model_label()})"


class PhoneFault(Base, TimestampMixin):
    """A recorded fault on a phone, optionally mapped to a required part type."""

    __tablename__ = "phone_faults"

    id: Mapped[int] = mapped_column(primary_key=True)
    phone_id: Mapped[int] = mapped_column(ForeignKey("phones.id"), nullable=False)
    description: Mapped[str] = mapped_column(String(200), nullable=False)
    required_part_type_id: Mapped[Optional[int]] = mapped_column(ForeignKey("part_types.id"))
    # Set when the fault was raised automatically by a failed test, so a later
    # PASS on the same test can resolve it again.
    test_definition_id: Mapped[Optional[int]] = mapped_column(ForeignKey("test_definitions.id"), nullable=True)
    status: Mapped[FaultStatus] = mapped_column(default=FaultStatus.OPEN)
    repair_id: Mapped[Optional[int]] = mapped_column(ForeignKey("repairs.id"))
    resolved_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))

    phone: Mapped["Phone"] = relationship(lazy="selectin", back_populates="faults")
    required_part_type: Mapped[Optional["PartTypeConfig"]] = relationship(lazy="selectin")


class TestResult(Base):
    """Append-only ledger of phone test outcomes. Latest row per
    (phone_id, test_definition_id) is the 'current' result."""

    __tablename__ = "test_results"

    id: Mapped[int] = mapped_column(primary_key=True)
    phone_id: Mapped[int] = mapped_column(ForeignKey("phones.id"), nullable=False, index=True)
    test_definition_id: Mapped[int] = mapped_column(ForeignKey("test_definitions.id"), nullable=False)
    result: Mapped[TestOutcome] = mapped_column(nullable=False)
    notes: Mapped[Optional[str]] = mapped_column(String(300))
    tested_by_discord_id: Mapped[Optional[int]] = mapped_column(BigInteger)
    tested_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    phone: Mapped["Phone"] = relationship(lazy="selectin", back_populates="test_results")
    test_definition: Mapped["TestDefinition"] = relationship(lazy="selectin")
