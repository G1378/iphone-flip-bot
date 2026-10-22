from __future__ import annotations

import asyncio
import datetime as dt
import logging
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks as discord_tasks

from app.bot.auth import _is_authorized, require_admin, require_authorized
from app.bot.embeds import error_embed, success_embed
from app.bot.pagination import send_paginated
from app.db import session_scope
from app.models.task_models import Task
from app.services import config_service
from app.services import phones as phones_service
from app.services import tasks as tasks_service
from app.services.tasks import TaskError

logger = logging.getLogger(__name__)

CHANNEL_PURPOSE = "tasks"
_PANEL_SETTING = "_internal_tasks_panel_message_id"

STATUS_EMOJI = {"TODO": "⬜", "IN_PROGRESS": "🟨", "BLOCKED": "🟥", "DONE": "✅", "CANCELLED": "➖"}
STATUS_LABEL = {"TODO": "To do", "IN_PROGRESS": "In progress", "BLOCKED": "Blocked", "DONE": "Done", "CANCELLED": "Cancelled"}
STATUS_COLOUR = {
    "TODO": discord.Colour.blurple(), "IN_PROGRESS": discord.Colour.gold(), "BLOCKED": discord.Colour.red(),
    "DONE": discord.Colour.green(), "CANCELLED": discord.Colour.dark_grey(),
}
PRIORITY_EMOJI = {"URGENT": "🚨", "HIGH": "🔺", "NORMAL": "▫️", "LOW": "🔽"}

PRIORITY_CHOICES = [app_commands.Choice(name=p.title(), value=p) for p in ("LOW", "NORMAL", "HIGH", "URGENT")]
STATUS_CHOICES = [app_commands.Choice(name=STATUS_LABEL[s], value=s) for s in ("TODO", "IN_PROGRESS", "BLOCKED", "DONE", "CANCELLED")]
SCOPE_CHOICES = [app_commands.Choice(name="Phone checklist", value="PHONE"), app_commands.Choice(name="General", value="GENERAL")]


# ====================================================================== rendering
def build_task_embed(task: Task, phone_label: Optional[tuple[str, str]] = None) -> discord.Embed:
    e = discord.Embed(
        title=f"{PRIORITY_EMOJI.get(task.priority, '▫️')} {task.title}",
        description=(task.description or "")[:1500] or None,
        colour=STATUS_COLOUR.get(task.status, discord.Colour.blurple()),
    )
    e.add_field(name="Status", value=f"{STATUS_EMOJI.get(task.status, '')} {STATUS_LABEL.get(task.status, task.status)}", inline=True)
    e.add_field(name="Priority", value=task.priority.title(), inline=True)
    e.add_field(name="Category", value=task.category or "General", inline=True)
    if phone_label:
        e.add_field(name="Phone", value=f"`{phone_label[0]}` {phone_label[1]}", inline=True)
    e.add_field(name="Assigned", value=f"<@{task.assignee_discord_id}>" if task.assignee_discord_id else "Unassigned", inline=True)
    if task.due_date:
        overdue = task.status in ("TODO", "IN_PROGRESS", "BLOCKED") and task.due_date < dt.date.today()
        e.add_field(name="Due", value=f"{task.due_date.isoformat()}{' ⏰ **overdue**' if overdue else ''}", inline=True)
    if task.status == "BLOCKED" and task.blocked_reason:
        e.add_field(name="🚫 Blocked on", value=task.blocked_reason[:1000], inline=False)
    if task.notes:
        e.add_field(name="Notes", value=task.notes[-900:], inline=False)
    if task.status == "DONE" and task.completed_at:
        by = f"<@{task.completed_by_discord_id}> " if task.completed_by_discord_id else "auto "
        e.add_field(name="Completed", value=f"{by}· <t:{int(task.completed_at.timestamp())}:R>", inline=False)
    e.set_footer(text=f"{task.internal_id} · {'checklist item' if task.template_id else 'manual ticket'}")
    return e


