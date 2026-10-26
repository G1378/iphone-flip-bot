"""The bot builds the eBay listing; a person checks it and presses Publish.

Covers the builder, the 'ready to publish' gate, what actually goes to eBay, ending a listing
(it needs the OFFER id), photos, one-time eBay setup, and the job-channel buttons."""
from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.integrations.ebay.client import description_to_html
from app.integrations.ebay.interface import EbayApiError, PolicyInfo
from app.integrations.ebay.mock import MockEbayClient
from app.models.enums import LockStatus, PartSourceType
from app.services import config_service, job_actions, job_panels, listing_builder, listings, phones
from app.services.job_actions import JobActionError
from tests.test_job_panels import _acts, _line, _panel
from tests.test_jobs_discord import (_in_channel, _job_channel, _press_start, world, CapturingConfirm)  # noqa: F401
from tests.test_tasks import _buy, _stock

pytestmark = pytest.mark.asyncio

SETTING_KEYS = ("ebay_fulfillment_policy_id", "ebay_payment_policy_id", "ebay_return_policy_id", "ebay_merchant_location_key")


async def _ready_phone(session, actor_id):
    """An iPhone 13, tested and ready to list, with the facts a listing needs."""
    phone = await _buy(session, actor_id)
    await phones.edit_phone(session, phone, actor_id, cosmetic_condition="Excellent", battery_health=91,
                            functional_condition="Fully tested, all working", lock_status=LockStatus.UNLOCKED)
    await phones.set_status(session, phone, "READY_FOR_LISTING", actor_id)
    return phone


async def _configure_ebay_account(session):
    for k in SETTING_KEYS:
        await config_service.set_setting(session, k, "x")


# ------------------------------------------------------------------ the builder
async def test_title_is_most_searched_words_first_and_never_over_80(session, actor_id):
    phone = await _ready_phone(session, actor_id)
    assert listing_builder.build_title(phone) == "Apple iPhone 13 128GB Blue Unlocked Battery 91%".replace("128GB", "128 GB")
    phone.colour = "Alpine Green Super Long Marketing Colour Name"
    phone.carrier = "x" * 30
    title = listing_builder.build_title(phone)
    assert len(title) <= 80 and title.startswith("Apple iPhone 13 128 GB")


async def test_item_specifics_only_contain_what_we_know(session, actor_id):
    phone = await _ready_phone(session, actor_id)
    a = listing_builder.build_aspects(phone)
    assert a == {"Brand": ["Apple"], "Model": ["Apple iPhone 13"], "Storage Capacity": ["128 GB"], "Colour": ["Blue"],
                 "Network": ["Unlocked"], "Lock Status": ["Network Unlocked"]}
    phone.lock_status, phone.colour = LockStatus.UNKNOWN, None
    a = listing_builder.build_aspects(phone)
    assert "Lock Status" not in a and "Network" not in a and "Colour" not in a


async def test_description_uses_the_record_and_leaves_out_unknowns(session, actor_id):
    phone = await _ready_phone(session, actor_id)
    await config_service.set_setting(session, "listing_whats_included", "Phone only. No charger or box.")
    built = await listing_builder.build_for_phone(session, phone)
    d = built.description
    assert "Battery health: 91%" in d and "Cosmetic: Excellent" in d and "WHAT'S INCLUDED\nPhone only." in d
    assert "REPAIR HISTORY" not in d and "IMEI" not in d          # nothing fitted; never publish the IMEI
    assert built.condition_description == "Excellent; fully tested: Fully tested, all working; battery health 91%."


async def test_a_fitted_part_is_disclosed_and_a_bought_in_part_gets_a_warning(session, actor_id):
    from app.models.inventory_models import Part
    phone = await _ready_phone(session, actor_id)
    screen = await _stock(session, actor_id, "Screen", model="iPhone 13")
    screen.source_type, screen.status, screen.installed_phone_id = PartSourceType.PURCHASED, "INSTALLED", phone.id
    await session.flush()
    await session.refresh(phone, ["installed_parts"])
    built = await listing_builder.build_for_phone(session, phone)
    assert "REPAIR HISTORY\n• Replacement screen fitted" in built.description
    assert any("screen was bought in" in w for w in built.warnings)


