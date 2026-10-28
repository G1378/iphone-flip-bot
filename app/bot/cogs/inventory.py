from __future__ import annotations

import datetime as dt
from decimal import Decimal, InvalidOperation
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from app.bot.auth import require_authorized
from app.bot.autocomplete import phone_colour_autocomplete, phone_model_autocomplete, phone_storage_autocomplete
from app.bot.embeds import error_embed, phone_embed, success_embed
from app.bot.phone_intake import ModelInfo, PhoneIntakeView, parse_price, start_intake
from app.bot.pagination import send_paginated
from app.db import session_scope
from app.models.enums import AcquisitionType, LockStatus
from app.services import config_service
from app.services import phones as phones_service
from app.services import testing as testing_service


class BuyPhoneModal(discord.ui.Modal, title="Buy a phone"):
    """The details dropdowns can't hold. One box per piece of information."""
    imei = discord.ui.TextInput(label="IMEI (optional)", required=False, max_length=30)
    serial = discord.ui.TextInput(label="Serial number (optional)", required=False, max_length=40)
    price = discord.ui.TextInput(label="Purchase price (£)", placeholder="250.00", required=True, max_length=12)
    source = discord.ui.TextInput(label="Bought from (e.g. CEX, eBay, Facebook)", required=False, max_length=80)
    notes = discord.ui.TextInput(label="Notes (optional)", required=False, style=discord.TextStyle.paragraph, max_length=500)

    def __init__(self, acquisition_type: AcquisitionType, model: ModelInfo, storage: Optional[str],
                 colour: Optional[str], wizard: PhoneIntakeView):
        super().__init__()
        self.acquisition_type, self.model, self.storage, self.colour, self.wizard = acquisition_type, model, storage, colour, wizard

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            price = parse_price(self.price.value)
        except ValueError as e:
            await interaction.response.send_message(embed=error_embed(str(e)), ephemeral=True)
            return
        async with session_scope() as session:
            data = phones_service.BuyPhoneInput(
                manufacturer="Apple",
                model=self.model.name,
                variant=None,
                storage=self.storage,
                colour=self.colour,
                imei=self.imei.value.strip() or None,
                serial_number=self.serial.value.strip() or None,
                carrier=None,
                purchase_price=price,
                purchase_date=dt.date.today(),
                seller_source=self.source.value.strip() or None,
                acquisition_type=self.acquisition_type,
                notes=self.notes.value or None,
                location_code=None,
            )
            try:
                phone = await phones_service.buy_phone(session, data, interaction.user.id)
            except config_service.UnknownPhoneModelError as e:
                await interaction.response.send_message(embed=error_embed(str(e)), ephemeral=True)
                return
            embed = phone_embed(phone)

        embed.title = f"✅ Bought — {embed.title}"
        await self.wizard.complete(interaction, embed)


class InventoryCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="buy", description="Record the purchase of a phone (resale or repair)")
    @app_commands.describe(acquisition_type="Why you bought this phone",
                           model="Optional: skip the generation/model dropdowns by naming the exact model")
    @app_commands.choices(acquisition_type=[
        app_commands.Choice(name="Resale (working, sell as-is)", value="RESALE"),
        app_commands.Choice(name="Repair (faulty, fix then sell)", value="REPAIR"),
    ])
    @app_commands.autocomplete(model=phone_model_autocomplete)
    @require_authorized()
    async def buy(self, interaction: discord.Interaction, acquisition_type: app_commands.Choice[str],
                  model: Optional[str] = None):
        acq = AcquisitionType(acquisition_type.value)
        await start_intake(
            interaction, heading="📱 Buy a phone", model_name=model,
            build_modal=lambda m, storage, colour, wizard: BuyPhoneModal(acq, m, storage, colour, wizard))

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
    @app_commands.describe(model="Correct the phone's exact model (blocked while parts are reserved or fitted)",
                           storage="Pick one of the sizes this model came in",
                           colour="Pick one of the colours this model came in")
    @app_commands.autocomplete(model=phone_model_autocomplete, storage=phone_storage_autocomplete,
                               colour=phone_colour_autocomplete)
    @require_authorized()
    async def edit_phone(
        self, interaction: discord.Interaction, internal_id: str,
        battery_health: Optional[int] = None, cosmetic_condition: Optional[str] = None,
        functional_condition: Optional[str] = None, colour: Optional[str] = None,
        notes: Optional[str] = None, model: Optional[str] = None, storage: Optional[str] = None,
    ):
        fields = {k: v for k, v in {
            "battery_health": battery_health, "cosmetic_condition": cosmetic_condition,
            "functional_condition": functional_condition, "colour": colour, "notes": notes,
            "storage": config_service.normalise_storage(storage) if storage else None,
        }.items() if v is not None}
        if not fields and not model:
            await interaction.response.send_message(embed=error_embed("Nothing to update — provide at least one field."), ephemeral=True)
            return
        async with session_scope() as session:
            p = await phones_service.get_phone(session, internal_id)
            if not p:
                await interaction.response.send_message(embed=error_embed(f"No phone found with ID `{internal_id}`."), ephemeral=True)
                return
            try:
                if model:
                    await phones_service.change_phone_model(session, p, model, interaction.user.id)
                if fields:
                    await phones_service.edit_phone(session, p, interaction.user.id, **fields)
            except (config_service.UnknownPhoneModelError, phones_service.PhoneModelChangeError) as e:
                await interaction.response.send_message(embed=error_embed(str(e)), ephemeral=True)
                return
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
                pt = await config_service.find_part_type(session, required_part_type)
                if pt is None:
                    valid = ", ".join(t.name for t in await config_service.list_part_types(session))
                    await interaction.response.send_message(
                        embed=error_embed(f"Unknown part type `{required_part_type}`. Known part types: {valid}"), ephemeral=True)
                    return
                part_type_id = pt.id
            await phones_service.add_fault(session, p, description, interaction.user.id, required_part_type_id=part_type_id)
            if p.current_status in ("PURCHASED", "AWAITING_TEST", "TESTED"):
                await phones_service.set_status(session, p, "REPAIR_REQUIRED", interaction.user.id, notes="Fault logged")
        await interaction.response.send_message(embed=success_embed(f"Fault logged on {internal_id}", description))


async def setup(bot: commands.Bot):
    await bot.add_cog(InventoryCog(bot))
