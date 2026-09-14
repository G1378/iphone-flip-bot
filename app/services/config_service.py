from __future__ import annotations

from decimal import Decimal
from typing import Optional, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.config_models import (
    AuthorizedUser,
    BotChannel,
    BusinessSetting,
    ConditionGrade,
    ExpenseCategory,
    Location,
    PartTypeConfig,
    PhoneModelConfig,
    PricingRule,
    TestDefinition,
    WorkflowStatus,
)
from app.services import defaults as d


# ----------------------------------------------------------------------
# Bootstrap / seeding (idempotent - safe to call every startup)
# ----------------------------------------------------------------------
async def seed_defaults_if_empty(session: AsyncSession) -> None:
    if not (await session.execute(select(WorkflowStatus.id).limit(1))).first():
        for entity_type, rows in (
            ("PHONE", d.PHONE_STATUSES),
            ("DONOR", d.DONOR_STATUSES),
            ("PART", d.PART_STATUSES),
            ("REPAIR", d.REPAIR_STATUSES),
            ("ORDER", d.ORDER_STATUSES),
            ("LISTING", d.LISTING_STATUSES),
        ):
            for i, (code, label, terminal) in enumerate(rows):
                session.add(
                    WorkflowStatus(
                        entity_type=entity_type, code=code, label=label,
                        sort_order=i, is_terminal=terminal,
                    )
                )

    if not (await session.execute(select(TestDefinition.id).limit(1))).first():
        for i, code in enumerate(d.DEFAULT_TEST_DEFINITIONS):
            session.add(
                TestDefinition(code=code, name=d.DEFAULT_TEST_LABELS[code], sort_order=i)
            )

    if not (await session.execute(select(PartTypeConfig.id).limit(1))).first():
        for name in d.DEFAULT_PART_TYPES:
            session.add(PartTypeConfig(name=name, category=None))

    if not (await session.execute(select(ConditionGrade.id).limit(1))).first():
        for code, label, order in d.DEFAULT_CONDITION_GRADES:
            session.add(ConditionGrade(code=code, label=label, sort_order=order))

    if not (await session.execute(select(ExpenseCategory.id).limit(1))).first():
        for name in d.DEFAULT_EXPENSE_CATEGORIES:
            session.add(ExpenseCategory(name=name))

    if not (await session.execute(select(BusinessSetting.key).limit(1))).first():
        for key, value, vtype, desc in d.DEFAULT_BUSINESS_SETTINGS:
            session.add(BusinessSetting(key=key, value=value, value_type=vtype, description=desc))

    if not (await session.execute(select(PricingRule.id).limit(1))).first():
        session.add(
            PricingRule(
                phone_model_id=None,
                target_margin_pct=Decimal("25.00"),
                min_profit_gbp=Decimal("20.00"),
                min_roi_pct=Decimal("15.00"),
            )
        )

    await session.flush()


# ----------------------------------------------------------------------
# Business settings (typed EAV)
# ----------------------------------------------------------------------
async def get_setting(session: AsyncSession, key: str, default: Optional[str] = None) -> Optional[str]:
    row = await session.get(BusinessSetting, key)
    return row.value if row else default


async def get_setting_decimal(session: AsyncSession, key: str, default: Decimal) -> Decimal:
    row = await session.get(BusinessSetting, key)
    if row is None:
        return default
    try:
        return Decimal(row.value)
    except Exception:
        return default


async def set_setting(session: AsyncSession, key: str, value: str, value_type: str = "string",
                       description: Optional[str] = None) -> BusinessSetting:
    row = await session.get(BusinessSetting, key)
    if row is None:
        row = BusinessSetting(key=key, value=value, value_type=value_type, description=description)
        session.add(row)
    else:
        row.value = value
        if description:
            row.description = description
    await session.flush()
    return row


async def all_settings(session: AsyncSession) -> Sequence[BusinessSetting]:
    return (await session.execute(select(BusinessSetting).order_by(BusinessSetting.key))).scalars().all()


