"""Visual condition (frame / back glass grades A* A B C D), colour matching, battery health.

Rules under test:
  * the frame and the back glass are graded separately, so a damaged one is never confused with the other
  * D must be replaced before sale (fault + required repair line, so a missing part "needs ordering")
  * C is sellable as is: a matching part IN STOCK is marked for replacement, but NOTHING is ever ordered
  * frame and back glass must match each other (not necessarily the phone's original colour)
  * battery health goes on the listing, or is explicitly 'not applicable'
"""
from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models.enums import FaultStatus, PartSourceType, RepairPartLineStatus, TestOutcome
from app.models.inventory_models import Part, PhoneFault
from app.services import config_service, cosmetics, listing_builder, orders as orders_service
from app.services import parts as parts_service, phone_tickets, repairs as repairs_service, testing as testing_service
from tests.conftest import model_id
from tests.test_tasks import _buy, _pass_everything, _ticket

pytestmark = pytest.mark.asyncio

FRAME, GLASS = cosmetics.FRAME, cosmetics.GLASS


async def _part(session, actor_id, part_type, colour, grade=None, source=PartSourceType.EXISTING_STOCK, model="iPhone 13"):
    p = await parts_service.create_part(session, parts_service.CreatePartInput(
        part_type_name=part_type, source_type=source, cost=Decimal("20"), grade_code=grade, colour=colour,
        compatible_model_ids=[await model_id(session, model)]), actor_id)
    return p


async def _frame(session, actor_id, colour, **kw):
    return await _part(session, actor_id, "Housing", colour, **kw)


async def _glass(session, actor_id, colour, **kw):
    return await _part(session, actor_id, "Back Glass", colour, **kw)


async def _supply(session, phone):
    stats = await phone_tickets.sync_phone_tickets(session)
    repair = await repairs_service.get_active_repair(session, phone)
    return {rp.required_part_type.name: stats.ctx.supply.get(rp.id, []) for rp in repair.parts
            if rp.status in (RepairPartLineStatus.REQUIRED, RepairPartLineStatus.RESERVED)}


# ------------------------------------------------------------------ pure helpers
async def test_grade_and_colour_helpers():
    assert cosmetics.normalise_grade("a*") == "A*" and cosmetics.normalise_grade(" grade b ") == "B"
    assert cosmetics.normalise_grade("E") is None
    assert cosmetics.part_role("Housing") == FRAME and cosmetics.part_role("Back Glass") == GLASS
    assert cosmetics.part_role("Screen") is None
    assert cosmetics.colours_match("Space Grey", "space-gray") and not cosmetics.colours_match("Blue", "Black")
    assert not cosmetics.colours_match(None, None)
    assert cosmetics.worst_grade("A*", "C", "B") == "C"


# ------------------------------------------------------------------ D: replace before sale
async def test_frame_graded_d_raises_a_frame_fault_and_leaves_the_back_glass_alone(session, actor_id):
    phone = await _buy(session, actor_id)
    out = await cosmetics.set_grade(session, phone, FRAME, "D", actor_id)
    assert out.fault_raised and out.repair_id
    assert phone.current_status == "REPAIR_REQUIRED"
    repair = await repairs_service.get_active_repair(session, phone)
    assert [rp.required_part_type.name for rp in repair.parts] == ["Housing"]
    assert "Frame graded D" in (await session.execute(select(PhoneFault))).scalars().one().description
    assert phone.frame_grade == "D" and phone.back_glass_grade is None


async def test_back_glass_graded_d_needs_a_back_glass_not_a_frame(session, actor_id):
    phone = await _buy(session, actor_id)
    await cosmetics.set_grade(session, phone, GLASS, "D", actor_id)
    repair = await repairs_service.get_active_repair(session, phone)
    assert [rp.required_part_type.name for rp in repair.parts] == ["Back Glass"]


async def test_d_with_nothing_in_stock_says_needs_ordering_in_the_phones_colour(session, actor_id):
    phone = await _buy(session, actor_id)     # Blue
    await cosmetics.set_grade(session, phone, FRAME, "D", actor_id)
    t = await _ticket(session, phone)
    assert t.stage == "REPAIR" and "needs ordering" in t.description and "Blue" in t.description


