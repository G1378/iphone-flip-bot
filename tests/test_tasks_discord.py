"""Exercises the Discord side of the ticket channel against a fake channel:
posting, editing in place, deleting a finished step, and the buttons.
No network: discord.py objects are replaced with tiny recorders."""
from __future__ import annotations

import datetime as dt
from contextlib import asynccontextmanager
from decimal import Decimal

import discord
import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bot.cogs import tasks as tasks_cog
from app.models.config_models import TestDefinition
from app.models.enums import AcquisitionType, TestOutcome
from app.models.task_models import Task
from app.services import config_service
from app.services import phones as phones_service
from app.services import testing as testing_service

CHANNEL_ID = 5555


class FakeMessage:
    def __init__(self, mid, embed, view):
        self.id, self.embed, self.view = mid, embed, view
        self.deleted = False


class FakePartialMessage:
    def __init__(self, channel, mid):
        self.channel, self.id = channel, mid

    async def edit(self, embed=None, view=None):
        msg = self.channel.messages.get(self.id)
        if msg is None or msg.deleted:
            raise discord.NotFound(_Resp(404), "gone")
        msg.embed, msg.view = embed, view
        self.channel.edits += 1

    async def delete(self):
        msg = self.channel.messages.get(self.id)
        if msg is None or msg.deleted:
            raise discord.NotFound(_Resp(404), "gone")
        msg.deleted = True

    async def reply(self, text):
        self.channel.replies.append(text)


class _Resp:
    def __init__(self, status):
        self.status, self.reason = status, "x"


class FakeChannel:
    id = CHANNEL_ID

    def __init__(self):
        self.messages: dict[int, FakeMessage] = {}
        self.edits = 0
        self.replies: list[str] = []
        self._next = 1000

    async def send(self, embed=None, view=None):
        self._next += 1
        msg = FakeMessage(self._next, embed, view)
        self.messages[msg.id] = msg
        return msg

    def get_partial_message(self, mid):
        return FakePartialMessage(self, mid)

    @property
    def live(self):
        return [m for m in self.messages.values() if not m.deleted]


class FakeBot:
    def __init__(self, channel):
        self.channel = channel

    def get_channel(self, _):
        return self.channel

    def get_partial_messageable(self, _):
        return self.channel

    def get_cog(self, _):
        return None


@pytest_asyncio.fixture()
async def world(engine, session, monkeypatch):
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

    monkeypatch.setattr(tasks_cog, "session_scope", scope)
    await config_service.add_channel(session, 1, CHANNEL_ID, "tasks")
    await session.commit()
    channel = FakeChannel()
    cog = tasks_cog.TasksCog(FakeBot(channel))
    cog._panel_checked = True  # the pinned panel isn't what's under test
    return cog, channel, scope


async def _buy_phone(scope, actor_id):
    async with scope() as s:
        phone = await phones_service.buy_phone(s, phones_service.BuyPhoneInput(
            manufacturer="Apple", model="iPhone 13", variant=None, storage="128GB", colour="Blue", imei=None,
            serial_number=None, carrier=None, purchase_price=Decimal("120"), purchase_date=dt.date(2026, 9, 1),
            seller_source="eBay", acquisition_type=AcquisitionType.RESALE, notes=None, location_code=None), actor_id)
        return phone.id


async def _fail_display(scope, phone_id, actor_id):
    async with scope() as s:
        phone = await s.get(tasks_cog.Phone, phone_id)
        d = (await s.execute(select(TestDefinition).where(TestDefinition.code == "display"))).scalar_one()
        await testing_service.record_and_apply(s, phone, d, TestOutcome.FAIL, actor_id)


async def test_ticket_is_posted_once_then_replaced_when_the_step_changes(world, actor_id):
    cog, channel, scope = world
    phone_id = await _buy_phone(scope, actor_id)

    await cog.engine_tick()
    assert len(channel.live) == 1
    first = channel.live[0]
    assert "Run the tests" in first.embed.title and "IP-" in str(first.embed.fields[3].value)

    await cog.engine_tick()                       # nothing changed: nothing posted, nothing edited
    assert len(channel.live) == 1 and channel.edits == 0

    await _fail_display(scope, phone_id, actor_id)
    await cog.engine_tick()
    assert first.deleted, "the finished step's message is deleted"
    assert len(channel.live) == 1, "still exactly one ticket for the phone"
    swap = channel.live[0]
    assert "Waiting for parts" in swap.embed.title
    assert "Blocked on" in [f.name.replace("🚫 ", "") for f in swap.embed.fields]
    assert all(c.custom_id != "task:cancel" for c in swap.view.children), "automatic tickets can't be cancelled"


async def test_a_part_arriving_edits_the_same_message(world, actor_id):
    cog, channel, scope = world
    phone_id = await _buy_phone(scope, actor_id)
    await _fail_display(scope, phone_id, actor_id)
    await cog.engine_tick()
    msg = channel.live[0]
    assert "Waiting for parts" in msg.embed.title

    from app.models.enums import PartSourceType
    from app.services import parts as parts_service
    async with scope() as s:
        await parts_service.create_part(s, parts_service.CreatePartInput(
            part_type_name="Screen", source_type=PartSourceType.EXISTING_STOCK, cost=Decimal("30"),
            compatible_model_ids=[(await config_service.resolve_phone_model(s, "iPhone 13")).id]), actor_id)
    await cog.engine_tick()
    assert channel.live == [msg], "same message, not a new one"
    assert msg.embed.title.startswith("🔧 Swap parts") and channel.edits == 1


async def test_phone_leaving_the_workflow_removes_its_ticket(world, actor_id):
    cog, channel, scope = world
    phone_id = await _buy_phone(scope, actor_id)
    await cog.engine_tick()
    assert len(channel.live) == 1
    async with scope() as s:
        phone = await s.get(tasks_cog.Phone, phone_id)
        await phones_service.set_status(s, phone, "SCRAPPED", actor_id)
    await cog.engine_tick()
    assert channel.live == []


async def test_deleted_message_is_reposted_not_lost(world, actor_id):
    cog, channel, scope = world
    await _buy_phone(scope, actor_id)
    await cog.engine_tick()
    channel.live[0].deleted = True               # someone deleted it by hand
    async with scope() as s:                      # and the ticket changes, forcing a redraw
        t = (await s.execute(select(Task))).scalar_one()
        t.version += 1
    await cog.engine_tick()                       # edit hits NotFound ...
    await cog.engine_tick()                       # ... and the next tick reposts
    assert len(channel.live) == 1


async def test_nothing_is_posted_until_the_channel_exists(world, actor_id, session):
    cog, channel, scope = world
    async with scope() as s:
        from app.models.config_models import BotChannel
        row = (await s.execute(select(BotChannel))).scalar_one()
        row.active = False
    await _buy_phone(scope, actor_id)
    await cog.engine_tick()
    assert channel.live == []
    async with scope() as s:
        row = (await s.execute(select(BotChannel))).scalar_one()
        row.active = True
    await cog.engine_tick()
    assert len(channel.live) == 1, "queued tickets appear once the channel is set up"


async def test_overdue_general_ticket_gets_one_reply(world, actor_id):
    cog, channel, scope = world
    from app.services import tasks as tasks_service
    async with scope() as s:
        await tasks_service.create_task(s, "Late job", actor_id, due_date=dt.date.today() - dt.timedelta(days=2))
    await cog.engine_tick()
    await cog.engine_tick()
    assert len(channel.replies) == 1 and "overdue" in channel.replies[0]
