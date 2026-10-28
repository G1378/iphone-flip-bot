"""The complete model list (logical order), per-model storage / colour options, and the buy-a-phone dropdowns."""
from __future__ import annotations

import re
from contextlib import asynccontextmanager
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bot import autocomplete, phone_intake
from app.models.config_models import PhoneModelConfig
from app.services import config_service, phone_catalogue as cat

pytestmark = pytest.mark.asyncio


# ------------------------------------------------------------------ the data itself
def test_catalogue_is_consistent():
    names = [m.name for m in cat.CATALOGUE]
    assert len(names) == len(set(names)), "duplicate model"
    assert len({config_service.model_key(n) for n in names}) == len(names), "two names resolve to one model"
    for m in cat.CATALOGUE:
        assert m.storage and m.colours, f"{m.name} needs storage and colour options"
        assert all(re.fullmatch(r"\d+(GB|TB)", s) for s in m.storage), m.name
        assert len(set(m.storage)) == len(m.storage) and len(set(m.colours)) == len(m.colours), m.name
        assert len(m.storage) <= 24 and len(m.colours) <= 24, "must fit a Discord dropdown with 'Other'"
    orders = [m.sort_order for m in cat.CATALOGUE]
    assert orders == sorted(orders) and len(set(orders)) == len(orders)


def test_every_series_fits_a_dropdown():
    assert len(cat.SERIES_ORDER) <= 24
    per_series = {}
    for m in cat.CATALOGUE:
        per_series.setdefault(m.series, []).append(m)
    assert len(per_series) == len(cat.SERIES_ORDER) and all(len(v) <= 25 for v in per_series.values())


def test_the_list_is_complete_and_in_logical_order():
    names = [m.name for m in cat.CATALOGUE]
    for earlier, later in [
        ("iPhone (1st generation)", "iPhone 3G"), ("iPhone 4S", "iPhone 5"), ("iPhone 6", "iPhone 6 Plus"),
        ("iPhone 8 Plus", "iPhone X"), ("iPhone XS Max", "iPhone 11"),
        ("iPhone 12 mini", "iPhone 12"), ("iPhone 12", "iPhone 12 Pro"), ("iPhone 12 Pro", "iPhone 12 Pro Max"),
        ("iPhone 12 Pro Max", "iPhone 13 mini"), ("iPhone 16e", "iPhone 16"), ("iPhone 16 Plus", "iPhone 16 Pro"),
        ("iPhone 17e", "iPhone 17"), ("iPhone 17", "iPhone Air"), ("iPhone Air", "iPhone 17 Pro"),
        ("iPhone 17 Pro Max", "iPhone 18 Pro"), ("iPhone 18 Pro Max", "iPhone Duo"),
    ]:
        assert names.index(earlier) < names.index(later), f"{earlier} should come before {later}"
    for must_have in ("iPhone SE (1st generation)", "iPhone SE (2nd generation)", "iPhone SE (3rd generation)",
                      "iPhone 5c", "iPhone 7 Plus", "iPhone XR", "iPhone 15 Plus", "iPhone 16e", "iPhone 17e"):
        assert must_have in names
    assert len(names) == 55, "the full line-up from the original iPhone to the iPhone Duo"


def test_known_options_match_what_apple_sold():
    by = cat.BY_NAME
    assert by["iPhone 18 Pro"].storage == ("256GB", "512GB", "1TB", "2TB")
    assert set(by["iPhone 18 Pro Max"].colours) == {"Black", "Silver", "Glacier", "Burgundy"}
    assert set(by["iPhone Duo"].colours) == {"Star White", "Night Sky"}
    assert by["iPhone 17e"].storage == ("256GB", "512GB") and "Soft Pink" in by["iPhone 17e"].colours
    assert by["iPhone 17 Pro Max"].storage[-1] == "2TB" and by["iPhone 17 Pro"].storage[-1] == "1TB"
    assert "Alpine Green" in by["iPhone 13 Pro"].colours and "128GB" in by["iPhone 13 Pro"].storage
    assert by["iPhone 15 Pro Max"].storage[0] == "256GB", "no 128GB Pro Max in the 15 series"
    assert "Pacific Blue" in by["iPhone 12 Pro"].colours and "Pacific Blue" not in by["iPhone 12"].colours
    assert "iPhone 18" not in by, "the base iPhone 18 is not released yet"