async def test_regrading_a_d_up_clears_the_fault_and_the_repair(session, actor_id):
    phone = await _buy(session, actor_id)
    await cosmetics.set_grade(session, phone, FRAME, "D", actor_id)
    out = await cosmetics.set_grade(session, phone, FRAME, "B", actor_id)
    assert out.fault_cleared
    assert (await session.execute(select(PhoneFault))).scalars().one().status == FaultStatus.RESOLVED
    assert await repairs_service.get_active_repair(session, phone) is None
    assert phone.current_status == "TESTED"


async def test_a_d_listing_is_blocked_until_replaced(session, actor_id):
    phone = await _buy(session, actor_id)
    await cosmetics.set_grade(session, phone, GLASS, "D", actor_id)
    from app.models.commerce_models import Listing
    draft = Listing(title="x", price=Decimal("100"), aspects={k: ["x"] for k in ("Brand", "Model", "Storage Capacity", "Colour")})
    phone.photo_urls = ["https://img/1.jpg"]
    assert any("back glass is graded D" in b for b in listing_builder.blockers(draft, phone))


# ------------------------------------------------------------------ C: replace only from stock, never order
async def test_c_with_a_matching_part_in_stock_marks_it_for_replacement(session, actor_id):
    frame = await _frame(session, actor_id, "Blue")
    phone = await _buy(session, actor_id)
    out = await cosmetics.set_grade(session, phone, FRAME, "C", actor_id)
    assert out.reserved and out.reserved.internal_id == frame.internal_id and frame.status == "RESERVED"
    assert not out.fault_raised, "a C isn't a fault: it's sellable as is"
    t = await _ticket(session, phone)
    assert t.stage == "REPAIR" and "Swap now" in t.description and frame.internal_id in t.description


async def test_c_with_nothing_in_stock_is_sold_as_c_and_nothing_is_ordered(session, actor_id):
    phone = await _buy(session, actor_id)
    await _pass_everything(session, phone, actor_id, condition=False)
    await cosmetics.set_grade(session, phone, GLASS, "A", actor_id)
    await cosmetics.set_battery(session, phone, actor_id, percent=88)
    out = await cosmetics.set_grade(session, phone, FRAME, "C", actor_id)
    assert out.no_stock and not out.fault_raised and not out.reserved
    assert await repairs_service.get_active_repair(session, phone) is None
    assert await orders_service.parts_needed(session) == [], "nothing to order for a C"
    await testing_service.finish_walkthrough(session, phone, actor_id)
    assert phone.current_status == "TESTED" and phone.cosmetic_condition == "Grade C"
    assert (await _ticket(session, phone)).stage == "PREP"


async def test_c_ignores_stock_in_the_wrong_colour(session, actor_id):
    await _frame(session, actor_id, "Black")    # phone is Blue and the back glass stays on it
    phone = await _buy(session, actor_id)
    out = await cosmetics.set_grade(session, phone, FRAME, "C", actor_id)
    assert out.no_stock and not out.reserved


async def test_c_ignores_a_part_that_is_no_better_than_what_is_there(session, actor_id):
    await _frame(session, actor_id, "Blue", grade="C")
    phone = await _buy(session, actor_id)
    assert (await cosmetics.set_grade(session, phone, FRAME, "C", actor_id)).no_stock


async def test_c_prefers_the_best_graded_part_in_stock(session, actor_id):
    await _frame(session, actor_id, "Blue", grade="B")
    best = await _frame(session, actor_id, "Blue", grade="A*")
    phone = await _buy(session, actor_id)
    assert (await cosmetics.set_grade(session, phone, FRAME, "C", actor_id)).reserved.id == best.id


async def test_regrading_c_up_releases_the_reserved_part(session, actor_id):
    frame = await _frame(session, actor_id, "Blue")
    phone = await _buy(session, actor_id)
    await cosmetics.set_grade(session, phone, FRAME, "C", actor_id)
    out = await cosmetics.set_grade(session, phone, FRAME, "B", actor_id)
    assert out.released and frame.status == "AVAILABLE"
    assert await repairs_service.get_active_repair(session, phone) is None


