"""Job channels: one Discord channel per phone while it is in the workshop.

Pressing Start on a phone ticket opens `#ip-000004-iphone-13...` under an
"Active jobs" category with a control panel at the bottom. The panel's buttons
are generated from the phone's real situation (see app.services.job_panels), so
only the actions that make sense right now appear. The channel is deleted the
moment the phone leaves the workshop (listed, sold, scrapped).
"""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from typing import Optional

import discord
from discord.ext import commands

from app.bot.auth import _is_authorized
from app.bot.cogs.testing import begin_walkthrough
from app.bot.embeds import analysis_embed, error_embed, phone_embed
from app.bot.views import ConfirmView
from app.db import session_scope
from app.models.enums import TestOutcome
from app.models.inventory_models import Phone
from app.models.repair_models import RepairPart
from app.models.task_models import JobChannel
from app.services import config_service
from app.services import finance as finance_service
from app.services import job_actions, jobs as jobs_service, phone_tickets
from app.services import part_failures
from app.services import parts as parts_service
from app.services import repairs as repairs_service
from app.services import tasks as tasks_service
from app.services import testing as testing_service
from app.services.job_actions import JobActionError
from app.services.job_panels import ActionSpec, PanelSpec, build_panel
from app.services.phone_tickets import PhoneTicketError

logger = logging.getLogger(__name__)

CATEGORY_NAME = "🔧 Active jobs"
MAX_CHANNELS_PER_CATEGORY = 50  # Discord's limit

STYLE = {"primary": discord.ButtonStyle.primary, "success": discord.ButtonStyle.success,
         "danger": discord.ButtonStyle.danger, "secondary": discord.ButtonStyle.secondary}
STAGE_COLOUR = {"TEST": discord.Colour.blue(), "REPAIR": discord.Colour.orange(), "VERIFY": discord.Colour.teal(),
                "PREP": discord.Colour.purple(), "LIST": discord.Colour.purple()}


# ====================================================================== rendering
def channel_name(internal_id: str, display_name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", f"{internal_id} {display_name}".lower()).strip("-")
    return slug[:100]


def panel_embed(panel: PanelSpec) -> discord.Embed:
    e = discord.Embed(title=panel.header, description=f"## {panel.title}\n{panel.body}"[:3900],
                      colour=STAGE_COLOUR.get(panel.stage, discord.Colour.blurple()))
    for name, value in panel.fields:
        e.add_field(name=name, value=value[:1024], inline=True)
    e.set_footer(text="Buttons change as the phone moves through the job. This channel closes when it's listed.")
    return e


def panel_view(panel: PanelSpec) -> discord.ui.View:
    """Line actions (one per part) fill the top rows, tools start on a fresh row."""
    view = discord.ui.View(timeout=None)
    row, col, last_group = 0, 0, None
    for a in panel.actions:
        if last_group is not None and a.group != last_group:
            row, col = row + 1, 0
        if col == 5:
            row, col = row + 1, 0
        if row > 4:
            logger.warning("Panel has more buttons than fit; dropping %s", a.label)
            break
        button = JobButton(a.act, a.ref, a.label, STYLE[a.style])
        button.item.row = row
        view.add_item(button)
        col += 1
        last_group = a.group
    return view


class JobButton(discord.ui.DynamicItem[discord.ui.Button], template=r"job:(?P<act>[a-z]+):(?P<ref>[0-9]+)"):
    """A panel button. The action and target live in the custom id, so panels
    keep working after the bot restarts without re-registering anything."""

    def __init__(self, act: str, ref: int, label: str = "", style: discord.ButtonStyle = discord.ButtonStyle.secondary):
        super().__init__(discord.ui.Button(label=label or act, style=style, custom_id=f"job:{act}:{ref}"))
        self.act, self.ref = act, ref

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match[str], /):
        return cls(match["act"], int(match["ref"]), item.label or "", item.style)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if await _is_authorized(interaction.user.id):
            return True
        await interaction.response.send_message("🔒 You're not authorized to use this bot.", ephemeral=True)
        return False

    async def callback(self, interaction: discord.Interaction) -> None:
        cog = interaction.client.get_cog("JobsCog")
        if cog is None:
            await interaction.response.send_message("Job channels aren't running right now.", ephemeral=True)
            return
        await cog.handle(interaction, self.act, self.ref)


