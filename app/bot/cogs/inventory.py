from __future__ import annotations

import datetime as dt
from decimal import Decimal, InvalidOperation
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from app.bot.auth import require_authorized
from app.bot.embeds import error_embed, phone_embed, success_embed
from app.bot.pagination import send_paginated
from app.db import session_scope
from app.models.enums import AcquisitionType, LockStatus
from app.services import phones as phones_service
from app.services import testing as testing_service


class BuyPhoneModal(discord.ui.Modal, title="Buy a phone"):
    model = discord.ui.TextInput(label="Model (e.g. iPhone 13 Pro)", required=True, max_length=80)
    storage_colour = discord.ui.TextInput(label="Storage & Colour (e.g. 128GB Graphite)", required=False, max_length=60)
    imei_serial = discord.ui.TextInput(label="IMEI / Serial (optional)", required=False, max_length=60)
    price_source = discord.ui.TextInput(label="Purchase price & source (e.g. 250.00 / CEX)", required=True, max_length=100)
    notes = discord.ui.TextInput(label="Notes (optional)", required=False, style=discord.TextStyle.paragraph, max_length=500)

    def __init__(self, acquisition_type: AcquisitionType):
        super().__init__()
        self.acquisition_type = acquisition_type

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            price_str, _, source = self.price_source.value.partition("/")
            price = Decimal(price_str.strip())
        except InvalidOperation:
            await interaction.response.send_message(
                embed=error_embed("Couldn't parse the purchase price. Use e.g. `250.00 / CEX`."), ephemeral=True
            )
            return

        storage, colour = None, None
        if self.storage_colour.value:
            parts = self.storage_colour.value.split()
            storage = next((p for p in parts if "gb" in p.lower() or "tb" in p.lower()), None)
            colour = " ".join(p for p in parts if p != storage) or None

        imei, serial = None, None
        if self.imei_serial.value:
            bits = self.imei_serial.value.split("/")
            imei = bits[0].strip() or None
            serial = bits[1].strip() if len(bits) > 1 else None

        async with session_scope() as session:
            data = phones_service.BuyPhoneInput(
                manufacturer="Apple",
                model=self.model.value.strip(),
                variant=None,
                storage=storage,
                colour=colour,
                imei=imei,
                serial_number=serial,
                carrier=None,
                purchase_price=price,
                purchase_date=dt.date.today(),
                seller_source=source.strip() or None,
                acquisition_type=self.acquisition_type,
                notes=self.notes.value or None,
                location_code=None,
            )
            phone = await phones_service.buy_phone(session, data, interaction.user.id)
            embed = phone_embed(phone)

        embed.title = f"✅ Bought — {embed.title}"
        await interaction.response.send_message(embed=embed)


class InventoryCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="buy", description="Record the purchase of a phone (resale or repair)")
    @app_commands.describe(acquisition_type="Why you bought this phone")
    @app_commands.choices(acquisition_type=[
        app_commands.Choice(name="Resale (working, sell as-is)", value="RESALE"),
        app_commands.Choice(name="Repair (faulty, fix then sell)", value="REPAIR"),
    ])
    @require_authorized()
    async def buy(self, interaction: discord.Interaction, acquisition_type: app_commands.Choice[str]):
        modal = BuyPhoneModal(AcquisitionType(acquisition_type.value))
        await interaction.response.send_modal(modal)

    @app_commands.command(name="stock", description="List current phone inventory")
    @app_commands.describe(status="Filter by status (optional)")
    @require_authorized()
    async def stock(self, interaction: discord.Interaction, status: Optional[str] = None):
        async with session_scope() as session:
            phones = await phones_service.list_stock(session, status=status.upper() if status else None)
            items = [(p.internal_id, p.display_name(), p.current_status, p.location.code if p.location else "—", p.purchase_price) for p in phones]

        def render(page_items, page, total_pages):
            e = discord.Embed(title=f"📱 Phone stock ({len(items)} total)", colour=discord.Colour.blue())
            if not page_items:
                e.description = "No phones found."
            for internal_id, name, status_, loc, price in page_items:
                e.add_field(name=f"{internal_id} — {name}", value=f"{status_} · {loc} · £{price}", inline=False)
            e.set_footer(text=f"Page {page + 1}/{total_pages}")
            return e

        await send_paginated(interaction, items, per_page=10, render=render)

    @app_commands.command(name="phone", description="Show full detail for a phone")
    @app_commands.describe(internal_id="e.g. IP-000001")
    @require_authorized()
    async def phone(self, interaction: discord.Interaction, internal_id: str):
        async with session_scope() as session:
            p = await phones_service.get_phone(session, internal_id)
            if not p:
                await interaction.response.send_message(embed=error_embed(f"No phone found with ID `{internal_id}`."), ephemeral=True)
                return
            test_summary = await testing_service.summary(session, p)
            embed = phone_embed(p, faults=p.faults, test_summary=test_summary)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="edit-phone", description="Edit a phone's details")
    @require_authorized()
    async def edit_phone(
        self, interaction: discord.Interaction, internal_id: str,
        battery_health: Optional[int] = None, cosmetic_condition: Optional[str] = None,
        functional_condition: Optional[str] = None, colour: Optional[str] = None,
        notes: Optional[str] = None,
    ):
        fields = {k: v for k, v in {
            "battery_health": battery_health, "cosmetic_condition": cosmetic_condition,
            "functional_condition": functional_condition, "colour": colour, "notes": notes,
        }.items() if v is not None}
        if not fields:
            await interaction.response.send_message(embed=error_embed("Nothing to update — provide at least one field."), ephemeral=True)
            return
        async with session_scope() as session:
            p = await phones_service.get_phone(session, internal_id)
            if not p:
                await interaction.response.send_message(embed=error_embed(f"No phone found with ID `{internal_id}`."), ephemeral=True)
                return
            await phones_service.edit_phone(session, p, interaction.user.id, **fields)
            embed = phone_embed(p)
        await interaction.response.send_message(embed=success_embed(f"Updated {internal_id}"), embeds=[embed])

    @app_commands.command(name="move", description="Move a phone to a physical location")
    @require_authorized()
    async def move(self, interaction: discord.Interaction, internal_id: str, location_code: str):
        async with session_scope() as session:
            p = await phones_service.get_phone(session, internal_id)
            if not p:
                await interaction.response.send_message(embed=error_embed(f"No phone found with ID `{internal_id}`."), ephemeral=True)
                return
            await phones_service.move_phone(session, p, location_code, interaction.user.id)
        await interaction.response.send_message(embed=success_embed(f"Moved {internal_id} to {location_code.upper()}"))

    @app_commands.command(name="status", description="Change a phone's workflow status")
    @require_authorized()
    async def set_status(self, interaction: discord.Interaction, internal_id: str, new_status: str):
        async with session_scope() as session:
            p = await phones_service.get_phone(session, internal_id)
            if not p:
                await interaction.response.send_message(embed=error_embed(f"No phone found with ID `{internal_id}`."), ephemeral=True)
                return
            try:
                await phones_service.set_status(session, p, new_status.upper(), interaction.user.id)
            except phones_service.InvalidStatusError as e:
                await interaction.response.send_message(embed=error_embed(str(e)), ephemeral=True)
                return
        await interaction.response.send_message(embed=success_embed(f"{internal_id} is now {new_status.upper()}"))

    @app_commands.command(name="fault", description="Log a fault on a phone")
    @require_authorized()
    async def fault(self, interaction: discord.Interaction, internal_id: str, description: str, required_part_type: Optional[str] = None):
        async with session_scope() as session:
            p = await phones_service.get_phone(session, internal_id)
            if not p:
                await interaction.response.send_message(embed=error_embed(f"No phone found with ID `{internal_id}`."), ephemeral=True)
                return
            part_type_id = None
            if required_part_type:
                from app.services import config_service
                pt = await config_service.get_or_create_part_type(session, required_part_type)
                part_type_id = pt.id
            await phones_service.add_fault(session, p, description, interaction.user.id, required_part_type_id=part_type_id)
            if p.current_status in ("PURCHASED", "AWAITING_TEST", "TESTED"):
                await phones_service.set_status(session, p, "REPAIR_REQUIRED", interaction.user.id, notes="Fault logged")
        await interaction.response.send_message(embed=success_embed(f"Fault logged on {internal_id}", description))


async def setup(bot: commands.Bot):
    await bot.add_cog(InventoryCog(bot))
