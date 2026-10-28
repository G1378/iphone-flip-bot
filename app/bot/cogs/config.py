from __future__ import annotations

from decimal import Decimal
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from app.bot.autocomplete import phone_model_autocomplete
from app.bot.auth import require_admin, require_authorized
from app.bot.embeds import error_embed, success_embed
from app.config import settings
from app.db import session_scope
from app.services import config_service
from app.services import maintenance as maintenance_service

ADD_REMOVE_LIST = [
    app_commands.Choice(name="add", value="add"),
    app_commands.Choice(name="remove", value="remove"),
    app_commands.Choice(name="list", value="list"),
]


class WipeConfirmModal(discord.ui.Modal, title="Wipe ALL data?"):
    confirm = discord.ui.TextInput(label="Type WIPE to confirm", required=True, max_length=10)

    def __init__(self, include_config: bool):
        super().__init__()
        self.include_config = include_config

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if self.confirm.value.strip() != "WIPE":
            await interaction.response.send_message(embed=error_embed("You didn't type WIPE, so nothing was deleted."), ephemeral=True)
            return
        await interaction.response.defer(thinking=True)
        cleaned = await _delete_discord_leftovers(interaction)
        async with session_scope() as session:
            result = await maintenance_service.wipe_all_data(session, include_config=self.include_config)
        e = discord.Embed(title="🧹 All data wiped", colour=discord.Colour.orange())
        e.description = (f"Deleted **{result.total}** rows across {len(result.rows_deleted)} tables. "
                         "Phone, donor, part and order numbers start again from 1.\n"
                         + ("Settings and catalogs were reset to defaults. " if self.include_config
                            else "Settings, locations, part types, models and test checklist were kept. ")
                         + "Authorized users and registered channels were not touched.")
        if cleaned:
            e.add_field(name="Discord clean-up", value=cleaned, inline=False)
        await interaction.followup.send(embed=e)


async def _delete_discord_leftovers(interaction: discord.Interaction) -> str:
    """Best effort: remove the job channels and ticket messages the wipe is about to orphan.
    Anything that fails is left for a person to delete by hand."""
    from app.models.task_models import JobChannel, Task
    from sqlalchemy import select
    async with session_scope() as session:
        channel_ids = list((await session.execute(select(JobChannel.channel_id))).scalars().all())
        messages = [(c, m) for c, m in (await session.execute(
            select(Task.discord_channel_id, Task.discord_message_id).where(Task.discord_message_id.is_not(None)))).all() if c]
    client, deleted_channels, deleted_messages, failed = interaction.client, 0, 0, 0
    for cid in channel_ids:
        try:
            ch = client.get_channel(cid) or await client.fetch_channel(cid)
            await ch.delete(reason="/config wipe-data")
            deleted_channels += 1
        except Exception:
            failed += 1
    for cid, mid in messages:
        try:
            ch = client.get_channel(cid) or await client.fetch_channel(cid)
            await (await ch.fetch_message(mid)).delete()
            deleted_messages += 1
        except Exception:
            failed += 1
    if not (channel_ids or messages):
        return ""
    return f"Deleted {deleted_channels} job channel(s) and {deleted_messages} ticket message(s)" + (
        f"; {failed} couldn't be removed (already gone, or missing permission)." if failed else ".")