# ====================================================================== forms
class _Form(discord.ui.Modal):
    """Modal whose inputs are created per instance (class-level inputs are shared between users)."""

    def __init__(self, cog: "JobsCog", phone_id: int, title: str):
        super().__init__(title=title)
        self.cog, self.phone_id = cog, phone_id

    def field(self, label: str, *, required: bool = True, long: bool = False, default: str = "", max_length: int = 200,
              placeholder: Optional[str] = None) -> discord.ui.TextInput:
        item = discord.ui.TextInput(label=label, required=required, default=default or None, max_length=max_length,
                                    placeholder=placeholder,
                                    style=discord.TextStyle.paragraph if long else discord.TextStyle.short)
        self.add_item(item)
        return item

    async def run(self, interaction: discord.Interaction, action) -> None:
        await self.cog.run_action(interaction, self.phone_id, action)


class FaultForm(_Form):
    def __init__(self, cog, phone_id):
        super().__init__(cog, phone_id, "Log a fault")
        self.description = self.field("What's wrong?", long=True, max_length=300)
        self.part = self.field("Part that fixes it (optional)", required=False, max_length=60, placeholder="e.g. Screen, Battery")

    async def on_submit(self, interaction):
        await self.run(interaction, lambda s, actor: job_actions.log_fault(
            s, self.phone_id, actor, description=self.description.value, part_type_name=self.part.value))


class DetailsForm(_Form):
    def __init__(self, cog, phone_id):
        super().__init__(cog, phone_id, "Edit details")
        self.battery = self.field("Battery health % (e.g. 87)", required=False, max_length=4)
        self.cosmetic = self.field("Cosmetic condition", required=False, max_length=100)
        self.functional = self.field("Functional condition", required=False, max_length=100)
        self.colour = self.field("Colour", required=False, max_length=40)
        self.notes = self.field("Notes", required=False, long=True, max_length=500)

    async def on_submit(self, interaction):
        await self.run(interaction, lambda s, actor: job_actions.edit_details(
            s, self.phone_id, actor, battery=self.battery.value, cosmetic=self.cosmetic.value,
            functional=self.functional.value, colour=self.colour.value, notes=self.notes.value))


class MoveForm(_Form):
    def __init__(self, cog, phone_id):
        super().__init__(cog, phone_id, "Move phone")
        self.code = self.field("Location code (e.g. B2)", max_length=20)

    async def on_submit(self, interaction):
        await self.run(interaction, lambda s, actor: job_actions.move(s, self.phone_id, actor, self.code.value))


class OrderForm(_Form):
    def __init__(self, cog, phone_id, rp_id: int, part_name: str):
        super().__init__(cog, phone_id, f"Order {part_name}"[:45])
        self.rp_id = rp_id
        self.supplier = self.field("Supplier", max_length=100)
        self.cost = self.field("Price for the part (£)", max_length=10, placeholder="35.00")
        self.shipping = self.field("Shipping (£, optional)", required=False, max_length=10)
        self.expected = self.field("Expected delivery (optional)", required=False, max_length=10, placeholder="2026-11-03")

    async def on_submit(self, interaction):
        await self.run(interaction, lambda s, actor: job_actions.order_part(
            s, self.phone_id, self.rp_id, actor, supplier=self.supplier.value, unit_cost=self.cost.value,
            shipping=self.shipping.value, expected=self.expected.value))


class FindPartForm(_Form):
    def __init__(self, cog, phone_id):
        super().__init__(cog, phone_id, "Find a part")
        self.part_type = self.field("Part (e.g. screen)", max_length=60)
        self.model = self.field("Phone model (optional)", required=False, max_length=60, placeholder="13 pro")

    async def on_submit(self, interaction):
        async with session_scope() as session:
            results = await parts_service.find_part_by_type_and_model(session, self.part_type.value, self.model.value)
            rows = [" · ".join(b for b in (f"`{p.internal_id}` {p.part_type.name if p.part_type else '?'}",
                                           p.location.code if p.location else "—", f"£{p.cost}", part_failures.proven_tag(p)) if b)
                    for p in results[:15]]
        e = discord.Embed(title=f"🔎 Parts matching '{self.part_type.value} {self.model.value}'".strip(),
                          description="\n".join(rows) or "No matching available parts.", colour=discord.Colour.blue())
        await interaction.response.send_message(embed=e, ephemeral=True)


