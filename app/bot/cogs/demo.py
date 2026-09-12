from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from app.bot.auth import require_admin
from app.bot.embeds import error_embed, success_embed
from app.bot.views import ConfirmView
from app.config import settings
from app.db import session_scope
from app.integrations.ebay.mock import MockEbayClient
from seed.demo_data import populate_demo_data


class DemoCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="demo", description="[DEV ONLY] Populate the database with realistic example data")
    @require_admin()
    async def demo(self, interaction: discord.Interaction):
        if settings.environment.lower() == "production":
            await interaction.response.send_message(
                embed=error_embed("`/demo` is disabled when ENVIRONMENT=production, to protect real business data."),
                ephemeral=True,
            )
            return

        async def do_populate(inner: discord.Interaction):
            # Demo listings are "published" against a disabled/mock eBay
            # client regardless of real eBay config, so demo data never
            # touches the real eBay account.
            demo_ebay = MockEbayClient(configured=True)
            async with session_scope() as session:
                counts = await populate_demo_data(session, demo_ebay)
            e = discord.Embed(title="✅ Demo data populated", colour=discord.Colour.green())
            for k, v in counts.items():
                e.add_field(name=k, value=str(v), inline=True)
            await inner.response.edit_message(content=None, embed=e, view=None)

        view = ConfirmView(
            interaction.user.id, do_populate,
            confirm_label="Yes, populate demo data",
        )
        await interaction.response.send_message(
            "This will create example phones, donors, parts, repairs, listings and sales in the database. Continue?",
            view=view, ephemeral=True,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(DemoCog(bot))