# ----------------------------------------------------------------------
# Authorized users
# ----------------------------------------------------------------------
async def is_authorized(session: AsyncSession, discord_user_id: int) -> bool:
    row = (
        await session.execute(
            select(AuthorizedUser).where(
                AuthorizedUser.discord_user_id == discord_user_id,
                AuthorizedUser.active.is_(True),
            )
        )
    ).scalar_one_or_none()
    return row is not None


async def is_admin(session: AsyncSession, discord_user_id: int) -> bool:
    row = (
        await session.execute(
            select(AuthorizedUser).where(
                AuthorizedUser.discord_user_id == discord_user_id,
                AuthorizedUser.active.is_(True),
            )
        )
    ).scalar_one_or_none()
    return bool(row and row.is_admin)


async def add_authorized_user(session: AsyncSession, discord_user_id: int, display_name: str,
                               added_by: int, is_admin_flag: bool = False) -> AuthorizedUser:
    existing = (
        await session.execute(select(AuthorizedUser).where(AuthorizedUser.discord_user_id == discord_user_id))
    ).scalar_one_or_none()
    if existing:
        existing.active = True
        existing.is_admin = is_admin_flag or existing.is_admin
        existing.display_name = display_name
        await session.flush()
        return existing
    row = AuthorizedUser(
        discord_user_id=discord_user_id, display_name=display_name,
        added_by_discord_id=added_by, is_admin=is_admin_flag, active=True,
    )
    session.add(row)
    await session.flush()
    return row


async def remove_authorized_user(session: AsyncSession, discord_user_id: int) -> bool:
    row = (
        await session.execute(select(AuthorizedUser).where(AuthorizedUser.discord_user_id == discord_user_id))
    ).scalar_one_or_none()
    if not row:
        return False
    row.active = False
    await session.flush()
    return True


async def list_authorized_users(session: AsyncSession) -> Sequence[AuthorizedUser]:
    return (
        await session.execute(select(AuthorizedUser).where(AuthorizedUser.active.is_(True)))
    ).scalars().all()


# ----------------------------------------------------------------------
# Channels
# ----------------------------------------------------------------------
async def add_channel(session: AsyncSession, guild_id: int, channel_id: int, purpose: str = "general") -> BotChannel:
    existing = (await session.execute(select(BotChannel).where(BotChannel.channel_id == channel_id))).scalar_one_or_none()
    if existing:
        existing.active = True
        existing.purpose = purpose
        await session.flush()
        return existing
    row = BotChannel(guild_id=guild_id, channel_id=channel_id, purpose=purpose)
    session.add(row)
    await session.flush()
    return row


async def notification_channel_ids(session: AsyncSession) -> list[int]:
    rows = (
        await session.execute(
            select(BotChannel.channel_id).where(BotChannel.purpose == "notifications", BotChannel.active.is_(True))
        )
    ).scalars().all()
    return list(rows)


async def get_channel_id_by_purpose(session: AsyncSession, purpose: str) -> Optional[int]:
    row = (
        await session.execute(
            select(BotChannel.channel_id).where(BotChannel.purpose == purpose, BotChannel.active.is_(True))
        )
    ).scalar_one_or_none()
    return row


async def all_registered_channels(session: AsyncSession) -> Sequence[BotChannel]:
    return (
        await session.execute(select(BotChannel).where(BotChannel.active.is_(True)))
    ).scalars().all()


# ----------------------------------------------------------------------
# Workflow statuses
# ----------------------------------------------------------------------
async def get_statuses(session: AsyncSession, entity_type: str) -> Sequence[WorkflowStatus]:
    return (
        await session.execute(
            select(WorkflowStatus)
            .where(WorkflowStatus.entity_type == entity_type, WorkflowStatus.active.is_(True))
            .order_by(WorkflowStatus.sort_order)
        )
    ).scalars().all()


async def status_codes(session: AsyncSession, entity_type: str) -> list[str]:
    return [s.code for s in await get_statuses(session, entity_type)]


async def is_valid_status(session: AsyncSession, entity_type: str, code: str) -> bool:
    return code in await status_codes(session, entity_type)


async def add_status(session: AsyncSession, entity_type: str, code: str, label: str,
                      is_terminal: bool = False) -> WorkflowStatus:
    row = WorkflowStatus(entity_type=entity_type, code=code.upper().replace(" ", "_"),
                          label=label, is_terminal=is_terminal, sort_order=999)
    session.add(row)
    await session.flush()
    return row