# ------------------------------------------------------------------ seeding and ordering
async def test_defaults_seed_series_order_and_options(session):
    rows = {m.model: m for m in await config_service.list_phone_models(session)}
    assert set(cat.BY_NAME) <= set(rows)
    m = rows["iPhone 14 Pro"]
    assert m.series == "iPhone 14 series" and m.storage_options == ["128GB", "256GB", "512GB", "1TB"]
    assert m.colourways == ["Space Black", "Silver", "Gold", "Deep Purple"]


async def test_list_comes_back_in_logical_order(session):
    names = [m.model for m in await config_service.list_phone_models(session)]
    assert names == [m.name for m in cat.CATALOGUE]


async def test_group_by_series_oldest_first_and_custom_models_last(session):
    await config_service.get_or_create_phone_model(session, "Apple", "iPhone Prototype")
    groups = config_service.group_by_series(await config_service.list_phone_models(session))
    assert [g for g, _ in groups[:2]] == ["iPhone (original)", "iPhone 3 series"]
    assert [g for g, _ in groups][-2:] == ["iPhone 18 series", "Other models"]
    assert [m.model for m in dict(groups)["iPhone 17 series"]] == [
        "iPhone 17e", "iPhone 17", "iPhone Air", "iPhone 17 Pro", "iPhone 17 Pro Max"]


async def test_existing_rows_without_options_are_filled_in_but_edits_are_kept(session):
    """What happens to a database that predates this feature, and to an admin's own changes."""
    rows = {m.model: m for m in await config_service.list_phone_models(session)}
    old = rows["iPhone 13"]
    old.series, old.sort_order, old.storage_options, old.colourways = None, 100000, None, None
    custom = rows["iPhone 12"]
    custom.storage_options, custom.colourways = ["64GB"], ["Black"]
    off = rows["iPhone 11"]
    off.active = False
    await session.flush()
    await config_service.ensure_default_phone_models(session)
    assert old.series == "iPhone 13 series" and old.storage_options == ["128GB", "256GB", "512GB"]
    assert "Midnight" in old.colourways
    assert (custom.storage_options, custom.colourways) == (["64GB"], ["Black"])
    assert off.active is False, "a model switched off stays off"


async def test_missing_models_are_added(session):
    row = (await session.execute(select(PhoneModelConfig).where(PhoneModelConfig.model == "iPhone Duo"))).scalar_one()
    await session.delete(row)
    await session.flush()
    await config_service.ensure_default_phone_models(session)
    assert "iPhone Duo" in {m.model for m in await config_service.list_phone_models(session)}


async def test_admin_can_set_options_for_a_custom_model(session):
    m = await config_service.get_or_create_phone_model(session, "Apple", "iPhone Prototype")
    await config_service.set_model_options(session, m, storage=["64", "128 gb", "1tb"], colours=[" Red ", "Blue"],
                                           series="iPhone 20 series")
    assert config_service.model_options(m) == (["64GB", "128GB", "1TB"], ["Red", "Blue"])
    assert config_service.series_of(m) == "iPhone 20 series"


def test_storage_normalising():
    n = config_service.normalise_storage
    assert [n(x) for x in ("128", "128gb", "128 GB", "256G", "1tb", "2T")] == ["128GB", "128GB", "128GB", "256GB", "1TB", "2TB"]


async def test_all_colourways_are_unique_and_sorted(session):
    colours = await config_service.all_colourways(session)
    assert len(colours) == len({c.lower() for c in colours}) and colours == sorted(colours, key=str.lower)
    assert {"Midnight", "Glacier", "Star White", "(PRODUCT)RED"} <= set(colours)


