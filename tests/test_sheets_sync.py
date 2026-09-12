from __future__ import annotations

from app.integrations.sheets.upsert_logic import compute_upsert_plan


def test_all_new_rows_are_appended_when_sheet_is_empty():
    plan = compute_upsert_plan(existing_id_to_row={}, desired_rows=[
        ["IP-000001", "iPhone 13 Pro", "SOLD"],
        ["IP-000002", "iPhone 12", "LISTED"],
    ])
    assert plan.updates == {}
    assert len(plan.appends) == 2


def test_existing_rows_are_updated_in_place_not_duplicated():
    existing = {"IP-000001": 2, "IP-000002": 3}
    plan = compute_upsert_plan(existing_id_to_row=existing, desired_rows=[
        ["IP-000001", "iPhone 13 Pro", "SOLD"],   # status changed since last sync
        ["IP-000002", "iPhone 12", "LISTED"],
    ])
    assert plan.appends == []
    assert plan.updates == {
        2: ["IP-000001", "iPhone 13 Pro", "SOLD"],
        3: ["IP-000002", "iPhone 12", "LISTED"],
    }


def test_mixed_new_and_existing_rows():
    existing = {"IP-000001": 2}
    plan = compute_upsert_plan(existing_id_to_row=existing, desired_rows=[
        ["IP-000001", "iPhone 13 Pro", "SOLD"],
        ["IP-000003", "iPhone 14", "PURCHASED"],  # brand new
    ])
    assert plan.updates == {2: ["IP-000001", "iPhone 13 Pro", "SOLD"]}
    assert plan.appends == [["IP-000003", "iPhone 14", "PURCHASED"]]


def test_repeated_sync_of_identical_data_produces_no_new_appends():
    """Running the same sync twice must never create duplicate rows -
    the second run should resolve every row to an update, never an append."""
    rows = [["IP-000001", "iPhone 13 Pro", "SOLD"], ["IP-000002", "iPhone 12", "LISTED"]]

    # first run: empty sheet
    plan1 = compute_upsert_plan(existing_id_to_row={}, desired_rows=rows)
    assert len(plan1.appends) == 2

    # simulate the sheet now containing those two rows at rows 2 and 3
    existing_after_first_run = {"IP-000001": 2, "IP-000002": 3}

    # second run with identical data
    plan2 = compute_upsert_plan(existing_id_to_row=existing_after_first_run, desired_rows=rows)
    assert plan2.appends == []
    assert len(plan2.updates) == 2
