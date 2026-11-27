"""The buy page: deals from eBay and Vinted that beat your profit rules, plus the eBay part finder."""
from __future__ import annotations

from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from app.bot.auth import require_authorized
from app.bot.autocomplete import phone_model_autocomplete
from app.bot.deal_cards import DealButton, post_deal_cards
from app.bot.embeds import error_embed
from app.db import session_scope
from app.integrations.ebay.interface import EbayApiError, EbayNotConfiguredError
from app.integrations.vinted.interface import VintedNotConfiguredError
from app.models.enums import DealSource, DealStatus
from app.services import config_service, deal_queue
from app.services import deals as deals_service

SOURCE_CHOICES = [
    app_commands.Choice(name="eBay and Vinted", value="all"),
    app_commands.Choice(name="eBay only", value="ebay"),
    app_commands.Choice(name="Vinted only", value="vinted"),
]
STATUS_CHOICES = [
    app_commands.Choice(name="Waiting for you", value="PENDING"),
    app_commands.Choice(name="Bought", value="BOUGHT"),
    app_commands.Choice(name="Rejected", value="REJECTED"),
    app_commands.Choice(name="Expired", value="EXPIRED"),
]


def _summary(label: str, result: deals_service.ScanResult) -> str:
    skipped = ", ".join(f"{n} {why}" for why, n in sorted(result.skipped.items(), key=lambda kv: -kv[1])) or "none"
    line = (f"**{label}**: looked at **{result.checked}** listings across **{result.watched}** watched model(s): "
            f"**{len(result.candidates)}** deal(s).\nSkipped: {skipped}.")
    if result.blocked:
        line += "\n🚧 Blocked by the site: the scan stopped early and will back off."
    if result.errors:
        line += "\n⚠️ " + "; ".join(result.errors[:3])
    return line


class DealsCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        # Buy-page buttons keep working across restarts: their ids carry the action and deal id.
        self.bot.add_dynamic_items(DealButton)

    # ------------------------------------------------------------------ scanning
    @app_commands.command(name="deal-scan", description="Look now for deals on the models you watch (cards go to the buy page)")
    @app_commands.describe(source="Where to look (default: everywhere that's switched on)")
    @app_commands.choices(source=SOURCE_CHOICES)
    @require_authorized()
    async def deal_scan(self, interaction: discord.Interaction, source: Optional[app_commands.Choice[str]] = None):
        await interaction.response.defer(thinking=True)
        which = source.value if source else "all"
        lines: list[str] = []
        watched = 0
        unavailable = False

        if which in ("all", "ebay"):
            try:
                async with deals_service.scan_lock(DealSource.EBAY.value):
                    async with session_scope() as session:
                        result = await deals_service.scan_deals(session, self.bot.ebay_client)
                lines.append(_summary("eBay", result))
                watched = max(watched, result.watched)
            except EbayNotConfiguredError:
                lines.append("**eBay**: isn't connected yet (`/config ebay`).")
                unavailable = True

        if which in ("all", "vinted"):
            vinted = self.bot.vinted_client
            if not vinted.configured:
                # An optional source stays quiet under "all", but says why if it was asked for by name.
                if which == "vinted":
                    lines.append(f"**Vinted**: {vinted.health().reason or 'not enabled'}")
                    unavailable = True
            else:
                try:
                    async with deals_service.scan_lock(DealSource.VINTED.value):
                        async with session_scope() as session:
                            result = await deals_service.scan_vinted(session, vinted)
                    lines.append(_summary("Vinted", result))
                    watched = max(watched, result.watched)
                except VintedNotConfiguredError as e:
                    lines.append(f"**Vinted**: {e}")
                    unavailable = True

        if not watched and not unavailable:
            await interaction.followup.send(embed=error_embed(
                "You aren't watching any models yet. Add one with `/pricing watch`, then `/pricing refresh`."))
            return

        async with session_scope() as session:
            ids = [a.id for a in await deal_queue.unposted_pending(session)]
        posted = await post_deal_cards(self.bot, ids, fallback_channel=interaction.channel) if ids else 0
        footer = f"\n🛒 **{posted}** new card(s) on the buy page." if posted else "\nNothing new for the buy page."
        await interaction.followup.send("\n\n".join(lines)[:1800] + footer)

    @app_commands.command(name="buy-page", description="Post any waiting deals that don't have a card yet")
    @require_authorized()
    async def buy_page(self, interaction: discord.Interaction):
        await interaction.response.defer(thinking=True)
        async with session_scope() as session:
            ids = [a.id for a in await deal_queue.unposted_pending(session)]
        posted = await post_deal_cards(self.bot, ids, fallback_channel=interaction.channel) if ids else 0
        async with session_scope() as session:
            counts = await deal_queue.counts(session)
        await interaction.followup.send(
            f"🛒 Posted **{posted}** card(s). **{counts.pending}** deal(s) are waiting for you "
            f"(≈ £{counts.pending_profit:,.2f} expected profit if you bought them all).")

    # ------------------------------------------------------------------ queue + health
    @app_commands.command(name="deals", description="What's on the buy page: waiting, bought, rejected")
    @app_commands.choices(status=STATUS_CHOICES)
    @require_authorized()
    async def deals(self, interaction: discord.Interaction, status: Optional[app_commands.Choice[str]] = None):
        wanted = status.value if status else DealStatus.PENDING.value
        async with session_scope() as session:
            counts = await deal_queue.counts(session)
            alerts = await deals_service.recent_alerts(session, 10, wanted)
            rows = []
            for a in alerts:
                profit = f"+£{a.expected_profit}" if a.expected_profit is not None else "?"
                rows.append(f"• #{a.id} **{a.phone_model.model}{' ' + a.storage if a.storage else ''}** · {a.source.title()} · "
                            f"£{a.total_cost} all in · {profit}" + (" · 🔧" if a.bucket == "FAULTY" else "") + f"\n  {a.url}")
        head = (f"🛒 **{counts.pending}** waiting · **{counts.bought}** bought · **{counts.rejected}** rejected · "
                f"**{counts.expired}** expired\n")
        body = "\n".join(rows) if rows else f"Nothing {wanted.lower()}."
        await interaction.response.send_message((head + body)[:1900])

    @app_commands.command(name="deal-status", description="Is the deal scanner healthy? (eBay, Vinted, the proxy)")
    @require_authorized()
    async def deal_status(self, interaction: discord.Interaction):
        sched = self.bot.job_scheduler
        e = discord.Embed(title="🛒 Deal scanner", colour=discord.Colour.blue())

        def job_line(job) -> str:
            if job is None or job.last_run_at is None:
                return "hasn't run yet"
            ok = "✅" if job.last_success else "⚠️"
            return (f"{ok} {job.last_run_at.strftime('%d %b %H:%M')} UTC · {job.last_summary or ''}"
                    + (f"\n{job.last_error}" if job.last_error else ""))

        e.add_field(name="eBay scan", value=job_line(sched.state.deals if sched else None)[:1000], inline=False)
        h = self.bot.vinted_client.health()
        if not h.configured:
            e.add_field(name="Vinted", value=f"Off: {h.reason}"[:1000], inline=False)
        else:
            v = job_line(sched.state.vinted if sched else None)
            v += f"\nRoutes: {h.routes_available}/{h.routes} available · {h.requests_ok} ok · {h.requests_blocked} blocked"
            if h.cooling:
                v += "\nCooling down: " + ", ".join(f"{name} ({mins} min)" for name, mins in h.cooling)
            if h.last_error:
                v += f"\nLast problem: {h.last_error}"
            e.add_field(name="Vinted", value=v[:1000], inline=False)
        async with session_scope() as session:
            c = await deal_queue.counts(session)
        e.add_field(name="Buy page", inline=False,
                    value=f"{c.pending} waiting (≈ £{c.pending_profit:,.2f}) · {c.bought} bought · {c.rejected} rejected · {c.expired} expired")
        await interaction.response.send_message(embed=e)

    @app_commands.command(name="vinted-test", description="Send ONE home-page request and ONE search to Vinted and show what came back")
    @require_authorized()
    async def vinted_test(self, interaction: discord.Interaction):
        await interaction.response.defer(thinking=True)
        vinted = self.bot.vinted_client
        if not vinted.configured:
            await interaction.followup.send(embed=error_embed(f"Vinted is off: {vinted.health().reason}"))
            return
        lines = await vinted.probe()
        await interaction.followup.send("```\n" + "\n".join(lines)[:1850] + "\n```")

    # ------------------------------------------------------------------ part finder
    @app_commands.command(name="ebay-parts", description="Cheapest eBay listings for a part, for one exact model")
    @app_commands.describe(part_type="e.g. screen, battery", model="Exact model (a 13 Pro screen is not a 13 screen)")
    @app_commands.autocomplete(model=phone_model_autocomplete)
    @require_authorized()
    async def ebay_parts(self, interaction: discord.Interaction, part_type: str, model: str):
        await interaction.response.defer(thinking=True)
        async with session_scope() as session:
            try:
                phone_model = await config_service.resolve_phone_model(session, model)
                offers = await deals_service.find_parts_on_ebay(session, self.bot.ebay_client, part_type, phone_model)
            except config_service.UnknownPhoneModelError as e:
                await interaction.followup.send(embed=error_embed(str(e)))
                return
            except EbayNotConfiguredError:
                await interaction.followup.send(embed=error_embed("eBay isn't connected yet (`/config ebay`)."))
                return
            except EbayApiError as e:
                await interaction.followup.send(embed=error_embed(f"eBay search failed: {e}"))
                return
        await interaction.followup.send(deals_service.format_part_offers(part_type, phone_model, offers)[:1900])


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(DealsCog(bot))
