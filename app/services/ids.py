from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.config_models import IdSequence

PREFIXES = {
    "phone": "IP",
    "donor": "DF",
    "part": "PT",
    "repair": "RP",
    "order": "PO",
    "listing": "LS",
    "sale": "SL",
    "expense": "EX",
}


async def next_internal_id(session: AsyncSession, kind: str) -> str:
    """Atomically allocate the next sequential ID for `kind` (e.g. 'phone' -> IP-000042).

    Uses SELECT ... FOR UPDATE so concurrent Discord interactions never
    collide, even across multiple bot processes talking to the same DB.
    """
    prefix = PREFIXES[kind]
    result = await session.execute(
        select(IdSequence).where(IdSequence.prefix == prefix).with_for_update()
    )
    row = result.scalar_one_or_none()
    if row is None:
        row = IdSequence(prefix=prefix, next_value=1)
        session.add(row)
        await session.flush()
    value = row.next_value
    row.next_value = value + 1
    await session.flush()
    return f"{prefix}-{value:06d}"
