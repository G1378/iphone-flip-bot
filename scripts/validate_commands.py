#!/usr/bin/env python3
"""Validate every registered slash command against Discord's API limits,
entirely locally (no network / bot token needed). Run this after changing
any cog to catch a CommandSyncFailure (HTTP 400, error code 50035) before
deploying - this same check also runs automatically in the test suite
(tests/test_command_tree.py).

Usage:
    python scripts/validate_commands.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.bot.command_validation import collect_command_tree_errors  # noqa: E402


async def main() -> int:
    from app.bot.client import INITIAL_COGS, build_bot

    bot = build_bot()
    for cog in INITIAL_COGS:
        await bot.load_extension(cog)

    top_level = bot.tree.get_commands()
    errors = collect_command_tree_errors(top_level)

    if errors:
        print(f"❌ {len(errors)} problem(s) found - these WILL cause Discord to reject command sync:\n")
        for e in errors:
            print(f"  - {e}")
        return 1

    print(f"✅ All {len(top_level)} top-level command(s) (and their subcommands/options/choices) are within Discord's limits.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
