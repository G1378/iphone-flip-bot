"""Job channels: one per phone, open from Start until the phone leaves the workshop.

DB bookkeeping only (no Discord imports). The Discord side lives in
app/bot/cogs/jobs.py."""
from __future__ import annotations

from typing import Optional, Sequence

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.inventory_models import Phone
from app.models.task_models import JobChannel

# A phone has a job channel only while it is in the workshop. The moment it is
# listed (or sold, scrapped...) the channel is deleted.
WORKSHOP_STATUSES = ("PURCHASED", "AWAITING_TEST", "TESTED", "REPAIR_REQUIRED", "AWAITING_PARTS", "READY_FOR_LISTING")
# Ticket steps that get a channel when Start is pressed. Later steps (post, close) don't.
JOB_STAGES = ("TEST", "REPAIR", "VERIFY", "PREP", "LIST")


async def get_job(session: AsyncSession, phone_id: int) -> Optional[JobChannel]:
    return (await session.execute(select(JobChannel).where(JobChannel.phone_id == phone_id))).scalar_one_or_none()


async def get_job_by_channel(session: AsyncSession, channel_id: int) -> Optional[JobChannel]:
    return (await session.execute(select(JobChannel).where(JobChannel.channel_id == channel_id))).scalar_one_or_none()


async def all_jobs(session: AsyncSession) -> Sequence[JobChannel]:
    return (await session.execute(select(JobChannel).order_by(JobChannel.id))).scalars().all()


async def register_job(session: AsyncSession, *, phone_id: int, guild_id: int, channel_id: int,
                       created_by: Optional[int]) -> Optional[JobChannel]:
    """Record a new job channel. Returns None if the phone already has one
    (two people pressed Start at once): the caller should delete its channel."""
    try:
        async with session.begin_nested():
            job = JobChannel(phone_id=phone_id, guild_id=guild_id, channel_id=channel_id, created_by_discord_id=created_by)
            session.add(job)
            await session.flush()
        return job
    except IntegrityError:
        return None


async def jobs_to_close(session: AsyncSession) -> Sequence[JobChannel]:
    """Jobs whose phone has left the workshop (listed, sold, scrapped...)."""
    return (await session.execute(
        select(JobChannel).join(Phone, JobChannel.phone_id == Phone.id)
        .where(Phone.current_status.not_in(WORKSHOP_STATUSES)))).scalars().all()


async def delete_job(session: AsyncSession, job: JobChannel) -> None:
    await session.delete(job)
    await session.flush()


async def drop_job_for_channel(session: AsyncSession, channel_id: int) -> Optional[int]:
    """The channel was deleted by hand: forget it and put the phone's ticket back
    to 'to do' so someone can press Start again. Returns the phone id."""
    from app.models.task_models import Task
    from app.services import tasks as tasks_service
    job = await get_job_by_channel(session, channel_id)
    if job is None:
        return None
    phone_id = job.phone_id
    await delete_job(session, job)
    ticket = await tasks_service.open_ticket_for_phone(session, phone_id)
    if ticket is not None and ticket.status == "IN_PROGRESS" and not ticket.system_blocked:
        ticket.status = "TODO"
        ticket.assignee_discord_id = None
        tasks_service.bump(ticket)
        await session.flush()
    return phone_id