# ------------------------------------------------------------------ dropdown helpers
def _models():
    return [phone_intake.ModelInfo(i, m.name, m.series, m.storage, m.colours) for i, m in enumerate(cat.CATALOGUE)]


def test_series_dropdown_is_newest_first_and_fits_discord():
    opts = phone_intake.series_options(_models())
    assert opts[0].value == "iPhone 18 series" and opts[-1].value == "iPhone (original)"
    assert len(opts) <= 25 and all(len(o.label) <= 100 and len(o.description or "") <= 100 for o in opts)
    assert "Duo" in opts[0].description


def test_model_dropdown_lists_the_series_in_tier_order():
    opts = phone_intake.model_options(_models(), "iPhone 12 series")
    assert [o.value for o in opts] == ["iPhone 12 mini", "iPhone 12", "iPhone 12 Pro", "iPhone 12 Pro Max"]
    assert opts[2].description == "128GB-512GB · 4 colours"


def test_value_dropdown_always_offers_other_and_never_exceeds_25():
    opts = phone_intake.value_options(("128GB", "256GB"), current="256GB")
    assert [o.value for o in opts] == ["128GB", "256GB", phone_intake.OTHER] and opts[1].default
    assert len(phone_intake.value_options(tuple(f"{i}GB" for i in range(60)))) == 25
    assert [o.value for o in phone_intake.value_options(())] == [phone_intake.OTHER]


def test_price_parsing():
    assert phone_intake.parse_price("£1,250.50") == Decimal("1250.50") and phone_intake.parse_price(" 250 ") == Decimal("250")
    for bad in ("", "abc", "-5"):
        with pytest.raises(ValueError):
            phone_intake.parse_price(bad)


# ------------------------------------------------------------------ the wizard's steps
def _interaction(user_id=1):
    i = MagicMock()
    i.user.id = user_id
    i.response.edit_message = AsyncMock()
    i.response.send_modal = AsyncMock()
    i.response.send_message = AsyncMock()
    return i


async def test_wizard_walks_series_model_storage_colour_details():
    built = []
    view = phone_intake.PhoneIntakeView(1, "Buy", _models(), lambda m, s, c, w: built.append((m.name, s, c)) or "MODAL")
    assert view.stage == "series" and [o.value for o in view.children[0].options][0] == "iPhone 18 series"
    i = _interaction()
    await view.on_choice(i, "iPhone 13 series")
    assert view.stage == "model" and {o.value for o in view.children[0].options} == {
        "iPhone 13 mini", "iPhone 13", "iPhone 13 Pro", "iPhone 13 Pro Max"}
    await view.on_choice(i, "iPhone 13 Pro")
    # storage and colour dropdowns only offer what a 13 Pro came in
    assert view.stage == "storage" and [o.value for o in view.children[0].options][:-1] == ["128GB", "256GB", "512GB", "1TB"]
    await view.on_choice(i, "256GB")
    assert view.stage == "colour" and "Alpine Green" in [o.value for o in view.children[0].options]
    assert "Starlight" not in [o.value for o in view.children[0].options], "that's a 13 colour, not a 13 Pro one"
    await view.on_choice(i, "Sierra Blue")
    assert view.stage == "details" and "Sierra Blue" in view.embed().description
    await view.open_details(i)
    assert built == [("iPhone 13 Pro", "256GB", "Sierra Blue")]
    i.response.send_modal.assert_awaited_once_with("MODAL")


async def test_wizard_with_a_named_model_skips_to_storage_and_has_no_back_from_there():
    model = next(m for m in _models() if m.name == "iPhone 17e")
    view = phone_intake.PhoneIntakeView(1, "Buy", _models(), lambda *a: None, model)
    assert view.stage == "storage" and [o.value for o in view.children[0].options][:-1] == ["256GB", "512GB"]
    assert not any(getattr(c, "label", None) == phone_intake.BACK_LABEL for c in view.children)


