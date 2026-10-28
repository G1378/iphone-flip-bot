from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Optional, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.config_models import PartTypeConfig, TestDefinition
from app.models.enums import FaultStatus, InventoryEventType, TestOutcome
from app.models.inventory_models import Phone, PhoneFault, TestResult
from app.models.repair_models import Repair
from app.services import config_service, cosmetics
from app.services import phones as phones_service
from app.services import repairs as repairs_service
from app.services.events import log_event

# Statuses that say "this phone is believed to work / is on the sell path".
# A failed test moves a phone out of these and into REPAIR_REQUIRED.
WORKING_STATUSES = ("PURCHASED", "AWAITING_TEST", "TESTED", "READY_FOR_LISTING", "LISTED")
# Finished with: record the result, but don't reopen the phone for repair.
CLOSED_STATUSES = ("SOLD", "SHIPPED", "COMPLETE", "SCRAPPED")
NEEDS_REPAIR_STATUS = "REPAIR_REQUIRED"
NEEDS_REPAIR_CONDITION = "Needs repair"


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


async def settle_after_fault_cleared(session: AsyncSession, phone: Phone, actor_discord_id: int, why: str) -> None:
    """A fault was cancelled outright (battery override, a D re-graded up): if that leaves nothing to repair,
    drop the empty repair and take the phone out of REPAIR_REQUIRED. It goes back to TESTED if every test and
    its condition are recorded, otherwise to AWAITING_TEST so the walkthrough carries on."""
    if phone.current_status != NEEDS_REPAIR_STATUS:
        return
    still_open = (await session.execute(select(PhoneFault).where(
        PhoneFault.phone_id == phone.id, PhoneFault.status == FaultStatus.OPEN))).scalars().first()
    if still_open is not None:
        return
    repair = await repairs_service.get_active_repair(session, phone)
    if repair is not None:
        if any(rp.status in ("REQUIRED", "RESERVED", "INSTALLED") for rp in repair.parts):
            return   # still something to fit (or already fitted: it needs verifying)
        await cosmetics.cancel_repair_if_empty(session, phone, repair, actor_discord_id)
    report = await build_report(session, phone)
    done = not report.failed and not report.not_tested and not report.condition_missing
    await phones_service.set_status(session, phone, "TESTED" if done else "AWAITING_TEST", actor_discord_id, notes=why)


BATTERY_TEST_CODE = "battery_health"   # the test definition the battery % drives (no separate PASS/FAIL question)


@dataclass
class BatteryOutcome:
    percent: Optional[int] = None
    not_applicable: bool = False
    below_minimum: bool = False      # under cosmetics.BATTERY_MIN_HEALTH
    overridden: bool = False         # under the minimum but kept anyway
    failed: bool = False             # recorded as a FAIL: the Battery part is needed
    apply: Optional["ApplyOutcome"] = None


async def apply_battery_health(
    session: AsyncSession, phone: Phone, actor_discord_id: int, *,
    percent: Optional[int] = None, not_applicable: bool = False, override: bool = False,
) -> BatteryOutcome:
    """Record the battery health % (or 'not applicable') and settle the battery test from it.

    Under 80% FAILS the battery test, which flags the Battery part exactly like any failed test
    (fault, repair line, 'needs ordering' if there's none in stock). `override=True` keeps the
    battery: the test passes with a note saying so. 80% and over passes."""
    await cosmetics.set_battery(session, phone, actor_discord_id, percent=percent, not_applicable=not_applicable)
    out = BatteryOutcome(percent=percent, not_applicable=not_applicable)
    definition = next((d for d in await config_service.list_test_definitions(session)
                       if d.code == BATTERY_TEST_CODE), None)
    if not_applicable:
        result, notes = TestOutcome.NOT_APPLICABLE, "Battery health not applicable"
    elif percent < cosmetics.BATTERY_MIN_HEALTH:
        out.below_minimum = True
        if override:
            out.overridden = True
            result, notes = TestOutcome.PASS, f"Battery health {percent}% (under {cosmetics.BATTERY_MIN_HEALTH}%): overridden, battery kept"
        else:
            out.failed = True
            result, notes = TestOutcome.FAIL, f"Battery health {percent}% (under {cosmetics.BATTERY_MIN_HEALTH}%)"
    else:
        result, notes = TestOutcome.PASS, f"Battery health {percent}%"
    if definition is not None:
        out.apply = await record_and_apply(session, phone, definition, result, actor_discord_id, notes=notes)
        if out.apply.fault_resolved:   # e.g. a low reading overridden, or a mistyped % corrected
            await settle_after_fault_cleared(session, phone, actor_discord_id, "Battery no longer needed")
    return out


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


