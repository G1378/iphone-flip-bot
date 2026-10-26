"""Wiping data while you're still experimenting.

`wipe_all_data` empties every business table (phones, donors, parts, orders, repairs,
listings, sales, expenses, tickets, history, prices, ID counters) so you can start again
from PHONE IP-000001. It never touches who may use the bot or which channels are
registered, so a wipe can't lock you out.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Base

# Never wiped: wiping these would lock everyone out of the bot / unregister its channels.
ALWAYS_KEEP = frozenset({"authorized_users", "bot_channels"})

# Settings and catalogs. Kept by default; reset to factory defaults with include_config=True.
CONFIG_TABLES = frozenset({
    "business_settings", "condition_grades", "expense_categories", "locations", "part_types",
    "phone_models", "task_templates", "workflow_statuses", "test_definitions", "pricing_rules",
})


@dataclass
class WipeResult:
    rows_deleted: dict[str, int] = field(default_factory=dict)
    kept: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return sum(self.rows_deleted.values())


def tables_to_wipe(include_config: bool) -> list[str]:
    """Table names in a safe delete order (children before the tables they point at)."""
    skip = ALWAYS_KEEP | (frozenset() if include_config else CONFIG_TABLES)
    return [t.name for t in reversed(Base.metadata.sorted_tables) if t.name not in skip]


async def wipe_all_data(session: AsyncSession, *, include_config: bool = False) -> WipeResult:
    """Delete every business row. With include_config=True the catalogs/settings are wiped too
    and re-seeded with the factory defaults (including the full phone model catalog)."""
    result = WipeResult()
    wanted = set(tables_to_wipe(include_config))
    for table in reversed(Base.metadata.sorted_tables):
        if table.name not in wanted:
            result.kept.append(table.name)
            continue
        count = (await session.execute(select(func.count()).select_from(table))).scalar_one()
        await session.execute(table.delete())
        result.rows_deleted[table.name] = int(count)
    await session.flush()

    if include_config:
        from app.services import config_service  # local import: avoids a cycle
        await config_service.seed_defaults_if_empty(session)
    return result
