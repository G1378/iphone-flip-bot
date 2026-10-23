"""The job-channel flow against fake Discord objects: pressing Start, the panel,
buttons, the ticket's 'in progress' link, and the channel disappearing on listing.
No network. discord.py objects are replaced with small recorders."""
from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace

import discord
import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bot.cogs import jobs as jobs_cog
from app.bot.cogs import tasks as tasks_cog
from app.models.enums import TestOutcome
from app.models.inventory_models import Part
from app.models.task_models import JobChannel, Task
from app.services import config_service
from app.services import phones as phones_service
from tests.test_tasks import _buy, _record, _stock

TASKS_CHANNEL_ID = 5555


class _Resp:
    def __init__(self, status):
        self.status, self.reason = status, "x"


class FakeMessage:
    def __init__(self, channel, mid, content, embed, view):
        self.channel, self.id, self.content, self.embed, self.view = channel, mid, content, embed, view
        self.deleted = self.pinned = False
        self.edits = 0

    async def pin(self):
        self.pinned = True

    def buttons(self) -> list[str]:
        if self.view is None:
            return []
        return [c.get("custom_id") or c.get("url") for row in self.view.to_components() for c in row["components"]]


class FakePartial:
    def __init__(self, channel, mid):
        self.channel, self.id = channel, mid

    def _msg(self):
        m = self.channel.messages.get(self.id)
        if m is None or m.deleted:
            raise discord.NotFound(_Resp(404), "gone")
        return m

    async def edit(self, embed=None, view=None, **_):
        m = self._msg()
        m.embed, m.view = embed, view
        m.edits += 1

    async def delete(self):
        self._msg().deleted = True

    async def reply(self, text):
        self.channel.replies.append(text)


class FakeChannel:
    def __init__(self, cid, name="chan", category=None):
        self.id, self.name, self.category = cid, name, category
        self.messages: dict[int, FakeMessage] = {}
        self.replies: list[str] = []
        self.deleted = False
        self._n = cid * 1000

    @property
    def mention(self):
        return f"<#{self.id}>"

    async def send(self, content=None, embed=None, view=None):
        self._n += 1
        m = FakeMessage(self, self._n, content, embed, view)
        self.messages[m.id] = m
        return m

    def get_partial_message(self, mid):
        return FakePartial(self, mid)

    async def delete(self, reason=None):
        self.deleted = True

    @property
    def live(self):
        return [m for m in self.messages.values() if not m.deleted]

    def panel(self) -> FakeMessage:
        return [m for m in self.live if m.embed is not None and m.view is not None][-1]


class FakeCategory:
    def __init__(self, name, n_channels=0):
        self.name = name
        self.channels = [object() for _ in range(n_channels)]


class FakeGuild:
    id = 42

    def __init__(self, bot):
        self.bot, self.categories, self.forbid, self._next = bot, [], False, 9000

    async def create_category(self, name, reason=None):
        cat = FakeCategory(name)
        self.categories.append(cat)
        return cat

    async def create_text_channel(self, name, category=None, topic=None, reason=None):
        if self.forbid:
            raise discord.Forbidden(_Resp(403), "missing perms")
        self._next += 1
        ch = FakeChannel(self._next, name, category)
        ch.topic = topic
        category.channels.append(ch)
        self.bot.channels[ch.id] = ch
        return ch


class FakeBot:
    def __init__(self):
        self.channels, self.cogs = {}, {}

    def get_channel(self, cid):
        return self.channels.get(cid)

    async def fetch_channel(self, cid):
        return self.channels[cid]

    def get_partial_messageable(self, cid):
        return self.channels[cid]

    def get_cog(self, name):
        return self.cogs.get(name)

    def add_dynamic_items(self, *_):
        pass


class FakeResponse:
    def __init__(self):
        self.sent, self.modals, self.edited, self.deferred, self._done = [], [], [], False, False

    def is_done(self):
        return self._done

    async def defer(self, ephemeral=False, **_):
        self.deferred, self._done = True, True

    async def send_message(self, content=None, embed=None, view=None, ephemeral=False):
        self.sent.append(SimpleNamespace(content=content, embed=embed, view=view, ephemeral=ephemeral))
        self._done = True

    async def send_modal(self, modal):
        self.modals.append(modal)
        self._done = True

    async def edit_message(self, content=None, embed=None, view=None):
        self.edited.append(SimpleNamespace(content=content, embed=embed, view=view))
        self._done = True