async def test_condition_keep_sells_a_c_as_is_but_refuses_for_a_d(session, actor_id):
    frame = await _frame(session, actor_id, "Blue")
    phone = await _buy(session, actor_id)
    await cosmetics.set_grade(session, phone, FRAME, "C", actor_id)
    freed = await cosmetics.waive_replacement(session, phone, FRAME, actor_id)
    assert freed.id == frame.id and frame.status == "AVAILABLE" and phone.frame_grade == "C"
    await cosmetics.set_grade(session, phone, GLASS, "D", actor_id)
    with pytest.raises(cosmetics.CosmeticError, match="graded D"):
        await cosmetics.waive_replacement(session, phone, GLASS, actor_id)


# ------------------------------------------------------------------ colour: frame and back glass match each other
async def test_replacing_both_uses_a_matching_pair_even_in_a_new_colour(session, actor_id):
    await _frame(session, actor_id, "Blue")        # lone Blue frame
    f2 = await _frame(session, actor_id, "Black")
    g2 = await _glass(session, actor_id, "Black")  # the only complete pair is Black
    phone = await _buy(session, actor_id)          # Blue phone
    await cosmetics.set_grade(session, phone, FRAME, "D", actor_id)
    await cosmetics.set_grade(session, phone, GLASS, "D", actor_id)
    supply = await _supply(session, phone)
    assert supply["Housing"][0].part.id == f2.id and supply["Back Glass"][0].part.id == g2.id


async def test_fitting_a_new_colour_pair_changes_the_phones_colour_and_grades(session, actor_id):
    f = await _frame(session, actor_id, "Black", grade="A")
    g = await _glass(session, actor_id, "Black", source=PartSourceType.PURCHASED)   # bought in new -> A*
    phone = await _buy(session, actor_id)
    await cosmetics.set_grade(session, phone, FRAME, "D", actor_id)
    await cosmetics.set_grade(session, phone, GLASS, "D", actor_id)
    repair = await repairs_service.get_active_repair(session, phone)
    for part in (f, g):
        rp = next(r for r in repair.parts if r.required_part_type_id == part.part_type_id)
        await parts_service.reserve_part(session, part, repair, rp, actor_id)
        await parts_service.install_part(session, part, phone, rp, actor_id)
    assert phone.colour == "Black"
    assert (phone.frame_grade, phone.back_glass_grade) == ("A", "A*")
    assert phone.cosmetic_condition == "Grade A"


async def test_replacing_one_must_match_the_one_that_stays(session, actor_id):
    black = await _frame(session, actor_id, "Black")
    phone = await _buy(session, actor_id)           # Blue, back glass stays
    await cosmetics.set_grade(session, phone, FRAME, "D", actor_id)
    supply = await _supply(session, phone)
    assert supply["Housing"][0].kind == "TO_ORDER" and supply["Housing"][0].colour == "Blue"
    repair = await repairs_service.get_active_repair(session, phone)
    with pytest.raises(parts_service.PartFitError, match="have to match"):
        await parts_service.reserve_part(session, black, repair, repair.parts[0], actor_id)
    blue = await _frame(session, actor_id, "midnight blue")
    with pytest.raises(parts_service.PartFitError):
        await parts_service.reserve_part(session, blue, repair, repair.parts[0], actor_id)
    ok = await _frame(session, actor_id, "BLUE")
    await parts_service.reserve_part(session, ok, repair, repair.parts[0], actor_id)


async def test_a_second_part_must_match_the_first_one_reserved(session, actor_id):
    f = await _frame(session, actor_id, "Black")
    g = await _glass(session, actor_id, "Blue")
    phone = await _buy(session, actor_id)
    await cosmetics.set_grade(session, phone, FRAME, "D", actor_id)
    await cosmetics.set_grade(session, phone, GLASS, "D", actor_id)
    repair = await repairs_service.get_active_repair(session, phone)
    rp = {r.required_part_type.name: r for r in repair.parts}
    await parts_service.reserve_part(session, f, repair, rp["Housing"], actor_id)
    with pytest.raises(parts_service.PartFitError, match="Black"):
        await parts_service.reserve_part(session, g, repair, rp["Back Glass"], actor_id)


async def test_a_frame_with_no_colour_is_never_offered(session, actor_id):
    await _frame(session, actor_id, None)
    phone = await _buy(session, actor_id)
    await cosmetics.set_grade(session, phone, FRAME, "D", actor_id)
    assert (await _supply(session, phone))["Housing"][0].kind == "TO_ORDER"
    part = (await session.execute(select(Part))).scalars().one()
    await parts_service.set_part_colour(session, part, "Blue", actor_id)
    assert (await _supply(session, phone))["Housing"][0].kind == "IN_STOCK"


