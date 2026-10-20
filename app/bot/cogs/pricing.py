from __future__ import annotations

from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from app.bot.auth import require_admin, require_authorized
from app.bot.embeds import error_embed, success_embed
from app.db import session_scope
from app.integrations.ebay.interface import EbayApiError, EbayNotConfiguredError
from app.jobs.pricing_sync_job import run_pricing_refresh
from app.services import config_service
from app.services import pricing as pricing_service


def _pounds(value) -> str:
    return f"£{value:,.0f}"


def _nice_model(name: str) -> str:
    return name.replace("Iphone", "iPhone").replace("iphone", "iPhone")


def _basis(obs) -> str:
    kind = "recent sales" if obs.source.value == "SOLD" else "current eBay listings"
    return f"based on {obs.sample_count} {kind}"


def _entry_text(rec) -> str:
    lines: list[str] = []

    # --- Working phone ---
    lines.append("**Working phone**")
    if rec.used_observation is None:
        lines.append("No price data yet. Run `/pricing refresh`.")
    else:
        lines.append(f"Sells for about **{_pounds(rec.used_observation.median_price)}** ({_basis(rec.used_observation)})")
        buy = rec.recommended_buy_used
        if buy is not None and buy > 0:
            lines.append(f"✅ Buy for up to **{_pounds(buy)}**")
        else:
            lines.append("❌ Not worth buying at today's prices")

    # --- Faulty phone ---
    lines.append("")
    lines.append("**Faulty / for parts**")
    if rec.faulty_observation is None:
        lines.append("No price data yet. Run `/pricing refresh`.")
    else:
        lines.append(f"Sells for about **{_pounds(rec.faulty_observation.median_price)}** as-is ({_basis(rec.faulty_observation)})")
    buy_f = rec.recommended_buy_faulty
    if rec.used_observation is not None:
        repair = _pounds(rec.repair_cost_assumption)
        repair_note = "your average repair cost" if rec.repair_cost_is_historical else "assumed repair cost"
        if buy_f is None or buy_f <= 0:
            lines.append("❌ Not worth buying at today's prices")
        else:
            lines.append(f"✅ Buy for up to **{_pounds(buy_f)}** if you repair it and resell it as a working phone ({repair_note}: {repair})")
            if rec.faulty_observation is not None and buy_f > rec.faulty_observation.median_price:
                lines.append(
                    f"⚠️ That is more than faulty ones normally sell for. "
                    f"Only pay it if the repair really costs {repair} or less."
                )
    return "\n".join(lines)


async def _build_table_embeds(session) -> list[discord.Embed]:
    entries = await pricing_service.list_watchlist(session)
    if not entries:
        return [discord.Embed(
            title="📈 Pricing",
            description="Nothing on the watchlist yet. Use `/pricing watch` to add a phone model.",
            colour=discord.Colour.blue(),
        )]

    embeds: list[discord.Embed] = []
    for i in range(0, len(entries), 10):  # keep each embed comfortably under Discord's size limits
        embed = discord.Embed(
            title="📈 Pricing" if i == 0 else "📈 Pricing (continued)",
            colour=discord.Colour.blue(),
        )
        for entry in entries[i:i + 10]:
            rec = await pricing_service.compute_recommendation(session, entry)
            storage = entry.storage or "any storage"
            embed.add_field(
                name=f"📱 {_nice_model(entry.phone_model.display_name())} · {storage}",
                value=_entry_text(rec),
                inline=False,
            )
        embeds.append(embed)

    embeds[-1].set_footer(text=(
        "\"Buy for up to\" is the most you should pay and still make your minimum profit after "
        "eBay fees and postage. \"Sells for about\" is the median price."
    ))
    return embeds


