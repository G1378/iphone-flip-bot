from __future__ import annotations

import datetime as dt
from decimal import Decimal, InvalidOperation
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from app.bot.auth import require_authorized
from app.bot.embeds import error_embed, success_embed
from app.bot.pagination import send_paginated
from app.bot.views import ConfirmView
from app.db import session_scope
from app.integrations.ebay.interface import EbayApiError, EbayNotConfiguredError
from app.jobs.ebay_sync_job import sync_ebay_orders
from app.services import config_service
from app.services import listings as listings_service
from app.services import phones as phones_service
from app.services import pricing as pricing_service


def _default_title(phone) -> str:
    bits = [phone.manufacturer, phone.model, phone.variant, phone.storage, phone.colour, phone.lock_status.value.title()]
    return " ".join(b for b in bits if b)[:80]


def _default_description(phone) -> str:
    lines = [
        f"{phone.display_name()}",
        f"Functional condition: {phone.functional_condition or 'Fully tested and working'}",
        f"Cosmetic condition: {phone.cosmetic_condition or 'See photos'}",
    ]
    if phone.battery_health:
        lines.append(f"Battery health: {phone.battery_health}%")
    lines.append(f"IMEI: {phone.imei or 'available on request'}")
    return "\n".join(lines)


class ListingDraftModal(discord.ui.Modal, title="eBay listing draft"):
    listing_title = discord.ui.TextInput(label="Title", max_length=80, required=True)
    price = discord.ui.TextInput(label="Price (GBP)", max_length=12, required=True)
    condition_text = discord.ui.TextInput(label="Condition (e.g. Excellent, Grade A-)", max_length=60, required=True)
    description = discord.ui.TextInput(label="Description", style=discord.TextStyle.paragraph, max_length=1000, required=True)

    def __init__(self, phone_internal_id: str, existing_listing_internal_id: Optional[str] = None,
                 defaults: Optional[dict] = None):
        super().__init__()
        self.phone_internal_id = phone_internal_id
        self.existing_listing_internal_id = existing_listing_internal_id
        if defaults:
            self.listing_title.default = defaults.get("title")
            self.price.default = str(defaults.get("price", ""))
            self.condition_text.default = defaults.get("condition_text")
            self.description.default = defaults.get("description")

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            price = Decimal(self.price.value.strip())
        except InvalidOperation:
            await interaction.response.send_message(embed=error_embed("Couldn't parse price."), ephemeral=True)
            return

        async with session_scope() as session:
            phone = await phones_service.get_phone(session, self.phone_internal_id)
            if not phone:
                await interaction.response.send_message(embed=error_embed("Phone not found."), ephemeral=True)
                return
            draft = listings_service.DraftListingInput(
                title=self.listing_title.value, description=self.description.value,
                condition_text=self.condition_text.value, price=price,
            )
            listing = await listings_service.create_draft(session, phone, draft, interaction.user.id)

        e = discord.Embed(title=f"📝 Draft listing — {listing.internal_id}", colour=discord.Colour.blue())
        e.add_field(name="Title", value=listing.title, inline=False)
        e.add_field(name="Price", value=f"£{listing.price}", inline=True)
        e.add_field(name="Condition", value=listing.condition_text, inline=True)
        e.add_field(name="Description", value=listing.description[:1024], inline=False)
        e.set_footer(text="Review, then Publish to push this live to eBay.")

        view = ListingPreviewView(interaction.user.id, listing.internal_id, phone.internal_id)
        await interaction.response.send_message(embed=e, view=view)


class ListingPreviewView(discord.ui.View):
    def __init__(self, invoker_id: int, listing_internal_id: str, phone_internal_id: str):
        super().__init__(timeout=600)
        self.invoker_id = invoker_id
        self.listing_internal_id = listing_internal_id
        self.phone_internal_id = phone_internal_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.invoker_id

    @discord.ui.button(label="Edit", style=discord.ButtonStyle.secondary, emoji="✏️")
    async def edit(self, interaction: discord.Interaction, button: discord.ui.Button):
        async with session_scope() as session:
            listing = await listings_service.get_listing(session, self.listing_internal_id)
            defaults = {"title": listing.title, "price": listing.price, "condition_text": listing.condition_text,
                        "description": listing.description}
        await interaction.response.send_modal(
            ListingDraftModal(self.phone_internal_id, self.listing_internal_id, defaults=defaults)
        )

    @discord.ui.button(label="Publish", style=discord.ButtonStyle.success, emoji="🚀")
    async def publish(self, interaction: discord.Interaction, button: discord.ui.Button):
        bot = interaction.client

        async def do_publish(inner: discord.Interaction):
            async with session_scope() as session:
                listing = await listings_service.get_listing(session, self.listing_internal_id)
                try:
                    published = await listings_service.publish(session, listing, bot.ebay_client, inner.user.id)
                except EbayNotConfiguredError:
                    await inner.response.edit_message(
                        content="eBay is not configured yet - ask an admin to set it up (`/config ebay`). The draft is saved.",
                        embed=None, view=None,
                    )
                    return
                except EbayApiError as e:
                    await inner.response.edit_message(content=f"eBay API error: {e}", embed=None, view=None)
                    return
            e = discord.Embed(title=f"🚀 Published — {published.internal_id}", colour=discord.Colour.green())
            e.add_field(name="eBay listing ID", value=published.ebay_listing_id, inline=True)
            if published.ebay_url:
                e.add_field(name="URL", value=published.ebay_url, inline=False)
            await inner.response.edit_message(content=None, embed=e, view=None)

        confirm_view = ConfirmView(interaction.user.id, do_publish, confirm_label="Yes, publish to eBay")
        await interaction.response.send_message(
            "Publish this listing to eBay now? This cannot be silently undone.", view=confirm_view, ephemeral=True,
        )

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.danger)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(content="Draft left unpublished — you can `/list` again anytime.", view=self)
        self.stop()


class EbayCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="list", description="Draft and publish an eBay listing for a phone")
    @require_authorized()
    async def list_phone(self, interaction: discord.Interaction, internal_id: str):
        async with session_scope() as session:
            phone = await phones_service.get_phone(session, internal_id)
            if not phone:
                await interaction.response.send_message(embed=error_embed(f"No phone found with ID `{internal_id}`."), ephemeral=True)
                return
            phone_model = await config_service.get_or_create_phone_model(session, phone.manufacturer, phone.model, phone.variant)
            default_price = await pricing_service.get_current_estimated_sale_price(session, phone) or Decimal("0")

        defaults = {
            "title": _default_title(phone), "price": default_price,
            "condition_text": phone.functional_condition or "Excellent - Fully Tested",
            "description": _default_description(phone),
        }
        await interaction.response.send_modal(ListingDraftModal(phone.internal_id, defaults=defaults))

    @app_commands.command(name="listing", description="Show a listing's current status")
    @require_authorized()
    async def listing(self, interaction: discord.Interaction, internal_id: str):
        async with session_scope() as session:
            listing = await listings_service.get_listing(session, internal_id)
            if not listing:
                await interaction.response.send_message(embed=error_embed(f"No listing found with ID `{internal_id}`."), ephemeral=True)
                return
            e = discord.Embed(title=f"📋 {listing.internal_id} — {listing.title}", colour=discord.Colour.blue())
            e.add_field(name="Status", value=listing.status, inline=True)
            e.add_field(name="Price", value=f"£{listing.price}", inline=True)
            e.add_field(name="Phone", value=listing.phone.internal_id, inline=True)
            if listing.ebay_listing_id:
                e.add_field(name="eBay listing ID", value=listing.ebay_listing_id, inline=True)
            if listing.ebay_url:
                e.add_field(name="URL", value=listing.ebay_url, inline=False)
        await interaction.response.send_message(embed=e)

    @app_commands.command(name="ebay-sync", description="Manually trigger an eBay order sync now")
    @require_authorized()
    async def ebay_sync(self, interaction: discord.Interaction, lookback_hours: int = 48):
        await interaction.response.defer(thinking=True)
        since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=lookback_hours)
        try:
            async with session_scope() as session:
                results = await sync_ebay_orders(session, self.bot.ebay_client, since=since, actor_discord_id=interaction.user.id)
        except EbayNotConfiguredError:
            await interaction.followup.send(embed=error_embed("eBay is not configured. See `/config ebay`."))
            return
        except EbayApiError as e:
            await interaction.followup.send(embed=error_embed(f"eBay API error: {e}"))
            return

        new_sales = [r for r in results if r.matched and not r.already_recorded]
        e = discord.Embed(title="🔄 eBay sync complete", colour=discord.Colour.blue())
        e.add_field(name="Orders checked", value=str(len(results)), inline=True)
        e.add_field(name="New sales recorded", value=str(len(new_sales)), inline=True)
        for r in new_sales:
            e.add_field(name=r.phone_internal_id, value=f"Sale {r.sale_internal_id} · Net profit £{r.net_profit}", inline=False)
        await interaction.followup.send(embed=e)

    @app_commands.command(name="ebay-status", description="Show eBay integration status")
    @require_authorized()
    async def ebay_status(self, interaction: discord.Interaction):
        configured = self.bot.ebay_client.configured
        e = discord.Embed(title="eBay integration status", colour=discord.Colour.green() if configured else discord.Colour.red())
        e.add_field(name="Configured", value="✅ Yes" if configured else "❌ No", inline=True)
        job = self.bot.job_scheduler.state.ebay if self.bot.job_scheduler else None
        if job and job.last_run_at:
            e.add_field(name="Last sync", value=str(job.last_run_at), inline=True)
            e.add_field(name="Last result", value=job.last_summary or job.last_error or "—", inline=False)
        await interaction.response.send_message(embed=e)


async def setup(bot: commands.Bot):
    await bot.add_cog(EbayCog(bot))