# ----------------------------------------------------------------------
# Failure handling: a failed test flags the phone and the parts needed
# ----------------------------------------------------------------------
def _looks_working(functional_condition: Optional[str]) -> bool:
    """True for free-text conditions like 'Working' / 'Fully working'; false
    for 'Not working', 'Faulty', 'Needs repair', empty, etc."""
    if not functional_condition:
        return False
    t = functional_condition.strip().lower()
    if any(bad in t for bad in ("not ", "non", "faulty", "broken", "repair", "fix", "dead", "fail")):
        return False
    return "working" in t or "functional" in t


@dataclass
class ApplyOutcome:
    """What recording one result did to the phone."""
    result: TestResult
    flagged: bool = False                      # a FAIL that flagged the phone (False for sold/closed phones)
    fault: Optional[PhoneFault] = None
    part_type: Optional[PartTypeConfig] = None  # part flagged as needed (None = needs manual diagnosis)
    repair: Optional[Repair] = None
    previous_status: Optional[str] = None
    new_status: Optional[str] = None
    was_listed: bool = False                   # phone has a live listing that now needs attention
    fault_resolved: bool = False               # a PASS/N-A cleared an earlier failure

    @property
    def status_changed(self) -> bool:
        return self.previous_status != self.new_status


async def _open_fault_for_test(session: AsyncSession, phone: Phone, definition: TestDefinition) -> Optional[PhoneFault]:
    stmt = select(PhoneFault).where(
        PhoneFault.phone_id == phone.id,
        PhoneFault.test_definition_id == definition.id,
        PhoneFault.status == FaultStatus.OPEN,
    )
    return (await session.execute(stmt)).scalars().first()


async def _flag_failure(session: AsyncSession, phone: Phone, definition: TestDefinition,
                         actor_discord_id: int, notes: Optional[str], out: ApplyOutcome) -> None:
    out.previous_status = out.new_status = phone.current_status
    if phone.current_status in CLOSED_STATUSES:
        return  # recorded, but a sold/scrapped phone isn't reopened for repair

    part_type = definition.part_type
    out.part_type = part_type

    # 1. Fault on the phone, carrying the part that fixes it. One open fault
    #    per test: re-failing the same test doesn't stack duplicates.
    fault = await _open_fault_for_test(session, phone, definition)
    if fault is None:
        description = f"{definition.name} failed testing"
        if notes:
            description = f"{description}: {notes}"
        fault = await phones_service.add_fault(
            session, phone, description[:200], actor_discord_id,
            required_part_type_id=part_type.id if part_type else None,
            test_definition_id=definition.id,
        )
    out.fault = fault

    # 2. A phone that was thought to be working now needs fixing.
    if phone.current_status in WORKING_STATUSES:
        out.was_listed = phone.current_status == "LISTED"
        await phones_service.set_status(
            session, phone, NEEDS_REPAIR_STATUS, actor_discord_id,
            notes=f"{definition.name} failed testing",
        )
        out.new_status = phone.current_status
    if _looks_working(phone.functional_condition):
        await phones_service.edit_phone(
            session, phone, actor_discord_id, functional_condition=NEEDS_REPAIR_CONDITION,
        )

    # 3. Put the needed part on the phone's repair (creating it if needed).
    out.repair = await repairs_service.sync_repair_with_open_faults(session, phone, actor_discord_id)
    out.flagged = True