class FakeFollowup:
    def __init__(self):
        self.sent = []

    async def send(self, content=None, embed=None, ephemeral=False, **_):
        self.sent.append(SimpleNamespace(content=content, embed=embed, ephemeral=ephemeral))


def interaction(bot, user_id, channel=None, guild=None, message=None):
    return SimpleNamespace(
        user=SimpleNamespace(id=user_id, display_name="Sam", mention=f"<@{user_id}>"), client=bot, guild=guild,
        channel=channel, channel_id=channel.id if channel else None, message=message,
        response=FakeResponse(), followup=FakeFollowup())


class CapturingConfirm:
    last = None

    def __init__(self, user_id, callback, confirm_label="Confirm"):
        CapturingConfirm.last = self
        self.callback = callback


@pytest_asyncio.fixture()
async def world(engine, session, monkeypatch, actor_id):
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

    @asynccontextmanager
    async def scope():
        async with factory() as s:
            try:
                yield s
                await s.commit()
            except Exception:
                await s.rollback()
                raise

    for mod in (tasks_cog, jobs_cog):
        monkeypatch.setattr(mod, "session_scope", scope)
    monkeypatch.setattr(jobs_cog, "ConfirmView", CapturingConfirm)

    bot = FakeBot()
    tasks_channel = FakeChannel(TASKS_CHANNEL_ID, "tasks")
    bot.channels[TASKS_CHANNEL_ID] = tasks_channel
    await config_service.add_channel(session, 1, TASKS_CHANNEL_ID, "tasks")
    await session.commit()

    tasks, jobs = tasks_cog.TasksCog(bot), jobs_cog.JobsCog(bot)
    tasks._panel_checked = True
    bot.cogs.update({"TasksCog": tasks, "JobsCog": jobs})
    return SimpleNamespace(bot=bot, tasks=tasks, jobs=jobs, tasks_channel=tasks_channel, guild=FakeGuild(bot),
                           scope=scope, session=session, actor=actor_id)


async def _phone_with_failed_screen(w, in_stock=True):
    if in_stock:
        await _stock(w.session, w.actor, "Screen", None)
    phone = await _buy(w.session, w.actor)
    await _record(w.session, phone, "display", TestOutcome.FAIL, w.actor)
    await w.session.commit()
    return phone.id


async def _ticket_message(w, phone_id) -> FakeMessage:
    async with w.scope() as s:
        t = (await s.execute(select(Task).where(Task.phone_id == phone_id, Task.status.in_(("TODO", "IN_PROGRESS", "BLOCKED"))))).scalar_one()
        mid = t.discord_message_id
    return w.tasks_channel.messages[mid]


async def _press_start(w, phone_id, user=7):
    msg = await _ticket_message(w, phone_id)
    inter = interaction(w.bot, user, w.tasks_channel, w.guild, message=SimpleNamespace(id=msg.id))
    await tasks_cog.TaskView().start.callback(inter)
    return inter


async def _job_channel(w, phone_id) -> FakeChannel:
    async with w.scope() as s:
        job = (await s.execute(select(JobChannel).where(JobChannel.phone_id == phone_id))).scalar_one()
    return w.bot.channels[job.channel_id]


def _in_channel(w, ch, user=7):
    return interaction(w.bot, user, ch, w.guild)


def _act_ref(buttons, act):
    return int(next(b for b in buttons if b.startswith(f"job:{act}:")).split(":")[2])


# ------------------------------------------------------------------ Start opens the channel
async def test_start_opens_a_channel_with_a_panel_and_the_ticket_becomes_a_link(world):
    w = world
    phone_id = await _phone_with_failed_screen(w)
    await w.tasks.engine_tick()
    ticket_msg = await _ticket_message(w, phone_id)
    assert "task:start" in ticket_msg.buttons(), "before Start the ticket has its normal buttons"

    inter = await _press_start(w, phone_id)

    assert [c.name for c in w.guild.categories] == ["🔧 Active jobs"]
    ch = await _job_channel(w, phone_id)
    assert ch.name.startswith("ip-000001") and ch.category.name == "🔧 Active jobs"
    panel = ch.panel()
    assert panel.pinned and "IP-000001" in panel.embed.title
    assert any(b.startswith("job:reserve:") for b in panel.buttons())
    assert inter.followup.sent[-1].content == f"Opened {ch.mention}"

    # The ticket in #tasks now says "In progress in #channel" and is just a link.
    ticket_msg = await _ticket_message(w, phone_id)
    status = next(f.value for f in ticket_msg.embed.fields if f.name == "Status")
    assert f"In progress in <#{ch.id}>" in status
    assert ticket_msg.buttons() == [f"https://discord.com/channels/{w.guild.id}/{ch.id}"]
    async with w.scope() as s:
        t = (await s.execute(select(Task).where(Task.phone_id == phone_id))).scalar_one()
    assert t.status == "IN_PROGRESS" and t.assignee_discord_id == 7