class ConfigCog(commands.GroupCog, group_name="config"):
    """Admin-only settings: users, locations, tests, pricing, eBay, Sheets."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ------------------------------------------------------------------
    @app_commands.command(name="wipe-data", description="DEV ONLY: delete every phone, part, order, sale and ticket and start again")
    @app_commands.describe(include_settings="Also reset settings, locations, models, part types and the test checklist to defaults")
    @require_admin()
    async def wipe_data(self, interaction: discord.Interaction, include_settings: bool = False):
        await interaction.response.send_modal(WipeConfirmModal(include_settings))

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
    @app_commands.command(name="tests", description="Manage the phone testing checklist and which part fixes each test")
    @app_commands.describe(
        name="Test name",
        part_type="Part flagged as needed when this test fails ('none' to clear). Re-run add on an existing test to change it.",
    )
    @app_commands.choices(action=ADD_REMOVE_LIST)
    @require_admin()
    async def tests(self, interaction: discord.Interaction, action: app_commands.Choice[str],
                    name: Optional[str] = None, part_type: Optional[str] = None):
        async with session_scope() as session:
            if action.value == "add":
                if not name:
                    await interaction.response.send_message(embed=error_embed("Provide a test name."), ephemeral=True)
                    return
                part = None
                if part_type and part_type.strip().lower() not in ("none", "-"):
                    part = await config_service.find_part_type(session, part_type)
                    if not part:
                        valid = ", ".join(p.name for p in await config_service.list_part_types(session))
                        await interaction.response.send_message(
                            embed=error_embed(f"Unknown part type `{part_type}`. Known part types: {valid}"), ephemeral=True)
                        return
                existing = await config_service.find_test_definition(session, name)
                if existing:
                    if part_type is None:
                        await interaction.response.send_message(
                            embed=error_embed(f"'{existing.name}' is already on the checklist. Pass `part_type` to change the part it flags."), ephemeral=True)
                        return
                    await config_service.set_test_part_type(session, existing, part)
                    msg = f"'{existing.name}' now flags **{part.name}** when it fails." if part else f"'{existing.name}' no longer maps to a part."
                    await interaction.response.send_message(embed=success_embed(msg))
                    return
                await config_service.add_test_definition(session, name, name, part_type_id=part.id if part else None)
                extra = f" (flags **{part.name}** when it fails)" if part else ""
                await interaction.response.send_message(embed=success_embed(f"Added test '{name}' to the checklist{extra}."))
            elif action.value == "list":
                defs = await config_service.list_test_definitions(session)
                e = discord.Embed(title="🧪 Testing checklist", colour=discord.Colour.blue())
                e.description = "\n".join(
                    f"• {d.name} → {d.part_type.name if d.part_type else '_no part_'}" for d in defs
                )[:4000] or "None configured."
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
    @app_commands.command(name="phone-models", description="Manage the phone model list: series, storage sizes, colours, prices")
    @app_commands.describe(
        model="Model name, e.g. 'iPhone 13 Pro'",
        default_sale_price="Default sale price",
        series="Which generation it belongs to, e.g. 'iPhone 13 series' (a new model goes under 'Other models' without one)",
        storage="Dropdown sizes, comma separated: 128GB,256GB,512GB",
        colours="Dropdown colours, comma separated: Midnight,Starlight,Blue")
    @app_commands.choices(action=ADD_REMOVE_LIST)
    @app_commands.autocomplete(model=phone_model_autocomplete)
    @require_admin()
    async def phone_models(self, interaction: discord.Interaction, action: app_commands.Choice[str],
                            model: Optional[str] = None, default_sale_price: Optional[float] = None,
                            series: Optional[str] = None, storage: Optional[str] = None, colours: Optional[str] = None):
        async with session_scope() as session:
            if action.value == "add":
                if not model:
                    await interaction.response.send_message(embed=error_embed("Provide a model name, e.g. 'iPhone 13 Pro'."), ephemeral=True)
                    return
                m = await config_service.get_or_create_phone_model(session, "Apple", model)
                if default_sale_price is not None:
                    m.default_sale_price = Decimal(str(default_sale_price))
                await config_service.set_model_options(
                    session, m, series=series,
                    storage=[x for x in storage.split(",")] if storage else None,
                    colours=[x for x in colours.split(",")] if colours else None)
                s_, c_ = config_service.model_options(m)
                await interaction.response.send_message(embed=success_embed(
                    f"Model '{m.model}' saved",
                    f"Series: {config_service.series_of(m)}\nStorage: {', '.join(s_) or '—'}\nColours: {', '.join(c_) or '—'}"
                    + (f"\nDefault price £{default_sale_price}" if default_sale_price else "")))
            else:
                groups = config_service.group_by_series(await config_service.list_phone_models(session))
                lines = []
                for name, ms in groups:   # oldest generation first, tiers ascending
                    lines.append(f"**{name}**")
                    lines += [f"• {m.model} (default price: £{m.default_sale_price or '—'})" for m in ms]
                text = "\n".join(lines) or "None configured."
                pages = [text[i:i + 3800] for i in range(0, len(text), 3800)] or [text]
                embeds = [discord.Embed(title="📱 Phone models" + (f" ({i + 1}/{len(pages)})" if len(pages) > 1 else ""),
                                        colour=discord.Colour.blue(), description=pg) for i, pg in enumerate(pages)]
                await interaction.response.send_message(embeds=embeds[:10])

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
    @app_commands.command(name="pricing-rules", description="Manually override sale price/thresholds for one model (wins over live /pricing data)")
    @app_commands.autocomplete(model=phone_model_autocomplete)
    @require_admin()
    async def pricing(self, interaction: discord.Interaction, model: str, default_sale_price: Optional[float] = None,
                       min_profit_gbp: Optional[float] = None, min_roi_pct: Optional[float] = None):
        async with session_scope() as session:
            try:
                phone_model = await config_service.resolve_phone_model(session, model)
            except config_service.UnknownPhoneModelError as e:
                await interaction.response.send_message(embed=error_embed(str(e)), ephemeral=True)
                return
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
    @app_commands.command(name="ebay-setup", description="Read your eBay postage/payment/returns policies + stock location and use them for listings")
    @app_commands.describe(postage_policy_id="Pick one if you have several", payment_policy_id="Pick one if you have several",
                           returns_policy_id="Pick one if you have several", location_key="Pick one if you have several",
                           postcode="No stock location on eBay yet? Create one at this postcode")
    @require_admin()
    async def ebay_setup(self, interaction: discord.Interaction, postage_policy_id: Optional[str] = None,
                         payment_policy_id: Optional[str] = None, returns_policy_id: Optional[str] = None,
                         location_key: Optional[str] = None, postcode: Optional[str] = None):
        from app.integrations.ebay.interface import EbayApiError, EbayNotConfiguredError
        from app.services import listings as listings_service
        await interaction.response.defer(thinking=True, ephemeral=True)
        try:
            async with session_scope() as session:
                result = await listings_service.configure_publishing(session, self.bot.ebay_client, {
                    "ebay_fulfillment_policy_id": postage_policy_id, "ebay_payment_policy_id": payment_policy_id,
                    "ebay_return_policy_id": returns_policy_id, "ebay_merchant_location_key": location_key,
                }, postcode=postcode)
        except EbayNotConfiguredError:
            await interaction.followup.send(embed=error_embed("eBay isn't connected yet (`/config ebay`)."), ephemeral=True)
            return
        except (EbayApiError, listings_service.ListingError) as e:
            await interaction.followup.send(embed=error_embed(str(e)), ephemeral=True)
            return
        names = {"ebay_fulfillment_policy_id": ("Postage policy", "postage_policy_id"),
                 "ebay_payment_policy_id": ("Payment policy", "payment_policy_id"),
                 "ebay_return_policy_id": ("Returns policy", "returns_policy_id"),
                 "ebay_merchant_location_key": ("Stock location", "location_key")}
        e = discord.Embed(title="eBay listing setup", colour=discord.Colour.blue())
        unresolved = False
        for key, (label, arg) in names.items():
            picked, options = result.chosen.get(key), result.options.get(key, [])
            if picked:
                e.add_field(name=f"✅ {label}", value=f"{picked.name}", inline=True)
            else:
                unresolved = True
                listing = ", ".join(f"{o.name} (`{o.id}`)" for o in options) or "none found on your eBay account"
                e.add_field(name=f"❌ {label}", value=f"Choose with `{arg}`: {listing}"[:1024], inline=False)
        if result.created_location:
            e.set_footer(text="Created a stock location called 'Workshop'.")
        elif unresolved:
            e.set_footer(text="Run it again, adding the ones that need choosing. No stock location? Add postcode:")
        await interaction.followup.send(embed=e, ephemeral=True)

    @app_commands.command(name="ebay", description="Show eBay integration configuration status")
    @require_admin()
    async def ebay(self, interaction: discord.Interaction):
        configured = settings.ebay_configured
        buy_configured = settings.ebay_buy_apis_configured
        e = discord.Embed(title="eBay configuration", colour=discord.Colour.green() if configured else discord.Colour.orange())
        e.add_field(name="Listings & orders (Sell APIs)", value="✅ Configured" if configured else "❌ Not configured", inline=False)
        e.add_field(name="Pricing lookups (Buy APIs)", value="✅ Configured" if buy_configured else "❌ Not configured", inline=False)
        e.add_field(name="Environment", value=settings.ebay_env, inline=True)
        e.add_field(name="Marketplace", value=settings.ebay_marketplace_id, inline=True)
        if not buy_configured:
            e.add_field(
                name="To enable pricing (/pricing)", value="Set `EBAY_CLIENT_ID` and `EBAY_CLIENT_SECRET` "
                                                             "in the bot's `.env` file — no user OAuth needed for this part.",
                inline=False,
            )
        if buy_configured and not configured:
            e.add_field(
                name="To enable listings/orders (/list, /ebay-sync)",
                value="Also set `EBAY_REFRESH_TOKEN` — run `python scripts/ebay_oauth_setup.py` once (see README).",
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
