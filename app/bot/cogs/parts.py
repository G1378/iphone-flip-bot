from __future__ import annotations

from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from app.bot.auth import require_authorized
from app.bot.embeds import error_embed, part_embed, success_embed
from app.bot.pagination import send_paginated
from app.db import session_scope
from app.models.enums import RepairPartLineStatus
from app.services import allocation as allocation_service
from app.services import parts as parts_service
from app.services import repairs as repairs_service


async def _find_repair_part_line(session, repair, part) -> Optional[object]:
    for rp in repair.parts:
        if rp.required_part_type_id == part.part_type_id and rp.status in (
            RepairPartLineStatus.REQUIRED, RepairPartLineStatus.RESERVED,
        ):
            return rp
    return None


class PartsCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="parts", description="List parts inventory")
    @require_authorized()
    async def parts(self, interaction: discord.Interaction, status: Optional[str] = None):
        async with session_scope() as session:
            all_parts = await parts_service.search_parts(session, status.upper() if status else "")
            items = [(p.internal_id, p.part_type.name if p.part_type else "?", p.status, p.cost,
                      p.location.code if p.location else "—") for p in all_parts]

        def render(page_items, page, total_pages):
            e = discord.Embed(title=f"🔩 Parts ({len(items)} shown)", colour=discord.Colour.blue())
            for internal_id, type_name, status_, cost, loc in page_items:
                e.add_field(name=f"{internal_id} — {type_name}", value=f"{status_} · £{cost} · {loc}", inline=False)
            e.set_footer(text=f"Page {page + 1}/{total_pages}")
            return e

        await send_paginated(interaction, items, per_page=10, render=render)

    @app_commands.command(name="part", description="Show full detail for a part")
    @require_authorized()
    async def part(self, interaction: discord.Interaction, internal_id: str):
        async with session_scope() as session:
            p = await parts_service.get_part(session, internal_id)
            if not p:
                await interaction.response.send_message(embed=error_embed(f"No part found with ID `{internal_id}`."), ephemeral=True)
                return
            history = await allocation_service.allocation_history(session, p)
            embed = part_embed(p)
            if history:
                lines = [f"£{h.allocated_amount} ({h.method.value}){' [current]' if h.is_current else ' [superseded]'}" for h in history]
                embed.add_field(name="Allocation history", value="\n".join(lines)[:1024], inline=False)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="find-part", description="Find available parts by type and model, e.g. 'screen' '13 pro'")
    @require_authorized()
    async def find_part(self, interaction: discord.Interaction, part_type: str, model: str = ""):
        async with session_scope() as session:
            results = await parts_service.find_part_by_type_and_model(session, part_type, model)
            items = [(p.internal_id, p.part_type.name if p.part_type else "?", p.testing_status.value,
                      p.location.code if p.location else "—", p.cost) for p in results]

        def render(page_items, page, total_pages):
            e = discord.Embed(title=f"🔍 Parts matching '{part_type} {model}'".strip(), colour=discord.Colour.blue())
            if not page_items:
                e.description = "No matching AVAILABLE parts found."
            for internal_id, type_name, test, loc, cost in page_items:
                e.add_field(name=f"{internal_id} — {type_name}", value=f"{test} · {loc} · £{cost}", inline=False)
            e.set_footer(text=f"Page {page + 1}/{total_pages}")
            return e

        await send_paginated(interaction, items, per_page=10, render=render)

    @app_commands.command(name="reserve-part", description="Reserve a part against a repair's requirement")
    @require_authorized()
    async def reserve_part(self, interaction: discord.Interaction, part_id: str, repair_id: str):
        async with session_scope() as session:
            part = await parts_service.get_part(session, part_id)
            repair = await repairs_service.get_repair(session, repair_id)
            if not part or not repair:
                await interaction.response.send_message(embed=error_embed("Part or repair not found."), ephemeral=True)
                return
            rp = await _find_repair_part_line(session, repair, part)
            if not rp:
                await interaction.response.send_message(
                    embed=error_embed(f"{repair.internal_id} has no outstanding requirement matching {part.part_type.name}."),
                    ephemeral=True,
                )
                return
            try:
                await parts_service.reserve_part(session, part, repair, rp, interaction.user.id)
            except parts_service.PartStateError as e:
                await interaction.response.send_message(embed=error_embed(str(e)), ephemeral=True)
                return
        await interaction.response.send_message(embed=success_embed(f"Reserved {part_id} for {repair_id}"))

    @app_commands.command(name="install-part", description="Install a reserved/available part into its repair's phone")
    @require_authorized()
    async def install_part(self, interaction: discord.Interaction, part_id: str, repair_id: str):
        async with session_scope() as session:
            part = await parts_service.get_part(session, part_id)
            repair = await repairs_service.get_repair(session, repair_id)
            if not part or not repair:
                await interaction.response.send_message(embed=error_embed("Part or repair not found."), ephemeral=True)
                return
            rp = None
            for candidate in repair.parts:
                if candidate.part_id == part.id and candidate.status in (RepairPartLineStatus.RESERVED, RepairPartLineStatus.REQUIRED):
                    rp = candidate
                    break
            if not rp:
                rp = await _find_repair_part_line(session, repair, part)
            if not rp:
                await interaction.response.send_message(embed=error_embed("No matching requirement line found for this part on this repair."), ephemeral=True)
                return
            try:
                await parts_service.install_part(session, part, repair.phone, rp, interaction.user.id)
            except parts_service.PartStateError as e:
                await interaction.response.send_message(embed=error_embed(str(e)), ephemeral=True)
                return
        await interaction.response.send_message(embed=success_embed(f"Installed {part_id} into {repair.phone.internal_id}"))

    @app_commands.command(name="remove-part", description="Remove an installed part (e.g. wrong part fitted)")
    @require_authorized()
    async def remove_part(self, interaction: discord.Interaction, part_id: str, notes: Optional[str] = None):
        async with session_scope() as session:
            part = await parts_service.get_part(session, part_id)
            if not part:
                await interaction.response.send_message(embed=error_embed(f"No part found with ID `{part_id}`."), ephemeral=True)
                return
            from sqlalchemy import select
            from app.models.repair_models import RepairPart
            rp = (await session.execute(
                select(RepairPart).where(RepairPart.part_id == part.id, RepairPart.status == RepairPartLineStatus.INSTALLED)
            )).scalar_one_or_none()
            try:
                await parts_service.remove_part(session, part, rp, interaction.user.id, notes=notes)
            except parts_service.PartStateError as e:
                await interaction.response.send_message(embed=error_embed(str(e)), ephemeral=True)
                return
        await interaction.response.send_message(embed=success_embed(f"Removed {part_id} — now AVAILABLE"))

    @app_commands.command(name="scrap-part", description="Scrap a part (no longer usable)")
    @require_authorized()
    async def scrap_part(self, interaction: discord.Interaction, part_id: str, notes: Optional[str] = None):
        async with session_scope() as session:
            part = await parts_service.get_part(session, part_id)
            if not part:
                await interaction.response.send_message(embed=error_embed(f"No part found with ID `{part_id}`."), ephemeral=True)
                return
            await parts_service.scrap_part(session, part, interaction.user.id, notes=notes)
        await interaction.response.send_message(embed=success_embed(f"Scrapped {part_id}"))

    @app_commands.command(name="move-part", description="Move a part to a physical location")
    @require_authorized()
    async def move_part(self, interaction: discord.Interaction, part_id: str, location_code: str):
        async with session_scope() as session:
            part = await parts_service.get_part(session, part_id)
            if not part:
                await interaction.response.send_message(embed=error_embed(f"No part found with ID `{part_id}`."), ephemeral=True)
                return
            await parts_service.move_part(session, part, location_code, interaction.user.id)
        await interaction.response.send_message(embed=success_embed(f"Moved {part_id} to {location_code.upper()}"))

    @app_commands.command(name="parts-needed", description="Show which parts are needed and what's missing from stock")
    @require_authorized()
    async def parts_needed(self, interaction: discord.Interaction):
        from app.services.orders import parts_needed as parts_needed_service
        async with session_scope() as session:
            lines = await parts_needed_service(session)

        e = discord.Embed(title="📦 Parts required", colour=discord.Colour.blue())
        if not lines:
            e.description = "Nothing outstanding — all active repairs are fully resourced. ✅"
            await interaction.response.send_message(embed=e)
            return

        required_lines = "\n".join(f"{l.part_type.name} × {l.required_qty}" for l in lines)
        stock_lines = "\n".join(f"{l.part_type.name} × {l.in_stock_qty}" for l in lines)
        order_lines = [l for l in lines if l.to_order_qty > 0]
        e.add_field(name="Required", value=required_lines or "—", inline=True)
        e.add_field(name="In stock", value=stock_lines or "—", inline=True)
        if order_lines:
            e.add_field(
                name="⚠️ ORDER REQUIRED",
                value="\n".join(f"{l.part_type.name} × {l.to_order_qty}" for l in order_lines),
                inline=False,
            )
            e.set_footer(text="Use /order to create a purchase order for the missing parts.")
        await interaction.response.send_message(embed=e)


async def setup(bot: commands.Bot):
    await bot.add_cog(PartsCog(bot))
