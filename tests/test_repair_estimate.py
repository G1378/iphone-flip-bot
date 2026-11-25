"""Repair costing for the buy page: read the listing, name the parts, price them from YOUR history first."""
from __future__ import annotations

from decimal import Decimal

import pytest

from app.models.enums import PartSourceType, TestOutcome
from app.services import config_service, parts as parts_service, repair_estimate as re_

FLAT = Decimal("60.00")


# ------------------------------------------------------------------ reading the listing (no DB)
@pytest.mark.parametrize("text,parts,board", [
    ("iPhone 13 128GB cracked screen for repair", ["Screen"], None),
    ("iPhone 13 cracked", ["Screen"], None),                                   # bare "cracked" = the screen
    ("iPhone 12 smashed back glass, screen perfect", ["Back Glass"], None),    # a comma ends the thought
    ("cracked back, screen is flawless", ["Back Glass"], None),
    ("iPhone 12 cracked screen and back", ["Screen", "Back Glass"], None),
    ("rear camera works, cracked screen", ["Screen"], None),
    ("rear camera cracked lens", ["Rear Camera"], None),
    ("front camera not working", ["Front Camera"], None),
    ("iPhone 13 cracked, battery health 70%", ["Screen", "Battery"], None),
    ("iPhone 11 battery health 74%", ["Battery"], None),
    ("iPhone 11 battery health 88% great condition", [], None),
    ("iPhone 11 battery needs replacing", ["Battery"], None),
    ("iPhone 13 not charging", ["Charging Port / Flex"], None),
    ("speaker crackling no sound", ["Speaker"], None),
    ("screen: cracked", ["Screen"], None),
    ("Sold as seen, screen has lines", ["Screen"], None),
    ("small dead pixels on screen", ["Screen"], None),
    ("Hairline crack on the screen", ["Screen"], None),
    ("cracked screen with protector on, charging port faulty", ["Screen", "Charging Port / Flex"], None),
    # the seller saying it is FINE must not count as a fault
    ("Good condition, no cracks, no scratches, fully working", [], None),
    ("Never been repaired, no damage, battery health 91%", [], None),
    ("no visible cracks", [], None),
    ("no signs of damage", [], None),
    ("not faulty, screen not cracked", [], None),
    ("battery health 100% no issues", [], None),
    # ...but "no charger" is not a denial of the crack that follows it
    ("no charger cracked screen", ["Screen"], None),
    ("no charger, cracked screen", ["Screen"], None),
    # a damaged protector or case is not a damaged phone
    ("Screen protector cracked but phone is perfect", [], None),
    ("cracked screen protector, phone perfect", [], None),
    ("phone case cracked", [], None),
    # nothing named
    ("faulty spares or repair", [], None),
    ("iPhone 13 128gb blue unlocked excellent condition", [], None),
    # not a priced repair
    ("phone won't turn on", [], "won't power on"),
    ("No power, stuck on apple logo", [], "won't power on"),
    ("iPhone 13 water damaged spares or repair", [], "water damage"),
    ("iPhone 13 face id not working", [], "Face ID / Touch ID fault"),
    ("iPhone 13 no face id", [], "Face ID / Touch ID fault"),
    ("Face ID works perfectly", [], None),
    ("iPhone dead", [], "dead"),
])
def test_scan_faults(text, parts, board):
    r = re_.scan_faults(text)
    assert sorted(r.parts) == sorted(parts)
    assert r.board_reason == board


def test_battery_health_is_read():
    assert re_.scan_faults("iPhone 11 battery health 74%").battery_health == 74
    assert re_.scan_faults("battery capacity: 91%").battery_health == 91


def test_part_cost_setting_parses_and_ignores_typos():
    assert re_.parse_part_costs("screen=70, battery = £25, nonsense, back glass=abc") == {
        "screen": Decimal("70"), "battery": Decimal("25")}
    assert re_.parse_part_costs("") == {}


# ------------------------------------------------------------------ pricing it
async def _model(session, name="iPhone 13"):
    return await config_service.resolve_phone_model(session, name)


async def _own_part(session, actor_id, part_type, cost, model, source=PartSourceType.PURCHASED):
    await parts_service.create_part(session, parts_service.CreatePartInput(
        part_type_name=part_type, source_type=source, cost=Decimal(cost), testing_status=TestOutcome.PASS,
        compatible_model_ids=[model.id]), actor_id)


@pytest.mark.asyncio
async def test_with_no_history_it_uses_the_defaults_plus_contingency(session):
    est = await re_.estimate_repair(session, await _model(session), "iPhone 13 cracked screen", FLAT)
    assert est.parts == ["Screen"] and not est.is_flat_assumption
    assert est.cost == Decimal("60.50")                       # £55 default + 10% contingency
    assert "screen £55 (default)" in est.basis and "10% contingency" in est.basis


@pytest.mark.asyncio
async def test_your_own_purchase_history_beats_the_defaults(session, actor_id):
    pm = await _model(session)
    await _own_part(session, actor_id, "Screen", "40.00", pm)
    await _own_part(session, actor_id, "Screen", "50.00", pm)
    est = await re_.estimate_repair(session, pm, "iPhone 13 cracked screen", FLAT)
    assert est.cost == Decimal("49.50")                       # avg £45 + 10%
    assert "your avg of 2 buys" in est.basis


@pytest.mark.asyncio
async def test_history_for_a_different_model_does_not_leak_in(session, actor_id):
    pro = await _model(session, "iPhone 13 Pro")
    await _own_part(session, actor_id, "Screen", "120.00", pro)         # a Pro screen is not a base screen
    est = await re_.estimate_repair(session, await _model(session), "cracked screen", FLAT)
    assert "default" in est.basis and est.cost == Decimal("60.50")


@pytest.mark.asyncio
async def test_donor_parts_do_not_count_as_what_a_replacement_costs(session, actor_id):
    pm = await _model(session)
    await _own_part(session, actor_id, "Screen", "3.00", pm, source=PartSourceType.DONOR)   # an allocation, not a price
    est = await re_.estimate_repair(session, pm, "cracked screen", FLAT)
    assert "default" in est.basis


@pytest.mark.asyncio
async def test_several_faults_add_up(session):
    est = await re_.estimate_repair(session, await _model(session), "cracked screen, battery health 70%", FLAT)
    assert est.parts == ["Screen", "Battery"]
    assert est.cost == Decimal("82.50")                       # (55 + 20) * 1.1


@pytest.mark.asyncio
async def test_your_settings_override_the_built_ins(session):
    await config_service.set_setting(session, "repair_part_cost_defaults", "screen=70", value_type="string")
    await config_service.set_setting(session, "repair_contingency_pct", "0", value_type="number")
    await config_service.set_setting(session, "repair_labour_gbp", "15", value_type="number")
    est = await re_.estimate_repair(session, await _model(session), "cracked screen", FLAT)
    assert est.cost == Decimal("85.00")                       # 70 + no contingency + 15 labour
    assert "£15 labour" in est.basis


@pytest.mark.asyncio
async def test_a_listing_that_names_no_fault_falls_back_to_the_flat_assumption(session):
    est = await re_.estimate_repair(session, await _model(session), "iPhone 13 spares or repair", FLAT)
    assert est.is_flat_assumption and est.cost == FLAT and "flat" in est.basis


@pytest.mark.asyncio
async def test_a_board_level_fault_is_flagged_so_the_caller_can_skip_it(session):
    est = await re_.estimate_repair(session, await _model(session), "iPhone 13 water damaged", FLAT)
    assert est.board_reason == "water damage" and "can't be priced" in est.basis
