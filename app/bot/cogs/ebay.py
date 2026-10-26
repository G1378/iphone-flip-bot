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
from app.models.inventory_models import Phone
from app.services import config_service, listing_builder
from app.services import listings as listings_service
from app.services import phones as phones_service
from app.services import pricing as pricing_service


MAX_PHOTO_BYTES = 10_000_000


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
    description = discord.ui.TextInput(label="Description", style=discord.TextStyle.paragraph, max_length=4000, required=True)

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
                except listings_service.ListingNotReady as e:
                    await inner.response.edit_message(content=str(e), embed=None, view=None)
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

    @app_commands.command(name="list", description="Build the eBay listing for a phone from its record, ready to publish")
    @require_authorized()
    async def list_phone(self, interaction: discord.Interaction, internal_id: str):
        async with session_scope() as session:
            phone = await phones_service.get_phone(session, internal_id)
            if not phone:
                await interaction.response.send_message(embed=error_embed(f"No phone found with ID `{internal_id}`."), ephemeral=True)
                return
            listing, built = await listings_service.build_or_refresh_draft(session, phone, interaction.user.id)
            todo = listing_builder.blockers(listing, phone)
            e = discord.Embed(title=f"📝 Draft listing — {listing.internal_id}", colour=discord.Colour.blue())
            e.add_field(name="Title", value=listing.title, inline=False)
            e.add_field(name="Price", value=f"£{listing.price}", inline=True)
            e.add_field(name="Condition", value=listing.condition_text or "—", inline=True)
            e.add_field(name="Photos", value=str(len(phone.photo_urls or [])), inline=True)
            e.add_field(name="Best Offer", value=(await listing_builder.best_offer_terms(session, listing, phone)).summary(),
                        inline=False)
            e.add_field(name="Description", value=listing.description[:1024], inline=False)
            if built.warnings:
                e.add_field(name="Worth a look", value="\n".join(f"⚠️ {w}" for w in built.warnings)[:1024], inline=False)
            if todo:
                e.add_field(name="Before it can be published", value="\n".join(f"• {t}" for t in todo)[:1024], inline=False)
            e.set_footer(text="Edit if needed, then Publish. Nothing goes live until you press it.")
            view = ListingPreviewView(interaction.user.id, listing.internal_id, phone.internal_id)
        await interaction.response.send_message(embed=e, view=view)

    @app_commands.command(name="photos", description="Add photos to a phone's eBay listing (in its job channel, no ID needed)")
    @app_commands.describe(photo1="Main photo (first one shows in search)", phone="Phone ID; leave blank inside its job channel",
                           replace="Replace the existing photos instead of adding to them")
    @require_authorized()
    async def photos(self, interaction: discord.Interaction, photo1: discord.Attachment,
                     photo2: Optional[discord.Attachment] = None, photo3: Optional[discord.Attachment] = None,
                     photo4: Optional[discord.Attachment] = None, photo5: Optional[discord.Attachment] = None,
                     photo6: Optional[discord.Attachment] = None, photo7: Optional[discord.Attachment] = None,
                     photo8: Optional[discord.Attachment] = None, phone: Optional[str] = None, replace: bool = False):
        from app.services import jobs as jobs_service
        files = [p for p in (photo1, photo2, photo3, photo4, photo5, photo6, photo7, photo8) if p is not None]
        bad = [f.filename for f in files if not (f.content_type or "").startswith("image/") or f.size > MAX_PHOTO_BYTES]
        if bad:
            await interaction.response.send_message(embed=error_embed(
                f"Only images up to {MAX_PHOTO_BYTES // 1_000_000} MB: {', '.join(bad)}"), ephemeral=True)
            return
        if not self.bot.ebay_client.configured:
            await interaction.response.send_message(embed=error_embed(
                "eBay isn't connected, so there's nowhere to host the photos yet (`/config ebay`)."), ephemeral=True)
            return
        await interaction.response.defer(thinking=True)
        async with session_scope() as session:
            if phone:
                target = await phones_service.get_phone(session, phone)
            else:
                job = await jobs_service.get_job_by_channel(session, interaction.channel_id)
                target = await session.get(Phone, job.phone_id) if job else None
            if target is None:
                await interaction.followup.send(embed=error_embed(
                    "Which phone? Run this in its job channel, or give the phone ID."))
                return
            urls = []
            try:
                for f in files:
                    urls.append(await self.bot.ebay_client.upload_image(await f.read(), f.filename, f.content_type))
            except EbayApiError as err:
                await interaction.followup.send(embed=error_embed(f"eBay wouldn't take a photo ({len(urls)} saved): {err}"))
                if urls:
                    await phones_service.add_photos(session, target, urls, interaction.user.id, replace=replace)
                return
            await phones_service.add_photos(session, target, urls, interaction.user.id, replace=replace)
            total, phone_id, label = len(target.photo_urls), target.id, target.internal_id
        await interaction.followup.send(embed=success_embed(f"📷 {len(urls)} photo(s) added to {label} ({total} total)."))
        jobs_cog = self.bot.get_cog("JobsCog")
        if jobs_cog:
            await jobs_cog.refresh_job(phone_id, repost=False)

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
            e.add_field(name=r.phone_internal_id, value=f"Sale {r.sale_internal_id} · Net profit £{r.net_profit}"
                        + (f"\n⚠️ {r.note}" if r.note else ""), inline=False)
        for r in (r for r in results if r.note and not (r.matched and not r.already_recorded)):
            e.add_field(name=f"Order {r.ebay_order_id}", value=f"⚠️ {r.note}", inline=False)
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
