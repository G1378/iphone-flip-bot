from __future__ import annotations

from typing import Any, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.events import InventoryEvent


async def log_event(
    session: AsyncSession,
    *,
    event_type: str,
    entity_type: str,
    entity_id: str,
    actor_discord_id: Optional[int] = None,
    related_entity_type: Optional[str] = None,
    related_entity_id: Optional[str] = None,
    previous_state: Optional[dict[str, Any]] = None,
    new_state: Optional[dict[str, Any]] = None,
    notes: Optional[str] = None,
) -> InventoryEvent:
    """Write one immutable audit-ledger row. Never update/delete these rows."""
    event = InventoryEvent(
        event_type=event_type,
        entity_type=entity_type,
        entity_id=entity_id,
        actor_discord_id=actor_discord_id,
        related_entity_type=related_entity_type,
        related_entity_id=related_entity_id,
        previous_state=previous_state,
        new_state=new_state,
        notes=notes,
    )
    session.add(event)
    await session.flush()
    return event