# ----------------------------------------------------------------------
# Locations
# ----------------------------------------------------------------------
async def get_location_by_code(session: AsyncSession, code: str) -> Optional[Location]:
    return (
        await session.execute(select(Location).where(Location.code == code.upper()))
    ).scalar_one_or_none()


async def add_location(session: AsyncSession, code: str, zone: Optional[str] = None,
                        description: Optional[str] = None) -> Location:
    existing = await get_location_by_code(session, code)
    if existing:
        existing.active = True
        return existing
    row = Location(code=code.upper(), zone=zone, description=description)
    session.add(row)
    await session.flush()
    return row


async def list_locations(session: AsyncSession) -> Sequence[Location]:
    return (
        await session.execute(select(Location).where(Location.active.is_(True)).order_by(Location.code))
    ).scalars().all()


# ----------------------------------------------------------------------
# Phone models / part types / test definitions / condition grades / expense categories
# ----------------------------------------------------------------------
async def get_or_create_phone_model(session: AsyncSession, manufacturer: str, model: str,
                                     variant: Optional[str] = None) -> PhoneModelConfig:
    stmt = select(PhoneModelConfig).where(
        PhoneModelConfig.manufacturer == manufacturer,
        PhoneModelConfig.model == model,
        PhoneModelConfig.variant == variant,
    )
    row = (await session.execute(stmt)).scalar_one_or_none()
    if row:
        return row
    row = PhoneModelConfig(manufacturer=manufacturer, model=model, variant=variant)
    session.add(row)
    await session.flush()
    return row


async def list_phone_models(session: AsyncSession) -> Sequence[PhoneModelConfig]:
    return (
        await session.execute(select(PhoneModelConfig).where(PhoneModelConfig.active.is_(True)).order_by(PhoneModelConfig.model))
    ).scalars().all()


async def get_or_create_part_type(session: AsyncSession, name: str) -> PartTypeConfig:
    row = (await session.execute(select(PartTypeConfig).where(PartTypeConfig.name == name))).scalar_one_or_none()
    if row:
        return row
    row = PartTypeConfig(name=name)
    session.add(row)
    await session.flush()
    return row


async def list_part_types(session: AsyncSession) -> Sequence[PartTypeConfig]:
    return (
        await session.execute(select(PartTypeConfig).where(PartTypeConfig.active.is_(True)).order_by(PartTypeConfig.name))
    ).scalars().all()


async def list_test_definitions(session: AsyncSession) -> Sequence[TestDefinition]:
    return (
        await session.execute(
            select(TestDefinition).where(TestDefinition.active.is_(True)).order_by(TestDefinition.sort_order)
        )
    ).scalars().all()


async def add_test_definition(session: AsyncSession, code: str, name: str) -> TestDefinition:
    row = TestDefinition(code=code.lower().replace(" ", "_"), name=name, sort_order=999)
    session.add(row)
    await session.flush()
    return row


async def list_condition_grades(session: AsyncSession) -> Sequence[ConditionGrade]:
    return (
        await session.execute(
            select(ConditionGrade).where(ConditionGrade.active.is_(True)).order_by(ConditionGrade.sort_order)
        )
    ).scalars().all()


async def list_expense_categories(session: AsyncSession) -> Sequence[ExpenseCategory]:
    return (
        await session.execute(select(ExpenseCategory).where(ExpenseCategory.active.is_(True)).order_by(ExpenseCategory.name))
    ).scalars().all()


async def get_pricing_rule(session: AsyncSession, phone_model_id: Optional[int]) -> Optional[PricingRule]:
    """Model-specific rule if one exists, else the global default (phone_model_id IS NULL)."""
    if phone_model_id is not None:
        row = (
            await session.execute(
                select(PricingRule).where(PricingRule.phone_model_id == phone_model_id, PricingRule.active.is_(True))
            )
        ).scalar_one_or_none()
        if row:
            return row
    return (
        await session.execute(
            select(PricingRule).where(PricingRule.phone_model_id.is_(None), PricingRule.active.is_(True))
        )
    ).scalar_one_or_none()
