"""Parts are model specific: a Pro part is not a base part, and 'Battery' alone means nothing.

Every test uses iPhone 13 (base) next to iPhone 13 Pro / Pro Max, the case that used to
be silently mixed up."""
from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.models import Base
from app.models.config_models import PhoneModelConfig
from app.models.enums import AcquisitionType, PartSourceType, TestOutcome
from app.models.inventory_models import Part
from app.services import config_service, donors, job_actions, maintenance, orders as orders_service
from app.services import parts as parts_service, phone_tickets, phones, pricing as pricing_service, repairs
from tests.conftest import model_id
from tests.test_job_panels import _line, _panel
from tests.test_tasks import _buy, _order, _record, _stock

pytestmark = pytest.mark.asyncio


async def _fail_display(session, phone, actor_id):
    await _record(session, phone, "display", TestOutcome.FAIL, actor_id)


async def _phone(session, actor_id, model):
    return await phones.buy_phone(session, phones.BuyPhoneInput(
        manufacturer="Apple", model=model, variant=None, storage="128GB", colour="Blue", imei=None,
        serial_number=None, carrier=None, purchase_price=Decimal("120"), purchase_date=dt.date(2026, 9, 1),
        seller_source="x", acquisition_type=AcquisitionType.RESALE, notes=None, location_code=None), actor_id)


# ------------------------------------------------------------------ catalog / buying
async def test_model_names_must_match_exactly(session):
    assert (await config_service.resolve_phone_model(session, "13 pro")).model == "iPhone 13 Pro"
    assert (await config_service.resolve_phone_model(session, "iphone 13")).model == "iPhone 13"
    assert (await config_service.resolve_phone_model(session, "iPhone 13 Pro Max")).model == "iPhone 13 Pro Max"
    with pytest.raises(config_service.UnknownPhoneModelError) as e:
        await config_service.resolve_phone_model(session, "iPhone 13 Prp")
    assert "iPhone 13 Pro" in str(e.value)
    with pytest.raises(config_service.UnknownPhoneModelError):
        await config_service.resolve_phone_model(session, "iPhone 99")


async def test_buying_an_unknown_model_is_refused_not_created(session, actor_id):
    before = await session.scalar(select(func.count()).select_from(PhoneModelConfig))
    with pytest.raises(config_service.UnknownPhoneModelError):
        await _phone(session, actor_id, "iPhone 13 pro maxx")
    assert await session.scalar(select(func.count()).select_from(PhoneModelConfig)) == before


async def test_a_bought_phone_is_linked_to_its_exact_catalog_model(session, actor_id):
    base = await _phone(session, actor_id, "iphone 13")
    pro = await _phone(session, actor_id, "13 Pro")
    assert (base.model, pro.model) == ("iPhone 13", "iPhone 13 Pro")
    assert base.phone_model_id == await model_id(session, "iPhone 13")
    assert pro.phone_model_id == await model_id(session, "iPhone 13 Pro")
    assert base.phone_model_id != pro.phone_model_id


# ------------------------------------------------------------------ parts must name a model
async def test_a_part_without_a_model_cannot_be_created(session, actor_id):
    with pytest.raises(parts_service.PartStateError, match="specific phone model"):
        await parts_service.create_part(session, parts_service.CreatePartInput(
            part_type_name="Battery", source_type=PartSourceType.EXISTING_STOCK, cost=Decimal("10")), actor_id)


async def test_an_order_line_without_a_model_is_refused(session, actor_id):
    pt = await config_service.get_or_create_part_type(session, "Battery")
    with pytest.raises(orders_service.OrderError, match="phone model"):
        await orders_service.create_order(
            session, "S", [orders_service.OrderLineInput(part_type_id=pt.id, quantity=1, unit_cost=Decimal("9"))], actor_id)


async def test_received_parts_are_tagged_with_the_ordered_model(session, actor_id):
    pt = await config_service.get_or_create_part_type(session, "Battery")
    order = await orders_service.create_order(session, "S", [orders_service.OrderLineInput(
        part_type_id=pt.id, quantity=2, unit_cost=Decimal("9"), phone_model_id=await model_id(session, "iPhone 13 Pro"))], actor_id)
    await orders_service.mark_ordered(session, order, actor_id)
    created = await orders_service.receive_order(session, await orders_service.get_order(session, order.internal_id), actor_id)
    assert [p.model_label() for p in created] == ["iPhone 13 Pro"] * 2
    assert created[0].display_name() == "Battery (iPhone 13 Pro)"


async def test_donor_teardown_tags_parts_with_the_donors_model(session, actor_id):
    from tests.test_allocation import _teardown_standard
    donor = await donors.buy_donor(session, donors.BuyDonorInput(
        manufacturer="Apple", model="iPhone 13 Pro Max", variant=None, storage="128GB", colour="Gold",
        purchase_price=Decimal("100"), purchase_date=dt.date(2026, 9, 1), seller_source="x",
        fault_description="dead", notes=None, location_code=None), actor_id)
    assert donor.phone_model_id == await model_id(session, "iPhone 13 Pro Max")
    created = await _teardown_standard(session, donor, actor_id)
    assert created and all(p.model_label() == "iPhone 13 Pro Max" for p in created)