async def test_pressing_start_twice_does_not_open_a_second_channel(world):
    w = world
    phone_id = await _phone_with_failed_screen(w)
    await w.tasks.engine_tick()
    await _press_start(w, phone_id)
    ch = await _job_channel(w, phone_id)
    again = interaction(w.bot, 8, w.tasks_channel, w.guild)
    await w.jobs.open_job(again, phone_id)
    assert again.followup.sent[-1].content == f"Already open: <#{ch.id}>"
    assert len(w.guild.categories[0].channels) == 1


async def test_missing_manage_channels_permission_is_explained(world):
    w = world
    phone_id = await _phone_with_failed_screen(w)
    await w.tasks.engine_tick()
    w.guild.forbid = True
    inter = await _press_start(w, phone_id)
    assert "Manage Channels" in inter.followup.sent[-1].embed.description
    async with w.scope() as s:
        assert (await s.execute(select(JobChannel))).first() is None


async def test_a_full_category_overflows_into_a_new_one(world):
    w = world
    w.guild.categories.append(FakeCategory("🔧 Active jobs", n_channels=50))
    phone_id = await _phone_with_failed_screen(w)
    await w.tasks.engine_tick()
    await _press_start(w, phone_id)
    assert [c.name for c in w.guild.categories] == ["🔧 Active jobs", "🔧 Active jobs 2"]


# ------------------------------------------------------------------ working the job from the channel
async def test_reserve_then_fitted_leaves_a_trail_and_keeps_the_panel_at_the_bottom(world):
    w = world
    phone_id = await _phone_with_failed_screen(w)
    await w.tasks.engine_tick()
    await _press_start(w, phone_id)
    ch = await _job_channel(w, phone_id)
    first_panel = ch.panel()

    await w.jobs.handle(_in_channel(w, ch), "reserve", _act_ref(first_panel.buttons(), "reserve"))
    assert first_panel.deleted, "old panel replaced"
    assert "reserved" in ch.live[-2].content.lower() and "Sam" in ch.live[-2].content
    panel = ch.panel()
    assert ch.live[-1] is panel and panel.pinned, "the panel is always the last message"
    assert any(b.startswith("job:fit:") for b in panel.buttons()) and not any(b.startswith("job:reserve:") for b in panel.buttons())

    await w.jobs.handle(_in_channel(w, ch), "fit", _act_ref(panel.buttons(), "fit"))
    panel = ch.panel()
    assert {"job:retest:", "job:complete:"} <= {b.rsplit(":", 1)[0] + ":" for b in panel.buttons()}
    async with w.scope() as s:
        part = (await s.execute(select(Part))).scalar_one()
    assert part.status == "INSTALLED"

    # The #tasks ticket moved on with it: one live ticket, still "in progress" and linked.
    live = [m for m in w.tasks_channel.live if m.embed is not None]
    assert len(live) == 1 and "Re-test" in live[0].embed.title
    assert live[0].buttons() == [f"https://discord.com/channels/{w.guild.id}/{ch.id}"]


async def test_ordering_a_missing_part_from_the_channel(world):
    w = world
    phone_id = await _phone_with_failed_screen(w, in_stock=False)
    await w.tasks.engine_tick()
    await _press_start(w, phone_id)
    ch = await _job_channel(w, phone_id)
    panel = ch.panel()
    assert [b.split(":")[1] for b in panel.buttons() if b.split(":")[1] in ("order", "reserve", "fit", "receive")] == ["order"]

    inter = _in_channel(w, ch)
    await w.jobs.handle(inter, "order", _act_ref(panel.buttons(), "order"))
    form = inter.response.modals[0]
    assert form.title == "Order Screen"
    form.supplier._value, form.cost._value, form.expected._value = "ScreenCo", "35.50", "2026-11-02"
    submit = _in_channel(w, ch)
    await form.on_submit(submit)

    assert "ScreenCo" in ch.live[-2].content and "PO-000001" in ch.live[-2].content
    assert any(b.startswith("job:receive:") for b in ch.panel().buttons())
    ticket = await _ticket_message(w, phone_id)
    assert "Waiting for parts" in ticket.embed.title
    assert "on order" in ticket.embed.description


