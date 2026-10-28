"""Job channels default to the parts you already have.

A part that is on the shelf must be offered to reserve and fit, never hidden behind "Order". Two things used
to hide it: the part type typed into the fault form ('battery' made a second, empty type) and frames / back
glass whose colour hadn't been recorded."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select

from app.bot.cogs import jobs as jobs_cog
from app.models.config_models import PartTypeConfig
from app.models.enums import PartSourceType
from app.models.inventory_models import Part
from app.services import config_service, job_actions, parts as parts_service, phone_tickets
from tests.test_jobs_discord import (_act_ref, _in_channel, _job_channel, _press, _press_start, interaction, world)  # noqa: F401
from tests.test_tasks import _buy, _order, _stock
from tests.conftest import model_id

pytestmark = pytest.mark.asyncio


async def _types(session):
    return sorted(t.name for t in (await session.execute(select(PartTypeConfig))).scalars().all())


async def _buttons(session, phone):
    from app.services import job_panels
    stats = await phone_tickets.sync_phone_tickets(session)
    return [a.label for a in job_panels.actions_for(stats.situations[phone.id], stats.ctx)]


# ------------------------------------------------------------------ part type matching
@pytest.mark.parametrize("typed,expected", [
    ("Battery", "Battery"), ("battery", "Battery"), ("  BATTERY ", "Battery"), ("back-glass", "Back Glass"),
    ("display", "Screen"), ("LCD", "Screen"), ("rear glass", "Back Glass"), ("frame", "Housing"),
    ("charging port", "Charging Port / Flex"), ("sim", "SIM Tray"),
])
async def test_typed_part_names_find_the_configured_type(session, typed, expected):
    assert (await config_service.find_part_type(session, typed)).name == expected


async def test_unknown_names_find_nothing_and_get_or_create_never_duplicates(session):
    assert await config_service.find_part_type(session, "flux capacitor") is None
    assert await config_service.find_part_type(session, "   ") is None
    before = await _types(session)
    for typed in ("battery", "BATTERY", "Display", "back glass"):
        await config_service.get_or_create_part_type(session, typed)
    assert await _types(session) == before, "a different spelling must not spawn a new, empty part type"
    assert (await config_service.get_or_create_part_type(session, "Wireless Coil")).name == "Wireless Coil"


async def test_an_alias_only_applies_when_the_configured_type_exists(session):
    screen = await config_service.find_part_type(session, "Screen")
    screen.name = "Display Assembly"
    await session.flush()
    assert (await config_service.find_part_type(session, "lcd")) is None, "'Screen' is gone, so 'lcd' means nothing"
    assert (await config_service.find_part_type(session, "display assembly")).id == screen.id


# ------------------------------------------------------------------ logging a fault finds the stock
@pytest.mark.parametrize("typed", ["Battery", "battery", "BATTERY"])
async def test_a_fault_for_a_part_in_stock_offers_reserve_not_order(session, actor_id, typed):
    stock = await _stock(session, actor_id, "Battery")
    phone = await _buy(session, actor_id)
    msg = await job_actions.log_fault(session, phone.id, actor_id, description="Drains fast", part_type_name=typed)
    assert stock.internal_id in msg and "Reserve" in msg
    buttons = await _buttons(session, phone)
    assert any("Reserve Battery" in b for b in buttons) and not any("Order" in b for b in buttons)


async def test_display_means_screen(session, actor_id):
    stock = await _stock(session, actor_id, "Screen")
    phone = await _buy(session, actor_id)
    await job_actions.log_fault(session, phone.id, actor_id, description="Cracked", part_type_name="Display")
    assert any(stock.internal_id in b for b in await _buttons(session, phone))


async def test_a_mistyped_part_is_refused_and_creates_nothing(session, actor_id):
    phone = await _buy(session, actor_id)
    before = await _types(session)
    with pytest.raises(job_actions.JobActionError, match="no part type called 'batery'.*Battery"):
        await job_actions.log_fault(session, phone.id, actor_id, description="x", part_type_name="batery")
    assert await _types(session) == before and not phone.faults


async def test_a_fault_with_no_part_still_works(session, actor_id):
    phone = await _buy(session, actor_id)
    assert (await job_actions.log_fault(session, phone.id, actor_id, description="Odd noise")).startswith("Fault logged")


async def test_part_choices_show_what_is_on_the_shelf_and_on_the_way(session, actor_id):
    await _stock(session, actor_id, "Battery")
    await _stock(session, actor_id, "Battery")
    await _stock(session, actor_id, "Screen", model="iPhone 14")      # wrong model: doesn't count for a 13
    await _order(session, actor_id, "Back Glass")
    phone = await _buy(session, actor_id)
    got = {c.name: c for c in await job_actions.part_choices(session, phone.id)}
    assert got["Battery"].in_stock == 2 and got["Battery"].on_order == 0
    assert got["Screen"].in_stock == 0
    assert got["Back Glass"].on_order == 1


# ------------------------------------------------------------------ colourless frames / back glass
async def _colourless(session, actor_id, type_name="Housing"):
    return await parts_service.create_part(session, parts_service.CreatePartInput(
        part_type_name=type_name, source_type=PartSourceType.EXISTING_STOCK, cost=10,
        compatible_model_ids=[await model_id(session, "iPhone 13")]), actor_id)


async def test_a_colourless_frame_is_offered_and_asks_for_its_colour(session, actor_id):
    part = await _colourless(session, actor_id)
    phone = await _buy(session, actor_id)                       # Blue
    await job_actions.log_fault(session, phone.id, actor_id, description="Bent", part_type_name="Housing")
    labels = await _buttons(session, phone)
    reserve = next(b for b in labels if "Reserve" in b)
    assert part.internal_id in reserve and "colour?" in reserve and not any("Order" in b for b in labels)

    from app.services import repairs as repairs_service
    rp = (await repairs_service.get_active_repair(session, phone)).parts[0]
    with pytest.raises(job_actions.PartColourNeeded) as ask:
        await job_actions.reserve(session, phone.id, rp.id, actor_id)
    assert ask.value.match_colour == "Blue" and "Midnight" in ask.value.choices and part.status == "AVAILABLE"

    await job_actions.reserve(session, phone.id, rp.id, actor_id, part_id=ask.value.part_id, part_colour="Blue")
    assert (part.status, part.colour) == ("RESERVED", "Blue")


async def test_a_colour_that_clashes_is_still_refused_when_reserving(session, actor_id):
    part = await _colourless(session, actor_id)
    phone = await _buy(session, actor_id)                       # Blue
    await job_actions.log_fault(session, phone.id, actor_id, description="Bent", part_type_name="Housing")
    from app.services import repairs as repairs_service
    rp = (await repairs_service.get_active_repair(session, phone)).parts[0]
    with pytest.raises(job_actions.JobActionError, match="have to match"):
        await job_actions.reserve(session, phone.id, rp.id, actor_id, part_id=part.id, part_colour="Black")
    assert part.status == "AVAILABLE"


# ------------------------------------------------------------------ say why when stock can't be used
async def test_the_ticket_says_why_stock_of_that_type_cant_be_used(session, actor_id):
    await _stock(session, actor_id, "Battery", model="iPhone 14")
    phone = await _buy(session, actor_id)
    await job_actions.log_fault(session, phone.id, actor_id, description="Drains", part_type_name="Battery")
    from tests.test_tasks import _ticket
    t = await _ticket(session, phone)
    assert "needs ordering" in t.description and "Battery in stock but not usable here: 1 for other models" in t.description


async def test_untagged_stock_is_called_out_with_the_fix(session, actor_id):
    from sqlalchemy import delete
    from app.models.inventory_models import PartCompatibleModel
    part = await _stock(session, actor_id, "Battery")
    await session.execute(delete(PartCompatibleModel).where(PartCompatibleModel.part_id == part.id))
    await session.flush()
    session.expire_all()
    phone = await _buy(session, actor_id)
    await job_actions.log_fault(session, phone.id, actor_id, description="Drains", part_type_name="Battery")
    from tests.test_tasks import _ticket
    t = await _ticket(session, phone)
    assert "1 with no model set (`/part-models`)" in t.description


# ------------------------------------------------------------------ the Discord flow
async def test_log_a_fault_button_shows_stock_first_then_asks_only_for_the_description(world):
    w = world
    await _stock(w.session, w.actor, "Battery")
    await _stock(w.session, w.actor, "Battery")
    phone = await _buy(w.session, w.actor)
    from app.services import phones as phones_service
    await phones_service.set_status(w.session, phone, "REPAIR_REQUIRED", w.actor)
    await w.session.commit()
    await w.tasks.engine_tick()
    await _press_start(w, phone.id)
    ch = await _job_channel(w, phone.id)

    inter = _in_channel(w, ch)
    await w.jobs.handle(inter, "fault", _act_ref(ch.panel().buttons(), "fault"))
    popup = inter.response.sent[0]
    assert popup.ephemeral and not inter.response.modals
    options = {o.label: o for o in popup.view.select.options}
    assert options["Battery"].description == "2 in stock: reserve and fit it" and options["Battery"].emoji.name == "✅"
    assert options["Screen"].description == "None in stock: would need ordering"
    ordered = [o.label for o in popup.view.select.options]
    assert ordered.index("Battery") < ordered.index("Screen"), "what is in stock is listed first"

    pick = _in_channel(w, ch)
    battery_id = next(k for k, name in popup.view.names.items() if name == "Battery")
    popup.view.select._values = [battery_id]          # what Discord fills in when the option is chosen
    await popup.view._picked(pick)
    form = pick.response.modals[0]
    assert isinstance(form, jobs_cog.FaultForm) and [c.label for c in form.children] == ["What's wrong?"]
    form.description._value = "Drains fast"
    await form.on_submit(_in_channel(w, ch))
    assert "is in stock, press **Reserve**" in ch.live[-2].content
    assert any(b.startswith("job:reserve:") for b in ch.panel().buttons())
    assert not any(b.startswith("job:order:") for b in ch.panel().buttons())


async def test_reserving_a_colourless_frame_in_the_channel_asks_the_colour_then_reserves(world):
    w = world
    await _colourless(w.session, w.actor)
    phone = await _buy(w.session, w.actor)
    from app.services import phones as phones_service
    await phones_service.set_status(w.session, phone, "REPAIR_REQUIRED", w.actor)
    await w.session.commit()
    async with w.scope() as s:
        await job_actions.log_fault(s, phone.id, w.actor, description="Bent frame", part_type_name="Housing")
    await w.tasks.engine_tick()
    await _press_start(w, phone.id)
    ch = await _job_channel(w, phone.id)

    inter = _in_channel(w, ch)
    captured = {}

    async def capture(content=None, embed=None, ephemeral=False, view=None, **_):
        captured.update(embed=embed, ephemeral=ephemeral, view=view)

    inter.followup.send = capture
    await w.jobs.handle(inter, "reserve", _act_ref(ch.panel().buttons(), "reserve"))
    assert "What colour is" in captured["embed"].title and captured["ephemeral"]
    async with w.scope() as s:
        assert (await s.execute(select(Part))).scalar_one().status == "AVAILABLE", "nothing reserved until the colour is known"

    view = captured["view"]
    assert isinstance(view, jobs_cog.PartColourView)
    same = next(b for b in view.children if "matches the phone" in (getattr(b, "label", "") or ""))
    assert "Blue" in same.label                       # the phone is Blue: one tap to say the part is too
    await same.callback(_in_channel(w, ch))

    async with w.scope() as s:
        part = (await s.execute(select(Part))).scalar_one()
    assert (part.status, part.colour) == ("RESERVED", "Blue")
    assert "reserved" in ch.live[-2].content.lower()
    assert any(b.startswith("job:fit:") for b in ch.panel().buttons())


async def test_the_colour_picker_offers_the_models_own_colours(world):
    need = job_actions.PartColourNeeded(1, 2, "Housing `PT-000001`", None, ["Midnight", "Starlight", "Blue"])
    view = jobs_cog.PartColourView(world.jobs, 7, 1, need)
    selects = [c for c in view.children if hasattr(c, "options")]
    assert [o.value for o in selects[0].options] == ["Midnight", "Starlight", "Blue"]
    assert not any("matches the phone" in (getattr(c, "label", "") or "") for c in view.children), \
        "no quick-confirm when there's nothing to match against"