class TaskView(discord.ui.View):
    """Buttons on every open ticket. Persistent: custom_ids are static and the
    ticket is looked up from the message the button sits on."""

    def __init__(self, status: str = "TODO"):
        super().__init__(timeout=None)
        by_id = {c.custom_id: c for c in self.children}
        if status == "BLOCKED":
            by_id["task:start"].label = "▶️ Resume"
        elif status == "IN_PROGRESS":
            by_id["task:start"].disabled = True
            by_id["task:start"].label = "▶️ Started"

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if await _is_authorized(interaction.user.id):
            return True
        await interaction.response.send_message("🔒 You're not authorized to use this bot.", ephemeral=True)
        return False

    @discord.ui.button(label="▶️ Start", style=discord.ButtonStyle.primary, row=0, custom_id="task:start")
    async def start(self, interaction: discord.Interaction, button: discord.ui.Button):
        await _mutate(interaction, lambda s, t: tasks_service.set_task_status(s, t, "IN_PROGRESS", interaction.user.id))

    @discord.ui.button(label="✅ Done", style=discord.ButtonStyle.success, row=0, custom_id="task:done")
    async def done(self, interaction: discord.Interaction, button: discord.ui.Button):
        await _mutate(interaction, lambda s, t: tasks_service.set_task_status(s, t, "DONE", interaction.user.id))

    @discord.ui.button(label="🚫 Block", style=discord.ButtonStyle.danger, row=0, custom_id="task:block")
    async def block(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(BlockModal(interaction.message.id))

    @discord.ui.button(label="🙋 Claim", style=discord.ButtonStyle.secondary, row=1, custom_id="task:claim")
    async def claim(self, interaction: discord.Interaction, button: discord.ui.Button):
        await _mutate(interaction, lambda s, t: tasks_service.claim_task(s, t, interaction.user.id))

    @discord.ui.button(label="✏️ Edit / note", style=discord.ButtonStyle.secondary, row=1, custom_id="task:edit")
    async def edit(self, interaction: discord.Interaction, button: discord.ui.Button):
        async with session_scope() as session:
            task = await tasks_service.get_task_by_message(session, interaction.message.id)
            if task is None:
                await interaction.response.send_message("This ticket is no longer in the database.", ephemeral=True)
                return
            modal = EditModal(interaction.message.id, task.title, task.description, task.priority, task.due_date)
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="🗑️ Cancel", style=discord.ButtonStyle.secondary, row=1, custom_id="task:cancel")
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await _mutate(interaction, lambda s, t: tasks_service.set_task_status(s, t, "CANCELLED", interaction.user.id))


class ClosedTaskView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if await _is_authorized(interaction.user.id):
            return True
        await interaction.response.send_message("🔒 You're not authorized to use this bot.", ephemeral=True)
        return False

    @discord.ui.button(label="↩️ Reopen", style=discord.ButtonStyle.secondary, custom_id="task:reopen")
    async def reopen(self, interaction: discord.Interaction, button: discord.ui.Button):
        await _mutate(interaction, lambda s, t: tasks_service.set_task_status(s, t, "TODO", interaction.user.id))


def view_for(task: Task) -> discord.ui.View:
    if task.status in ("DONE", "CANCELLED"):
        return ClosedTaskView()
    return TaskView(task.status)


async def _render(session, task: Task) -> tuple[discord.Embed, discord.ui.View]:
    labels = await tasks_service.phone_labels(session, {task.phone_id} if task.phone_id else set())
    return build_task_embed(task, labels.get(task.phone_id)), view_for(task)


async def _mutate(interaction: discord.Interaction, mutator) -> None:
    """Run a change against the ticket that owns the clicked message, then
    redraw that message in place."""
    async with session_scope() as session:
        task = await tasks_service.get_task_by_message(session, interaction.message.id)
        if task is None:
            await interaction.response.send_message("This ticket is no longer in the database.", ephemeral=True)
            return
        try:
            await mutator(session, task)
        except TaskError as e:
            await interaction.response.send_message(embed=error_embed(str(e)), ephemeral=True)
            return
        task.posted_version = task.version  # we are redrawing it right now
        embed, view = await _render(session, task)
    await interaction.response.edit_message(embed=embed, view=view)