async def test_a_bad_form_value_is_explained_privately_and_changes_nothing(world):
    w = world
    phone_id = await _phone_with_failed_screen(w, in_stock=False)
    await w.tasks.engine_tick()
    await _press_start(w, phone_id)
    ch = await _job_channel(w, phone_id)
    panel_before = ch.panel()
    inter = _in_channel(w, ch)
    await w.jobs.handle(inter, "order", _act_ref(panel_before.buttons(), "order"))
    form = inter.response.modals[0]
    form.supplier._value, form.cost._value = "ScreenCo", "lots"
    submit = _in_channel(w, ch)
    await form.on_submit(submit)
    assert "number" in submit.followup.sent[-1].embed.description and submit.followup.sent[-1].ephemeral
    assert ch.panel() is panel_before and not panel_before.deleted


async def test_a_part_arriving_updates_the_panel_in_place(world):
    w = world
    phone_id = await _phone_with_failed_screen(w, in_stock=False)
    await w.tasks.engine_tick()
    await _press_start(w, phone_id)
    ch = await _job_channel(w, phone_id)
    panel = ch.panel()
    assert not any(b.startswith("job:reserve:") for b in panel.buttons())

    await _stock(w.session, w.actor, "Screen", None)
    await w.session.commit()
    await w.tasks.engine_tick()

    assert ch.panel() is panel and panel.edits == 1, "same message, edited, no new post"
    assert any(b.startswith("job:reserve:") for b in panel.buttons())


async def test_testing_buttons_start_the_walkthrough(world, monkeypatch):
    w = world
    phone = await _buy(w.session, w.actor)
    await w.session.commit()
    await w.tasks.engine_tick()
    await _press_start(w, phone.id)
    ch = await _job_channel(w, phone.id)
    assert {b.split(":")[1] for b in ch.panel().buttons()} == {"walk", "one", "fault", "edit", "move", "skip", "details"}

    calls = []

    async def fake_walkthrough(inter, internal_id, **kw):
        calls.append((internal_id, kw))
    monkeypatch.setattr(jobs_cog, "begin_walkthrough", fake_walkthrough)
    await w.jobs.handle(_in_channel(w, ch), "walk", phone.id)
    assert calls[0][0] == "IP-000001" and calls[0][1]["only_definition_ids"] is None and callable(calls[0][1]["on_finish"])


async def test_finishing_a_step_asks_first_then_closes_the_channel_once_listed(world):
    w = world
    phone = await _buy(w.session, w.actor)
    await phones_service.set_status(w.session, phone, "READY_FOR_LISTING", w.actor)
    await w.session.commit()
    await w.tasks.engine_tick()
    await _press_start(w, phone.id)
    ch = await _job_channel(w, phone.id)

    inter = _in_channel(w, ch)
    await w.jobs.handle(inter, "done", phone.id)
    assert "Listed" in inter.response.sent[0].embed.description and inter.response.sent[0].ephemeral
    assert not ch.deleted, "nothing happens until it's confirmed"

    await CapturingConfirm.last.callback(_in_channel(w, ch))   # the person presses "Do it"
    assert ch.deleted, "listed -> the channel is deleted straight away"
    async with w.scope() as s:
        assert (await s.execute(select(JobChannel))).first() is None
    assert [m for m in w.tasks_channel.live if m.embed is not None] == [], "and the ticket is gone too"


async def test_deleting_the_channel_by_hand_puts_the_ticket_back_to_start(world):
    w = world
    phone_id = await _phone_with_failed_screen(w)
    await w.tasks.engine_tick()
    await _press_start(w, phone_id)
    ch = await _job_channel(w, phone_id)
    await w.jobs.on_guild_channel_delete(ch)
    async with w.scope() as s:
        assert (await s.execute(select(JobChannel))).first() is None
        t = (await s.execute(select(Task).where(Task.phone_id == phone_id))).scalar_one()
    assert t.status == "TODO" and t.assignee_discord_id is None
    assert "task:start" in (await _ticket_message(w, phone_id)).buttons()


async def test_a_button_in_a_channel_that_is_no_longer_a_job_is_refused(world):
    w = world
    stray = FakeChannel(1234)
    inter = interaction(w.bot, 7, stray, w.guild)
    await w.jobs.handle(inter, "reserve", 1)
    assert "isn't an active job" in inter.response.sent[0].content
