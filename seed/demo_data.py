from __future__ import annotations

import datetime as dt
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.integrations.ebay.interface import EbayClientInterface
from app.models.enums import AcquisitionType, PartSourceType, TestOutcome
from app.services import allocation as allocation_service
from app.services import config_service
from app.services import donors as donors_service
from app.services import finance as finance_service
from app.services import listings as listings_service
from app.services import orders as orders_service
from app.services import parts as parts_service
from app.services import phones as phones_service
from app.services import repairs as repairs_service

DEMO_ACTOR_ID = 0  # system/demo actor


async def populate_demo_data(session: AsyncSession, ebay: EbayClientInterface) -> dict[str, int]:
    """Populate a realistic working scenario:
    - 4 faulty phones bought for repair
    - 2 donor phones torn down for parts
    - donor cost allocated pro-rata across recovered parts
    - a purchased-parts order received into stock
    - repairs completed using a mix of donor and purchased parts
    - 3 phones listed and sold (via the mock eBay client), 1 left mid-repair
    - a couple of business expenses

    Never call this in production - it creates real-looking financial
    records. The /demo Discord command refuses to run unless
    ENVIRONMENT=development.
    """
    today = dt.date.today()
    counts = {"phones": 0, "donors": 0, "parts": 0, "repairs": 0, "sales": 0, "orders": 0, "expenses": 0}

    await config_service.seed_defaults_if_empty(session)
    await config_service.get_or_create_phone_model(session, "Apple", "iPhone 13 Pro")
    await config_service.get_or_create_phone_model(session, "Apple", "iPhone 12")
    await config_service.get_or_create_phone_model(session, "Apple", "iPhone 14 Pro")
    for loc in ["A1", "A2", "A3", "B1", "B2", "B3", "C1", "C2", "D1"]:
        await config_service.add_location(session, loc)

    # ---------------- Donors ----------------
    donor1 = await donors_service.buy_donor(
        session,
        donors_service.BuyDonorInput(
            manufacturer="Apple", model="iPhone 13 Pro", variant=None, storage="128GB", colour="Graphite",
            purchase_price=Decimal("180.00"), purchase_date=today - dt.timedelta(days=20),
            seller_source="CEX", fault_description="Cracked housing, screen shattered", notes=None, location_code=None,
        ),
        DEMO_ACTOR_ID,
    )
    donor2 = await donors_service.buy_donor(
        session,
        donors_service.BuyDonorInput(
            manufacturer="Apple", model="iPhone 12", variant=None, storage="64GB", colour="Blue",
            purchase_price=Decimal("90.00"), purchase_date=today - dt.timedelta(days=15),
            seller_source="Facebook Marketplace", fault_description="Water damage, no power", notes=None, location_code=None,
        ),
        DEMO_ACTOR_ID,
    )
    counts["donors"] = 2

    recovered1 = await donors_service.teardown(session, donor1, [
        donors_service.RecoveredPartInput("Screen", "Good", TestOutcome.PASS, "A-", None, "B1"),
        donors_service.RecoveredPartInput("Rear Camera", "Good", TestOutcome.PASS, "A", None, "C1"),
        donors_service.RecoveredPartInput("Battery", "91% health", TestOutcome.PASS, "B", None, "D1"),
        donors_service.RecoveredPartInput("Housing", "Cracked", TestOutcome.FAIL, "SCRAP", "Original fault", None, discarded=True),
    ], DEMO_ACTOR_ID)
    screen1, camera1, battery1, housing1 = recovered1

    recovered2 = await donors_service.teardown(session, donor2, [
        donors_service.RecoveredPartInput("Screen", "Good, minor scratch", TestOutcome.PASS, "B", None, "B2"),
        donors_service.RecoveredPartInput("Housing", "Excellent", TestOutcome.PASS, "A", None, "A2"),
        donors_service.RecoveredPartInput("Logic Board", "Water damaged, DOA", TestOutcome.FAIL, "SCRAP", "Corrosion", None, discarded=True),
    ], DEMO_ACTOR_ID)
    screen2, housing2, _board2 = recovered2
    counts["parts"] += len(recovered1) + len(recovered2)

    await allocation_service.allocate_pro_rata(session, donor1, [
        allocation_service.ProRataLine(part_id=screen1.id, assigned_value=Decimal("70")),
        allocation_service.ProRataLine(part_id=camera1.id, assigned_value=Decimal("40")),
        allocation_service.ProRataLine(part_id=battery1.id, assigned_value=Decimal("30")),
        allocation_service.ProRataLine(part_id=housing1.id, assigned_value=Decimal("40")),
    ], DEMO_ACTOR_ID, notes="Demo pro-rata allocation")

    await allocation_service.allocate_pro_rata(session, donor2, [
        allocation_service.ProRataLine(part_id=screen2.id, assigned_value=Decimal("50")),
        allocation_service.ProRataLine(part_id=housing2.id, assigned_value=Decimal("40")),
    ], DEMO_ACTOR_ID, notes="Demo pro-rata allocation")

    # ---------------- Purchased parts order (received) ----------------
    battery_type = await config_service.get_or_create_part_type(session, "Battery")
    screen_type = await config_service.get_or_create_part_type(session, "Screen")
    order = await orders_service.create_order(
        session, "MobileSentrix", [
            orders_service.OrderLineInput(part_type_id=battery_type.id, quantity=2, unit_cost=Decimal("18.00")),
            orders_service.OrderLineInput(part_type_id=screen_type.id, quantity=1, unit_cost=Decimal("55.00")),
        ],
        DEMO_ACTOR_ID, shipping_cost=Decimal("8.00"),
    )
    await orders_service.mark_ordered(session, order, DEMO_ACTOR_ID)
    purchased_parts = await orders_service.receive_order(session, order, DEMO_ACTOR_ID)
    counts["orders"] = 1
    counts["parts"] += len(purchased_parts)
    purchased_battery = next(p for p in purchased_parts if p.part_type_id == battery_type.id)
    purchased_screen = next(p for p in purchased_parts if p.part_type_id == screen_type.id)

    # ---------------- 4 faulty phones bought for repair ----------------
    phone1 = await phones_service.buy_phone(session, phones_service.BuyPhoneInput(
        manufacturer="Apple", model="iPhone 13 Pro", variant=None, storage="128GB", colour="Graphite",
        imei="356789012345671", serial_number="DMO0001", carrier="Unlocked",
        purchase_price=Decimal("250.00"), purchase_date=today - dt.timedelta(days=10), seller_source="Trade-in",
        acquisition_type=AcquisitionType.REPAIR, notes="Cracked screen", location_code="A1",
    ), DEMO_ACTOR_ID)
    await phones_service.add_fault(session, phone1, "Cracked screen", DEMO_ACTOR_ID, required_part_type_id=screen_type.id)

    phone2 = await phones_service.buy_phone(session, phones_service.BuyPhoneInput(
        manufacturer="Apple", model="iPhone 12", variant=None, storage="64GB", colour="Blue",
        imei="356789012345672", serial_number="DMO0002", carrier="Unlocked",
        purchase_price=Decimal("140.00"), purchase_date=today - dt.timedelta(days=9), seller_source="Auction",
        acquisition_type=AcquisitionType.REPAIR, notes="Battery degraded", location_code="A2",
    ), DEMO_ACTOR_ID)
    await phones_service.add_fault(session, phone2, "Battery health 68%, needs replacing", DEMO_ACTOR_ID, required_part_type_id=battery_type.id)

    phone3 = await phones_service.buy_phone(session, phones_service.BuyPhoneInput(
        manufacturer="Apple", model="iPhone 13 Pro", variant=None, storage="256GB", colour="Silver",
        imei="356789012345673", serial_number="DMO0003", carrier="Unlocked",
        purchase_price=Decimal("270.00"), purchase_date=today - dt.timedelta(days=8), seller_source="CEX",
        acquisition_type=AcquisitionType.REPAIR, notes="Camera fault", location_code="A3",
    ), DEMO_ACTOR_ID)
    camera_type = await config_service.get_or_create_part_type(session, "Rear Camera")
    await phones_service.add_fault(session, phone3, "Rear camera not focusing", DEMO_ACTOR_ID, required_part_type_id=camera_type.id)

    phone4 = await phones_service.buy_phone(session, phones_service.BuyPhoneInput(
        manufacturer="Apple", model="iPhone 12", variant=None, storage="128GB", colour="Black",
        imei="356789012345674", serial_number="DMO0004", carrier="Unlocked",
        purchase_price=Decimal("150.00"), purchase_date=today - dt.timedelta(days=5), seller_source="Facebook Marketplace",
        acquisition_type=AcquisitionType.REPAIR, notes="Screen + housing damage", location_code="A1",
    ), DEMO_ACTOR_ID)
    housing_type = await config_service.get_or_create_part_type(session, "Housing")
    await phones_service.add_fault(session, phone4, "Cracked screen", DEMO_ACTOR_ID, required_part_type_id=screen_type.id)
    await phones_service.add_fault(session, phone4, "Damaged housing", DEMO_ACTOR_ID, required_part_type_id=housing_type.id)
    counts["phones"] = 4

    # ---------------- Repairs: reserve/install using a mix of donor + purchased parts ----------------
    repair1 = await repairs_service.create_repair_for_open_faults(session, phone1, DEMO_ACTOR_ID)
    rp1 = repair1.parts[0]
    await parts_service.reserve_part(session, screen1, repair1, rp1, DEMO_ACTOR_ID)
    await parts_service.install_part(session, screen1, phone1, rp1, DEMO_ACTOR_ID)
    await repairs_service.complete_repair(session, repair1, DEMO_ACTOR_ID)
    await phones_service.set_status(session, phone1, "READY_FOR_LISTING", DEMO_ACTOR_ID)

    repair2 = await repairs_service.create_repair_for_open_faults(session, phone2, DEMO_ACTOR_ID)
    rp2 = repair2.parts[0]
    await parts_service.reserve_part(session, purchased_battery, repair2, rp2, DEMO_ACTOR_ID)
    await parts_service.install_part(session, purchased_battery, phone2, rp2, DEMO_ACTOR_ID)
    await repairs_service.complete_repair(session, repair2, DEMO_ACTOR_ID)
    await phones_service.set_status(session, phone2, "READY_FOR_LISTING", DEMO_ACTOR_ID)

    repair3 = await repairs_service.create_repair_for_open_faults(session, phone3, DEMO_ACTOR_ID)
    rp3 = repair3.parts[0]
    await parts_service.reserve_part(session, camera1, repair3, rp3, DEMO_ACTOR_ID)
    await parts_service.install_part(session, camera1, phone3, rp3, DEMO_ACTOR_ID)
    await repairs_service.complete_repair(session, repair3, DEMO_ACTOR_ID)
    await phones_service.set_status(session, phone3, "READY_FOR_LISTING", DEMO_ACTOR_ID)

    # phone4 stays mid-repair: screen installed (shared donor-source part type,
    # demonstrating "parts shared between repairs" - screen2 came from a
    # *different* donor than phone1's screen), housing still outstanding.
    repair4 = await repairs_service.create_repair_for_open_faults(session, phone4, DEMO_ACTOR_ID)
    rp4_screen = next(rp for rp in repair4.parts if rp.required_part_type_id == screen_type.id)
    await parts_service.reserve_part(session, screen2, repair4, rp4_screen, DEMO_ACTOR_ID)
    await parts_service.install_part(session, screen2, phone4, rp4_screen, DEMO_ACTOR_ID)
    await phones_service.set_status(session, phone4, "AWAITING_PARTS", DEMO_ACTOR_ID, notes="Housing still required")
    counts["repairs"] = 4

    # ---------------- List + sell 3 of the 4 phones ----------------
    for phone, price in ((phone1, Decimal("399.00")), (phone2, Decimal("280.00")), (phone3, Decimal("459.00"))):
        draft = listings_service.DraftListingInput(
            title=f"{phone.display_name()} - Unlocked - Tested Working", description="Fully tested, grade A condition.",
            condition_text="Excellent", price=price,
        )
        listing = await listings_service.create_draft(session, phone, draft, DEMO_ACTOR_ID)
        published = await listings_service.publish(session, listing, ebay, DEMO_ACTOR_ID)
        fee = (price * Decimal("0.128") + Decimal("0.30")).quantize(Decimal("0.01"))
        await finance_service.record_sale(
            session, phone, sale_price=price, sold_at=dt.datetime.now(dt.timezone.utc), actor_discord_id=DEMO_ACTOR_ID,
            listing=listing, ebay_order_id=f"DEMO-ORDER-{phone.internal_id}", ebay_fees=fee,
        )
        counts["sales"] += 1

    # ---------------- A couple of expenses ----------------
    from app.models.commerce_models import Expense
    from app.services.ids import next_internal_id as _next_id
    postage_cat = next((c for c in await config_service.list_expense_categories(session) if c.name == "Postage Out"), None)
    tools_cat = next((c for c in await config_service.list_expense_categories(session) if c.name == "Tools"), None)
    for cat, amount, desc in [(postage_cat, Decimal("6.50"), "Royal Mail Tracked 24 - batch postage"),
                               (tools_cat, Decimal("35.00"), "Replacement screwdriver set")]:
        if cat:
            expense_id = await _next_id(session, "expense")
            session.add(Expense(internal_id=expense_id, category_id=cat.id, amount=amount, description=desc,
                                 incurred_at=today, created_by_discord_id=DEMO_ACTOR_ID))
            counts["expenses"] += 1

    await session.flush()
    return counts