# ------------------------------------------------------------------ hard block
async def test_wrong_model_part_is_never_offered_reserved_or_fitted(session, actor_id):
    base = await _phone(session, actor_id, "iPhone 13")
    pro_screen = await _stock(session, actor_id, "Screen", model="iPhone 13 Pro")
    await phones.add_fault(session, base, "Cracked", actor_id,
                           required_part_type_id=(await config_service.get_or_create_part_type(session, "Screen")).id)
    repair = await repairs.create_repair_for_open_faults(session, base, actor_id)
    rp = repair.parts[0]

    assert await parts_service.find_compatible_available_parts(session, rp.required_part_type_id, base.phone_model_id) == []
    with pytest.raises(parts_service.PartFitError, match="iPhone 13 Pro"):
        await parts_service.reserve_part(session, pro_screen, repair, rp, actor_id)
    with pytest.raises(parts_service.PartFitError):
        await parts_service.install_part(session, pro_screen, base, rp, actor_id)
    plan = await repairs.repair_plan(session, repair)
    assert plan[0].candidates == []


async def test_matching_model_part_is_offered(session, actor_id):
    base = await _phone(session, actor_id, "iPhone 13")
    ok = await _stock(session, actor_id, "Screen", model="iPhone 13")
    await _stock(session, actor_id, "Screen", model="iPhone 13 Pro")
    found = await parts_service.find_compatible_available_parts(
        session, ok.part_type_id, base.phone_model_id)
    assert [p.id for p in found] == [ok.id]


async def test_a_part_explicitly_tagged_for_two_models_fits_both(session, actor_id):
    shared = await parts_service.create_part(session, parts_service.CreatePartInput(
        part_type_name="Screen", source_type=PartSourceType.EXISTING_STOCK, cost=Decimal("30"),
        compatible_model_ids=[await model_id(session, "iPhone 12"), await model_id(session, "iPhone 12 Pro")]), actor_id)
    assert shared.fits_model(await model_id(session, "iPhone 12"))
    assert shared.fits_model(await model_id(session, "iPhone 12 Pro"))
    assert not shared.fits_model(await model_id(session, "iPhone 12 Pro Max"))


async def test_find_part_requires_an_exact_model(session, actor_id):
    await _stock(session, actor_id, "Screen", model="iPhone 13")
    await _stock(session, actor_id, "Screen", model="iPhone 13 Pro")
    base = await parts_service.find_part_by_type_and_model(session, "screen", "13")
    pro = await parts_service.find_part_by_type_and_model(session, "screen", "13 pro")
    assert [p.model_label() for p in base] == ["iPhone 13"]
    assert [p.model_label() for p in pro] == ["iPhone 13 Pro"]
    with pytest.raises(config_service.UnknownPhoneModelError):
        await parts_service.find_part_by_type_and_model(session, "screen", "")


# ------------------------------------------------------------------ tickets / job panel
async def test_ticket_says_to_order_when_only_the_pro_part_is_in_stock(session, actor_id):
    phone = await _buy(session, actor_id)                      # iPhone 13 (base)
    await _stock(session, actor_id, "Screen", model="iPhone 13 Pro")
    await _fail_display(session, phone, actor_id)
    sit = phone_tickets.assess(phone, await phone_tickets.load_context(session, await phone_tickets.load_phones(session)))
    kinds = [u.kind for rp in sit.outstanding for u in (await phone_tickets.load_context(
        session, await phone_tickets.load_phones(session))).supply.get(rp.id, [])]
    assert kinds == ["TO_ORDER"]


async def test_a_pro_order_does_not_cover_a_base_phone(session, actor_id):
    phone = await _buy(session, actor_id)
    await _order(session, actor_id, "Screen", model="iPhone 13 Pro")
    await _fail_display(session, phone, actor_id)
    ctx = await phone_tickets.load_context(session, await phone_tickets.load_phones(session))
    sit = phone_tickets.assess(phone, ctx)
    assert [ctx.supply[rp.id][0].kind for rp in sit.outstanding] == ["TO_ORDER"]


async def test_the_right_model_in_stock_and_the_wrong_one_two_phones(session, actor_id):
    base = await _buy(session, actor_id)
    pro = await _phone(session, actor_id, "iPhone 13 Pro")
    await _stock(session, actor_id, "Screen", model="iPhone 13 Pro")
    await _fail_display(session, base, actor_id)
    await _fail_display(session, pro, actor_id)
    ctx = await phone_tickets.load_context(session, await phone_tickets.load_phones(session))
    by_phone = {p.id: [ctx.supply[rp.id][0].kind for rp in phone_tickets.assess(p, ctx).outstanding] for p in (base, pro)}
    assert by_phone[base.id] == ["TO_ORDER"] and by_phone[pro.id] == ["IN_STOCK"]