# ====================================================================== small ephemeral pickers
class _Picker(discord.ui.View):
    def __init__(self, invoker_id: int, timeout: float = 180):
        super().__init__(timeout=timeout)
        self.invoker_id = invoker_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.invoker_id:
            await interaction.response.send_message("This menu isn't yours.", ephemeral=True)
            return False
        return True


class SingleTestView(_Picker):
    """Pick one test, then record PASS / FAIL / N/A / Not tested for it."""

    def __init__(self, cog: "JobsCog", invoker_id: int, phone_id: int, definitions):
        super().__init__(invoker_id)
        self.cog, self.phone_id, self.selected = cog, phone_id, None
        self.defs = {str(d.id): d for d in definitions}
        select = discord.ui.Select(placeholder="Which test?", options=[
            discord.SelectOption(label=d.name[:100], value=str(d.id)) for d in list(definitions)[:25]])
        select.callback = self._picked
        self.select = select
        self.add_item(select)
        for label, outcome, style in (("PASS", TestOutcome.PASS, discord.ButtonStyle.success),
                                      ("FAIL", TestOutcome.FAIL, discord.ButtonStyle.danger),
                                      ("N/A", TestOutcome.NOT_APPLICABLE, discord.ButtonStyle.secondary),
                                      ("Reset", TestOutcome.NOT_TESTED, discord.ButtonStyle.secondary)):
            b = discord.ui.Button(label=label, style=style, row=1)
            b.callback = self._make_result(outcome)
            self.add_item(b)

    async def _picked(self, interaction: discord.Interaction):
        self.selected = self.defs[self.select.values[0]]
        await interaction.response.send_message(f"**{self.selected.name}**: now press the result.", ephemeral=True)

    def _make_result(self, outcome: TestOutcome):
        async def callback(interaction: discord.Interaction):
            if self.selected is None:
                await interaction.response.send_message("Pick a test first.", ephemeral=True)
                return
            definition = self.selected

            async def action(session, actor):
                phone = await session.get(Phone, self.phone_id, populate_existing=True)
                out = await testing_service.record_and_apply(session, phone, definition, outcome, actor)
                extra = ""
                if out.flagged:
                    extra = f" → needs **{out.part_type.name}**" if out.part_type else " → needs inspection"
                return f"{definition.name}: {outcome.value.replace('_', ' ').lower()}{extra}"

            await self.cog.run_action(interaction, self.phone_id, action)
        return callback


class CandidateView(_Picker):
    """Choose which in-stock part to reserve when there's more than one."""

    def __init__(self, cog: "JobsCog", invoker_id: int, phone_id: int, rp_id: int, parts):
        super().__init__(invoker_id)
        for part in list(parts)[:5]:
            where = part.location.code if part.location else "?"
            b = discord.ui.Button(label=f"{part.internal_id} · £{part.cost} · {where}", style=discord.ButtonStyle.primary)
            b.callback = self._make(cog, phone_id, rp_id, part.id)
            self.add_item(b)

    @staticmethod
    def _make(cog, phone_id, rp_id, part_id):
        async def callback(interaction: discord.Interaction):
            await cog.run_action(interaction, phone_id, lambda s, actor: job_actions.reserve(s, phone_id, rp_id, actor, part_id=part_id))
        return callback


class FittedPartsView(_Picker):
    def __init__(self, cog: "JobsCog", invoker_id: int, phone_id: int, fitted):
        super().__init__(invoker_id)
        select = discord.ui.Select(placeholder="Which part came out?", options=[
            discord.SelectOption(label=f"{part.internal_id} · {rp.required_part_type.name}"[:100], value=str(part.id))
            for rp, part in fitted[:25]])

        async def picked(interaction: discord.Interaction):
            part_id = int(select.values[0])
            await interaction.response.edit_message(
                content="What's happening to it?", view=DispositionView(cog, invoker_id, phone_id, part_id))
        select.callback = picked
        self.add_item(select)


