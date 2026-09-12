from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from app.bot.auth import require_authorized
from app.bot.embeds import donor_embed, error_embed, success_embed
from app.bot.pagination import send_paginated
from app.bot.views import ConfirmView
from app.db import session_scope
from app.models.enums import TestOutcome
from app.services import allocation as allocation_service
from app.services import donors as donors_service


class BuyDonorModal(discord.ui.Modal, title="Buy a donor phone"):
    model = discord.ui.TextInput(label="Model (e.g. iPhone 13 Pro)", required=True, max_length=80)
    storage_colour = discord.ui.TextInput(label="Storage & Colour (e.g. 128GB Graphite)", required=False, max_length=60)
    price_source = discord.ui.TextInput(label="Purchase price & source (e.g. 180.00 / CEX)", required=True, max_length=100)
    fault = discord.ui.TextInput(label="Fault (why it's a donor, not resale)", required=True, max_length=200)
    notes = discord.ui.TextInput(label="Notes (optional)", required=False, style=discord.TextStyle.paragraph, max_length=500)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            price_str, _, source = self.price_source.value.partition("/")
            price = Decimal(price_str.strip())
        except InvalidOperation:
            await interaction.response.send_message(embed=error_embed("Couldn't parse the purchase price."), ephemeral=True)
            return
        storage, colour = None, None
        if self.storage_colour.value:
            parts = self.storage_colour.value.split()
            storage = next((p for p in parts if "gb" in p.lower() or "tb" in p.lower()), None)
            colour = " ".join(p for p in parts if p != storage) or None

        async with session_scope() as session:
            data = donors_service.BuyDonorInput(
                manufacturer="Apple", model=self.model.value.strip(), variant=None, storage=storage, colour=colour,
                purchase_price=price, purchase_date=dt.date.today(), seller_source=source.strip() or None,
                fault_description=self.fault.value, notes=self.notes.value or None, location_code=None,
            )
            donor = await donors_service.buy_donor(session, data, interaction.user.id)
            embed = donor_embed(donor)
        embed.title = f"✅ Bought donor — {embed.title}"
        await interaction.response.send_message(embed=embed)


@dataclass
class PendingRecoveredPart:
    part_type_name: str
    condition: Optional[str]
    test_result: TestOutcome
    grade_code: Optional[str]
    notes: Optional[str]
    location_code: Optional[str]
    discarded: bool


class AddPartModal(discord.ui.Modal, title="Recovered / discarded part"):
    part_type = discord.ui.TextInput(label="Part type (e.g. Screen, Battery, Housing)", required=True, max_length=60)
    result_grade = discord.ui.TextInput(label="Test result & grade (e.g. PASS A- or DISCARD)", required=True, max_length=60)
    location = discord.ui.TextInput(label="Storage location (blank if discarded)", required=False, max_length=30)
    notes = discord.ui.TextInput(label="Notes (optional)", required=False, max_length=200)

    def __init__(self, view: "TeardownView"):
        super().__init__()
        self.view_ref = view

    async def on_submit(self, interaction: discord.Interaction) -> None:
        raw = self.result_grade.value.strip()
        discarded = raw.upper().startswith("DISCARD")
        outcome = TestOutcome.NOT_TESTED
        grade = None
        if not discarded:
            bits = raw.split()
            if bits:
                try:
                    outcome = TestOutcome(bits[0].upper())
                except ValueError:
                    outcome = TestOutcome.NOT_TESTED
            if len(bits) > 1:
                grade = bits[1]

        self.view_ref.pending.append(PendingRecoveredPart(
            part_type_name=self.part_type.value.strip(),
            condition=None,
            test_result=outcome,
            grade_code=grade,
            notes=self.notes.value or None,
            location_code=self.location.value.strip() or None,
            discarded=discarded,
        ))
        await interaction.response.edit_message(embed=self.view_ref.build_embed(), view=self.view_ref)


