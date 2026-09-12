from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from app.bot.auth import require_authorized
from app.bot.embeds import error_embed
from app.db import session_scope
from app.services import search as search_service


class SearchCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="search", description="Search everything: internal ID, IMEI, serial, model, status, eBay ID, etc.")
    @require_authorized()
    async def search(self, interaction: discord.Interaction, term: str):
        async with session_scope() as session:
            results = await search_service.global_search(session, term)

        if results.is_empty():
            await interaction.response.send_message(embed=error_embed(f"No results for `{term}`."), ephemeral=True)
            return

        e = discord.Embed(title=f"🔍 Search results for '{term}'", colour=discord.Colour.blue())
        if results.phones:
            e.add_field(name="📱 Phones", value="\n".join(
                f"`{p.internal_id}` {p.display_name()} — {p.current_status}" for p in results.phones
            )[:1024], inline=False)
        if results.parts:
            e.add_field(name="🔩 Parts", value="\n".join(
                f"`{p.internal_id}` {p.part_type.name if p.part_type else '?'} — {p.status}" for p in results.parts
            )[:1024], inline=False)
        if results.donors:
            e.add_field(name="🔧 Donors", value="\n".join(
                f"`{d.internal_id}` {d.display_name()} — {d.current_status}" for d in results.donors
            )[:1024], inline=False)
        if results.listings:
            e.add_field(name="📋 Listings", value="\n".join(
                f"`{l.internal_id}` {l.title[:40]} — {l.status}" + (f" (eBay: {l.ebay_listing_id})" if l.ebay_listing_id else "")
                for l in results.listings
            )[:1024], inline=False)
        if results.sales:
            e.add_field(name="💷 Sales", value="\n".join(
                f"`{s.internal_id}` £{s.sale_price}" + (f" (eBay order: {s.ebay_order_id})" if s.ebay_order_id else "")
                for s in results.sales
            )[:1024], inline=False)
        await interaction.response.send_message(embed=e)


async def setup(bot: commands.Bot):
    await bot.add_cog(SearchCog(bot))