async def test_wizard_back_button_steps_back():
    view = phone_intake.PhoneIntakeView(1, "Buy", _models(), lambda *a: None)
    i = _interaction()
    await view.on_choice(i, "iPhone 15 series")
    await view.on_choice(i, "iPhone 15")
    await view.go_back(i)
    assert view.stage == "model"
    await view.go_back(i)
    assert view.stage == "series"


async def test_wizard_other_opens_a_type_it_box_and_normalises_storage():
    view = phone_intake.PhoneIntakeView(1, "Buy", _models(), lambda *a: None,
                                        next(m for m in _models() if m.name == "iPhone 14"))
    i = _interaction()
    await view.on_choice(i, phone_intake.OTHER)
    modal = i.response.send_modal.await_args.args[0]
    assert isinstance(modal, phone_intake.TypeItModal) and view.stage == "storage"
    await view._typed_storage(i, "1tb")
    assert view.storage == "1TB" and view.stage == "colour"
    await view._typed_colour(i, "product red")
    assert view.colour == "Product Red" and view.stage == "details"


async def test_only_the_buyer_can_use_the_wizard():
    view = phone_intake.PhoneIntakeView(1, "Buy", _models(), lambda *a: None)
    other = _interaction(user_id=2)
    assert await view.interaction_check(other) is False
    other.response.send_message.assert_awaited_once()
    assert await view.interaction_check(_interaction(1)) is True


async def test_completing_saves_message_and_posts_the_result():
    view = phone_intake.PhoneIntakeView(1, "Buy", _models(), lambda *a: None)
    i = _interaction()
    i.followup.send = AsyncMock()
    await view.complete(i, "EMBED")
    i.response.edit_message.assert_awaited_once()
    i.followup.send.assert_awaited_once_with(embed="EMBED")


# ------------------------------------------------------------------ real buy path with the dropdown values
async def test_a_phone_bought_through_the_dropdowns_keeps_storage_and_colour_separate(session, actor_id):
    import datetime as dt
    from app.models.enums import AcquisitionType
    from app.services import phones as phones_service
    p = await phones_service.buy_phone(session, phones_service.BuyPhoneInput(
        manufacturer="Apple", model="iPhone 18 Pro", variant=None, storage="1TB", colour="Glacier", imei=None,
        serial_number=None, carrier=None, purchase_price=Decimal("900"), purchase_date=dt.date.today(),
        seller_source="CEX", acquisition_type=AcquisitionType.RESALE, notes=None, location_code=None), actor_id)
    assert (p.model, p.storage, p.colour) == ("iPhone 18 Pro", "1TB", "Glacier") and p.phone_model_id


# ------------------------------------------------------------------ autocomplete
@pytest.fixture()
def scope(engine, monkeypatch):
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

    @asynccontextmanager
    async def _scope():
        async with factory() as s:
            yield s
            await s.commit()

    monkeypatch.setattr(autocomplete, "session_scope", _scope)
    monkeypatch.setattr(phone_intake, "session_scope", _scope)
    return _scope


async def test_model_autocomplete_is_newest_generation_first_then_tier_order(session, scope):
    await session.commit()
    names = [c.value for c in await autocomplete.phone_model_autocomplete(MagicMock(), "")]
    assert names[:3] == ["iPhone 18 Pro", "iPhone 18 Pro Max", "iPhone Duo"] and names[3] == "iPhone 17e"
    assert [c.value for c in await autocomplete.phone_model_autocomplete(MagicMock(), "13")][0] == "iPhone 13"
    pro = [c.value for c in await autocomplete.phone_model_autocomplete(MagicMock(), "12 pro")]
    assert pro == ["iPhone 12 Pro", "iPhone 12 Pro Max"]