async def test_warnings_say_what_is_missing_instead_of_inventing_it(session, actor_id):
    phone = await _buy(session, actor_id)           # nothing recorded beyond what buying needs
    built = await listing_builder.build_for_phone(session, phone)
    text = " | ".join(built.warnings)
    for expected in ("Lock status is unknown", "No cosmetic condition", "No battery health", "What's included"):
        assert expected in text
    assert "see photos" in built.description


async def test_rebuilding_refreshes_the_same_draft_and_keeps_a_manual_price(session, actor_id):
    phone = await _ready_phone(session, actor_id)
    draft, built = await listings.build_or_refresh_draft(session, phone, actor_id)
    assert built.price is None and draft.price == Decimal("0")
    draft.price = Decimal("310.00")
    phone.battery_health = 88
    again, _ = await listings.build_or_refresh_draft(session, phone, actor_id)
    assert again.id == draft.id and again.price == Decimal("310.00") and "Battery 88%" in again.title


# ------------------------------------------------------------------ the publish gate
async def test_publish_is_refused_until_everything_ebay_needs_is_there(session, actor_id):
    phone = await _ready_phone(session, actor_id)
    draft, _ = await listings.build_or_refresh_draft(session, phone, actor_id)
    ebay = MockEbayClient()
    with pytest.raises(listings.ListingNotReady) as e:
        await listings.publish(session, draft, ebay, actor_id)
    msg = str(e.value)
    assert "No photos yet" in msg and "Set a price" in msg and "/config ebay-setup" in msg
    assert ebay.listings == {} and draft.status == "DRAFT"          # nothing was sent


async def test_publish_sends_the_full_listing_to_ebay(session, actor_id):
    phone = await _ready_phone(session, actor_id)
    await _configure_ebay_account(session)
    await phones.add_photos(session, phone, ["https://img/1.jpg", "https://img/2.jpg"], actor_id)
    draft, _ = await listings.build_or_refresh_draft(session, phone, actor_id)
    draft.price = Decimal("299.00")
    ebay = MockEbayClient()
    published = await listings.publish(session, draft, ebay, actor_id)
    sent = ebay.listings[published.ebay_listing_id]
    assert sent["image_urls"] == ["https://img/1.jpg", "https://img/2.jpg"]
    assert sent["aspects"]["Storage Capacity"] == ["128 GB"] and sent["condition_description"].startswith("Excellent")
    assert sent["fulfillment_policy_id"] == sent["payment_policy_id"] == sent["return_policy_id"] == "x"
    assert sent["merchant_location_key"] == "x"
    assert published.status == "ACTIVE" and phone.current_status == "LISTED"


async def test_unconfigured_ebay_is_reported_before_anything_else(session, actor_id):
    phone = await _ready_phone(session, actor_id)
    draft, _ = await listings.build_or_refresh_draft(session, phone, actor_id)
    from app.integrations.ebay.interface import EbayNotConfiguredError
    with pytest.raises(EbayNotConfiguredError):
        await listings.publish(session, draft, MockEbayClient(configured=False), actor_id)


# ------------------------------------------------------------------ ending a listing
async def _live_listing(session, actor_id, ebay):
    phone = await _ready_phone(session, actor_id)
    await _configure_ebay_account(session)
    await phones.add_photos(session, phone, ["https://img/1.jpg"], actor_id)
    draft, _ = await listings.build_or_refresh_draft(session, phone, actor_id)
    draft.price = Decimal("299.00")
    return await listings.publish(session, draft, ebay, actor_id)


async def test_ending_a_listing_uses_the_offer_id_not_the_listing_id(session, actor_id):
    ebay = MockEbayClient()
    live = await _live_listing(session, actor_id, ebay)
    assert live.ebay_offer_id != live.ebay_listing_id
    await listings.end_listing(session, live, ebay, actor_id)
    assert live.status == "ENDED" and ebay.listings[live.ebay_listing_id]["status"] == "ENDED"
    with pytest.raises(EbayApiError):                      # the old bug: sending the public listing id
        await ebay.end_listing(live.ebay_listing_id)


