from __future__ import annotations

import functools
import logging
from typing import Callable

import discord
from discord import app_commands

from app.config import settings
from app.db import session_scope
from app.services import config_service

logger = logging.getLogger(__name__)


async def _is_authorized(discord_user_id: int) -> bool:
    if discord_user_id in settings.bootstrap_admin_id_list:
        return True
    async with session_scope() as session:
        return await config_service.is_authorized(session, discord_user_id)


async def _is_admin(discord_user_id: int) -> bool:
    if discord_user_id in settings.bootstrap_admin_id_list:
        return True
    async with session_scope() as session:
        return await config_service.is_admin(session, discord_user_id)


def require_authorized():
    """Slash-command check: only users in the authorized_users table (or
    the .env bootstrap admin list, used only to configure the very first
    admin) may use the bot at all."""

    async def predicate(interaction: discord.Interaction) -> bool:
        if await _is_authorized(interaction.user.id):
            return True
        await interaction.response.send_message(
            "🔒 You're not authorized to use this bot. Ask an admin to run "
            "`/config users add` to grant you access.",
            ephemeral=True,
        )
        return False

    return app_commands.check(predicate)


def require_admin():
    """Slash-command check for admin-only operations (most of /config)."""

    async def predicate(interaction: discord.Interaction) -> bool:
        if await _is_admin(interaction.user.id):
            return True
        await interaction.response.send_message(
            "🔒 This command requires admin privileges.", ephemeral=True,
        )
        return False

    return app_commands.check(predicate)
