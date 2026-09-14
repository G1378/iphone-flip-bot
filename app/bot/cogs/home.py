from __future__ import annotations

import datetime as dt
from decimal import Decimal, InvalidOperation
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from app.bot.auth import require_admin, require_authorized
from app.bot.embeds import error_embed, success_embed
from app.db import session_scope
from app.services import config_service
from app.services import donors as donors_service
from app.services import finance as finance_service
from app.services import phones as phones_service

# (purpose key, channel name, colour emoji used in name, cheat-sheet of commands)
CHANNEL_PLAN: list[tuple[str, str, str]] = [
    ("home", "🏠-home", "This is your control panel. Run `/home` any time to refresh it."),
    ("inventory", "📱-inventory", "`/buy` `/stock` `/phone` `/edit-phone` `/move` `/status` `/fault` `/testing` `/test` `/test-result` `/search`"),
    ("donors-parts", "🔩-donors-parts", "`/donor` `/donors` `/teardown` `/donor-parts` `/allocate-cost` `/parts` `/part` `/find-part` `/reserve-part` `/install-part` `/remove-part` `/scrap-part` `/move-part` `/parts-needed`"),
    ("repairs", "🔧-repairs", "`/repair` `/repair-status` `/analyse` `/order` `/orders` `/receive`"),
    ("finance", "💷-finance", "`/report` `/stock-value` `/profit` `/sales` `/expenses`"),
    ("ebay-listings", "🏷️-ebay-listings", "`/list` `/listing` `/ebay-sync` `/ebay-status`"),
    ("admin", "⚙️-admin", "`/config business` `/config users` `/config channels` `/config locations` `/config tests` `/config part-types` `/config phone-models` `/config expense-types` `/config statuses` `/config thresholds` `/config pricing` `/config ebay` `/config sheets` `/config reset-defaults` `/demo`"),
    ("notifications", "🔔-notifications", "Automatic only: sale alerts, sync results, and error reports post here — no commands needed."),
]
CHANNEL_LOOKUP = {key: (name, cheat) for key, name, cheat in CHANNEL_PLAN}

# Which channels get a jump-button on the home dashboard, and in what order.
JUMP_ORDER = ["inventory", "donors-parts", "repairs", "finance", "ebay-listings", "admin", "notifications"]


async def _build_home_embed(session, bot) -> discord.Embed:
    business_name = await config_service.get_setting(session, "business_name", "My Phone Business")
    currency = await config_service.get_setting(session, "currency_symbol", "£")

    stock = await finance_service.stock_value(session)
    awaiting_parts = len(await phones_service.list_stock(session, status="AWAITING_PARTS"))
    donors_awaiting = len(await donors_service.list_donors(session, status="AWAITING_TEARDOWN"))

    today = dt.date.today()
    month_report = await finance_service.period_report(session, today.replace(day=1), today)

    e = discord.Embed(
        title=f"🏠 {business_name} — Control Panel",
        description="Configure the bot or jump to a channel using the buttons below. Nothing here runs typed commands for you.",
        colour=discord.Colour.blurple(),
    )
    e.add_field(name="Stock value", value=f"{currency}{stock['total']}", inline=True)
    e.add_field(name="Awaiting parts", value=str(awaiting_parts), inline=True)
    e.add_field(name="Donors awaiting teardown", value=str(donors_awaiting), inline=True)
    e.add_field(name="This month's net profit", value=f"{currency}{month_report.net_profit}", inline=True)
    e.add_field(name="eBay", value="✅ Configured" if bot.ebay_client.configured else "❌ Not configured", inline=True)
    e.add_field(name="Google Sheets", value="✅ Configured" if bot.sheets_client.configured else "❌ Not configured", inline=True)
    e.set_footer(text="Last refreshed")
    e.timestamp = dt.datetime.now(dt.timezone.utc)
    return e


async def _build_home_view(session, guild_id: int, invoker_id: int) -> "HomeView":
    channel_ids: dict[str, int] = {}
    for key in JUMP_ORDER:
        channel_id = await config_service.get_channel_id_by_purpose(session, key)
        if channel_id:
            channel_ids[key] = channel_id
    return HomeView(guild_id, invoker_id, channel_ids)