async def test_a_listing_with_no_offer_id_is_not_silently_marked_ended(session, actor_id):
    ebay = MockEbayClient()
    live = await _live_listing(session, actor_id, ebay)
    live.ebay_offer_id = None
    with pytest.raises(listings.ListingError, match="no eBay offer id"):
        await listings.end_listing(session, live, ebay, actor_id)
    assert live.status == "ACTIVE"


# ------------------------------------------------------------------ photos
async def test_photos_keep_their_order_and_replace_or_cap(session, actor_id):
    phone = await _buy(session, actor_id)
    await phones.add_photos(session, phone, ["a", "b"], actor_id)
    await phones.add_photos(session, phone, ["c"], actor_id)
    assert phone.photo_urls == ["a", "b", "c"]
    await phones.add_photos(session, phone, ["z"], actor_id, replace=True)
    assert phone.photo_urls == ["z"]
    await phones.add_photos(session, phone, [str(i) for i in range(40)], actor_id)
    assert len(phone.photo_urls) == phones.MAX_PHOTOS and phone.photo_urls[0] == "z"


async def test_the_mock_hosts_uploaded_photos(session):
    ebay = MockEbayClient()
    url = await ebay.upload_image(b"x", "front.jpg", "image/jpeg")
    assert url.startswith("https://") and ebay.uploaded_images == [url]


# ------------------------------------------------------------------ one-time eBay setup
async def test_setup_picks_the_obvious_policies_and_asks_about_ambiguous_ones(session):
    ebay = MockEbayClient()
    ebay.setup.fulfillment_policies = [PolicyInfo("F1", "Tracked 24"), PolicyInfo("F2", "Tracked 48")]  # no default
    result = await listings.configure_publishing(session, ebay)
    assert result.chosen["ebay_fulfillment_policy_id"] is None            # two options, none default: don't guess
    assert result.chosen["ebay_payment_policy_id"].id == "PP-1" and result.chosen["ebay_merchant_location_key"].id == "workshop"
    assert await config_service.get_setting(session, "ebay_payment_policy_id") == "PP-1"
    assert not await config_service.get_setting(session, "ebay_fulfillment_policy_id")

    result = await listings.configure_publishing(session, ebay, {"ebay_fulfillment_policy_id": "F2"})
    assert result.chosen["ebay_fulfillment_policy_id"].name == "Tracked 48"
    with pytest.raises(listings.ListingError, match="no fulfillment policies"):
        await listings.configure_publishing(session, ebay, {"ebay_fulfillment_policy_id": "nope"})


async def test_setup_can_create_a_stock_location(session):
    ebay = MockEbayClient()
    ebay.setup.locations = []
    result = await listings.configure_publishing(session, ebay, postcode="SW1A 1AA")
    assert result.created_location and result.chosen["ebay_merchant_location_key"].id == "workshop"


# ------------------------------------------------------------------ description -> eBay HTML
def test_description_html_escapes_and_structures():
    html = description_to_html("IPHONE 13\nBlue <unlocked>\n\nREPAIR HISTORY\n• Replacement screen fitted\n• Battery\n\nThanks")
    assert html == ("<h3>IPHONE 13</h3><p>Blue &lt;unlocked&gt;</p><h3>REPAIR HISTORY</h3>"
                    "<ul><li>Replacement screen fitted</li><li>Battery</li></ul><p>Thanks</p>")


# ------------------------------------------------------------------ the job-channel buttons
async def test_list_stage_offers_build_then_publish_edit_rebuild(session, actor_id):
    phone = await _ready_phone(session, actor_id)
    panel = await _panel(session, phone)
    assert panel.stage == "LIST" and _acts(panel) == ["buildlisting", "done", "fault", "details"]
    assert dict(panel.fields)["Photos"] == "0"

    draft, _ = await listings.build_or_refresh_draft(session, phone, actor_id)
    panel = await _panel(session, phone)
    assert _acts(panel) == ["publishlisting", "editlisting", "buildlisting", "done", "fault", "details"]
    publish = _line(panel, "publishlisting")
    assert publish.ref == draft.id and publish.style == "secondary"      # not ready: not green
    assert "No photos yet" in panel.body and "Set a price" in panel.body


