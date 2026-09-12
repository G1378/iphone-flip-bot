from __future__ import annotations

from decimal import Decimal
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from app.bot.auth import require_admin, require_authorized
from app.bot.embeds import error_embed, success_embed
from app.config import settings
from app.db import session_scope
from app.services import config_service

ADD_REMOVE_LIST = [
    app_commands.Choice(name="add", value="add"),
    app_commands.Choice(name="remove", value="remove"),
    app_commands.Choice(name="list", value="list"),
]


class ConfigCog(commands.GroupCog, group_name="config"):
    """Admin-only settings: users, locations, tests, pricing, eBay, Sheets."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ------------------------------------------------------------------
    @app_commands.command(name="business", description="View or set a business setting (name, currency, fee assumptions, etc.)")
    @require_admin()
    async def business(self, interaction: discord.Interaction, key: Optional[str] = None, value: Optional[str] = None):
        async with session_scope() as session:
            if key and value is not None:
                value_type = "number" if _looks_numeric(value) else "string"
                await config_service.set_setting(session, key, value, value_type=value_type)
                await interaction.response.send_message(embed=success_embed(f"Set `{key}` = `{value}`"))
                return
            all_settings = await config_service.all_settings(session)
        e = discord.Embed(title="⚙️ Business settings", colour=discord.Colour.blue())
        for s in all_settings:
            e.add_field(name=s.key, value=s.value, inline=True)
        e.set_footer(text="Use /config business key:<name> value:<new value> to change one.")
        await interaction.response.send_message(embed=e)

    # ------------------------------------------------------------------
    @app_commands.command(name="users", description="Manage who is allowed to use this bot")
    @app_commands.choices(action=ADD_REMOVE_LIST)
    @require_admin()
    async def users(self, interaction: discord.Interaction, action: app_commands.Choice[str],
                     user: Optional[discord.User] = None, admin: bool = False):
        async with session_scope() as session:
            if action.value == "add":
                if not user:
                    await interaction.response.send_message(embed=error_embed("Provide a user to add."), ephemeral=True)
                    return
                await config_service.add_authorized_user(session, user.id, str(user), interaction.user.id, admin)
                await interaction.response.send_message(embed=success_embed(f"Authorized {user.mention}" + (" as admin" if admin else "")))
            elif action.value == "remove":
                if not user:
                    await interaction.response.send_message(embed=error_embed("Provide a user to remove."), ephemeral=True)
                    return
                removed = await config_service.remove_authorized_user(session, user.id)
                await interaction.response.send_message(embed=success_embed(f"Removed {user.mention}" if removed else "That user wasn't authorized."))
            else:
                users_list = await config_service.list_authorized_users(session)
                e = discord.Embed(title="👥 Authorized users", colour=discord.Colour.blue())
                e.description = "\n".join(f"<@{u.discord_user_id}> {'(admin)' if u.is_admin else ''}" for u in users_list) or "None configured yet."
                await interaction.response.send_message(embed=e)

    # ------------------------------------------------------------------
    @app_commands.command(name="channels", description="Set which channel receives sale/error notifications")
    @require_admin()
    async def channels(self, interaction: discord.Interaction, channel: discord.TextChannel, purpose: str = "notifications"):
        async with session_scope() as session:
            await config_service.add_channel(session, interaction.guild_id, channel.id, purpose=purpose)
        await interaction.response.send_message(embed=success_embed(f"{channel.mention} registered for `{purpose}`"))

    # ------------------------------------------------------------------
    @app_commands.command(name="locations", description="Manage physical storage locations (e.g. B1, C2)")
    @app_commands.choices(action=ADD_REMOVE_LIST)
    @require_admin()
    async def locations(self, interaction: discord.Interaction, action: app_commands.Choice[str],
                         code: Optional[str] = None, zone: Optional[str] = None):
        async with session_scope() as session:
            if action.value == "add":
                if not code:
                    await interaction.response.send_message(embed=error_embed("Provide a location code, e.g. B1."), ephemeral=True)
                    return
                await config_service.add_location(session, code, zone=zone)
                await interaction.response.send_message(embed=success_embed(f"Location {code.upper()} added."))
            elif action.value == "list":
                locs = await config_service.list_locations(session)
                e = discord.Embed(title="📍 Locations", colour=discord.Colour.blue())
                e.description = "\n".join(f"**{l.code}** — {l.zone or ''}" for l in locs) or "None configured."
                await interaction.response.send_message(embed=e)
            else:
                await interaction.response.send_message(embed=error_embed("Locations are soft-deactivated by convention only; not implemented for remove in this MVP."), ephemeral=True)

    # ------------------------------------------------------------------
    @app_commands.command(name="tests", description="Manage the phone testing checklist")
    @app_commands.choices(action=ADD_REMOVE_LIST)
    @require_admin()
    async def tests(self, interaction: discord.Interaction, action: app_commands.Choice[str], name: Optional[str] = None):
        async with session_scope() as session:
            if action.value == "add":
                if not name:
                    await interaction.response.send_message(embed=error_embed("Provide a test name."), ephemeral=True)
                    return
                await config_service.add_test_definition(session, name, name)
                await interaction.response.send_message(embed=success_embed(f"Added test '{name}' to the checklist."))
            elif action.value == "list":
                defs = await config_service.list_test_definitions(session)
                e = discord.Embed(title="🧪 Testing checklist", colour=discord.Colour.blue())
                e.description = "\n".join(f"• {d.name}" for d in defs) or "None configured."
                await interaction.response.send_message(embed=e)
            else:
                if not name:
                    await interaction.response.send_message(embed=error_embed("Provide the test name to remove."), ephemeral=True)
                    return
                from sqlalchemy import select
                from app.models.config_models import TestDefinition
                row = (await session.execute(select(TestDefinition).where(TestDefinition.name.ilike(name)))).scalar_one_or_none()
                if row:
                    row.active = False
                    await interaction.response.send_message(embed=success_embed(f"Removed test '{row.name}'."))
                else:
                    await interaction.response.send_message(embed=error_embed("Test not found."), ephemeral=True)

    # ------------------------------------------------------------------
    @app_commands.command(name="part-types", description="Manage part type catalog")
    @app_commands.choices(action=ADD_REMOVE_LIST)
    @require_admin()
    async def part_types(self, interaction: discord.Interaction, action: app_commands.Choice[str], name: Optional[str] = None):
        async with session_scope() as session:
            if action.value == "add":
                if not name:
                    await interaction.response.send_message(embed=error_embed("Provide a part type name."), ephemeral=True)
                    return
                await config_service.get_or_create_part_type(session, name)
                await interaction.response.send_message(embed=success_embed(f"Added part type '{name}'."))
            else:
                types = await config_service.list_part_types(session)
                e = discord.Embed(title="🔩 Part types", colour=discord.Colour.blue())
                e.description = "\n".join(f"• {t.name}" for t in types) or "None configured."
                await interaction.response.send_message(embed=e)

    # ------------------------------------------------------------------
    @app_commands.command(name="phone-models", description="Manage phone model catalog and default sale prices")
    @app_commands.choices(action=ADD_REMOVE_LIST)
    @require_admin()
    async def phone_models(self, interaction: discord.Interaction, action: app_commands.Choice[str],
                            model: Optional[str] = None, default_sale_price: Optional[float] = None):
        async with session_scope() as session:
            if action.value == "add":
                if not model:
                    await interaction.response.send_message(embed=error_embed("Provide a model name, e.g. 'iPhone 13 Pro'."), ephemeral=True)
                    return
                m = await config_service.get_or_create_phone_model(session, "Apple", model)
                if default_sale_price is not None:
                    m.default_sale_price = Decimal(str(default_sale_price))
                await interaction.response.send_message(embed=success_embed(f"Model '{model}' saved" + (f" (default price £{default_sale_price})" if default_sale_price else "")))
            else:
                models = await config_service.list_phone_models(session)
                e = discord.Embed(title="📱 Phone models", colour=discord.Colour.blue())
                e.description = "\n".join(f"• {m.model} (default price: £{m.default_sale_price or '—'})" for m in models) or "None configured."
                await interaction.response.send_message(embed=e)

    # ------------------------------------------------------------------
    @app_commands.command(name="expense-types", description="Manage expense categories")
    @app_commands.choices(action=ADD_REMOVE_LIST)
    @require_admin()
    async def expense_types(self, interaction: discord.Interaction, action: app_commands.Choice[str], name: Optional[str] = None):
        async with session_scope() as session:
            if action.value == "add":
                if not name:
                    await interaction.response.send_message(embed=error_embed("Provide an expense category name."), ephemeral=True)
                    return
                from app.models.config_models import ExpenseCategory
                from sqlalchemy import select
                existing = (await session.execute(select(ExpenseCategory).where(ExpenseCategory.name.ilike(name)))).scalar_one_or_none()
                if not existing:
                    session.add(ExpenseCategory(name=name))
                await interaction.response.send_message(embed=success_embed(f"Expense category '{name}' saved."))
            else:
                cats = await config_service.list_expense_categories(session)
                e = discord.Embed(title="🧾 Expense categories", colour=discord.Colour.blue())
                e.description = "\n".join(f"• {c.name}" for c in cats) or "None configured."
                await interaction.response.send_message(embed=e)

    # ------------------------------------------------------------------
    @app_commands.command(name="statuses", description="Manage workflow statuses for an entity type")
    @app_commands.choices(action=ADD_REMOVE_LIST, entity_type=[
        app_commands.Choice(name="PHONE", value="PHONE"), app_commands.Choice(name="PART", value="PART"),
        app_commands.Choice(name="DONOR", value="DONOR"), app_commands.Choice(name="REPAIR", value="REPAIR"),
        app_commands.Choice(name="ORDER", value="ORDER"), app_commands.Choice(name="LISTING", value="LISTING"),
    ])
    @require_admin()
    async def statuses(self, interaction: discord.Interaction, action: app_commands.Choice[str],
                        entity_type: app_commands.Choice[str], code: Optional[str] = None):
        async with session_scope() as session:
            if action.value == "add":
                if not code:
                    await interaction.response.send_message(embed=error_embed("Provide a status code."), ephemeral=True)
                    return
                await config_service.add_status(session, entity_type.value, code, code.title())
                await interaction.response.send_message(embed=success_embed(f"Added {entity_type.value} status '{code.upper()}'."))
            else:
                codes = await config_service.status_codes(session, entity_type.value)
                e = discord.Embed(title=f"🔀 {entity_type.value} statuses", colour=discord.Colour.blue())
                e.description = "\n".join(f"• {c}" for c in codes) or "None configured."
                await interaction.response.send_message(embed=e)

    # ------------------------------------------------------------------
    @app_commands.command(name="thresholds", description="Set repair decision thresholds (min profit, min ROI, target margin)")
    @require_admin()
    async def thresholds(self, interaction: discord.Interaction, min_profit_gbp: Optional[float] = None,
                          min_roi_pct: Optional[float] = None, target_margin_pct: Optional[float] = None):
        async with session_scope() as session:
            if min_profit_gbp is not None:
                await config_service.set_setting(session, "min_profit_gbp", str(min_profit_gbp), "number")
            if min_roi_pct is not None:
                await config_service.set_setting(session, "min_roi_pct", str(min_roi_pct), "number")
            if target_margin_pct is not None:
                await config_service.set_setting(session, "target_margin_pct", str(target_margin_pct), "number")
            current_profit = await config_service.get_setting(session, "min_profit_gbp")
            current_roi = await config_service.get_setting(session, "min_roi_pct")
            current_margin = await config_service.get_setting(session, "target_margin_pct")
        e = discord.Embed(title="🎯 Repair decision thresholds", colour=discord.Colour.blue())
        e.add_field(name="Min profit", value=f"£{current_profit}", inline=True)
        e.add_field(name="Min ROI", value=f"{current_roi}%", inline=True)
        e.add_field(name="Target margin", value=f"{current_margin}%", inline=True)
        await interaction.response.send_message(embed=e)

    # ------------------------------------------------------------------
    @app_commands.command(name="pricing", description="Set per-model pricing rule overrides")
    @require_admin()
    async def pricing(self, interaction: discord.Interaction, model: str, default_sale_price: Optional[float] = None,
                       min_profit_gbp: Optional[float] = None, min_roi_pct: Optional[float] = None):
        async with session_scope() as session:
            phone_model = await config_service.get_or_create_phone_model(session, "Apple", model)
            from app.models.config_models import PricingRule
            from sqlalchemy import select
            rule = (await session.execute(select(PricingRule).where(PricingRule.phone_model_id == phone_model.id))).scalar_one_or_none()
            if not rule:
                rule = PricingRule(phone_model_id=phone_model.id)
                session.add(rule)
            if default_sale_price is not None:
                rule.default_sale_price = Decimal(str(default_sale_price))
            if min_profit_gbp is not None:
                rule.min_profit_gbp = Decimal(str(min_profit_gbp))
            if min_roi_pct is not None:
                rule.min_roi_pct = Decimal(str(min_roi_pct))
        await interaction.response.send_message(embed=success_embed(f"Pricing rule saved for {model}"))

    # ------------------------------------------------------------------
    @app_commands.command(name="ebay", description="Show eBay integration configuration status")
    @require_admin()
    async def ebay(self, interaction: discord.Interaction):
        configured = settings.ebay_configured
        e = discord.Embed(title="eBay configuration", colour=discord.Colour.green() if configured else discord.Colour.orange())
        e.add_field(name="Status", value="✅ Configured" if configured else "❌ Not configured", inline=False)
        e.add_field(name="Environment", value=settings.ebay_env, inline=True)
        e.add_field(name="Marketplace", value=settings.ebay_marketplace_id, inline=True)
        if not configured:
            e.add_field(
                name="To configure", value="Set `EBAY_CLIENT_ID`, `EBAY_CLIENT_SECRET` and `EBAY_REFRESH_TOKEN` "
                                            "in the bot's `.env` file (see README) — credentials never go through Discord.",
                inline=False,
            )
        await interaction.response.send_message(embed=e, ephemeral=True)

    # ------------------------------------------------------------------
    @app_commands.command(name="sheets", description="Show Google Sheets integration configuration status")
    @require_admin()
    async def sheets(self, interaction: discord.Interaction):
        configured = settings.sheets_configured
        e = discord.Embed(title="Google Sheets configuration", colour=discord.Colour.green() if configured else discord.Colour.orange())
        e.add_field(name="Status", value="✅ Configured" if configured else "❌ Not configured", inline=False)
        if settings.google_sheets_spreadsheet_id:
            e.add_field(name="Spreadsheet ID", value=settings.google_sheets_spreadsheet_id, inline=False)
        if not configured:
            e.add_field(
                name="To configure", value="Set `GOOGLE_SHEETS_SPREADSHEET_ID` and place a service-account JSON at "
                                            "`GOOGLE_SERVICE_ACCOUNT_FILE` (see README) — credentials never go through Discord.",
                inline=False,
            )
        await interaction.response.send_message(embed=e, ephemeral=True)

    # ------------------------------------------------------------------
    @app_commands.command(name="reset-defaults", description="Re-seed default statuses/tests/part types/grades if missing")
    @require_admin()
    async def reset_defaults(self, interaction: discord.Interaction):
        async with session_scope() as session:
            await config_service.seed_defaults_if_empty(session)
        await interaction.response.send_message(embed=success_embed("Defaults ensured (only adds what's missing, never overwrites existing config)."))


def _looks_numeric(value: str) -> bool:
    try:
        float(value)
        return True
    except ValueError:
        return False


async def setup(bot: commands.Bot):
    await bot.add_cog(ConfigCog(bot))