class BusinessSettingsModal(discord.ui.Modal, title="Business settings"):
    business_name = discord.ui.TextInput(label="Business name", required=False, max_length=100)
    default_postage_cost = discord.ui.TextInput(label="Default postage cost", required=False, max_length=10)
    default_packaging_cost = discord.ui.TextInput(label="Default packaging cost", required=False, max_length=10)
    min_profit_gbp = discord.ui.TextInput(label="Minimum acceptable profit", required=False, max_length=10)
    min_roi_pct = discord.ui.TextInput(label="Minimum acceptable ROI %", required=False, max_length=10)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        updates = {
            "business_name": (self.business_name.value, "string"),
            "default_postage_cost": (self.default_postage_cost.value, "number"),
            "default_packaging_cost": (self.default_packaging_cost.value, "number"),
            "min_profit_gbp": (self.min_profit_gbp.value, "number"),
            "min_roi_pct": (self.min_roi_pct.value, "number"),
        }
        changed = []
        async with session_scope() as session:
            for key, (value, value_type) in updates.items():
                if value.strip():
                    if value_type == "number":
                        try:
                            Decimal(value.strip())
                        except InvalidOperation:
                            await interaction.response.send_message(
                                embed=error_embed(f"'{value}' isn't a valid number for {key}."), ephemeral=True,
                            )
                            return
                    await config_service.set_setting(session, key, value.strip(), value_type)
                    changed.append(key)
        if changed:
            await interaction.response.send_message(embed=success_embed("Business settings updated", ", ".join(changed)), ephemeral=True)
        else:
            await interaction.response.send_message("Nothing changed — leave a field blank to keep its current value.", ephemeral=True)


class AddLocationModal(discord.ui.Modal, title="Add a storage location"):
    code = discord.ui.TextInput(label="Location code (e.g. B1)", required=True, max_length=30)
    zone = discord.ui.TextInput(label="Zone / shelf (e.g. Shelf B)", required=False, max_length=50)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        async with session_scope() as session:
            await config_service.add_location(session, self.code.value, zone=self.zone.value or None)
        await interaction.response.send_message(embed=success_embed(f"Location {self.code.value.upper()} added."), ephemeral=True)


class AuthorizeUserSelect(discord.ui.UserSelect):
    def __init__(self):
        super().__init__(placeholder="Select a user to authorize...", min_values=1, max_values=1)

    async def callback(self, interaction: discord.Interaction):
        user = self.values[0]
        async with session_scope() as session:
            await config_service.add_authorized_user(session, user.id, str(user), interaction.user.id, is_admin_flag=False)
        await interaction.response.edit_message(
            content=f"✅ {user.mention} can now use the bot (non-admin). "
                    f"To grant admin rights instead, use `/config users action:add user:{user} admin:true`.",
            view=None,
        )


class AuthorizeUserView(discord.ui.View):
    def __init__(self, invoker_id: int):
        super().__init__(timeout=120)
        self.invoker_id = invoker_id
        self.add_item(AuthorizeUserSelect())

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.invoker_id


