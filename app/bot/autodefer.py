"""Safety net for Discord's 3-second slash-command deadline.

Discord shows "<bot> didn't respond in time" if a command isn't acknowledged
within 3 seconds. On a Raspberry Pi, a command that does database or network
work first can occasionally miss that window.

This module makes that impossible, bot-wide, without touching each command:

1. AutoDeferTree: if a slash command hasn't responded after ~1.5s, it is
   automatically deferred ("<bot> is thinking...").
2. send_message fallback: once a command has been deferred (by us or by the
   command itself), a later `interaction.response.send_message(...)` is sent
   as a followup instead of raising InteractionResponded.
3. Loop-lag monitor: logs a warning if something blocks the event loop, so
   the real cause of any slowness shows up in `docker compose logs bot`.
"""
from __future__ import annotations

import asyncio
import logging
import time

import discord
from discord import app_commands

logger = logging.getLogger(__name__)

AUTO_DEFER_AFTER_SECONDS = 1.5

_background_tasks: set[asyncio.Task] = set()


async def _auto_defer(interaction: discord.Interaction) -> None:
    await asyncio.sleep(AUTO_DEFER_AFTER_SECONDS)
    if interaction.response.is_done():
        return
    try:
        await interaction.response.defer(thinking=True)
        name = interaction.command.qualified_name if interaction.command else "?"
        logger.warning("/%s was slow to respond (>%.1fs) - auto-deferred.", name, AUTO_DEFER_AFTER_SECONDS)
    except (discord.InteractionResponded, discord.HTTPException):
        pass  # the command responded at the same moment, or the interaction expired


class AutoDeferTree(app_commands.CommandTree):
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        # Runs at the very start of every slash command, before any DB work.
        task = asyncio.create_task(_auto_defer(interaction))
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)
        return True


_original_send_message = discord.InteractionResponse.send_message


async def _send_message_with_followup_fallback(self: discord.InteractionResponse, *args, **kwargs):
    if self.is_done():
        return await self._parent.followup.send(*args, **kwargs)
    try:
        return await _original_send_message(self, *args, **kwargs)
    except discord.InteractionResponded:
        return await self._parent.followup.send(*args, **kwargs)


def install_send_message_fallback() -> None:
    discord.InteractionResponse.send_message = _send_message_with_followup_fallback  # type: ignore[method-assign]


async def monitor_event_loop_lag(threshold_seconds: float = 1.0, interval_seconds: float = 0.5) -> None:
    """Log a warning whenever the event loop was blocked for longer than the
    threshold. If you see these, something is running synchronous/blocking
    code on the bot's event loop."""
    while True:
        started = time.monotonic()
        await asyncio.sleep(interval_seconds)
        lag = time.monotonic() - started - interval_seconds
        if lag > threshold_seconds:
            logger.warning("Event loop was blocked for %.1fs - Discord commands may time out.", lag)
