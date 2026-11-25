"""The buy page: one card per deal, with [Bought] [Reject] buttons.

Buttons are `DynamicItem`s (the same pattern as the job-channel buttons in cogs/jobs.py): the action and
deal id live in the custom id, so cards keep working after the bot restarts and need no re-registering.

  deal:bought:<id>  opens a short form (what you actually paid, optional IMEI...) and, on submit, creates the
                    phone in inventory (services/deal_queue.mark_bought)
  deal:reject:<id>  one tap, no form: marks it rejected and greys the card out
  deal:undo:<id>    puts a rejected card back (a mis-tap on a phone is easy)

Whatever happens, the card is edited in place to show the new state, so the channel doubles as a log.
"""
from __future__ import annotations

import datetime as dt
import logging
import re
from decimal import Decimal
from typing import Optional

import discord

from app.bot.auth import _is_authorized
from app.bot.embeds import error_embed, phone_embed
from app.bot.phone_intake import parse_price
from app.db import session_scope
from app.models.enums import DealSource, DealStatus
from app.models.pricing_models import DealAlert
from app.services import config_service, deal_queue

logger = logging.getLogger(__name__)

BUY_PAGE_PURPOSE = "buy-page"      # the bot_channels purpose key (see cogs/home.py CHANNEL_PLAN)


def _gbp(value) -> str:
    return f"£{Decimal(value):,.2f}"


def _model_line(a: DealAlert) -> str:
    return f"{a.phone_model.model}{' ' + a.storage if a.storage else ''}"


