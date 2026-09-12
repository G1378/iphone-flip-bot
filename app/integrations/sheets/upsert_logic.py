from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class UpsertPlan:
    # 1-indexed sheet row number -> full row values to write
    updates: dict[int, list[str]] = field(default_factory=dict)
    # rows to append at the end (order preserved)
    appends: list[list[str]] = field(default_factory=list)


def compute_upsert_plan(
    existing_id_to_row: dict[str, int],
    desired_rows: list[list[str]],
    id_col_index: int = 0,
) -> UpsertPlan:
    """Given the sheet's current internal_id -> row_number mapping and the
    full set of rows we want present, decide what to update in place vs.
    append. This is the core of idempotent sync (spec section 21: 'Do not
    create duplicate rows on repeated syncs. Store database IDs in the
    spreadsheet to make reconciliation reliable.') and is deliberately
    pure/synchronous so it can be unit tested without touching the Google
    Sheets API at all.
    """
    plan = UpsertPlan()
    for row in desired_rows:
        row_id = row[id_col_index]
        existing_row_num = existing_id_to_row.get(row_id)
        if existing_row_num is not None:
            plan.updates[existing_row_num] = row
        else:
            plan.appends.append(row)
    return plan