class TeardownView(discord.ui.View):
    def __init__(self, invoker_id: int, donor):
        super().__init__(timeout=600)
        self.invoker_id = invoker_id
        self.donor = donor
        self.pending: list[PendingRecoveredPart] = []

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.invoker_id:
            await interaction.response.send_message("Only the person running this teardown can use these buttons.", ephemeral=True)
            return False
        return True

    def build_embed(self) -> discord.Embed:
        e = discord.Embed(
            title=f"🔩 Teardown — {self.donor.internal_id} {self.donor.display_name()}",
            description="Add each recovered or discarded part, then press **Finish Teardown**.",
            colour=discord.Colour.blue(),
        )
        if not self.pending:
            e.add_field(name="Parts", value="_none added yet_", inline=False)
        else:
            lines = []
            for p in self.pending:
                if p.discarded:
                    lines.append(f"🗑️ {p.part_type_name} — discarded")
                else:
                    lines.append(f"✅ {p.part_type_name} — {p.test_result.value} {p.grade_code or ''} @ {p.location_code or '?'}")
            e.add_field(name=f"Parts ({len(self.pending)})", value="\n".join(lines)[:1024], inline=False)
        return e

    @discord.ui.button(label="Add Part", style=discord.ButtonStyle.primary, emoji="➕")
    async def add_part(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(AddPartModal(self))

    @discord.ui.button(label="Finish Teardown", style=discord.ButtonStyle.success, emoji="✅")
    async def finish(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.pending:
            await interaction.response.send_message("Add at least one part first.", ephemeral=True)
            return
        async with session_scope() as session:
            donor = await donors_service.get_donor(session, self.donor.internal_id)
            recovered = [
                donors_service.RecoveredPartInput(
                    part_type_name=p.part_type_name, condition=p.condition, test_result=p.test_result,
                    grade_code=p.grade_code, notes=p.notes, location_code=p.location_code, discarded=p.discarded,
                )
                for p in self.pending
            ]
            created = await donors_service.teardown(session, donor, recovered, interaction.user.id)

        e = discord.Embed(title=f"✅ Teardown complete — {donor.internal_id}", colour=discord.Colour.green())
        lines = [f"`{part.internal_id}` — {part.part_type.name if part.part_type else '?'}" for part in created]
        e.add_field(name="Parts created", value="\n".join(lines)[:1024] or "none", inline=False)
        e.add_field(
            name="Next step", value=f"Run `/allocate-cost donor_id:{donor.internal_id}` to allocate the donor's "
                                     f"£{donor.purchase_price} cost across these parts.",
            inline=False,
        )
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(embed=e, view=self)
        self.stop()


class AllocationModal(discord.ui.Modal, title="Allocate donor cost"):
    lines = discord.ui.TextInput(
        label="One 'PART_ID: value' per line",
        style=discord.TextStyle.paragraph,
        placeholder="PT-000044: 70\nPT-000045: 40\nPT-000046: 30",
        required=True,
    )

    def __init__(self, donor_internal_id: str, method: str):
        super().__init__()
        self.donor_internal_id = donor_internal_id
        self.method = method  # "MANUAL" | "PRO_RATA"

    async def on_submit(self, interaction: discord.Interaction) -> None:
        pairs: list[tuple[str, Decimal]] = []
        for raw_line in self.lines.value.splitlines():
            raw_line = raw_line.strip()
            if not raw_line:
                continue
            if ":" not in raw_line:
                await interaction.response.send_message(embed=error_embed(f"Couldn't parse line: `{raw_line}` (expected `PART_ID: value`)"), ephemeral=True)
                return
            part_id, _, value_str = raw_line.partition(":")
            try:
                value = Decimal(value_str.strip())
            except InvalidOperation:
                await interaction.response.send_message(embed=error_embed(f"Couldn't parse amount on line: `{raw_line}`"), ephemeral=True)
                return
            pairs.append((part_id.strip().upper(), value))

        async with session_scope() as session:
            donor = await donors_service.get_donor(session, self.donor_internal_id)
            if not donor:
                await interaction.response.send_message(embed=error_embed("Donor not found."), ephemeral=True)
                return
            from app.services.parts import get_part
            resolved = []
            for part_id_str, value in pairs:
                part = await get_part(session, part_id_str)
                if not part:
                    await interaction.response.send_message(embed=error_embed(f"Unknown part `{part_id_str}`."), ephemeral=True)
                    return
                resolved.append((part.id, value))

            try:
                if self.method == "MANUAL":
                    created = await allocation_service.allocate_manual(
                        session, donor, [allocation_service.ManualAllocationLine(part_id=pid, amount=v) for pid, v in resolved],
                        interaction.user.id,
                    )
                else:
                    created = await allocation_service.allocate_pro_rata(
                        session, donor, [allocation_service.ProRataLine(part_id=pid, assigned_value=v) for pid, v in resolved],
                        interaction.user.id,
                    )
            except allocation_service.AllocationError as e:
                await interaction.response.send_message(embed=error_embed(str(e)), ephemeral=True)
                return

        e = discord.Embed(title=f"✅ Cost allocated — {donor.internal_id}", colour=discord.Colour.green())
        total = sum((c.allocated_amount for c in created), Decimal("0"))
        for c in created:
            e.add_field(name=f"Part #{c.part_id}", value=f"£{c.allocated_amount}", inline=True)
        e.add_field(name="Total allocated", value=f"£{total} of £{donor.purchase_price}", inline=False)
        await interaction.response.send_message(embed=e)


class AllocationMethodView(discord.ui.View):
    def __init__(self, donor_internal_id: str, invoker_id: int):
        super().__init__(timeout=120)
        self.donor_internal_id = donor_internal_id
        self.invoker_id = invoker_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.invoker_id

    @discord.ui.button(label="Manual amounts", style=discord.ButtonStyle.primary)
    async def manual(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(AllocationModal(self.donor_internal_id, "MANUAL"))

    @discord.ui.button(label="Pro-rata (by relative value)", style=discord.ButtonStyle.secondary)
    async def pro_rata(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(AllocationModal(self.donor_internal_id, "PRO_RATA"))


class DonorsCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="donor", description="Buy a donor phone (for parts harvesting)")
    @require_authorized()
    async def donor(self, interaction: discord.Interaction):
        await interaction.response.send_modal(BuyDonorModal())

    @app_commands.command(name="donors", description="List donor phones")
    @require_authorized()
    async def donors(self, interaction: discord.Interaction, status: Optional[str] = None):
        async with session_scope() as session:
            donors_list = await donors_service.list_donors(session, status=status.upper() if status else None)
            items = [(d.internal_id, d.display_name(), d.current_status, d.purchase_price) for d in donors_list]

        def render(page_items, page, total_pages):
            e = discord.Embed(title=f"🔩 Donors ({len(items)} total)", colour=discord.Colour.blue())
            for internal_id, name, status_, price in page_items:
                e.add_field(name=f"{internal_id} — {name}", value=f"{status_} · £{price}", inline=False)
            e.set_footer(text=f"Page {page + 1}/{total_pages}")
            return e

        await send_paginated(interaction, items, per_page=10, render=render)

    @app_commands.command(name="teardown", description="Guided donor teardown: record recovered/discarded parts")
    @require_authorized()
    async def teardown(self, interaction: discord.Interaction, internal_id: str):
        async with session_scope() as session:
            donor = await donors_service.get_donor(session, internal_id)
            if not donor:
                await interaction.response.send_message(embed=error_embed(f"No donor found with ID `{internal_id}`."), ephemeral=True)
                return
            if donor.current_status != "AWAITING_TEARDOWN":
                await interaction.response.send_message(embed=error_embed(f"{internal_id} is not AWAITING_TEARDOWN (currently {donor.current_status})."), ephemeral=True)
                return
        view = TeardownView(interaction.user.id, donor)
        await interaction.response.send_message(embed=view.build_embed(), view=view)

    @app_commands.command(name="donor-parts", description="Show all parts recovered from a donor and their current status")
    @require_authorized()
    async def donor_parts(self, interaction: discord.Interaction, internal_id: str):
        async with session_scope() as session:
            donor = await donors_service.get_donor(session, internal_id)
            if not donor:
                await interaction.response.send_message(embed=error_embed(f"No donor found with ID `{internal_id}`."), ephemeral=True)
                return
            recovered = [p for p in donor.parts_recovered if p.status != "SCRAPPED"]
            discarded = [p for p in donor.parts_recovered if p.status == "SCRAPPED"]
            unallocated = await allocation_service.unallocated_amount(session, donor)
            embed = donor_embed(donor, recovered_count=len(recovered), discarded_count=len(discarded))
            for p in donor.parts_recovered:
                status_line = f"{p.status} · £{p.cost}"
                if p.installed_phone_id:
                    status_line += f" · installed in phone #{p.installed_phone_id}"
                embed.add_field(name=f"{p.internal_id} — {p.part_type.name if p.part_type else '?'}", value=status_line, inline=True)
            embed.add_field(name="Unallocated donor cost", value=f"£{unallocated}", inline=False)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="allocate-cost", description="Allocate a donor's purchase cost across recovered parts")
    @require_authorized()
    async def allocate_cost(self, interaction: discord.Interaction, donor_id: str):
        async with session_scope() as session:
            donor = await donors_service.get_donor(session, donor_id)
            if not donor:
                await interaction.response.send_message(embed=error_embed(f"No donor found with ID `{donor_id}`."), ephemeral=True)
                return
        view = AllocationMethodView(donor.internal_id, interaction.user.id)
        await interaction.response.send_message(
            f"How do you want to allocate **£{donor.purchase_price}** across {donor.internal_id}'s recovered parts?",
            view=view, ephemeral=True,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(DonorsCog(bot))
