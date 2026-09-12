from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.integrations.sheets.client import SheetsClient, SheetsNotConfiguredError
from app.integrations.sheets.sync import sync_all

logger = logging.getLogger(__name__)


async def run_sheets_sync(session: AsyncSession, client: SheetsClient) -> dict[str, tuple[int, int]]:
    """Thin wrapper so both the scheduler and the /report sheets-sync
    Discord command share one code path. Raises SheetsNotConfiguredError
    if Sheets isn't set up - callers decide how to surface that."""
    if not client.configured:
        raise SheetsNotConfiguredError("Google Sheets is not configured.")
    return await sync_all(session, client)