async def test_storage_and_colour_autocomplete_follow_the_phones_model(session, scope, actor_id):
    from tests.test_tasks import _buy
    phone = await _buy(session, actor_id)           # an iPhone 13
    await session.commit()
    ctx = SimpleNamespace(namespace=SimpleNamespace(internal_id=phone.internal_id))
    assert [c.value for c in await autocomplete.phone_storage_autocomplete(ctx, "")] == ["128GB", "256GB", "512GB"]
    assert "Starlight" in [c.value for c in await autocomplete.phone_colour_autocomplete(ctx, "")]
    assert [c.value for c in await autocomplete.phone_colour_autocomplete(ctx, "mid")] == ["Midnight"]
    assert await autocomplete.phone_colour_autocomplete(SimpleNamespace(namespace=SimpleNamespace()), "") == []


async def test_load_models_for_the_wizard_is_in_catalogue_order(session, scope):
    await session.commit()
    models = await phone_intake.load_models()
    assert [m.name for m in models] == [m.name for m in cat.CATALOGUE]
    assert next(m for m in models if m.name == "iPhone 16e").storage == ("128GB", "256GB", "512GB")


# ------------------------------------------------------------------ the details modals: one box per field
def _fill(modal, **values):
    for name, text in values.items():
        getattr(modal, name)._value = text      # what Discord does when the user submits the box


async def test_buy_modal_saves_each_box_separately_and_hands_back_to_the_wizard(session, scope, monkeypatch, actor_id):
    from app.bot.cogs import inventory as inv
    from app.models.enums import AcquisitionType
    from app.services import phones as phones_service
    monkeypatch.setattr(inv, "session_scope", scope)
    wizard = MagicMock()
    wizard.complete = AsyncMock()
    model = next(m for m in _models() if m.name == "iPhone 15 Pro")
    modal = inv.BuyPhoneModal(AcquisitionType.RESALE, model, "256GB", "Blue Titanium", wizard)
    _fill(modal, imei="356789012345678", serial="F2LXYZ123", price="£412.50", source="CEX", notes="boxed")
    interaction = _interaction(actor_id)
    await modal.on_submit(interaction)
    wizard.complete.assert_awaited_once()
    async with scope() as s:
        phone = await phones_service.get_phone(s, "IP-000001")
    assert (phone.model, phone.storage, phone.colour) == ("iPhone 15 Pro", "256GB", "Blue Titanium")
    assert (phone.imei, phone.serial_number, phone.seller_source, phone.notes) == ("356789012345678", "F2LXYZ123", "CEX", "boxed")
    assert phone.purchase_price == Decimal("412.50")


async def test_buy_modal_with_a_bad_price_keeps_the_wizard_open(session, scope, monkeypatch, actor_id):
    from app.bot.cogs import inventory as inv
    from app.models.enums import AcquisitionType
    monkeypatch.setattr(inv, "session_scope", scope)
    wizard = MagicMock()
    wizard.complete = AsyncMock()
    modal = inv.BuyPhoneModal(AcquisitionType.REPAIR, _models()[0], None, None, wizard)
    _fill(modal, price="two hundred")
    interaction = _interaction(actor_id)
    await modal.on_submit(interaction)
    wizard.complete.assert_not_awaited()
    assert interaction.response.send_message.await_args.kwargs["ephemeral"] is True


async def test_donor_modal_has_its_own_box_for_each_field(session, scope, monkeypatch, actor_id):
    from app.bot.cogs import donors as don
    from app.models.inventory_models import Donor
    monkeypatch.setattr(don, "session_scope", scope)
    wizard = MagicMock()
    wizard.complete = AsyncMock()
    model = next(m for m in _models() if m.name == "iPhone 12 Pro")
    modal = don.BuyDonorModal(model, "128GB", "Pacific Blue", wizard)
    assert [c.label for c in modal.children][:3] == ["Purchase price (£)", "Bought from (e.g. CEX, eBay, Facebook)",
                                                     "Fault (why it's a donor, not resale)"]
    _fill(modal, price="80", source="eBay", fault="Cracked frame", notes="")
    await modal.on_submit(_interaction(actor_id))
    wizard.complete.assert_awaited_once()
    async with scope() as s:
        donor = (await s.execute(select(Donor))).scalars().one()
    assert (donor.storage, donor.colour, donor.seller_source) == ("128GB", "Pacific Blue", "eBay")
