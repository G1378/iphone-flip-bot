from __future__ import annotations

import logging
from typing import Optional

from app.config import settings
from app.integrations.sheets.upsert_logic import compute_upsert_plan

logger = logging.getLogger(__name__)


class SheetsNotConfiguredError(RuntimeError):
    """Raised when a sync is attempted but no spreadsheet/service account is configured."""


class SheetsClient:
    """Thin wrapper around gspread. All sheet I/O for the app goes through
    this class so the idempotent-upsert behaviour lives in one place.

    PostgreSQL remains authoritative at all times (spec section 21) - this
    class only ever *pushes* data outward; nothing reads business state
    back from Sheets.
    """

    SCOPES = [
        "https://www.googleapis.com/auth/spreadsheets",
    ]

    def __init__(self) -> None:
        self.configured = settings.sheets_configured
        self._gc = None
        self._sh = None

    def _ensure_client(self):
        if self._gc is not None:
            return self._gc
        if not self.configured:
            raise SheetsNotConfiguredError(
                "Google Sheets is not configured. Set GOOGLE_SHEETS_SPREADSHEET_ID and "
                "provide a service account JSON at GOOGLE_SERVICE_ACCOUNT_FILE."
            )
        import gspread
        from google.oauth2.service_account import Credentials

        creds = Credentials.from_service_account_file(settings.google_service_account_file, scopes=self.SCOPES)
        self._gc = gspread.authorize(creds)
        self._sh = self._gc.open_by_key(settings.google_sheets_spreadsheet_id)
        return self._gc

    def _spreadsheet(self):
        self._ensure_client()
        return self._sh

    def get_or_create_worksheet(self, title: str, headers: list[str]):
        sh = self._spreadsheet()
        try:
            ws = sh.worksheet(title)
        except Exception:
            ws = sh.add_worksheet(title=title, rows=200, cols=max(10, len(headers)))
            ws.update("A1", [headers])
            return ws

        first_row = ws.row_values(1)
        if first_row != headers:
            ws.update("A1", [headers])
        return ws

    def upsert_rows(self, worksheet, rows: list[list[str]], id_col_index: int = 0) -> tuple[int, int]:
        """Idempotent upsert keyed on the value in id_col_index (always the
        internal DB id, e.g. IP-000001). Returns (updated_count, appended_count)."""
        if not rows:
            return (0, 0)

        existing_values = worksheet.get_all_values()
        existing_id_to_row: dict[str, int] = {}
        for i, row in enumerate(existing_values[1:], start=2):  # skip header, 1-indexed
            if len(row) > id_col_index and row[id_col_index]:
                existing_id_to_row[row[id_col_index]] = i

        plan = compute_upsert_plan(existing_id_to_row, rows, id_col_index)

        for row_num, values in plan.updates.items():
            worksheet.update(f"A{row_num}", [values])

        if plan.appends:
            worksheet.append_rows(plan.appends, value_input_option="RAW")

        return (len(plan.updates), len(plan.appends))
