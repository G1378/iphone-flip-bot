from __future__ import annotations

import datetime as dt
from typing import Optional

from sqlalchemy import (
    BigInteger, Boolean, Date, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, func, text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin

# Plain strings (not DB enums) so adding a status/priority never needs a migration.
TASK_STATUSES = ("TODO", "IN_PROGRESS", "BLOCKED", "DONE", "CANCELLED")
OPEN_TASK_STATUSES = ("TODO", "IN_PROGRESS", "BLOCKED")
TASK_PRIORITIES = ("LOW", "NORMAL", "HIGH", "URGENT")


class TaskTemplate(Base, TimestampMixin):
    """A reusable GENERAL (business-level) ticket definition. recur_days=None
    means a one-off (opened by `/tasks setup-general`); otherwise a new ticket
    is opened every `recur_days` once the last one is closed.

    Phone tickets are NOT templates: they are derived from each phone's real
    state by app.services.phone_tickets."""

    __tablename__ = "task_templates"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(80), unique=True, nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text)
    category: Mapped[str] = mapped_column(String(40), default="General")
    scope: Mapped[str] = mapped_column(String(10), default="GENERAL", index=True)
    priority: Mapped[str] = mapped_column(String(10), default="NORMAL")
    recur_days: Mapped[Optional[int]] = mapped_column(Integer)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class Task(Base, TimestampMixin):
    """One ticket = one embed in the tasks channel.

    scope=PHONE   -> automatic. `stage` says which step the phone is on
                     (TEST, REPAIR, VERIFY, LIST, SHIP, CLOSE). A phone has at
                     most ONE open ticket at any time (enforced by a partial
                     unique index); when its step changes the old ticket is
                     closed (message deleted) and a new one is opened.
    scope=GENERAL -> business jobs and routines, created from templates or by hand.
    """

    __tablename__ = "tasks"
    __table_args__ = (
        Index(
            "uq_task_one_open_ticket_per_phone", "phone_id", unique=True,
            postgresql_where=text("phone_id IS NOT NULL AND status IN ('TODO','IN_PROGRESS','BLOCKED')"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    internal_id: Mapped[str] = mapped_column(String(20), unique=True, nullable=False, index=True)  # TK-000001

    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text)
    category: Mapped[str] = mapped_column(String(40), default="General")
    scope: Mapped[str] = mapped_column(String(10), default="GENERAL")
    priority: Mapped[str] = mapped_column(String(10), default="NORMAL")
    status: Mapped[str] = mapped_column(String(20), default="TODO", index=True)

    template_id: Mapped[Optional[int]] = mapped_column(ForeignKey("task_templates.id", ondelete="SET NULL"))
    phone_id: Mapped[Optional[int]] = mapped_column(ForeignKey("phones.id", ondelete="SET NULL"), index=True)
    stage: Mapped[Optional[str]] = mapped_column(String(20))           # phone tickets only
    content_hash: Mapped[Optional[str]] = mapped_column(String(40))    # change detection for phone tickets

    assignee_discord_id: Mapped[Optional[int]] = mapped_column(BigInteger)
    due_date: Mapped[Optional[dt.date]] = mapped_column(Date)
    blocked_reason: Mapped[Optional[str]] = mapped_column(Text)
    # True when the engine (not a person) blocked it, e.g. "waiting for parts",
    # so the engine may also unblock it when the parts arrive.
    system_blocked: Mapped[bool] = mapped_column(Boolean, default=False)
    notes: Mapped[Optional[str]] = mapped_column(Text)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)

    created_by_discord_id: Mapped[Optional[int]] = mapped_column(BigInteger)
    completed_by_discord_id: Mapped[Optional[int]] = mapped_column(BigInteger)
    completed_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))

    # Discord rendering bookkeeping: any change bumps `version`; the poster loop
    # (re)draws the embed whenever posted_version < version.
    version: Mapped[int] = mapped_column(Integer, default=1)
    posted_version: Mapped[int] = mapped_column(Integer, default=0)
    discord_channel_id: Mapped[Optional[int]] = mapped_column(BigInteger)
    discord_message_id: Mapped[Optional[int]] = mapped_column(BigInteger, unique=True, index=True)
    overdue_notified: Mapped[bool] = mapped_column(Boolean, default=False)


class PhoneTicketOverride(Base):
    """A person pressed Done on a step the bot could not detect as finished
    (e.g. skipped testing). The engine then skips that step for that phone."""

    __tablename__ = "phone_ticket_overrides"
    __table_args__ = (UniqueConstraint("phone_id", "stage", name="uq_phone_ticket_override"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    phone_id: Mapped[int] = mapped_column(ForeignKey("phones.id", ondelete="CASCADE"), nullable=False, index=True)
    stage: Mapped[str] = mapped_column(String(20), nullable=False)
    created_by_discord_id: Mapped[Optional[int]] = mapped_column(BigInteger)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class JobChannel(Base):
    """The Discord channel opened for one phone when someone presses Start.

    One per phone (unique), created on Start and deleted the moment the phone
    leaves the workshop (listed, sold, scrapped...). It holds a pinned control
    panel whose buttons are generated from the phone's real situation."""

    __tablename__ = "job_channels"

    id: Mapped[int] = mapped_column(primary_key=True)
    phone_id: Mapped[int] = mapped_column(ForeignKey("phones.id", ondelete="CASCADE"), nullable=False, unique=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    channel_id: Mapped[int] = mapped_column(BigInteger, nullable=False, unique=True, index=True)
    panel_message_id: Mapped[Optional[int]] = mapped_column(BigInteger)
    panel_hash: Mapped[Optional[str]] = mapped_column(String(40))
    created_by_discord_id: Mapped[Optional[int]] = mapped_column(BigInteger)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
