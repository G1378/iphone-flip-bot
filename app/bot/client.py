from __future__ import annotations

import logging

import discord
from discord.ext import commands

from app.config import settings
from app.db import session_scope
from app.integrations.ebay import get_ebay_client
from app.integrations.sheets import SheetsClient
from app.jobs.scheduler import JobScheduler
from app.services import config_service

logger = logging.getLogger(__name__)

INITIAL_COGS = [
    "app.bot.cogs.home",
    "app.bot.cogs.inventory",
    "app.bot.cogs.testing",
    "app.bot.cogs.donors",
    "app.bot.cogs.parts",
    "app.bot.cogs.repairs",
    "app.bot.cogs.orders",
    "app.bot.cogs.ebay",
    "app.bot.cogs.finance",
    "app.bot.cogs.config",
    "app.bot.cogs.search",
    "app.bot.cogs.demo",
]


class FlipBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.message_content = False  # bot is slash-command only, no need to read messages
        super().__init__(command_prefix="!disabled-", intents=intents)

        self.ebay_client = get_ebay_client()
        self.sheets_client = SheetsClient()
        self.job_scheduler: JobScheduler | None = None

    async def setup_hook(self) -> None:
        # Ensure default configuration exists before any command runs.
        async with session_scope() as session:
            await config_service.seed_defaults_if_empty(session)

        for cog in INITIAL_COGS:
            await self.load_extension(cog)
            logger.info("Loaded cog %s", cog)

        if settings.discord_guild_id:
            guild = discord.Object(id=settings.discord_guild_id)
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            logger.info("Synced %d commands to guild %s (fast dev sync)", len(synced), settings.discord_guild_id)
        else:
            synced = await self.tree.sync()
            logger.info("Synced %d global commands (may take up to 1h to propagate)", len(synced))

        self.job_scheduler = JobScheduler(self.ebay_client, self.sheets_client, self._notify_channels)
        self.job_scheduler.start()

    async def _notify_channels(self, message: str) -> None:
        async with session_scope() as session:
            channel_ids = await config_service.notification_channel_ids(session)
        for channel_id in channel_ids:
            channel = self.get_channel(channel_id)
            if channel is None:
                try:
                    channel = await self.fetch_channel(channel_id)
                except discord.HTTPException:
                    continue
            try:
                await channel.send(message)
            except discord.HTTPException as e:
                logger.warning("Failed to notify channel %s: %s", channel_id, e)

    async def on_ready(self) -> None:
        logger.info("Logged in as %s (id=%s)", self.user, self.user.id if self.user else "?")

    async def close(self) -> None:
        if self.job_scheduler:
            await self.job_scheduler.shutdown()
        if hasattr(self.ebay_client, "aclose"):
            await self.ebay_client.aclose()
        await super().close()


async def on_app_command_error(interaction: discord.Interaction, error: discord.app_commands.AppCommandError) -> None:
    """Global fallback error handler - never leak a stack trace or secret
    into Discord; always give the user a clear, actionable message."""
    from app.bot.embeds import error_embed
    from app.integrations.ebay.interface import EbayApiError, EbayNotConfiguredError
    from app.integrations.sheets.client import SheetsNotConfiguredError

    original = getattr(error, "original", error)

    if isinstance(original, EbayNotConfiguredError):
        msg = "eBay integration is not configured yet. Ask an admin to set it up (see `/config ebay`)."
    elif isinstance(original, SheetsNotConfiguredError):
        msg = "Google Sheets integration is not configured yet. Ask an admin to set it up (see `/config sheets`)."
    elif isinstance(original, EbayApiError):
        msg = f"eBay API error: {original}"
    elif isinstance(original, (ValueError,)):
        msg = str(original)
    elif isinstance(error, discord.app_commands.CheckFailure):
        return  # the check itself already sent a message
    else:
        logger.exception("Unhandled app command error", exc_info=original)
        msg = "Something went wrong. This has been logged."

    embed = error_embed(msg)
    try:
        if interaction.response.is_done():
            await interaction.followup.send(embed=embed, ephemeral=True)
        else:
            await interaction.response.send_message(embed=embed, ephemeral=True)
    except discord.HTTPException:
        pass


def build_bot() -> FlipBot:
    bot = FlipBot()
    bot.tree.on_error = on_app_command_error
    return bot