def _how(a: DealAlert, now: dt.datetime) -> str:
    if a.is_auction and a.ends_at:
        mins = max(0, int((a.ends_at - now).total_seconds() // 60))
        return f"auction · ends in {mins // 60}h {mins % 60:02d}m · current bid, it may rise"
    return "buy it now" if a.source == DealSource.EBAY.value else "fixed price · make an offer"


_STATE = {
    DealStatus.BOUGHT.value: ("✅ BOUGHT", discord.Colour.blue()),
    DealStatus.REJECTED.value: ("❌ REJECTED", discord.Colour.dark_grey()),
    DealStatus.EXPIRED.value: ("⌛ EXPIRED", discord.Colour.light_grey()),
    DealStatus.ARCHIVED.value: ("🗄️ ARCHIVED", discord.Colour.light_grey()),
}


def deal_embed(a: DealAlert, now: Optional[dt.datetime] = None) -> discord.Embed:
    """The card. Laid out for a phone screen: the three numbers that decide it come first."""
    now = now or dt.datetime.now(dt.timezone.utc)
    faulty = a.bucket == "FAULTY"
    prefix, colour = _STATE.get(a.status, (None, discord.Colour.orange() if faulty else discord.Colour.green()))
    title = f"{_model_line(a)} · {'🔧 faulty' if faulty else 'working'}"
    e = discord.Embed(title=(f"{prefix} — {title}" if prefix else title)[:256], url=a.url, colour=colour)
    source = "🟢 Vinted" if a.source == DealSource.VINTED.value else "🔵 eBay"
    desc = f"**{a.title[:200]}**\n{source} · {_how(a, now)}"
    if a.status == DealStatus.BOUGHT.value and a.decided_by_discord_id:
        desc += f"\nBought by <@{a.decided_by_discord_id}>"
    elif a.status == DealStatus.REJECTED.value and a.decided_by_discord_id:
        desc += f"\nRejected by <@{a.decided_by_discord_id}>" + (f": {a.decision_note}" if a.decision_note else "")
    elif a.status == DealStatus.EXPIRED.value and a.decision_note:
        desc += f"\nExpired: {a.decision_note}"
    e.description = desc

    parts = [f"{_gbp(a.price)} item", f"{_gbp(a.postage)} post"]
    if a.buyer_fee:
        parts.append(f"{_gbp(a.buyer_fee)} fee")
    e.add_field(name="You'd pay", value=f"**{_gbp(a.total_cost)}**\n" + " + ".join(parts), inline=True)
    if a.resale_estimate is not None:
        e.add_field(name="Sells for", value=f"**{_gbp(a.resale_estimate)}**\n{a.resale_basis or ''}", inline=True)
    if a.expected_profit is not None:
        e.add_field(name="Profit", value=f"**{_gbp(a.expected_profit)}**\nROI {a.roi_pct}%", inline=True)
    if a.max_item_price is not None:
        e.add_field(name="Pay up to", value=f"**{_gbp(a.max_item_price)}**\nfor the phone (max all-in {_gbp(a.max_buy)})", inline=True)
    if faulty:
        e.add_field(name="Repair estimate", value=f"**{_gbp(a.repair_estimate)}**\n{(a.repair_basis or 'flat assumption')[:300]}", inline=False)
    seller = a.seller or "?"
    if a.seller_feedback_pct is not None:
        seller += f" · {a.seller_feedback_pct}%"
    if a.seller_reviews is not None:
        seller += f" · {a.seller_reviews} review{'s' if a.seller_reviews != 1 else ''}"
    e.add_field(name="Seller", value=seller[:1000], inline=False)
    if a.flags and a.status == DealStatus.PENDING.value:
        lines = [f"• {f.strip()}" for f in a.flags.split(";") if f.strip()]
        e.add_field(name="⚠️ Check before buying", value="\n".join(lines)[:1000], inline=False)
    if a.image_url:
        e.set_thumbnail(url=a.image_url)
    e.set_footer(text=f"Deal #{a.id} · found {a.created_at.strftime('%d %b %H:%M') if a.created_at else 'just now'}")
    return e


def deal_view(a: DealAlert) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    if a.status == DealStatus.PENDING.value:
        view.add_item(DealButton("bought", a.id, "Bought", discord.ButtonStyle.success))
        view.add_item(DealButton("reject", a.id, "Reject", discord.ButtonStyle.danger))
    elif a.status == DealStatus.REJECTED.value:
        view.add_item(DealButton("undo", a.id, "Undo reject", discord.ButtonStyle.secondary))
    if a.url:
        view.add_item(discord.ui.Button(label="Open listing", url=a.url, emoji="🔗"))
    return view


class DealButton(discord.ui.DynamicItem[discord.ui.Button], template=r"deal:(?P<act>[a-z]+):(?P<id>[0-9]+)"):
    def __init__(self, act: str, deal_id: int, label: str = "", style: discord.ButtonStyle = discord.ButtonStyle.secondary):
        super().__init__(discord.ui.Button(label=label or act, style=style, custom_id=f"deal:{act}:{deal_id}"))
        self.act, self.deal_id = act, deal_id

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match[str], /):
        return cls(match["act"], int(match["id"]), item.label or "", item.style)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if await _is_authorized(interaction.user.id):
            return True
        await interaction.response.send_message("🔒 You're not authorized to use this bot.", ephemeral=True)
        return False

    async def callback(self, interaction: discord.Interaction) -> None:
        try:
            if self.act == "bought":
                await self._open_bought_form(interaction)
            elif self.act == "reject":
                await self._reject(interaction)
            elif self.act == "undo":
                await self._undo(interaction)
            else:
                await interaction.response.send_message("That button isn't recognised any more.", ephemeral=True)
        except discord.HTTPException:
            logger.exception("Buy-page button failed")

    # ------------------------------------------------------------------ actions
    async def _stale(self, interaction: discord.Interaction, message: str) -> None:
        """The button was pressed on a card whose state has moved on: say so, and bring the card up to date."""
        await interaction.response.send_message(embed=error_embed(message), ephemeral=True)
        async with session_scope() as session:
            alert = await deal_queue.get_alert(session, self.deal_id)
            if alert is not None and interaction.message is not None:
                try:
                    await interaction.message.edit(embed=deal_embed(alert), view=deal_view(alert))
                except discord.HTTPException:
                    pass

    async def _open_bought_form(self, interaction: discord.Interaction) -> None:
        async with session_scope() as session:
            alert = await deal_queue.get_alert(session, self.deal_id)
        if alert is None or alert.status != DealStatus.PENDING.value:
            await self._stale(interaction, f"Deal #{self.deal_id} is already {alert.status.lower() if alert else 'gone'}.")
            return
        await interaction.response.send_modal(BoughtModal(alert))

    async def _reject(self, interaction: discord.Interaction) -> None:
        async with session_scope() as session:
            try:
                alert = await deal_queue.mark_rejected(session, self.deal_id, actor_discord_id=interaction.user.id)
            except deal_queue.DealStateError as e:
                msg = str(e)
            else:
                msg = None
                embed, view = deal_embed(alert), deal_view(alert)
        if msg:
            await self._stale(interaction, msg)
            return
        await interaction.response.edit_message(embed=embed, view=view)

    async def _undo(self, interaction: discord.Interaction) -> None:
        async with session_scope() as session:
            try:
                alert = await deal_queue.restore_rejected(session, self.deal_id, actor_discord_id=interaction.user.id)
            except deal_queue.DealStateError as e:
                msg = str(e)
            else:
                msg = None
                embed, view = deal_embed(alert), deal_view(alert)
        if msg:
            await self._stale(interaction, msg)
            return
        await interaction.response.edit_message(embed=embed, view=view)


class BoughtModal(discord.ui.Modal, title="Mark as bought"):
    """What you actually paid and, if you have them, the details. Inputs are built per instance because
    class-level inputs would be shared between everyone who opens the form at once."""

    def __init__(self, alert: DealAlert) -> None:
        super().__init__()
        self.deal_id = alert.id
        self.paid = discord.ui.TextInput(label="Total paid incl. postage & fees (£)", default=str(alert.total_cost), max_length=12)
        self.storage = discord.ui.TextInput(label="Storage (optional)", default=alert.storage, required=False, max_length=10,
                                            placeholder="e.g. 128GB")
        self.colour = discord.ui.TextInput(label="Colour (optional)", required=False, max_length=40)
        self.imei = discord.ui.TextInput(label="IMEI (optional)", required=False, max_length=30)
        self.notes = discord.ui.TextInput(label="Notes (optional)", required=False, style=discord.TextStyle.paragraph, max_length=400)
        for item in (self.paid, self.storage, self.colour, self.imei, self.notes):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            paid = parse_price(self.paid.value)
        except ValueError as e:
            await interaction.response.send_message(embed=error_embed(str(e)), ephemeral=True)
            return
        async with session_scope() as session:
            try:
                alert, phone = await deal_queue.mark_bought(
                    session, self.deal_id, total_paid=paid, actor_discord_id=interaction.user.id,
                    storage=self.storage.value, colour=self.colour.value, imei=self.imei.value, notes=self.notes.value)
            except (deal_queue.DealStateError, config_service.UnknownPhoneModelError) as e:
                await interaction.response.send_message(embed=error_embed(str(e)), ephemeral=True)
                return
            card, view = deal_embed(alert), deal_view(alert)
            phone_card = phone_embed(phone)
        phone_card.title = f"✅ Added to inventory — {phone_card.title}"
        if interaction.message is not None:
            await interaction.response.edit_message(embed=card, view=view)
            await interaction.followup.send(embed=phone_card)
        else:
            await interaction.response.send_message(embed=phone_card)


# ---------------------------------------------------------------------------------- posting
async def _resolve_channel(bot, channel_id: int):
    channel = bot.get_channel(channel_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(channel_id)
        except discord.HTTPException:
            return None
    return channel


async def post_deal_cards(bot, alert_ids: list[int], fallback_channel=None) -> int:
    """Post a card for each pending deal that has none yet. Goes to the buy-page channel; if there isn't one,
    to `fallback_channel` (where /deal-scan was run), else the notification channels. Returns how many were posted."""
    async with session_scope() as session:
        buy_page_id = await config_service.get_channel_id_by_purpose(session, BUY_PAGE_PURPOSE)
        notify_ids = await config_service.notification_channel_ids(session)
    target = await _resolve_channel(bot, buy_page_id) if buy_page_id else None
    if target is None and fallback_channel is not None:
        target = fallback_channel
    if target is None:
        for cid in notify_ids:
            target = await _resolve_channel(bot, cid)
            if target is not None:
                break
    if target is None:
        logger.warning("Buy page: no channel to post %d deal card(s) in. Run /setup-channels.", len(alert_ids))
        return 0

    posted = 0
    for alert_id in alert_ids:
        async with session_scope() as session:
            alert = await deal_queue.get_alert(session, alert_id)
            if alert is None or alert.status != DealStatus.PENDING.value or alert.message_id:
                continue
            embed, view = deal_embed(alert), deal_view(alert)
        try:
            message = await target.send(embed=embed, view=view)
        except discord.HTTPException as e:
            logger.warning("Could not post deal card #%s: %s", alert_id, e)
            continue
        async with session_scope() as session:
            await deal_queue.record_message(session, alert_id, target.id, message.id)
        posted += 1
    return posted


async def refresh_card(bot, alert: DealAlert) -> None:
    """Bring an existing card up to date (e.g. after it expired). Quietly does nothing if it can't."""
    if not (alert.message_channel_id and alert.message_id):
        return
    channel = await _resolve_channel(bot, alert.message_channel_id)
    if channel is None:
        return
    try:
        await channel.get_partial_message(alert.message_id).edit(embed=deal_embed(alert), view=deal_view(alert))
    except discord.HTTPException:
        logger.debug("Could not refresh card for deal #%s", alert.id, exc_info=True)
