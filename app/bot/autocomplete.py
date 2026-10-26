"""Autocomplete for any slash-command argument that names a phone model.

Models are picked from the catalog (base, mini, Plus, Pro and Pro Max are separate
entries) so nobody types "13" and gets the wrong generation's parts or prices. The
service layer still validates what was submitted (a user can type past the menu)."""
from __future__ import annotations

import discord
from discord import app_commands

from app.db import session_scope
from app.services import config_service


async def phone_model_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    needle = config_service.model_key(current)
    async with session_scope() as session:
        models = await config_service.list_phone_models(session)
    names = [m.model for m in models]
    hits = [n for n in names if needle in config_service.model_key(n)] if needle else names
    # Exact name first, so "13" lists "iPhone 13" above "iPhone 13 Pro".
    hits.sort(key=lambda n: (config_service.model_key(n) != needle, len(n), n))
    return [app_commands.Choice(name=n[:100], value=n[:100]) for n in hits[:25]]
