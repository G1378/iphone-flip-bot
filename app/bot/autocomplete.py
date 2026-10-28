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
    """Newest generation first, each generation's models in tier order (mini, base, Plus, Pro, Pro Max)."""
    needle = config_service.model_key(current)
    async with session_scope() as session:
        models = await config_service.list_phone_models(session)
    hits = [m for m in models if not needle or needle in config_service.model_key(m.model)]
    # An exact name first ("13" lists "iPhone 13" above "iPhone 13 Pro"), then newest series, tiers ascending.
    hits.sort(key=lambda m: (config_service.model_key(m.model) != needle, -(m.sort_order // 100), m.sort_order % 100, m.model))
    return [app_commands.Choice(name=m.model[:100], value=m.model[:100]) for m in hits[:25]]


async def _phone_model_options(interaction: discord.Interaction):
    """(storage, colours) offered for the phone named in this command's internal_id box, if there is one."""
    internal_id = getattr(interaction.namespace, "internal_id", None)
    if not internal_id:
        return [], []
    from app.services import phones as phones_service
    async with session_scope() as session:
        phone = await phones_service.get_phone(session, str(internal_id).upper())
        if phone is None or phone.phone_model_id is None:
            return [], []
        model = await session.get(config_service.PhoneModelConfig, phone.phone_model_id)
        return config_service.model_options(model) if model else ([], [])


def _filter(values: list[str], current: str) -> list[app_commands.Choice[str]]:
    needle = (current or "").lower().replace(" ", "")
    return [app_commands.Choice(name=v[:100], value=v[:100]) for v in values
            if not needle or needle in v.lower().replace(" ", "")][:25]


async def phone_storage_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    """Storage sizes this phone's model came in."""
    return _filter((await _phone_model_options(interaction))[0], current)


async def phone_colour_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    """Colourways this phone's model came in."""
    return _filter((await _phone_model_options(interaction))[1], current)


async def any_colour_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    """Every colour name in the catalogue (for parts, which fit several models)."""
    async with session_scope() as session:
        colours = await config_service.all_colourways(session)
    return _filter(colours, current)