class HomeView(discord.ui.View):
    def __init__(self, guild_id: int, invoker_id: int, channel_ids: dict[str, int]):
        super().__init__(timeout=None)  # the home dashboard is meant to stay usable indefinitely
        self.invoker_id = invoker_id

        # Row 0/1: jump-to-channel link buttons (up to 5 per row, Discord's link buttons need no auth check)
        row = 0
        col = 0
        for key in JUMP_ORDER:
            channel_id = channel_ids.get(key)
            if not channel_id:
                continue
            name, _ = CHANNEL_LOOKUP[key]
            url = f"https://discord.com/channels/{guild_id}/{channel_id}"
            self.add_item(discord.ui.Button(style=discord.ButtonStyle.link, label=name.replace("-", " ").strip(), url=url, row=row))
            col += 1
            if col >= 5:
                col = 0
                row += 1
                if row >= 2:
                    break  # keep well within Discord's 5-row limit

    async def _require_admin(self, interaction: discord.Interaction) -> bool:
        async with session_scope() as session:
            ok = await config_service.is_admin(session, interaction.user.id)
        if not ok:
            await interaction.response.send_message("This action requires admin privileges.", ephemeral=True)
        return ok

    @discord.ui.button(label="⚙️ Business Settings", style=discord.ButtonStyle.primary, row=2)
    async def business_settings(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._require_admin(interaction):
            return
        await interaction.response.send_modal(BusinessSettingsModal())

    @discord.ui.button(label="📍 Add Location", style=discord.ButtonStyle.primary, row=2)
    async def add_location(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._require_admin(interaction):
            return
        await interaction.response.send_modal(AddLocationModal())

    @discord.ui.button(label="👥 Authorize User", style=discord.ButtonStyle.primary, row=2)
    async def authorize_user(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._require_admin(interaction):
            return
        await interaction.response.send_message("Pick a user to authorize:", view=AuthorizeUserView(interaction.user.id), ephemeral=True)

    @discord.ui.button(label="🔄 Refresh", style=discord.ButtonStyle.secondary, row=2)
    async def refresh(self, interaction: discord.Interaction, button: discord.ui.Button):
        async with session_scope() as session:
            embed = await _build_home_embed(session, interaction.client)
            view = await _build_home_view(session, interaction.guild_id, interaction.user.id)
        await interaction.response.edit_message(embed=embed, view=view)

    @discord.ui.button(label="eBay Status", style=discord.ButtonStyle.secondary, row=3)
    async def ebay_status(self, interaction: discord.Interaction, button: discord.ui.Button):
        bot = interaction.client
        configured = bot.ebay_client.configured
        e = discord.Embed(title="eBay integration status", colour=discord.Colour.green() if configured else discord.Colour.red())
        e.add_field(name="Configured", value="✅ Yes" if configured else "❌ No", inline=False)
        job = bot.job_scheduler.state.ebay if bot.job_scheduler else None
        if job and job.last_run_at:
            e.add_field(name="Last sync", value=str(job.last_run_at), inline=True)
            e.add_field(name="Result", value=job.last_summary or job.last_error or "—", inline=False)
        await interaction.response.send_message(embed=e, ephemeral=True)

    @discord.ui.button(label="Sheets Status", style=discord.ButtonStyle.secondary, row=3)
    async def sheets_status(self, interaction: discord.Interaction, button: discord.ui.Button):
        bot = interaction.client
        configured = bot.sheets_client.configured
        e = discord.Embed(title="Google Sheets status", colour=discord.Colour.green() if configured else discord.Colour.red())
        e.add_field(name="Configured", value="✅ Yes" if configured else "❌ No", inline=False)
        job = bot.job_scheduler.state.sheets if bot.job_scheduler else None
        if job and job.last_run_at:
            e.add_field(name="Last sync", value=str(job.last_run_at), inline=True)
            e.add_field(name="Result", value=job.last_summary or job.last_error or "—", inline=False)
        await interaction.response.send_message(embed=e, ephemeral=True)

    @discord.ui.button(label="📊 Monthly Report", style=discord.ButtonStyle.secondary, row=3)
    async def monthly_report(self, interaction: discord.Interaction, button: discord.ui.Button):
        today = dt.date.today()
        async with session_scope() as session:
            r = await finance_service.period_report(session, today.replace(day=1), today)
            currency = await config_service.get_setting(session, "currency_symbol", "£")
        e = discord.Embed(title=f"📊 This month ({today.strftime('%B %Y')})", colour=discord.Colour.blue())
        e.add_field(name="Revenue", value=f"{currency}{r.revenue}", inline=True)
        e.add_field(name="Net profit", value=f"{currency}{r.net_profit}", inline=True)
        e.add_field(name="ROI", value=f"{r.roi_pct}%", inline=True)
        e.add_field(name="Phones sold", value=str(r.phones_sold), inline=True)
        e.add_field(name="Phones purchased", value=str(r.phones_purchased), inline=True)
        e.add_field(name="Avg profit/phone", value=f"{currency}{r.avg_profit_per_phone}", inline=True)
        await interaction.response.send_message(embed=e, ephemeral=True)


class HomeCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="setup-channels", description="[Admin] Create the standard set of channels for this bot")
    @require_admin()
    async def setup_channels(self, interaction: discord.Interaction):
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(embed=error_embed("Run this inside a server, not a DM."), ephemeral=True)
            return

        await interaction.response.defer(thinking=True)
        created_mentions = []
        async with session_scope() as session:
            for key, name, cheat_sheet in CHANNEL_PLAN:
                existing_id = await config_service.get_channel_id_by_purpose(session, key)
                channel = guild.get_channel(existing_id) if existing_id else None

                if channel is None:
                    try:
                        channel = await guild.create_text_channel(name)
                    except discord.Forbidden:
                        await interaction.followup.send(embed=error_embed(
                            "I don't have permission to create channels here. Grant me the **Manage Channels** "
                            "permission (Server Settings → Roles), or create channels yourself and register each "
                            "one with `/config channels`."
                        ))
                        return
                    await config_service.add_channel(session, guild.id, channel.id, purpose=key)
                    try:
                        pin_msg = await channel.send(embed=discord.Embed(
                            title=name, description=cheat_sheet, colour=discord.Colour.blurple(),
                        ))
                        await pin_msg.pin()
                    except discord.HTTPException:
                        pass
                    created_mentions.append(channel.mention)
                else:
                    await config_service.add_channel(session, guild.id, channel.id, purpose=key)

            home_channel_id = await config_service.get_channel_id_by_purpose(session, "home")

        if created_mentions:
            summary = "\n".join(created_mentions)
        else:
            summary = "All standard channels already existed and are now registered."
        e = discord.Embed(title="✅ Channels set up", description=summary, colour=discord.Colour.green())
        if home_channel_id:
            e.add_field(name="Next step", value=f"Run `/home` in <#{home_channel_id}> to post the control panel.", inline=False)
        await interaction.followup.send(embed=e)

    @app_commands.command(name="home", description="Show the control panel: quick config + jump to other channels")
    @require_authorized()
    async def home(self, interaction: discord.Interaction):
        if interaction.guild_id is None:
            await interaction.response.send_message(embed=error_embed("Run this inside a server, not a DM."), ephemeral=True)
            return
        async with session_scope() as session:
            embed = await _build_home_embed(session, self.bot)
            view = await _build_home_view(session, interaction.guild_id, interaction.user.id)
        await interaction.response.send_message(embed=embed, view=view)


async def setup(bot: commands.Bot):
    await bot.add_cog(HomeCog(bot))