class RefreshButton(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="🔄 Refresh now", style=discord.ButtonStyle.primary, custom_id="pricing:refresh")
    async def refresh(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(thinking=True)
        bot = interaction.client
        try:
            async with session_scope() as session:
                await run_pricing_refresh(session, bot.ebay_client, interaction.user.id)
                embeds = await _build_table_embeds(session)
        except EbayNotConfiguredError:
            await interaction.followup.send(embed=error_embed(
                "eBay pricing isn't configured. Set EBAY_CLIENT_ID and EBAY_CLIENT_SECRET in `.env` "
                "(no user OAuth needed for pricing - see `/config ebay`)."
            ))
            return
        except EbayApiError as e:
            await interaction.followup.send(embed=error_embed(f"eBay API error: {e}"))
            return

        await interaction.followup.send(embeds=embeds, view=RefreshButton())


class PricingCog(commands.GroupCog, group_name="pricing"):
    """Live eBay pricing, buy-price recommendations, and the watchlist."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self) -> None:
        # Re-register the Refresh button so it keeps working on old messages
        # after the bot restarts.
        self.bot.add_view(RefreshButton())

    @app_commands.command(name="watch", description="Add a phone model (and optionally storage) to the pricing watchlist")
    @require_admin()
    async def watch(self, interaction: discord.Interaction, model: str, storage: Optional[str] = None):
        async with session_scope() as session:
            phone_model = await config_service.get_or_create_phone_model(session, "Apple", model)
            await pricing_service.add_to_watchlist(session, phone_model, storage, interaction.user.id)
        await interaction.response.send_message(
            embed=success_embed(f"Watching {model}" + (f" {storage}" if storage else "") + " for pricing.")
        )

    @app_commands.command(name="unwatch", description="Remove a phone model (and optional storage) from the pricing watchlist")
    @require_admin()
    async def unwatch(self, interaction: discord.Interaction, model: str, storage: Optional[str] = None):
        async with session_scope() as session:
            phone_model = await config_service.get_or_create_phone_model(session, "Apple", model)
            removed = await pricing_service.remove_from_watchlist(session, phone_model, storage, interaction.user.id)
        if removed:
            await interaction.response.send_message(embed=success_embed(f"Stopped watching {model}" + (f" {storage}" if storage else "")))
        else:
            await interaction.response.send_message(embed=error_embed("That model/storage wasn't on the watchlist."), ephemeral=True)

    @app_commands.command(name="watchlist", description="Show the pricing watchlist")
    @require_authorized()
    async def watchlist(self, interaction: discord.Interaction):
        async with session_scope() as session:
            entries = await pricing_service.list_watchlist(session)
        if not entries:
            await interaction.response.send_message(embed=error_embed("Watchlist is empty. Use `/pricing watch` to add a model."), ephemeral=True)
            return
        e = discord.Embed(title="👀 Pricing watchlist", colour=discord.Colour.blue())
        e.description = "\n".join(f"• {entry.phone_model.display_name()} — {entry.storage or 'any storage'}" for entry in entries)
        await interaction.response.send_message(embed=e)

    @app_commands.command(name="table", description="Show the pricing table (with a refresh button)")
    @require_authorized()
    async def table(self, interaction: discord.Interaction):
        await interaction.response.defer(thinking=True)
        async with session_scope() as session:
            embeds = await _build_table_embeds(session)
        await interaction.followup.send(embeds=embeds, view=RefreshButton())

    @app_commands.command(name="refresh", description="Refresh eBay pricing for every watched model now")
    @require_authorized()
    async def refresh(self, interaction: discord.Interaction):
        await interaction.response.defer(thinking=True)
        try:
            async with session_scope() as session:
                results = await run_pricing_refresh(session, self.bot.ebay_client, interaction.user.id)
        except EbayNotConfiguredError:
            await interaction.followup.send(embed=error_embed(
                "eBay pricing isn't configured. Set EBAY_CLIENT_ID and EBAY_CLIENT_SECRET in `.env`."
            ))
            return
        except EbayApiError as e:
            await interaction.followup.send(embed=error_embed(f"eBay API error: {e}"))
            return
        await interaction.followup.send(embed=success_embed(
            "Pricing refreshed",
            f"{results['watched']} model(s) watched, {results['observed']} updated, {results['no_data']} had no data.",
        ))


async def setup(bot: commands.Bot):
    await bot.add_cog(PricingCog(bot))