# ====================================================================== modals
class BlockModal(discord.ui.Modal, title="What's blocking this?"):
    reason = discord.ui.TextInput(label="Blocked on", style=discord.TextStyle.paragraph, max_length=500,
                                  placeholder="e.g. waiting on screen from supplier, due Friday")

    def __init__(self, message_id: int):
        super().__init__()
        self.message_id = message_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        async with session_scope() as session:
            task = await tasks_service.get_task_by_message(session, self.message_id)
            if task is None:
                await interaction.response.send_message("This ticket is no longer in the database.", ephemeral=True)
                return
            try:
                await tasks_service.set_task_status(session, task, "BLOCKED", interaction.user.id, reason=self.reason.value)
            except TaskError as e:
                await interaction.response.send_message(embed=error_embed(str(e)), ephemeral=True)
                return
            task.posted_version = task.version
            embed, view = await _render(session, task)
        await interaction.response.edit_message(embed=embed, view=view)


class EditModal(discord.ui.Modal, title="Edit ticket"):
    def __init__(self, message_id: int, title: str, description: Optional[str], priority: str, due: Optional[dt.date]):
        super().__init__()
        self.message_id = message_id
        # Inputs are built per instance: class-level TextInputs are shared between
        # modal instances, so prefilling them there would leak one ticket's text into another's.
        self.title_in = discord.ui.TextInput(label="Title", max_length=200, default=title)
        self.desc_in = discord.ui.TextInput(label="Description", style=discord.TextStyle.paragraph,
                                            required=False, max_length=1500, default=description or "")
        self.priority_in = discord.ui.TextInput(label="Priority (low / normal / high / urgent)", required=False,
                                                max_length=10, default=priority.lower())
        self.due_in = discord.ui.TextInput(label="Due (2026-11-03, 3d, tomorrow; blank = unchanged)", required=False,
                                           max_length=12, default=due.isoformat() if due else "")
        self.note_in = discord.ui.TextInput(label="Add a note (appended to Notes)", style=discord.TextStyle.paragraph,
                                            required=False, max_length=400)
        for item in (self.title_in, self.desc_in, self.priority_in, self.due_in, self.note_in):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        async with session_scope() as session:
            task = await tasks_service.get_task_by_message(session, self.message_id)
            if task is None:
                await interaction.response.send_message("This ticket is no longer in the database.", ephemeral=True)
                return
            try:
                await tasks_service.update_task(
                    session, task, title=self.title_in.value, description=self.desc_in.value,
                    priority=self.priority_in.value or None, due_date=tasks_service.parse_due(self.due_in.value),
                    note=self.note_in.value, actor_discord_id=interaction.user.id,
                )
            except TaskError as e:
                await interaction.response.send_message(embed=error_embed(str(e)), ephemeral=True)
                return
            task.posted_version = task.version
            embed, view = await _render(session, task)
        await interaction.response.edit_message(embed=embed, view=view)


class NewTicketModal(discord.ui.Modal, title="New ticket"):
    title_in = discord.ui.TextInput(label="What needs doing?", max_length=200)
    desc_in = discord.ui.TextInput(label="Details", style=discord.TextStyle.paragraph, required=False, max_length=1500)
    priority_in = discord.ui.TextInput(label="Priority (low / normal / high / urgent)", required=False, max_length=10, default="normal")
    due_in = discord.ui.TextInput(label="Due (2026-11-03, 3d, tomorrow)", required=False, max_length=12)
    phone_in = discord.ui.TextInput(label="Phone ID, if it's about one (e.g. IP-000004)", required=False, max_length=20)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        try:
            async with session_scope() as session:
                task = await tasks_service.create_task(
                    session, self.title_in.value, interaction.user.id, description=self.desc_in.value,
                    priority=self.priority_in.value or "NORMAL", due_date=tasks_service.parse_due(self.due_in.value),
                    phone_internal_id=self.phone_in.value,
                )
                ticket_id = task.internal_id
        except TaskError as e:
            await interaction.followup.send(embed=error_embed(str(e)), ephemeral=True)
            return
        cog = interaction.client.get_cog("TasksCog")
        if cog:
            await cog.flush_posts()
        await interaction.followup.send(embed=success_embed(f"Ticket {ticket_id} created"), ephemeral=True)


