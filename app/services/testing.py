from __future__ import annotations

import datetime as dt
from typing import Optional, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import InventoryEventType, TestOutcome
from app.models.inventory_models import Phone, TestResult
from app.models.config_models import TestDefinition
from app.services.events import log_event


async def record_result(
    session: AsyncSession, phone: Phone, test_definition: TestDefinition,
    result: TestOutcome, actor_discord_id: int, notes: Optional[str] = None,
) -> TestResult:
    row = TestResult(
        phone_id=phone.id,
        test_definition_id=test_definition.id,
        result=result,
        notes=notes,
        tested_by_discord_id=actor_discord_id,
        tested_at=dt.datetime.now(dt.timezone.utc),
    )
    session.add(row)
    await session.flush()
    await log_event(
        session,
        event_type=InventoryEventType.PHONE_TESTED.value,
        entity_type="PHONE",
        entity_id=phone.internal_id,
        actor_discord_id=actor_discord_id,
        new_state={"test": test_definition.name, "result": result.value},
        notes=notes,
    )
    return row


async def current_results(session: AsyncSession, phone_id: int) -> dict[int, TestResult]:
    """Latest TestResult per test_definition_id for a phone (append-only ledger)."""
    stmt = (
        select(TestResult)
        .where(TestResult.phone_id == phone_id)
        .order_by(TestResult.test_definition_id, TestResult.tested_at.desc())
    )
    rows = (await session.execute(stmt)).scalars().all()
    latest: dict[int, TestResult] = {}
    for row in rows:
        if row.test_definition_id not in latest:
            latest[row.test_definition_id] = row
    return latest


async def summary(session: AsyncSession, phone: Phone) -> list[tuple[TestDefinition, Optional[TestResult]]]:
    from app.services.config_service import list_test_definitions

    definitions = await list_test_definitions(session)
    latest = await current_results(session, phone.id)
    return [(d, latest.get(d.id)) for d in definitions]


async def has_any_failures(session: AsyncSession, phone: Phone) -> bool:
    latest = await current_results(session, phone.id)
    return any(r.result == TestOutcome.FAIL for r in latest.values())