async def _clear_failure(session: AsyncSession, phone: Phone, definition: TestDefinition, out: ApplyOutcome) -> None:
    """The test now passes (or doesn't apply): resolve the fault its earlier
    failure raised and drop the part that was only needed for it."""
    fault = await _open_fault_for_test(session, phone, definition)
    if fault is None:
        return
    fault.status = FaultStatus.RESOLVED
    fault.resolved_at = dt.datetime.now(dt.timezone.utc)
    await session.flush()
    await repairs_service.release_fault_from_repair(session, fault)
    out.fault = fault
    out.fault_resolved = True


async def record_and_apply(
    session: AsyncSession, phone: Phone, test_definition: TestDefinition,
    result: TestOutcome, actor_discord_id: int, notes: Optional[str] = None,
) -> ApplyOutcome:
    """Record a test result and act on it.

    FAIL  -> raise a fault, flag the part that fixes it, move a working phone
             to REPAIR_REQUIRED and make sure its repair lists that part.
    PASS / NOT_APPLICABLE -> resolve any fault an earlier failure of this
             test raised.
    """
    row = await record_result(session, phone, test_definition, result, actor_discord_id, notes=notes)
    out = ApplyOutcome(result=row, previous_status=phone.current_status, new_status=phone.current_status)
    if result == TestOutcome.FAIL:
        await _flag_failure(session, phone, test_definition, actor_discord_id, notes, out)
    elif result in (TestOutcome.PASS, TestOutcome.NOT_APPLICABLE):
        await _clear_failure(session, phone, test_definition, out)
    return out


# ----------------------------------------------------------------------
# Walkthrough wrap-up
# ----------------------------------------------------------------------
@dataclass
class TestReport:
    passed: int = 0
    not_applicable: int = 0
    not_tested: int = 0
    failed: list[tuple[TestDefinition, Optional[PartTypeConfig]]] = field(default_factory=list)
    parts_needed: list[str] = field(default_factory=list)  # de-duplicated part type names
    repair_id: Optional[str] = None
    status: str = ""
    condition_missing: list[str] = field(default_factory=list)  # frame / back glass grade, battery health

    __test__ = False  # not a pytest test class


async def finish_walkthrough(session: AsyncSession, phone: Phone, actor_discord_id: int) -> TestReport:
    """Wrap up a walkthrough: a phone that has every test PASS/N-A, its condition recorded (frame and
    back glass grade, battery health) and hasn't been looked at yet becomes TESTED. Returns the phone's current state
    (taken from the database, so earlier failures that were skipped this
    time still count)."""
    report = await build_report(session, phone)
    if (
        not report.failed and not report.not_tested and not report.condition_missing
        and phone.current_status in ("PURCHASED", "AWAITING_TEST")
    ):
        await phones_service.set_status(session, phone, "TESTED", actor_discord_id, notes="All tests passed")
        report.status = phone.current_status
    return report


async def build_report(session: AsyncSession, phone: Phone) -> TestReport:
    report = TestReport(status=phone.current_status, condition_missing=cosmetics.missing_condition(phone))
    for definition, result in await summary(session, phone):
        outcome = result.result if result else TestOutcome.NOT_TESTED
        if outcome == TestOutcome.PASS:
            report.passed += 1
        elif outcome == TestOutcome.NOT_APPLICABLE:
            report.not_applicable += 1
        elif outcome == TestOutcome.FAIL:
            report.failed.append((definition, definition.part_type))
            if definition.part_type and definition.part_type.name not in report.parts_needed:
                report.parts_needed.append(definition.part_type.name)
        else:
            report.not_tested += 1
    repair = await repairs_service.get_active_repair(session, phone)
    report.repair_id = repair.internal_id if repair else None
    return report