# ====================================================================== control panel
def _list_embed(title: str, tasks, labels: dict, colour=discord.Colour.blurple()) -> discord.Embed:
    e = discord.Embed(title=title, colour=colour)
    if not tasks:
        e.description = "Nothing here. 🎉"
        return e
    lines = []
    for t in tasks:
        bits = [f"{STATUS_EMOJI[t.status]} {PRIORITY_EMOJI[t.priority]} `{t.internal_id}` {t.title}"]
        extra = []
        if t.phone_id in labels:
            extra.append(labels[t.phone_id][0])
        if t.due_date:
            extra.append(("⏰ " if t.due_date < dt.date.today() and t.status != "DONE" else "") + t.due_date.isoformat())
        if t.assignee_discord_id:
            extra.append(f"<@{t.assignee_discord_id}>")
        if extra:
            bits.append("· " + " · ".join(extra))
        lines.append(" ".join(bits))
    e.description = "\n".join(lines)[:4000]
    return e


async def _summary_embed() -> discord.Embed:
    async with session_scope() as session:
        s = await tasks_service.summary_counts(session)
    by = s["by_status"]
    e = discord.Embed(title="📊 Task board", colour=discord.Colour.blurple())
    e.add_field(name="⬜ To do", value=str(by.get("TODO", 0)), inline=True)
    e.add_field(name="🟨 In progress", value=str(by.get("IN_PROGRESS", 0)), inline=True)
    e.add_field(name="🟥 Blocked", value=str(by.get("BLOCKED", 0)), inline=True)
    e.add_field(name="✅ Done", value=str(by.get("DONE", 0)), inline=True)
    e.add_field(name="⏰ Overdue", value=str(s["overdue"]), inline=True)
    e.add_field(name="🚨 Urgent", value=str(s["urgent"]), inline=True)
    e.set_footer(text=f"{s['upcoming_hidden']} checklist items are waiting for their phone to reach that stage")
    return e


class TaskPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if await _is_authorized(interaction.user.id):
            return True
        await interaction.response.send_message("🔒 You're not authorized to use this bot.", ephemeral=True)
        return False

    @discord.ui.button(label="➕ New ticket", style=discord.ButtonStyle.success, custom_id="taskpanel:new")
    async def new(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(NewTicketModal())

    @discord.ui.button(label="📋 My tasks", style=discord.ButtonStyle.primary, custom_id="taskpanel:mine")
    async def mine(self, interaction: discord.Interaction, button: discord.ui.Button):
        async with session_scope() as session:
            rows = await tasks_service.list_tasks(session, assignee_discord_id=interaction.user.id)
            labels = await tasks_service.phone_labels(session, {t.phone_id for t in rows if t.phone_id})
        await interaction.response.send_message(embed=_list_embed("📋 Your open tasks", rows[:25], labels), ephemeral=True)

    @discord.ui.button(label="🔥 Urgent & overdue", style=discord.ButtonStyle.danger, custom_id="taskpanel:urgent")
    async def urgent(self, interaction: discord.Interaction, button: discord.ui.Button):
        async with session_scope() as session:
            rows = [t for t in await tasks_service.list_tasks(session)
                    if t.priority == "URGENT" or (t.due_date and t.due_date < dt.date.today())]
            labels = await tasks_service.phone_labels(session, {t.phone_id for t in rows if t.phone_id})
        await interaction.response.send_message(embed=_list_embed("🔥 Urgent & overdue", rows[:25], labels, discord.Colour.red()), ephemeral=True)

    @discord.ui.button(label="📊 Summary", style=discord.ButtonStyle.secondary, custom_id="taskpanel:summary")
    async def summary(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(embed=await _summary_embed(), ephemeral=True)


def _panel_embed() -> discord.Embed:
    return discord.Embed(
        title="✅ Task board",
        description=("Every job for the business lives here as its own ticket.\n"
                     "• **Phone tickets** appear automatically as each phone reaches that stage (intake → test → repair → "
                     "list → ship).\n• **General tickets** are business jobs and routines.\n"
                     "Use the buttons on a ticket to start, finish, block or claim it."),
        colour=discord.Colour.blurple(),
    )


# ====================================================================== phone checklist view
class PhoneChecklistView(discord.ui.View):
    def __init__(self, phone_id: int, has_checklist: bool, hidden_left: int, invoker_id: int):
        super().__init__(timeout=180)
        self.phone_id = phone_id
        self.invoker_id = invoker_id
        if not has_checklist:
            self.add_item(self._button("Generate checklist", self._generate, discord.ButtonStyle.success))
        elif hidden_left:
            self.add_item(self._button(f"Release remaining {hidden_left} now", self._release, discord.ButtonStyle.primary))

    def _button(self, label, cb, style):
        b = discord.ui.Button(label=label, style=style)
        b.callback = cb
        return b

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.invoker_id

    async def _generate(self, interaction: discord.Interaction):
        async with session_scope() as session:
            n = await tasks_service.ensure_phone_checklist(session, self.phone_id)
            from sqlalchemy import select
            from app.models.inventory_models import Phone
            status = (await session.execute(select(Phone.current_status).where(Phone.id == self.phone_id))).scalar_one()
            await tasks_service._release_or_complete(session, self.phone_id, f"PHONE:{status}")
        await interaction.response.edit_message(embed=success_embed(f"Created {n} checklist items", "Run the command again to see them."), view=None)
        cog = interaction.client.get_cog("TasksCog")
        if cog:
            await cog.flush_posts()

    async def _release(self, interaction: discord.Interaction):
        async with session_scope() as session:
            n = await tasks_service.release_all_for_phone(session, self.phone_id)
        await interaction.response.edit_message(embed=success_embed(f"Released {n} tickets into the channel"), view=None)
        cog = interaction.client.get_cog("TasksCog")
        if cog:
            await cog.flush_posts()


# ====================================================================== cog
class TasksCog(commands.GroupCog, group_name="tasks"):
    """Task tickets: per-phone checklists and general business to-dos."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._post_lock = asyncio.Lock()
        self._panel_checked = False

    async def cog_load(self) -> None:
        self.bot.add_view(TaskView())
        self.bot.add_view(ClosedTaskView())
        self.bot.add_view(TaskPanelView())
        self.engine_loop.start()

    async def cog_unload(self) -> None:
        self.engine_loop.cancel()

    # ------------------------------------------------------------ background engine
    @discord_tasks.loop(seconds=20)
    async def engine_loop(self) -> None:
        try:
            async with session_scope() as session:
                stats = await tasks_service.reconcile_triggers(session)
            if stats.checklists_created or stats.released or stats.auto_completed or stats.recurring_opened:
                logger.info("Task engine: %d checklist items created, %d released, %d auto-completed, %d recurring opened",
                            stats.checklists_created, stats.released, stats.auto_completed, stats.recurring_opened)
            if not self._panel_checked:
                await self._ensure_panel()
            await self.flush_posts()
            await self._notify_overdue()
            await self._archive_old()
        except Exception:  # never let one bad tick kill the loop
            logger.exception("Task engine tick failed")

    @engine_loop.before_loop
    async def _before_engine(self) -> None:
        await self.bot.wait_until_ready()

    async def _tasks_channel(self) -> Optional[discord.abc.Messageable]:
        async with session_scope() as session:
            channel_id = await config_service.get_channel_id_by_purpose(session, CHANNEL_PURPOSE)
        if not channel_id:
            return None
        channel = self.bot.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except discord.HTTPException:
                return None
        return channel

    async def flush_posts(self) -> None:
        """Post new tickets and redraw changed ones. Safe to call from anywhere."""
        async with self._post_lock:
            channel = await self._tasks_channel()
            if channel is None:
                return  # channel not set up yet; tickets stay queued
            async with session_scope() as session:
                pending = await tasks_service.tasks_needing_post(session)
                labels = await tasks_service.phone_labels(session, {t.phone_id for t in pending if t.phone_id})
                for task in pending:
                    # Closed before it was ever shown: nothing to post.
                    if task.discord_message_id is None and task.status in ("DONE", "CANCELLED"):
                        task.posted_version = task.version
                        continue
                    embed = build_task_embed(task, labels.get(task.phone_id))
                    view = view_for(task)
                    try:
                        if task.discord_message_id is None:
                            msg = await channel.send(embed=embed, view=view)
                            task.discord_message_id = msg.id
                            task.discord_channel_id = channel.id
                        else:
                            target = self.bot.get_partial_messageable(task.discord_channel_id or channel.id)
                            await target.get_partial_message(task.discord_message_id).edit(embed=embed, view=view)
                        task.posted_version = task.version
                    except discord.NotFound:
                        task.discord_message_id = None  # message was deleted: repost next tick
                    except discord.HTTPException as e:
                        logger.warning("Could not post %s: %s", task.internal_id, e)
                        break  # likely rate-limited; commit what we did and retry next tick
                    await asyncio.sleep(0.5)

    async def _notify_overdue(self) -> None:
        channel = await self._tasks_channel()
        if channel is None:
            return
        async with session_scope() as session:
            for task in await tasks_service.tasks_overdue_unnotified(session):
                try:
                    target = self.bot.get_partial_messageable(task.discord_channel_id or channel.id)
                    who = f" <@{task.assignee_discord_id}>" if task.assignee_discord_id else ""
                    await target.get_partial_message(task.discord_message_id).reply(f"⏰ **{task.internal_id}** is overdue.{who}")
                except discord.HTTPException:
                    pass
                task.overdue_notified = True
                task.version += 1  # redraw so the card shows the overdue marker
                await asyncio.sleep(0.5)

    async def _archive_old(self) -> None:
        async with session_scope() as session:
            raw = await config_service.get_setting(session, "tasks_archive_days", "3")
            try:
                days = int(raw)
            except (TypeError, ValueError):
                days = 3
            if days <= 0:
                return
            for task in await tasks_service.tasks_to_archive(session, days):
                try:
                    target = self.bot.get_partial_messageable(task.discord_channel_id)
                    await target.get_partial_message(task.discord_message_id).delete()
                except discord.NotFound:
                    pass
                except discord.HTTPException:
                    continue
                task.discord_message_id = None
                task.posted_version = task.version
                await asyncio.sleep(0.3)

    async def _ensure_panel(self) -> None:
        self._panel_checked = True
        channel = await self._tasks_channel()
        if channel is None:
            self._panel_checked = False  # try again once the channel exists
            return
        async with session_scope() as session:
            stored = await config_service.get_setting(session, _PANEL_SETTING)
        if stored and stored.isdigit():
            try:
                await channel.fetch_message(int(stored))
                return
            except discord.NotFound:
                pass
            except discord.HTTPException:
                return
        msg = await channel.send(embed=_panel_embed(), view=TaskPanelView())
        try:
            await msg.pin()
        except discord.HTTPException:
            pass
        async with session_scope() as session:
            await config_service.set_setting(session, _PANEL_SETTING, str(msg.id))

    # ------------------------------------------------------------ commands
    @app_commands.command(name="add", description="Create a ticket")
    @app_commands.describe(title="What needs doing", description="Details", priority="How urgent",
                           due="2026-11-03, 3d, today or tomorrow", phone="Phone ID if it's about one (IP-000004)",
                           category="e.g. Testing, Parts & Ordering, Admin", assignee="Who should do it")
    @app_commands.choices(priority=PRIORITY_CHOICES)
    @require_authorized()
    async def add(self, interaction: discord.Interaction, title: str, description: Optional[str] = None,
                  priority: Optional[app_commands.Choice[str]] = None, due: Optional[str] = None,
                  phone: Optional[str] = None, category: Optional[str] = None, assignee: Optional[discord.Member] = None):
        try:
            async with session_scope() as session:
                task = await tasks_service.create_task(
                    session, title, interaction.user.id, description=description,
                    priority=priority.value if priority else "NORMAL", due_date=tasks_service.parse_due(due),
                    category=category or "General", phone_internal_id=phone,
                    assignee_discord_id=assignee.id if assignee else None)
                ticket_id = task.internal_id
        except TaskError as e:
            await interaction.response.send_message(embed=error_embed(str(e)), ephemeral=True)
            return
        await self.flush_posts()
        await interaction.response.send_message(embed=success_embed(f"Ticket {ticket_id} created"), ephemeral=True)

    @app_commands.command(name="list", description="List tickets (open ones by default)")
    @app_commands.describe(status="Filter by status", phone="Only this phone (IP-000004)", category="Only this category",
                           mine="Only tickets assigned to you", scope="Phone checklist or general")
    @app_commands.choices(status=STATUS_CHOICES, scope=SCOPE_CHOICES)
    @require_authorized()
    async def list_(self, interaction: discord.Interaction, status: Optional[app_commands.Choice[str]] = None,
                    phone: Optional[str] = None, category: Optional[str] = None, mine: bool = False,
                    scope: Optional[app_commands.Choice[str]] = None):
        async with session_scope() as session:
            rows = await tasks_service.list_tasks(
                session, status=status.value if status else None, phone_internal_id=phone, category=category,
                assignee_discord_id=interaction.user.id if mine else None, scope=scope.value if scope else None)
            labels = await tasks_service.phone_labels(session, {t.phone_id for t in rows if t.phone_id})
        title = f"📋 Tickets ({len(rows)})"

        def render(page_items, page, total_pages):
            e = _list_embed(title, page_items, labels)
            e.set_footer(text=f"Page {page + 1}/{total_pages}")
            return e

        await send_paginated(interaction, rows, per_page=15, render=render, ephemeral=True)

    @app_commands.command(name="phone", description="Show the full checklist for one phone")
    @app_commands.describe(phone_id="e.g. IP-000004")
    @require_authorized()
    async def phone(self, interaction: discord.Interaction, phone_id: str):
        async with session_scope() as session:
            phone = await phones_service.get_phone(session, phone_id)
            if phone is None:
                await interaction.response.send_message(embed=error_embed(f"No phone `{phone_id.upper()}`."), ephemeral=True)
                return
            rows = await tasks_service.phone_checklist(session, phone.id)
            header = f"{phone.internal_id} — {phone.display_name()} ({phone.current_status})"
            phone_pk = phone.id
        e = discord.Embed(title=f"🧾 {header}", colour=discord.Colour.blurple())
        if not rows:
            e.description = "No checklist yet for this phone."
        else:
            done = sum(1 for t in rows if t.status == "DONE")
            live = sum(1 for t in rows if t.status != "CANCELLED")
            e.description = f"**{done}/{live}** done"
            by_cat: dict[str, list[str]] = {}
            for t in rows:
                if t.status == "CANCELLED":
                    continue
                mark = STATUS_EMOJI[t.status] if t.released or t.status == "DONE" else "🔒"
                by_cat.setdefault(t.category, []).append(f"{mark} {t.title}")
            for cat, lines in by_cat.items():
                e.add_field(name=cat, value="\n".join(lines)[:1024], inline=False)
            e.set_footer(text="🔒 = waiting for the phone to reach that stage")
        hidden = sum(1 for t in rows if not t.released and t.status == "TODO")
        view = PhoneChecklistView(phone_pk, bool(rows), hidden, interaction.user.id)
        await interaction.response.send_message(embed=e, view=view if view.children else discord.utils.MISSING, ephemeral=True)

    @app_commands.command(name="summary", description="Task counts: open, blocked, overdue, urgent")
    @require_authorized()
    async def summary(self, interaction: discord.Interaction):
        await interaction.response.send_message(embed=await _summary_embed(), ephemeral=True)

    @app_commands.command(name="panel", description="[Admin] Post the task control panel in this channel")
    @require_admin()
    async def panel(self, interaction: discord.Interaction):
        await interaction.response.send_message(embed=_panel_embed(), view=TaskPanelView())

    @app_commands.command(name="setup-general", description="[Admin] Open the general business to-do tickets")
    @require_admin()
    async def setup_general(self, interaction: discord.Interaction):
        async with session_scope() as session:
            n = await tasks_service.setup_general(session)
        await self.flush_posts()
        await interaction.response.send_message(embed=success_embed(
            f"Opened {n} general tickets", "Recurring ones come back on their own after you close them."), ephemeral=True)

    @app_commands.command(name="template-list", description="[Admin] Show the task templates")
    @app_commands.describe(scope="Phone checklist or general")
    @app_commands.choices(scope=SCOPE_CHOICES)
    @require_admin()
    async def template_list(self, interaction: discord.Interaction, scope: Optional[app_commands.Choice[str]] = None):
        async with session_scope() as session:
            rows = await tasks_service.list_templates(session, scope=scope.value if scope else None, include_inactive=True)
            items = [(t.code, t.title, t.scope, t.trigger or (f"every {t.recur_days}d" if t.recur_days else "one-off"), t.active) for t in rows]

        def render(page_items, page, total_pages):
            e = discord.Embed(title=f"🧩 Task templates ({len(items)})", colour=discord.Colour.blurple())
            e.description = "\n".join(f"{'✅' if a else '⏸️'} `{c}` {ti} · {sc.lower()} · {tr}" for c, ti, sc, tr, a in page_items)[:4000] or "None."
            e.set_footer(text=f"Page {page + 1}/{total_pages}")
            return e

        await send_paginated(interaction, items, per_page=12, render=render, ephemeral=True)

    @app_commands.command(name="template-add", description="[Admin] Add a task template")
    @app_commands.describe(code="Short unique code, e.g. check_charger", title="Ticket title", scope="Phone checklist or general",
                           category="e.g. Testing", trigger="Phone only, e.g. PHONE:TESTED or REPAIR:COMPLETE",
                           priority="Default priority", recur_days="General only: reopen every N days",
                           due_days="Days after release that it's due")
    @app_commands.choices(scope=SCOPE_CHOICES, priority=PRIORITY_CHOICES)
    @require_admin()
    async def template_add(self, interaction: discord.Interaction, code: str, title: str,
                           scope: app_commands.Choice[str], category: str, trigger: Optional[str] = None,
                           priority: Optional[app_commands.Choice[str]] = None, recur_days: Optional[int] = None,
                           due_days: Optional[int] = None):
        try:
            async with session_scope() as session:
                tpl = await tasks_service.add_template(
                    session, code=code, title=title, scope=scope.value, category=category, trigger=trigger,
                    priority=priority.value if priority else "NORMAL", recur_days=recur_days, due_after_days=due_days)
                code_out = tpl.code
        except TaskError as e:
            await interaction.response.send_message(embed=error_embed(str(e)), ephemeral=True)
            return
        await interaction.response.send_message(embed=success_embed(
            f"Template `{code_out}` added", "New phones get it automatically. Existing phones keep their current checklist."), ephemeral=True)

    @app_commands.command(name="template-toggle", description="[Admin] Turn a task template on or off")
    @app_commands.describe(code="Template code", active="True = on, False = off")
    @require_admin()
    async def template_toggle(self, interaction: discord.Interaction, code: str, active: bool):
        try:
            async with session_scope() as session:
                await tasks_service.set_template_active(session, code, active)
        except TaskError as e:
            await interaction.response.send_message(embed=error_embed(str(e)), ephemeral=True)
            return
        await interaction.response.send_message(embed=success_embed(f"`{code}` is now {'on' if active else 'off'}"), ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(TasksCog(bot))
