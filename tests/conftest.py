from __future__ import annotations

import asyncio
import datetime as dt
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.models import Base
from app.services import config_service

TEST_DATABASE_URL = "postgresql+asyncpg://postgres:postgres@localhost:5432/iphoneflip_test"


@pytest_asyncio.fixture()
async def engine():
    eng = create_async_engine(TEST_DATABASE_URL, echo=False)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture()
async def session(engine):
    """Each test gets a fresh session. We don't wrap in a single rolled-back
    transaction because several services open their own nested flushes;
    instead we truncate all tables before each test for isolation."""
    session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with engine.begin() as conn:
        for table in reversed(Base.metadata.sorted_tables):
            await conn.execute(table.delete())
    async with session_factory() as s:
        await config_service.seed_defaults_if_empty(s)
        await s.commit()
        yield s
        await s.rollback()


@pytest.fixture()
def actor_id() -> int:
    return 999999


@pytest.fixture()
def today() -> dt.date:
    return dt.date(2026, 9, 1)
