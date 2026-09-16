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


def _fmt_obs(obs) -> str:
    if not obs:
        return "no data yet"
    source_tag = "sold" if obs.source.value == "SOLD" else "est."
    return f"£{obs.median_price} ({source_tag}, n={obs.sample_count})"


async def _build_table_text(session) -> str:
    entries = await pricing_service.list_watchlist(session)
    if not entries:
        return "Nothing on the watchlist yet. Use /pricing watch to add a model."

    lines = [f"{'MODEL':<28}{'STORAGE':<9}{'USED':<20}{'FAULTY':<20}{'BUY(used)':<12}{'BUY(faulty)':<12}"]
    lines.append("-" * len(lines[0]))
    for entry in entries:
        rec = await pricing_service.compute_recommendation(session, entry)
        model_name = entry.phone_model.display_name()[:27]
        storage = entry.storage or "any"
        used_txt = _fmt_obs(rec.used_observation)[:19]
        faulty_txt = _fmt_obs(rec.faulty_observation)[:19]
        buy_used = f"£{rec.recommended_buy_used}" if rec.recommended_buy_used is not None else "—"
        buy_faulty = f"£{rec.recommended_buy_faulty}" if rec.recommended_buy_faulty is not None else "—"
        lines.append(f"{model_name:<28}{storage:<9}{used_txt:<20}{faulty_txt:<20}{buy_used:<12}{buy_faulty:<12}")
    return "\n".join(lines)


class RefreshButton(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="🔄 Refresh now", style=discord.ButtonStyle.primary)
    async def refresh(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(thinking=True)
        bot = interaction.client
        try:
            async with session_scope() as session:
                await run_pricing_refresh(session, bot.ebay_client, interaction.user.id)
                table_text = await _build_table_text(session)
        except EbayNotConfiguredError:
            await interaction.followup.send(embed=error_embed(
                "eBay pricing isn't configured. Set EBAY_CLIENT_ID and EBAY_CLIENT_SECRET in `.env` "
                "(no user OAuth needed for pricing - see `/config ebay`)."
            ))
            return
        except EbayApiError as e:
            await interaction.followup.send(embed=error_embed(f"eBay API error: {e}"))
            return

        e = discord.Embed(title="📈 Pricing table", description=f"```\n{table_text}\n```", colour=discord.Colour.blue())
        e.set_footer(text="Prices are medians. 'sold' = actual eBay sold comps, 'est.' = active-listing estimate (sold data unavailable).")
        await interaction.followup.send(embed=e, view=RefreshButton())


class PricingCog(commands.GroupCog, group_name="pricing"):
    """Live eBay pricing, buy-price recommendations, and the watchlist."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

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
            table_text = await _build_table_text(session)
        e = discord.Embed(title="📈 Pricing table", description=f"```\n{table_text}\n```", colour=discord.Colour.blue())
        e.set_footer(text="Prices are medians. 'sold' = actual eBay sold comps, 'est.' = active-listing estimate (sold data unavailable).")
        await interaction.followup.send(embed=e, view=RefreshButton())

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
