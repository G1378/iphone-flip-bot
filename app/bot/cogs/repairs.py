from __future__ import annotations

from decimal import Decimal
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from app.bot.auth import require_authorized
from app.bot.embeds import analysis_embed, error_embed, repair_plan_embed, success_embed
from app.bot.views import ConfirmView
from app.db import session_scope
from app.services import config_service
from app.services import finance as finance_service
from app.services import parts as parts_service
from app.services import phones as phones_service
from app.services import repairs as repairs_service


class CandidateView(discord.ui.View):
    """Shown after picking a requirement line: Reserve / Order / Cancel."""

    def __init__(self, invoker_id: int, repair_internal_id: str, repair_part_id: int, candidates: list):
        super().__init__(timeout=120)
        self.invoker_id = invoker_id
        self.repair_internal_id = repair_internal_id
        self.repair_part_id = repair_part_id
        for c in candidates[:4]:
            self.add_item(self._make_reserve_button(c))

    def _make_reserve_button(self, part) -> discord.ui.Button:
        button = discord.ui.Button(
            label=f"Reserve {part.internal_id} (£{part.cost})", style=discord.ButtonStyle.success,
        )

        async def callback(interaction: discord.Interaction):
            if interaction.user.id != self.invoker_id:
                await interaction.response.send_message("Not your repair plan.", ephemeral=True)
                return
            async with session_scope() as session:
                repair = await repairs_service.get_repair(session, self.repair_internal_id)
                fresh_part = await parts_service.get_part(session, part.internal_id)
                rp = next((r for r in repair.parts if r.id == self.repair_part_id), None)
                if not rp or not fresh_part:
                    await interaction.response.send_message(embed=error_embed("That requirement or part no longer exists."), ephemeral=True)
                    return
                try:
                    await parts_service.reserve_part(session, fresh_part, repair, rp, interaction.user.id)
                except parts_service.PartStateError as e:
                    await interaction.response.send_message(embed=error_embed(str(e)), ephemeral=True)
                    return
            await interaction.response.edit_message(
                content=f"✅ Reserved `{fresh_part.internal_id}` for `{rp.required_part_type.name}` on {self.repair_internal_id}.",
                embed=None, view=None,
            )

        button.callback = callback
        return button

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.invoker_id


class RequirementSelect(discord.ui.Select):
    def __init__(self, invoker_id: int, repair_internal_id: str, plan):
        self.invoker_id = invoker_id
        self.repair_internal_id = repair_internal_id
        self.plan_by_value: dict[str, object] = {}
        options = []
        for item in plan:
            value = str(item.repair_part.id)
            self.plan_by_value[value] = item
            options.append(discord.SelectOption(
                label=item.repair_part.required_part_type.name,
                value=value,
                description=f"{len(item.candidates)} candidate(s) available",
            ))
        super().__init__(placeholder="Choose a requirement to assign a part...", options=options[:25])

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.invoker_id:
            await interaction.response.send_message("Not your repair plan.", ephemeral=True)
            return
        item = self.plan_by_value[self.values[0]]
        if not item.candidates:
            await interaction.response.send_message(
                f"No AVAILABLE `{item.repair_part.required_part_type.name}` in stock. Use `/order` to order one.",
                ephemeral=True,
            )
            return
        view = CandidateView(self.invoker_id, self.repair_internal_id, item.repair_part.id, list(item.candidates))
        lines = "\n".join(f"`{c.internal_id}` £{c.cost} · {c.testing_status.value} · {c.location.code if c.location else '?'}" for c in item.candidates[:4])
        await interaction.response.send_message(f"Candidates for **{item.repair_part.required_part_type.name}**:\n{lines}", view=view, ephemeral=True)


class RepairPlanView(discord.ui.View):
    def __init__(self, invoker_id: int, repair_internal_id: str, plan):
        super().__init__(timeout=300)
        if plan:
            self.add_item(RequirementSelect(invoker_id, repair_internal_id, plan))


class RepairsCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="repair", description="Plan (or view) the repair for a phone's open faults")
    @require_authorized()
    async def repair(self, interaction: discord.Interaction, internal_id: str):
        async with session_scope() as session:
            phone = await phones_service.get_phone(session, internal_id)
            if not phone:
                await interaction.response.send_message(embed=error_embed(f"No phone found with ID `{internal_id}`."), ephemeral=True)
                return
            repair = await repairs_service.create_repair_for_open_faults(session, phone, interaction.user.id)
            phone_model = await config_service.get_or_create_phone_model(session, phone.manufacturer, phone.model, phone.variant)
            plan = await repairs_service.repair_plan(session, repair, phone_model.id)
            embed = repair_plan_embed(phone, plan)
            embed.set_footer(text=f"Repair ID: {repair.internal_id}")

        view = RepairPlanView(interaction.user.id, repair.internal_id, plan)
        await interaction.response.send_message(embed=embed, view=view if plan else None)

    @app_commands.command(name="repair-status", description="Show a repair's current status and parts")
    @require_authorized()
    async def repair_status(self, interaction: discord.Interaction, internal_id: str):
        async with session_scope() as session:
            repair = await repairs_service.get_repair(session, internal_id)
            if not repair:
                await interaction.response.send_message(embed=error_embed(f"No repair found with ID `{internal_id}`."), ephemeral=True)
                return
            e = discord.Embed(title=f"🔧 {repair.internal_id} — {repair.phone.internal_id} {repair.phone.display_name()}",
                               colour=discord.Colour.blue())
            e.add_field(name="Status", value=repair.status, inline=True)
            e.add_field(name="Labour cost", value=f"£{repair.labour_cost}", inline=True)
            e.add_field(name="External repair cost", value=f"£{repair.external_repair_cost}", inline=True)
            for rp in repair.parts:
                part_bit = f"`{rp.part.internal_id}`" if rp.part else "—"
                e.add_field(name=rp.required_part_type.name, value=f"{rp.status.value} · {part_bit}", inline=True)
            e.add_field(name="Total cost", value=f"£{repair.total_cost()}", inline=False)

        outstanding = [rp for rp in repair.parts if rp.status.value not in ("INSTALLED", "CANCELLED")]

        async def do_complete(inner_interaction: discord.Interaction):
            async with session_scope() as session:
                r = await repairs_service.get_repair(session, repair.internal_id)
                try:
                    await repairs_service.complete_repair(session, r, inner_interaction.user.id)
                except repairs_service.RepairError as err:
                    await inner_interaction.response.edit_message(content=str(err), embed=None, view=None)
                    return
                if r.phone.current_status not in ("SOLD", "SHIPPED", "COMPLETE"):
                    await phones_service.set_status(session, r.phone, "READY_FOR_LISTING", inner_interaction.user.id)
            await inner_interaction.response.edit_message(content=f"✅ {repair.internal_id} marked COMPLETE.", embed=None, view=None)

        if not outstanding and repair.status != "COMPLETE":
            view = ConfirmView(interaction.user.id, do_complete, confirm_label="Complete Repair")
            await interaction.response.send_message(embed=e, view=view)
        else:
            await interaction.response.send_message(embed=e)

    @app_commands.command(name="analyse", description="Economic analysis: is this repair/phone worth it?")
    @require_authorized()
    async def analyse(self, interaction: discord.Interaction, internal_id: str, expected_sale_price: Optional[float] = None):
        async with session_scope() as session:
            phone = await phones_service.get_phone(session, internal_id)
            if not phone:
                await interaction.response.send_message(embed=error_embed(f"No phone found with ID `{internal_id}`."), ephemeral=True)
                return
            price = Decimal(str(expected_sale_price)) if expected_sale_price is not None else None
            analysis = await finance_service.analyse_phone(session, phone, expected_sale_price=price)
            embed = analysis_embed(phone, analysis)
        await interaction.response.send_message(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(RepairsCog(bot))
