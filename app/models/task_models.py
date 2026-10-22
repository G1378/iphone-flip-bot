from __future__ import annotations

import datetime as dt
from typing import Optional

from sqlalchemy import BigInteger, Boolean, Date, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin

# Plain strings (not DB enums) so adding a status/priority never needs a migration.
TASK_STATUSES = ("TODO", "IN_PROGRESS", "BLOCKED", "DONE", "CANCELLED")
OPEN_TASK_STATUSES = ("TODO", "IN_PROGRESS", "BLOCKED")
TASK_PRIORITIES = ("LOW", "NORMAL", "HIGH", "URGENT")


class TaskTemplate(Base, TimestampMixin):
    """A reusable task definition, seeded from app.services.task_templates and
    editable from Discord (`/tasks template-*`).

    scope=PHONE   -> copied onto every new phone as a checklist. `trigger` says
                     when the ticket appears in the channel, e.g.
                     "PHONE:TESTED" or "REPAIR:PARTS_RESERVED,REPAIR:IN_PROGRESS"
                     (comma-separated = any of).
    scope=GENERAL -> business-level to-do. recur_days=None means a one-off
                     (created once by `/tasks setup-general`); otherwise a new
                     ticket is opened every `recur_days` once the last is closed.
    """

    __tablename__ = "task_templates"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(80), unique=True, nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text)
    category: Mapped[str] = mapped_column(String(40), default="General")
    scope: Mapped[str] = mapped_column(String(10), default="PHONE", index=True)
    trigger: Mapped[Optional[str]] = mapped_column(String(120))
    auto_complete_trigger: Mapped[Optional[str]] = mapped_column(String(120))
    priority: Mapped[str] = mapped_column(String(10), default="NORMAL")
    due_after_days: Mapped[Optional[int]] = mapped_column(Integer)
    recur_days: Mapped[Optional[int]] = mapped_column(Integer)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class Task(Base, TimestampMixin):
    """One ticket. Rendered as one embed (with buttons) in the tasks channel."""

    __tablename__ = "tasks"

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

    assignee_discord_id: Mapped[Optional[int]] = mapped_column(BigInteger)
    due_date: Mapped[Optional[dt.date]] = mapped_column(Date)
    blocked_reason: Mapped[Optional[str]] = mapped_column(Text)
    notes: Mapped[Optional[str]] = mapped_column(Text)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)

    # False = checklist item that exists but hasn't reached its stage yet, so it
    # is not shown in Discord.
    released: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
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


class TaskTriggerState(Base):
    """Last status the task engine has seen for a phone/repair, so the
    reconcile loop only reacts to *changes*."""

    __tablename__ = "task_trigger_state"
    __table_args__ = (UniqueConstraint("entity_type", "entity_id", name="uq_task_trigger_entity"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    entity_type: Mapped[str] = mapped_column(String(10), nullable=False)  # PHONE | REPAIR
    entity_id: Mapped[int] = mapped_column(Integer, nullable=False)
    last_status: Mapped[Optional[str]] = mapped_column(String(30))