async def test_an_order_in_the_wrong_colour_does_not_cover_the_need(session, actor_id):
    phone = await _buy(session, actor_id)           # Blue
    await cosmetics.set_grade(session, phone, FRAME, "D", actor_id)
    pt = await config_service.get_or_create_part_type(session, "Housing")
    mid = await model_id(session, "iPhone 13")

    async def order(colour):
        o = await orders_service.create_order(session, "S", [orders_service.OrderLineInput(
            part_type_id=pt.id, quantity=1, unit_cost=Decimal("20"), phone_model_id=mid, colour=colour)], actor_id)
        await orders_service.mark_ordered(session, o, actor_id)
        return o

    await order("Black")
    assert (await _supply(session, phone))["Housing"][0].kind == "TO_ORDER"
    await order("Blue")
    assert (await _supply(session, phone))["Housing"][0].kind == "ON_ORDER"


async def test_received_parts_carry_the_ordered_colour(session, actor_id):
    pt = await config_service.get_or_create_part_type(session, "Back Glass")
    o = await orders_service.create_order(session, "S", [orders_service.OrderLineInput(
        part_type_id=pt.id, quantity=2, unit_cost=Decimal("9"), phone_model_id=await model_id(session, "iPhone 13"),
        colour="Starlight")], actor_id)
    created = await orders_service.receive_order(session, await orders_service.get_order(session, o.internal_id), actor_id)
    assert {p.colour for p in created} == {"Starlight"}


# ------------------------------------------------------------------ battery health + testing gate + listing
async def test_battery_health_percent_or_not_applicable(session, actor_id):
    phone = await _buy(session, actor_id)
    with pytest.raises(cosmetics.CosmeticError):
        await cosmetics.set_battery(session, phone, actor_id, percent=140)
    await cosmetics.set_battery(session, phone, actor_id, percent=87)
    assert phone.battery_health == 87 and not phone.battery_health_na
    await cosmetics.set_battery(session, phone, actor_id, not_applicable=True)
    assert phone.battery_health is None and phone.battery_health_na


async def test_testing_is_not_finished_until_condition_is_recorded(session, actor_id):
    phone = await _buy(session, actor_id)
    await _pass_everything(session, phone, actor_id, condition=False)
    report = await testing_service.finish_walkthrough(session, phone, actor_id)
    assert phone.current_status != "TESTED"
    assert report.condition_missing == ["frame grade", "back glass grade", "battery health"]
    t = await _ticket(session, phone)
    assert t.stage == "TEST" and "frame grade" in t.description
    await cosmetics.set_grade(session, phone, FRAME, "A*", actor_id)
    await cosmetics.set_grade(session, phone, GLASS, "B", actor_id)
    await cosmetics.set_battery(session, phone, actor_id, not_applicable=True)   # N/A counts as recorded
    await testing_service.finish_walkthrough(session, phone, actor_id)
    assert phone.current_status == "TESTED" and (await _ticket(session, phone)).stage == "PREP"


async def test_listing_shows_frame_and_back_glass_grades_and_battery(session, actor_id):
    phone = await _buy(session, actor_id)
    await cosmetics.set_grade(session, phone, FRAME, "A", actor_id)
    await cosmetics.set_grade(session, phone, GLASS, "C", actor_id)
    await cosmetics.set_battery(session, phone, actor_id, percent=91)
    built = await listing_builder.build_for_phone(session, phone)
    assert "Cosmetic grade: C" in built.description
    assert "Frame: grade A" in built.description and "Back glass: grade C" in built.description
    assert "Battery health: 91%" in built.description and "Battery 91%" in built.title
    assert "frame A, back glass C" in built.condition_description
    assert built.condition_text == "Grade C"
    assert not any("battery" in w.lower() or "graded" in w.lower() for w in built.warnings)


async def test_listing_with_battery_not_applicable_says_so_and_does_not_nag(session, actor_id):
    phone = await _buy(session, actor_id)
    await cosmetics.set_battery(session, phone, actor_id, not_applicable=True)
    built = await listing_builder.build_for_phone(session, phone)
    assert "Battery health: not shown by this phone" in built.description
    assert not any("battery" in w.lower() for w in built.warnings)
    assert any("Frame hasn't been graded" in w for w in built.warnings)
