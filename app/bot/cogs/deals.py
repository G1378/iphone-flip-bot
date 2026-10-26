"""Deals found on eBay for the models you watch, and the part finder."""
from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from app.bot.auth import require_authorized
from app.bot.autocomplete import phone_model_autocomplete
from app.bot.embeds import error_embed
from app.db import session_scope
from app.integrations.ebay.interface import EbayApiError, EbayNotConfiguredError
from app.services import config_service
from app.services import deals as deals_service


class DealsCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(name="deal-scan", description="Look on eBay now for deals on the models you watch")
    @require_authorized()
    async def deal_scan(self, interaction: discord.Interaction):
        await interaction.response.defer(thinking=True)
        try:
            async with session_scope() as session:
                result = await deals_service.scan_deals(session, self.bot.ebay_client)
                messages = [deals_service.format_alert(c) for c in result.candidates]
        except EbayNotConfiguredError:
            await interaction.followup.send(embed=error_embed("eBay isn't connected yet (`/config ebay`)."))
            return
        if not result.watched:
            await interaction.followup.send(embed=error_embed(
                "You aren't watching any models yet. Add one with `/pricing watch`, then `/pricing refresh`."))
            return
        skipped = ", ".join(f"{n} {why}" for why, n in sorted(result.skipped.items(), key=lambda kv: -kv[1])) or "none"
        summary = (f"Looked at **{result.checked}** listings across **{result.watched}** watched model(s): "
                   f"**{len(messages)}** deal(s).\nSkipped: {skipped}.")
        if result.errors:
            summary += "\n⚠️ " + "; ".join(result.errors[:3])
        await interaction.followup.send(summary)
        for m in messages:
            await interaction.channel.send(m)

    @app_commands.command(name="deals", description="The deals the scanner has found recently")
    @require_authorized()
    async def deals(self, interaction: discord.Interaction):
        async with session_scope() as session:
            alerts = await deals_service.recent_alerts(session, 10)
            rows = [f"• **{a.phone_model.model}** £{a.price}+£{a.postage} (max £{a.max_buy}) · {a.bucket.lower()}\n  {a.url}"
                    for a in alerts]
        await interaction.response.send_message("\n".join(rows)[:1900] if rows else "No deals found yet.")

    @app_commands.command(name="ebay-parts", description="Cheapest eBay listings for a part, for one exact model")
    @app_commands.describe(part_type="e.g. screen, battery", model="Exact model (a 13 Pro screen is not a 13 screen)")
    @app_commands.autocomplete(model=phone_model_autocomplete)
    @require_authorized()
    async def ebay_parts(self, interaction: discord.Interaction, part_type: str, model: str):
        await interaction.response.defer(thinking=True)
        async with session_scope() as session:
            try:
                phone_model = await config_service.resolve_phone_model(session, model)
                offers = await deals_service.find_parts_on_ebay(session, self.bot.ebay_client, part_type, phone_model)
            except config_service.UnknownPhoneModelError as e:
                await interaction.followup.send(embed=error_embed(str(e)))
                return
            except EbayNotConfiguredError:
                await interaction.followup.send(embed=error_embed("eBay isn't connected yet (`/config ebay`)."))
                return
            except EbayApiError as e:
                await interaction.followup.send(embed=error_embed(f"eBay search failed: {e}"))
                return
        await interaction.followup.send(deals_service.format_part_offers(part_type, phone_model, offers)[:1900])


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(DealsCog(bot))