async def test_publish_button_turns_green_only_when_ready_and_hash_changes(session, actor_id):
    phone = await _ready_phone(session, actor_id)
    draft, _ = await listings.build_or_refresh_draft(session, phone, actor_id)
    before = (await _panel(session, phone)).content_hash()
    draft.price = Decimal("299")
    await phones.add_photos(session, phone, ["https://img/1.jpg"], actor_id)
    panel = await _panel(session, phone)
    assert _line(panel, "publishlisting").style == "success" and "Ready to publish" in panel.body
    assert "£299" in _line(panel, "publishlisting").label and panel.content_hash() != before


async def test_job_actions_build_edit_publish(session, actor_id):
    phone = await _ready_phone(session, actor_id)
    await _configure_ebay_account(session)
    await phones.add_photos(session, phone, ["https://img/1.jpg"], actor_id)

    msg = await job_actions.build_listing(session, phone.id, actor_id)
    assert "Built LS-" in msg and "⚠️" in msg and "no price yet" in msg
    draft = await listings.get_draft_for_phone(session, phone.id)

    for bad_price in ("abc", "0", "-5"):
        with pytest.raises(JobActionError):
            await job_actions.edit_listing(session, phone.id, draft.id, actor_id, title="t", price=bad_price,
                                           condition_text="Excellent", description="d")
    await job_actions.edit_listing(session, phone.id, draft.id, actor_id, title="My title", price="£305.50",
                                   condition_text="Excellent", description="Hello")
    assert (draft.title, draft.price, draft.description) == ("My title", Decimal("305.50"), "Hello")

    ebay = MockEbayClient()
    out = await job_actions.publish_listing(session, phone.id, draft.id, actor_id, ebay)
    assert "Published" in out and phone.current_status == "LISTED"
    with pytest.raises(JobActionError, match="isn't there any more"):     # a second press finds no draft
        await job_actions.publish_listing(session, phone.id, draft.id, actor_id, ebay)


async def test_job_action_publish_reports_what_is_missing_in_plain_english(session, actor_id):
    phone = await _ready_phone(session, actor_id)
    await job_actions.build_listing(session, phone.id, actor_id)
    draft = await listings.get_draft_for_phone(session, phone.id)
    with pytest.raises(JobActionError, match="No photos yet"):
        await job_actions.publish_listing(session, phone.id, draft.id, actor_id, MockEbayClient())
    with pytest.raises(JobActionError, match="isn't connected"):
        await job_actions.publish_listing(session, phone.id, draft.id, actor_id, MockEbayClient(configured=False))


# ------------------------------------------------------------------ the buttons in Discord
async def test_publish_button_in_the_channel_blocks_then_confirms_then_publishes(world):
    w = world
    w.bot.ebay_client = MockEbayClient()
    phone = await _ready_phone(w.session, w.actor)
    await _configure_ebay_account(w.session)
    await w.session.commit()
    await w.tasks.engine_tick()
    await _press_start(w, phone.id)
    ch = await _job_channel(w, phone.id)

    await w.jobs.handle(_in_channel(w, ch), "buildlisting", phone.id)
    async with w.scope() as s:
        draft = await listings.get_draft_for_phone(s, phone.id)
        draft_id = draft.id

    blocked = _in_channel(w, ch)
    await w.jobs.handle(blocked, "publishlisting", draft_id)
    assert "No photos yet" in blocked.response.sent[0].embed.description and blocked.response.sent[0].ephemeral
    assert not w.bot.ebay_client.listings

    async with w.scope() as s:
        p = await s.get(type(phone), phone.id)
        await phones.add_photos(s, p, ["https://img/1.jpg"], w.actor)
        (await listings.get_draft_for_phone(s, phone.id)).price = Decimal("299")

    ready = _in_channel(w, ch)
    await w.jobs.handle(ready, "publishlisting", draft_id)
    assert ready.response.sent[0].embed.title == "Publish this to eBay?" and not w.bot.ebay_client.listings, \
        "nothing goes live until the second tap"

    await CapturingConfirm.last.callback(_in_channel(w, ch))
    assert len(w.bot.ebay_client.listings) == 1
    async with w.scope() as s:
        assert (await s.get(type(phone), phone.id)).current_status == "LISTED"