class FailReasonForm(_Form):
    """Asked after choosing what to do with a part that didn't work. The reason is
    optional but is kept (it feeds the supplier quality numbers)."""

    def __init__(self, cog, phone_id, title: str, runner, *, from_ephemeral: bool = True):
        super().__init__(cog, phone_id, title[:45])
        self.runner, self.from_ephemeral = runner, from_ephemeral
        self.reason = self.field("What went wrong? (optional)", required=False, long=True, max_length=200,
                                 placeholder="e.g. no touch, dead on arrival, flickers")

    async def on_submit(self, interaction):
        if self.from_ephemeral:  # close the little menu we came from so it can't be pressed twice
            await interaction.response.edit_message(content="Recording…", embed=None, view=None)
        await self.run(interaction, lambda s, actor: self.runner(s, actor, self.reason.value))


FAIL_CHOICES = (("SCRAP", "🗑️ Scrap it", discord.ButtonStyle.danger),
                ("RETURN", "📮 Return to supplier", discord.ButtonStyle.primary),
                ("FAULTY", "🧰 Keep as faulty", discord.ButtonStyle.secondary))


class FitCheckView(_Picker):
    """Pressing Fitted asks: does it work? The fit is only recorded once answered."""

    def __init__(self, cog: "JobsCog", invoker_id: int, phone_id: int, rp_id: int, part_label: str):
        super().__init__(invoker_id)
        self.cog, self.phone_id, self.rp_id, self.part_label = cog, phone_id, rp_id, part_label

    @discord.ui.button(label="✅ It works", style=discord.ButtonStyle.success)
    async def works(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="Recording…", embed=None, view=None)
        await self.cog.run_action(interaction, self.phone_id,
                                  lambda s, actor: job_actions.fit_confirmed(s, self.phone_id, self.rp_id, actor))

    @discord.ui.button(label="❌ It doesn't work", style=discord.ButtonStyle.danger)
    async def broken(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = discord.Embed(title=f"{self.part_label} doesn't work", colour=discord.Colour.red(),
                              description="What should happen to it? Its cost is written off as a business loss, "
                                          "not charged to this phone.")
        await interaction.response.edit_message(embed=embed, view=FailChoiceView(
            self.cog, self.invoker_id, self.phone_id, self.rp_id, self.part_label))

    @discord.ui.button(label="Not fitted yet", style=discord.ButtonStyle.secondary)
    async def not_yet(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="No problem. Press **Fitted** again once it's in.", embed=None, view=None)


class FailChoiceView(_Picker):
    def __init__(self, cog: "JobsCog", invoker_id: int, phone_id: int, rp_id: int, part_label: str):
        super().__init__(invoker_id)
        self.cog, self.phone_id, self.rp_id, self.part_label = cog, phone_id, rp_id, part_label
        for action, label, style in FAIL_CHOICES:
            b = discord.ui.Button(label=label, style=style)
            b.callback = self._make(action, label)
            self.add_item(b)

    def _make(self, action: str, label: str):
        async def callback(interaction: discord.Interaction):
            async def runner(session, actor, reason):
                return await job_actions.fail_fit(session, self.phone_id, self.rp_id, actor, action=action, reason=reason)
            await interaction.response.send_modal(FailReasonForm(self.cog, self.phone_id, label.split(" ", 1)[1], runner))
        return callback


class DispositionView(_Picker):
    """A fitted part is coming out: wrong part (back to stock) or it failed (scrap / return / faulty)."""

    def __init__(self, cog: "JobsCog", invoker_id: int, phone_id: int, part_id: int):
        super().__init__(invoker_id)
        self.cog, self.phone_id, self.part_id = cog, phone_id, part_id
        back = discord.ui.Button(label="↩️ Wrong part, back to stock", style=discord.ButtonStyle.secondary)
        back.callback = self._stock
        self.add_item(back)
        for action, label, style in FAIL_CHOICES:
            b = discord.ui.Button(label=label, style=style, row=1)
            b.callback = self._make(action, label)
            self.add_item(b)

    async def _stock(self, interaction: discord.Interaction):
        await interaction.response.edit_message(content="Recording…", view=None)
        await self.cog.run_action(interaction, self.phone_id,
                                  lambda s, actor: job_actions.unfit(s, self.phone_id, self.part_id, actor))

    def _make(self, action: str, label: str):
        async def callback(interaction: discord.Interaction):
            async def runner(session, actor, reason):
                return await job_actions.unfit(session, self.phone_id, self.part_id, actor, disposition=action, reason=reason)
            await interaction.response.send_modal(FailReasonForm(self.cog, self.phone_id, label.split(" ", 1)[1], runner))
        return callback


class ResolveFaultView(_Picker):
    def __init__(self, cog: "JobsCog", invoker_id: int, phone_id: int, faults):
        super().__init__(invoker_id)
        select = discord.ui.Select(placeholder="Which fault is fixed?", options=[
            discord.SelectOption(label=f.description[:100], value=str(f.id)) for f in faults[:25]])

        async def picked(interaction: discord.Interaction):
            fault_id = int(select.values[0])
            await interaction.response.edit_message(content="Recording…", view=None)
            await cog.run_action(interaction, phone_id, lambda s, a: job_actions.resolve_fault(s, phone_id, fault_id, a))
        select.callback = picked
        self.add_item(select)


# ====================================================================== cog
@dataclass
class PanelPlan:
    edits: list = field(default_factory=list)   # (job_id, channel_id, message_id, spec, hash)
    closes: list = field(default_factory=list)  # (job_id, channel_id)


class JobsCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._open_lock = asyncio.Lock()
        self._panel_lock = asyncio.Lock()

    async def cog_load(self) -> None:
        self.bot.add_dynamic_items(JobButton)

    # ------------------------------------------------------------ opening
    async def _category(self, guild: discord.Guild) -> discord.CategoryChannel:
        existing = [c for c in guild.categories if c.name == CATEGORY_NAME or c.name.startswith(CATEGORY_NAME + " ")]
        for cat in sorted(existing, key=lambda c: c.name):
            if len(cat.channels) < MAX_CHANNELS_PER_CATEGORY:
                return cat
        name = CATEGORY_NAME if not existing else f"{CATEGORY_NAME} {len(existing) + 1}"
        return await guild.create_category(name, reason="Job channels")

    async def _panel_for(self, session, phone_id: int) -> Optional[PanelSpec]:
        phones = await phone_tickets.load_phones(session)
        phone = await session.get(Phone, phone_id, populate_existing=True)
        if phone is None:
            return None
        if phone_id not in {p.id for p in phones}:
            phones.append(phone)
        ctx = await phone_tickets.load_context(session, phones)
        sit = phone_tickets.assess(phone, ctx)
        return None if sit is None else build_panel(sit, ctx)

    async def open_job(self, interaction: discord.Interaction, phone_id: int) -> None:
        """Start pressed on a phone ticket: open (or point to) its job channel."""
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        if guild is None:
            await interaction.followup.send(embed=error_embed("Job channels only work inside a server."), ephemeral=True)
            return
        async with self._open_lock:
            async with session_scope() as session:
                existing = await jobs_service.get_job(session, phone_id)
                if existing:
                    await interaction.followup.send(f"Already open: <#{existing.channel_id}>", ephemeral=True)
                    return
                phone = await session.get(Phone, phone_id, populate_existing=True)
                if phone is None or phone.current_status not in jobs_service.WORKSHOP_STATUSES:
                    await interaction.followup.send(embed=error_embed("That phone isn't in the workshop any more."), ephemeral=True)
                    return
                panel = await self._panel_for(session, phone_id)
                if panel is None:
                    await interaction.followup.send(embed=error_embed("That phone doesn't need a job right now."), ephemeral=True)
                    return
                name, label = channel_name(phone.internal_id, phone.display_name()), f"{phone.internal_id} {phone.display_name()}"
            try:
                category = await self._category(guild)
                channel = await guild.create_text_channel(
                    name, category=category, topic=f"{label} · closes when it's listed",
                    reason=f"Job started by {interaction.user}")
            except discord.Forbidden:
                await interaction.followup.send(embed=error_embed(
                    "I need the **Manage Channels** permission to open job channels."), ephemeral=True)
                return
            async with session_scope() as session:
                job = await jobs_service.register_job(session, phone_id=phone_id, guild_id=guild.id,
                                                      channel_id=channel.id, created_by=interaction.user.id)
                if job is None:  # someone else got there first
                    existing = await jobs_service.get_job(session, phone_id)
                    await channel.delete(reason="Duplicate job channel")
                    await interaction.followup.send(f"Already open: <#{existing.channel_id}>", ephemeral=True)
                    return
                ticket = await tasks_service.open_ticket_for_phone(session, phone_id)
                if ticket is not None and ticket.status in ("TODO", "IN_PROGRESS"):
                    ticket.status, ticket.assignee_discord_id = "IN_PROGRESS", interaction.user.id
                    tasks_service.bump(ticket)
                job_id = job.id
            await channel.send(f"{interaction.user.mention} started this job. Everything for this phone happens here, "
                               "and the channel disappears once it's listed.")
            await self._post_panel(channel, job_id, panel)
        await interaction.followup.send(f"Opened {channel.mention}", ephemeral=True)
        tasks_cog = self.bot.get_cog("TasksCog")
        if tasks_cog:
            await tasks_cog.flush_posts()  # the ticket becomes "In progress in #channel"

    # ------------------------------------------------------------ the panel
    async def _post_panel(self, channel, job_id: int, panel: PanelSpec) -> None:
        """Send the panel as the newest message, pin it, and remember it."""
        old_id = None
        async with session_scope() as session:
            job = await session.get(JobChannel, job_id)
            old_id = job.panel_message_id if job else None
        msg = await channel.send(embed=panel_embed(panel), view=panel_view(panel))
        try:
            await msg.pin()
        except discord.HTTPException:
            pass
        if old_id:
            try:
                await channel.get_partial_message(old_id).delete()
            except discord.HTTPException:
                pass
        async with session_scope() as session:
            job = await session.get(JobChannel, job_id)
            if job:
                job.panel_message_id, job.panel_hash = msg.id, panel.content_hash()

    async def refresh_job(self, phone_id: int, *, repost: bool) -> None:
        """Redraw one job's panel. `repost` puts it at the bottom of the channel
        (after a person did something); otherwise it's edited where it is."""
        async with self._panel_lock:
            async with session_scope() as session:
                job = await jobs_service.get_job(session, phone_id)
                if job is None:
                    return
                panel = await self._panel_for(session, phone_id)
                job_id, channel_id, message_id, old_hash = job.id, job.channel_id, job.panel_message_id, job.panel_hash
            if panel is None:
                return
            channel = self.bot.get_channel(channel_id)
            if channel is None:
                return
            if repost or message_id is None:
                await self._post_panel(channel, job_id, panel)
            elif panel.content_hash() != old_hash:
                await self._edit_panel(channel, job_id, message_id, panel)

    async def _edit_panel(self, channel, job_id: int, message_id: int, panel: PanelSpec) -> None:
        try:
            await channel.get_partial_message(message_id).edit(embed=panel_embed(panel), view=panel_view(panel))
        except discord.NotFound:
            await self._post_panel(channel, job_id, panel)
            return
        async with session_scope() as session:
            job = await session.get(JobChannel, job_id)
            if job:
                job.panel_hash = panel.content_hash()

    # ------------------------------------------------------------ engine hooks
    async def plan(self, session, stats: phone_tickets.SyncStats) -> PanelPlan:
        """Decide (inside the sync's session) which panels changed and which jobs are over."""
        plan = PanelPlan()
        closing = {j.id for j in await jobs_service.jobs_to_close(session)}
        for job in await jobs_service.all_jobs(session):
            if job.id in closing:
                plan.closes.append((job.id, job.channel_id))
                continue
            sit = stats.situations.get(job.phone_id)
            if sit is None or stats.ctx is None:
                continue
            panel = build_panel(sit, stats.ctx)
            if panel.content_hash() != job.panel_hash:
                plan.edits.append((job.id, job.channel_id, job.panel_message_id, panel))
        return plan

    async def apply(self, plan: PanelPlan) -> None:
        async with self._panel_lock:
            for job_id, channel_id, message_id, panel in plan.edits:
                channel = self.bot.get_channel(channel_id)
                if channel is None:
                    continue
                if message_id is None:
                    await self._post_panel(channel, job_id, panel)
                else:
                    await self._edit_panel(channel, job_id, message_id, panel)
            for job_id, channel_id in plan.closes:
                await self._close(job_id, channel_id)

    async def _close(self, job_id: int, channel_id: int) -> None:
        channel = self.bot.get_channel(channel_id)
        if channel is not None:
            try:
                await channel.delete(reason="Phone left the workshop")
            except discord.NotFound:
                pass
            except discord.HTTPException:
                logger.warning("Could not delete job channel %s", channel_id)
                return
        async with session_scope() as session:
            job = await session.get(JobChannel, job_id)
            if job:
                await jobs_service.delete_job(session, job)

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel) -> None:
        async with session_scope() as session:
            phone_id = await jobs_service.drop_job_for_channel(session, channel.id)
        if phone_id is not None:
            tasks_cog = self.bot.get_cog("TasksCog")
            if tasks_cog:
                await tasks_cog.flush_posts()

    # ------------------------------------------------------------ button handling
    async def run_action(self, interaction: discord.Interaction, phone_id: int, action) -> None:
        """Do something to the phone, say what happened in the channel, and put the
        panel back at the bottom with whatever buttons are now relevant."""
        if not interaction.response.is_done():
            await interaction.response.defer()
        try:
            async with session_scope() as session:
                message = await action(session, interaction.user.id)
        except (JobActionError, PhoneTicketError, part_failures.PartFailureError, parts_service.PartStateError,
                repairs_service.RepairError) as e:
            await interaction.followup.send(embed=error_embed(str(e)), ephemeral=True)
            return
        channel = interaction.channel
        if channel is not None and message:
            await channel.send(f"**{interaction.user.display_name}**: {message}")
        tasks_cog = self.bot.get_cog("TasksCog")
        if tasks_cog:
            await tasks_cog.engine_tick()  # tickets and the #tasks channel follow immediately
        await self.refresh_job(phone_id, repost=True)

    async def handle(self, interaction: discord.Interaction, act: str, ref: int) -> None:
        async with session_scope() as session:
            job = await jobs_service.get_job_by_channel(session, interaction.channel_id)
            phone_id = job.phone_id if job else None
        if phone_id is None:
            await interaction.response.send_message("This channel isn't an active job any more.", ephemeral=True)
            return
        user = interaction.user.id

        # ---- one-press actions on a specific part / order
        simple = {
            "reserve": lambda s, a: job_actions.reserve(s, phone_id, ref, a),
            "retry": lambda s, a: job_actions.retry_part(s, phone_id, ref, a),
            "receive": lambda s, a: job_actions.receive(s, ref, a),
            "placed": lambda s, a: job_actions.mark_placed(s, ref, a),
            "plan": lambda s, a: job_actions.plan_repair(s, phone_id, a),
        }
        if act in simple:
            await self.run_action(interaction, phone_id, simple[act])
            return

        # ---- Fitted: parts aren't tested beforehand, so ask now whether it works
        if act == "fit":
            async with session_scope() as session:
                rp = await session.get(RepairPart, ref, populate_existing=True)
                if rp is None or rp.part is None:
                    await interaction.response.send_message("Reserve the part first.", ephemeral=True)
                    return
                label = f"`{rp.part.internal_id}` ({rp.required_part_type.name})"
            embed = discord.Embed(title=f"Fitted {label}. Does it work?", colour=discord.Colour.gold(),
                                  description="Check the phone with the part in. The fit is only recorded once you answer.")
            await interaction.response.send_message(
                embed=embed, view=FitCheckView(self, user, phone_id, ref, label), ephemeral=True)
            return
        if act == "resolve":
            async with session_scope() as session:
                faults = await job_actions.open_undiagnosed(session, phone_id)
                view = ResolveFaultView(self, user, phone_id, faults) if faults else None
            if view is None:
                await interaction.response.send_message("There's no open fault to resolve.", ephemeral=True)
                return
            await interaction.response.send_message("Which fault is fixed?", ephemeral=True, view=view)
            return

        # ---- forms
        if act == "order":
            async with session_scope() as session:
                rp = await session.get(RepairPart, ref)
                name = rp.required_part_type.name if rp else "part"
            await interaction.response.send_modal(OrderForm(self, phone_id, ref, name))
            return
        forms = {"fault": FaultForm, "edit": DetailsForm, "move": MoveForm, "find": FindPartForm}
        if act in forms:
            await interaction.response.send_modal(forms[act](self, phone_id))
            return

        # ---- testing
        if act in ("walk", "retest"):
            async with session_scope() as session:
                phone = await session.get(Phone, phone_id, populate_existing=True)
                internal_id = phone.internal_id
                only = None
                if act == "retest":
                    phones = await phone_tickets.load_phones(session)
                    ctx = await phone_tickets.load_context(session, phones)
                    sit = phone_tickets.assess(phone, ctx)
                    only = {d.id for d in phone_tickets.retest_definitions(sit)} if sit else None

            async def after():
                await self.refresh_job(phone_id, repost=True)
            await begin_walkthrough(interaction, internal_id, only_definition_ids=only, on_finish=after)
            return
        if act == "one":
            async with session_scope() as session:
                definitions = list(await config_service.list_test_definitions(session))
            await interaction.response.send_message("Pick a test, then the result:", ephemeral=True,
                                                    view=SingleTestView(self, user, phone_id, definitions))
            return

        # ---- pickers
        if act == "alt":
            try:
                async with session_scope() as session:
                    parts = await job_actions.alternatives(session, phone_id, ref)
                    view = CandidateView(self, user, phone_id, ref, parts) if parts else None
            except JobActionError as e:
                await interaction.response.send_message(embed=error_embed(str(e)), ephemeral=True)
                return
            if view is None:
                await interaction.response.send_message("There's nothing in stock to choose from.", ephemeral=True)
                return
            await interaction.response.send_message("Which one do you want to reserve?", ephemeral=True, view=view)
            return
        if act == "unfit":
            async with session_scope() as session:
                phone = await session.get(Phone, phone_id, populate_existing=True)
                repair = await repairs_service.get_active_repair(session, phone)
                fitted = job_actions.fitted_parts(repair)
                view = FittedPartsView(self, user, phone_id, fitted) if fitted else None
            if view is None:
                await interaction.response.send_message("Nothing is fitted yet.", ephemeral=True)
                return
            await interaction.response.send_message("Which part came out?", ephemeral=True, view=view)
            return

        # ---- finishing a step (asks first, says exactly what will happen)
        if act in ("skip", "complete", "done"):
            await self._confirm_finish(interaction, phone_id, act)
            return

        # ---- read-only
        if act == "details":
            async with session_scope() as session:
                phone = await session.get(Phone, phone_id, populate_existing=True)
                summary = await testing_service.summary(session, phone)
                embed = phone_embed(phone, faults=phone.faults, test_summary=summary)
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return
        if act == "analyse":
            async with session_scope() as session:
                phone = await session.get(Phone, phone_id, populate_existing=True)
                embed = analysis_embed(phone, await finance_service.analyse_phone(session, phone))
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return
        await interaction.response.send_message("That button isn't available any more.", ephemeral=True)

    async def _confirm_finish(self, interaction: discord.Interaction, phone_id: int, act: str) -> None:
        async with session_scope() as session:
            phone = await session.get(Phone, phone_id, populate_existing=True)
            phones = await phone_tickets.load_phones(session)
            ctx = await phone_tickets.load_context(session, phones)
            sit = phone_tickets.assess(phone, ctx)
            if sit is None:
                await interaction.response.send_message("There's nothing to finish right now.", ephemeral=True)
                return
            stage = "TEST" if act == "skip" else sit.stage
            preview = await phone_tickets.preview_manual_complete(session, phone, stage)

        async def on_confirm(inner: discord.Interaction) -> None:
            await inner.response.edit_message(content="Working…", embed=None, view=None)

            async def action(session, actor):
                p = await session.get(Phone, phone_id, populate_existing=True)
                return await phone_tickets.manual_complete(session, p, stage, actor)
            await self.run_action(inner, phone_id, action)

        embed = discord.Embed(title="Finish this step?", description=preview, colour=discord.Colour.gold())
        await interaction.response.send_message(
            embed=embed, view=ConfirmView(interaction.user.id, on_confirm, confirm_label="Do it"), ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(JobsCog(bot))
