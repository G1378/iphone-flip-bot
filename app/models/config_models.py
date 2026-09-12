from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Optional

from sqlalchemy import BigInteger, ForeignKey, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin


class AuthorizedUser(Base, TimestampMixin):
    """Discord users allowed to operate the bot. `/config users` manages this."""

    __tablename__ = "authorized_users"

    id: Mapped[int] = mapped_column(primary_key=True)
    discord_user_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False, index=True)
    display_name: Mapped[Optional[str]] = mapped_column(String(200))
    is_admin: Mapped[bool] = mapped_column(default=False)
    added_by_discord_id: Mapped[Optional[int]] = mapped_column(BigInteger)
    active: Mapped[bool] = mapped_column(default=True)


class BotChannel(Base, TimestampMixin):
    """Discord channels the bot is allowed to operate/notify in."""

    __tablename__ = "bot_channels"

    id: Mapped[int] = mapped_column(primary_key=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    channel_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False)
    purpose: Mapped[str] = mapped_column(String(50), default="general")  # general | notifications | sales
    active: Mapped[bool] = mapped_column(default=True)


class BusinessSetting(Base, TimestampMixin):
    """Simple typed key/value store for business settings configured via /config business.

    Examples of keys: business_name, currency, default_postage_cost,
    default_packaging_cost, ebay_fee_pct_assumption, min_profit_gbp,
    min_roi_pct, target_margin_pct.
    """

    __tablename__ = "business_settings"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    value_type: Mapped[str] = mapped_column(String(20), default="string")  # string|number|bool
    description: Mapped[Optional[str]] = mapped_column(Text)


class Location(Base, TimestampMixin):
    """Physical storage location, e.g. Shelf B -> B1."""

    __tablename__ = "locations"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(30), unique=True, nullable=False)  # "B1"
    zone: Mapped[Optional[str]] = mapped_column(String(50))  # "Shelf B"
    description: Mapped[Optional[str]] = mapped_column(String(200))
    active: Mapped[bool] = mapped_column(default=True)


class PhoneModelConfig(Base, TimestampMixin):
    """Catalog of known phone models, used for validation, compatibility and pricing."""

    __tablename__ = "phone_models"
    __table_args__ = (UniqueConstraint("manufacturer", "model", "variant", name="uq_phone_model"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    manufacturer: Mapped[str] = mapped_column(String(50), default="Apple")
    model: Mapped[str] = mapped_column(String(80), nullable=False)     # "iPhone 13 Pro"
    variant: Mapped[Optional[str]] = mapped_column(String(50))         # "Pro Max" already in model; variant for e.g. "Global"
    default_sale_price: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 2))
    active: Mapped[bool] = mapped_column(default=True)

    def display_name(self) -> str:
        return f"{self.manufacturer} {self.model}".strip()


class PartTypeConfig(Base, TimestampMixin):
    """Catalog of part types, e.g. Screen, Battery, Rear Camera, Housing."""

    __tablename__ = "part_types"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(80), unique=True, nullable=False)
    category: Mapped[Optional[str]] = mapped_column(String(50))  # display/battery/camera/housing/...
    active: Mapped[bool] = mapped_column(default=True)


class ExpenseCategory(Base, TimestampMixin):
    __tablename__ = "expense_categories"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(80), unique=True, nullable=False)
    active: Mapped[bool] = mapped_column(default=True)


class ConditionGrade(Base, TimestampMixin):
    __tablename__ = "condition_grades"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(10), unique=True, nullable=False)  # "A", "A-", "B", "SCRAP"
    label: Mapped[Optional[str]] = mapped_column(String(100))
    sort_order: Mapped[int] = mapped_column(default=0)
    active: Mapped[bool] = mapped_column(default=True)


class WorkflowStatus(Base, TimestampMixin):
    """Configurable status values per entity type (PHONE, PART, DONOR, REPAIR, ORDER, LISTING).

    Business logic references well-known `code` values (see
    app.services.defaults) but admins may add/rename/deactivate statuses
    from /config statuses without a schema change.
    """

    __tablename__ = "workflow_statuses"
    __table_args__ = (UniqueConstraint("entity_type", "code", name="uq_workflow_status"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    entity_type: Mapped[str] = mapped_column(String(20), nullable=False)  # PHONE|PART|DONOR|REPAIR|ORDER|LISTING
    code: Mapped[str] = mapped_column(String(40), nullable=False)
    label: Mapped[Optional[str]] = mapped_column(String(80))
    sort_order: Mapped[int] = mapped_column(default=0)
    is_terminal: Mapped[bool] = mapped_column(default=False)
    active: Mapped[bool] = mapped_column(default=True)


class TestDefinition(Base, TimestampMixin):
    """Configurable phone testing checklist item, e.g. 'Touchscreen', 'Face ID'."""

    __tablename__ = "test_definitions"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(60), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    sort_order: Mapped[int] = mapped_column(default=0)
    active: Mapped[bool] = mapped_column(default=True)


class PricingRule(Base, TimestampMixin):
    """Target margin / thresholds, optionally scoped to a specific phone model.

    A row with phone_model_id = NULL is the global default used by
    /analyse when no model-specific rule exists.
    """

    __tablename__ = "pricing_rules"

    id: Mapped[int] = mapped_column(primary_key=True)
    phone_model_id: Mapped[Optional[int]] = mapped_column(ForeignKey("phone_models.id"), nullable=True)
    target_margin_pct: Mapped[Optional[Decimal]] = mapped_column(Numeric(6, 2))
    min_profit_gbp: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 2))
    min_roi_pct: Mapped[Optional[Decimal]] = mapped_column(Numeric(6, 2))
    default_sale_price: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 2))
    active: Mapped[bool] = mapped_column(default=True)

    phone_model: Mapped[Optional["PhoneModelConfig"]] = relationship(lazy="selectin")


class IdSequence(Base):
    """Backing store for human-friendly sequential internal IDs (IP-000001, DF-000001, ...).

    Incremented inside a DB transaction with SELECT ... FOR UPDATE so it is
    safe under concurrent Discord interactions.
    """

    __tablename__ = "id_sequences"

    prefix: Mapped[str] = mapped_column(String(10), primary_key=True)
    next_value: Mapped[int] = mapped_column(default=1, nullable=False)