async def test_ordering_from_a_job_channel_orders_the_phones_exact_model(session, actor_id):
    phone = await _phone(session, actor_id, "iPhone 13 Pro Max")
    await _fail_display(session, phone, actor_id)
    rp_id = _line(await _panel(session, phone), "order").ref
    msg = await job_actions.order_part(session, phone.id, rp_id, actor_id, supplier="X", unit_cost="40")
    assert "Screen (iPhone 13 Pro Max)" in msg
    line = (await session.execute(select(orders_service.OrderLine))).scalars().one()
    assert line.phone_model_id == await model_id(session, "iPhone 13 Pro Max")


# ------------------------------------------------------------------ parts needed
async def test_parts_needed_is_per_model(session, actor_id):
    base = await _buy(session, actor_id)
    pro = await _phone(session, actor_id, "iPhone 13 Pro")
    for p in (base, pro):
        screen = await config_service.get_or_create_part_type(session, "Screen")
        await phones.add_fault(session, p, "Cracked", actor_id, required_part_type_id=screen.id)
        await repairs.create_repair_for_open_faults(session, p, actor_id)
    await _stock(session, actor_id, "Screen", model="iPhone 13 Pro")
    needed = {n.label: (n.required_qty, n.in_stock_qty, n.to_order_qty) for n in await orders_service.parts_needed(session)}
    assert needed == {"Screen (iPhone 13)": (1, 0, 1), "Screen (iPhone 13 Pro)": (1, 1, 0)}


# ------------------------------------------------------------------ changing a phone's model
async def test_correcting_a_phones_model_rematches_and_is_blocked_while_parts_are_in_use(session, actor_id):
    phone = await _phone(session, actor_id, "iPhone 13")
    changed = await phones.change_phone_model(session, phone, "iPhone 13 Pro", actor_id)
    assert changed.model == "iPhone 13 Pro" and changed.phone_model_id == await model_id(session, "iPhone 13 Pro")

    screen = await _stock(session, actor_id, "Screen", model="iPhone 13 Pro")
    st = await config_service.get_or_create_part_type(session, "Screen")
    await phones.add_fault(session, phone, "Cracked", actor_id, required_part_type_id=st.id)
    repair = await repairs.create_repair_for_open_faults(session, phone, actor_id)
    await parts_service.reserve_part(session, screen, repair, repair.parts[0], actor_id)
    with pytest.raises(phones.PhoneModelChangeError):
        await phones.change_phone_model(session, phone, "iPhone 13", actor_id)
    with pytest.raises(ValueError):
        await phones.edit_phone(session, phone, actor_id, model="iPhone 13")


async def test_retagging_a_part_is_blocked_while_reserved(session, actor_id):
    part = await _stock(session, actor_id, "Screen", model="iPhone 13")
    await parts_service.set_part_models(session, part, [await model_id(session, "iPhone 13 Pro")], actor_id)
    assert part.model_label() == "iPhone 13 Pro"
    with pytest.raises(parts_service.PartStateError):
        await parts_service.set_part_models(session, part, [], actor_id)


# ------------------------------------------------------------------ pricing queries
async def test_ebay_queries_exclude_sibling_models(session):
    base = await config_service.resolve_phone_model(session, "iPhone 13")
    pro = await config_service.resolve_phone_model(session, "iPhone 13 Pro")
    pmax = await config_service.resolve_phone_model(session, "iPhone 13 Pro Max")
    q_base = pricing_service.build_search_query(base, "128GB")
    q_pro = pricing_service.build_search_query(pro, "128GB")
    q_max = pricing_service.build_search_query(pmax, "128GB")
    assert all(f"-{w}" in q_base for w in ("pro", "max", "plus", "mini"))
    assert "-pro" not in q_pro and "-max" in q_pro and "-mini" in q_pro
    assert "-pro" not in q_max and "-max" not in q_max and "-plus" in q_max


# ------------------------------------------------------------------ wipe
async def test_wipe_clears_business_data_and_keeps_access_and_config(session, actor_id):
    await config_service.add_authorized_user(session, 42, "me", 1, True)
    await _stock(session, actor_id, "Screen", model="iPhone 13")
    await _buy(session, actor_id)
    await session.commit()

    result = await maintenance.wipe_all_data(session)
    await session.commit()
    assert result.rows_deleted["phones"] == 1 and result.rows_deleted["parts"] == 1
    for t in ("phones", "parts", "inventory_events", "id_sequences", "tasks"):
        assert await session.scalar(select(func.count()).select_from(Base.metadata.tables[t])) == 0
    for t in ("authorized_users", "phone_models", "part_types", "test_definitions"):
        assert await session.scalar(select(func.count()).select_from(Base.metadata.tables[t])) > 0
    assert (await _buy(session, actor_id)).internal_id == "IP-000001"          # numbering restarts


async def test_wipe_with_settings_resets_catalogs_to_defaults(session, actor_id):
    await config_service.get_or_create_phone_model(session, "Apple", "iPhone 99 Test")
    await maintenance.wipe_all_data(session, include_config=True)
    await session.commit()
    names = {m.model for m in await config_service.list_phone_models(session)}
    assert "iPhone 99 Test" not in names and "iPhone 13 Pro" in names
    assert await session.scalar(select(func.count()).select_from(Base.metadata.tables["part_types"])) > 0
