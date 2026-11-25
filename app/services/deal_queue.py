"""The buy page's state machine: what happens when you press Bought or Reject.

A deal card moves  PENDING -> BOUGHT | REJECTED | EXPIRED.  Rejected can be put back (a mis-tap on a
phone is the usual reason). Bought is final here: it creates a real inventory record, which you then
manage with the normal commands, so there is deliberately no "un-buy".

Every transition locks the deal's row first (SELECT ... FOR UPDATE), so two people pressing Bought at
once, or a double-tap, can only ever create ONE phone.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import AcquisitionType, DealSource, DealStatus, InventoryEventType
from app.models.inventory_models import Phone
from app.models.pricing_models import DealAlert
from app.services import config_service
from app.services import phones as phones_service
from app.services.events import log_event


class DealStateError(ValueError):
    """The deal isn't in a state that allows this (already bought, already rejected, gone). The message is
    written for a person: it is shown in Discord as is."""


async def get_alert(session: AsyncSession, alert_id: int) -> Optional[DealAlert]:
    return (await session.execute(
        select(DealAlert).where(DealAlert.id == alert_id).execution_options(populate_existing=True)
    )).scalar_one_or_none()


async def _locked(session: AsyncSession, alert_id: int) -> DealAlert:
    row = (await session.execute(
        select(DealAlert).where(DealAlert.id == alert_id).with_for_update().execution_options(populate_existing=True)
    )).scalar_one_or_none()
    if row is None:
        raise DealStateError(f"Deal #{alert_id} doesn't exist any more.")
    return row


def _source_label(alert: DealAlert) -> str:
    return "Vinted" if alert.source == DealSource.VINTED.value else "eBay"


async def mark_bought(
    session: AsyncSession, alert_id: int, *, total_paid: Decimal, actor_discord_id: int,
    storage: Optional[str] = None, colour: Optional[str] = None, imei: Optional[str] = None,
    notes: Optional[str] = None, today: Optional[dt.date] = None,
) -> tuple[DealAlert, Phone]:
    """Record the purchase: creates the phone in inventory (REPAIR if the deal was faulty, otherwise RESALE).

    `total_paid` is what actually left your account, postage and fees included: that is the phone's
    cost basis, so every later profit figure is true."""
    alert = await _locked(session, alert_id)
    if alert.status != DealStatus.PENDING.value:
        who = f" by <@{alert.decided_by_discord_id}>" if alert.decided_by_discord_id else ""
        raise DealStateError(f"Deal #{alert.id} was already {alert.status.lower()}{who}.")
    if total_paid <= 0:
        raise DealStateError("The amount paid must be more than £0.")

    model = alert.phone_model
    storage_value = config_service.normalise_storage(storage) if storage and storage.strip() else alert.storage
    bits = [f"Bought via the buy page (deal #{alert.id}, {_source_label(alert)}). {alert.url}"]
    if alert.expected_profit is not None:
        bits.append(f"At the time: resale ~£{alert.resale_estimate}, expected profit £{alert.expected_profit} (ROI {alert.roi_pct}%).")
    if alert.bucket == "FAULTY":
        bits.append(f"Repair estimate £{alert.repair_estimate}: {alert.repair_basis or 'flat assumption'}.")
    if alert.flags:
        bits.append(f"Flags: {alert.flags}")
    if notes and notes.strip():
        bits.append(notes.strip())

    phone = await phones_service.buy_phone(
        session,
        phones_service.BuyPhoneInput(
            manufacturer=model.manufacturer, model=model.model, variant=model.variant, storage=storage_value,
            colour=colour.strip() if colour and colour.strip() else None,
            imei=imei.strip() if imei and imei.strip() else None, serial_number=None, carrier=None,
            purchase_price=total_paid.quantize(Decimal("0.01")), purchase_date=today or dt.date.today(),
            seller_source=_source_label(alert),
            acquisition_type=AcquisitionType.REPAIR if alert.bucket == "FAULTY" else AcquisitionType.RESALE,
            notes="\n".join(bits)[:2000], location_code=None,
        ),
        actor_discord_id,
    )
    alert.status = DealStatus.BOUGHT.value
    alert.phone_id = phone.id
    alert.decided_by_discord_id = actor_discord_id
    alert.decided_at = dt.datetime.now(dt.timezone.utc)
    await session.flush()
    await log_event(
        session, event_type=InventoryEventType.DEAL_BOUGHT.value, entity_type="PHONE", entity_id=phone.internal_id,
        actor_discord_id=actor_discord_id, related_entity_type="SYSTEM", related_entity_id=f"DEAL-{alert.id}",
        new_state={"deal_id": alert.id, "source": alert.source, "paid": str(total_paid), "asked_all_in": str(alert.total_cost),
                   "expected_profit": str(alert.expected_profit) if alert.expected_profit is not None else None},
        notes=alert.url,
    )
    return alert, phone


async def mark_rejected(session: AsyncSession, alert_id: int, *, actor_discord_id: int,
                         reason: Optional[str] = None) -> DealAlert:
    alert = await _locked(session, alert_id)
    if alert.status != DealStatus.PENDING.value:
        raise DealStateError(f"Deal #{alert.id} was already {alert.status.lower()}.")
    alert.status = DealStatus.REJECTED.value
    alert.decision_note = (reason or "").strip()[:500] or None
    alert.decided_by_discord_id = actor_discord_id
    alert.decided_at = dt.datetime.now(dt.timezone.utc)
    await session.flush()
    await log_event(
        session, event_type=InventoryEventType.DEAL_REJECTED.value, entity_type="SYSTEM", entity_id=f"DEAL-{alert.id}",
        actor_discord_id=actor_discord_id, new_state={"source": alert.source, "title": alert.title[:100]}, notes=alert.decision_note,
    )
    return alert


async def restore_rejected(session: AsyncSession, alert_id: int, *, actor_discord_id: int,
                            now: Optional[dt.datetime] = None) -> DealAlert:
    """Undo a reject. Not allowed once an auction has ended: there is nothing left to buy."""
    now = now or dt.datetime.now(dt.timezone.utc)
    alert = await _locked(session, alert_id)
    if alert.status != DealStatus.REJECTED.value:
        raise DealStateError(f"Deal #{alert.id} isn't rejected (it is {alert.status.lower()}), so there is nothing to undo.")
    if alert.is_auction and alert.ends_at and alert.ends_at < now:
        raise DealStateError("That auction has already ended.")
    alert.status = DealStatus.PENDING.value
    alert.decision_note = None
    alert.decided_by_discord_id = None
    alert.decided_at = None
    await session.flush()
    await log_event(session, event_type=InventoryEventType.DEAL_RESTORED.value, entity_type="SYSTEM",
                    entity_id=f"DEAL-{alert.id}", actor_discord_id=actor_discord_id)
    return alert


async def expire_stale(session: AsyncSession, *, now: Optional[dt.datetime] = None,
                        ttl_hours: Decimal = Decimal("48")) -> list[DealAlert]:
    """Pending cards whose auction has ended, or that nobody acted on for `ttl_hours`: a listing that old
    has very likely sold, and a stale card on the buy page costs you attention. Returns what it expired,
    so the caller can grey the cards out."""
    now = now or dt.datetime.now(dt.timezone.utc)
    cutoff = now - dt.timedelta(hours=float(ttl_hours))
    rows = (await session.execute(
        select(DealAlert).where(
            DealAlert.status == DealStatus.PENDING.value,
            or_(DealAlert.created_at < cutoff, (DealAlert.is_auction.is_(True)) & (DealAlert.ends_at < now)),
        ).with_for_update()
    )).scalars().all()
    for a in rows:
        a.decision_note = "auction ended" if (a.is_auction and a.ends_at and a.ends_at < now) else f"not acted on in {ttl_hours.normalize():f}h"
        a.status = DealStatus.EXPIRED.value
        a.decided_at = now
    await session.flush()
    return list(rows)


async def pending_alerts(session: AsyncSession, limit: int = 25) -> list[DealAlert]:
    return list((await session.execute(
        select(DealAlert).where(DealAlert.status == DealStatus.PENDING.value).order_by(DealAlert.id.desc()).limit(limit)
    )).scalars().all())


async def unposted_pending(session: AsyncSession) -> list[DealAlert]:
    """Pending deals with no card in Discord yet (e.g. the bot was down, or no channel was set up)."""
    return list((await session.execute(
        select(DealAlert).where(DealAlert.status == DealStatus.PENDING.value, DealAlert.message_id.is_(None)).order_by(DealAlert.id)
    )).scalars().all())


@dataclass
class QueueCounts:
    pending: int = 0
    bought: int = 0
    rejected: int = 0
    expired: int = 0
    pending_profit: Decimal = Decimal("0")     # what's on the table if every pending deal were bought and sold as estimated


async def counts(session: AsyncSession) -> QueueCounts:
    q = QueueCounts()
    for status, n in (await session.execute(select(DealAlert.status, func.count()).group_by(DealAlert.status))).all():
        if hasattr(q, status.lower()):
            setattr(q, status.lower(), n)
    q.pending_profit = (await session.execute(
        select(func.coalesce(func.sum(DealAlert.expected_profit), 0)).where(DealAlert.status == DealStatus.PENDING.value)
    )).scalar_one()
    return q


async def record_message(session: AsyncSession, alert_id: int, channel_id: int, message_id: int) -> None:
    alert = await get_alert(session, alert_id)
    if alert is not None:
        alert.message_channel_id, alert.message_id = channel_id, message_id
        await session.flush()
