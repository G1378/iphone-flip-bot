from __future__ import annotations

import pytest

from app.bot.client import INITIAL_COGS, build_bot
from app.bot.command_validation import collect_command_tree_errors


@pytest.mark.asyncio
async def test_command_tree_is_within_discord_limits():
    """Loads every cog and checks every command/group/option/choice against
    Discord's documented limits (name/description length and charset,
    max options, max choices, max subcommands per group, no duplicate
    names). A violation here is exactly what causes a CommandSyncFailure
    (HTTP 400, error code 50035) when the bot actually tries to start -
    this test catches it at commit time instead of on the Pi."""
    bot = build_bot()
    for cog in INITIAL_COGS:
        await bot.load_extension(cog)

    errors = collect_command_tree_errors(bot.tree.get_commands())
    assert not errors, "Command tree violates Discord's limits:\n" + "\n".join(f"  - {e}" for e in errors)
