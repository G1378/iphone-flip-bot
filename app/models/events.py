from __future__ import annotations

import datetime as dt
from typing import Optional

from sqlalchemy import BigInteger, DateTime, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class InventoryEvent(Base):
    """Immutable audit ledger. Every meaningful state change in the system
    writes exactly one row here (spec section 24). Rows are never updated
    or deleted - corrections are made via new MANUAL_ADJUSTMENT events."""

    __tablename__ = "inventory_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    occurred_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )
    event_type: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    actor_discord_id: Mapped[Optional[int]] = mapped_column(BigInteger)

    entity_type: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    entity_id: Mapped[str] = mapped_column(String(20), nullable=False, index=True)  # internal_id

    related_entity_type: Mapped[Optional[str]] = mapped_column(String(20))
    related_entity_id: Mapped[Optional[str]] = mapped_column(String(20))

    previous_state: Mapped[Optional[dict]] = mapped_column(JSONB)
    new_state: Mapped[Optional[dict]] = mapped_column(JSONB)

    notes: Mapped[Optional[str]] = mapped_column(Text)
