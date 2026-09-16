from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from app.config import settings
from app.db import session_scope
from app.integrations.ebay.interface import EbayClientInterface, EbayApiError, EbayNotConfiguredError
from app.integrations.sheets.client import SheetsClient, SheetsNotConfiguredError
from app.jobs.ebay_sync_job import sync_ebay_orders
from app.jobs.pricing_sync_job import run_pricing_refresh
from app.jobs.sheets_sync_job import run_sheets_sync
from app.services import config_service

logger = logging.getLogger(__name__)

# Internal (non-business) settings keys used purely to persist sync cursors
# across restarts - the Pi may reboot mid-day and we don't want to either
# miss orders or refetch everything from the beginning of time.
_EBAY_CURSOR_KEY = "_internal_last_ebay_sync_at"
_DEFAULT_LOOKBACK_HOURS = 48


@dataclass
class JobStatus:
    name: str
    last_run_at: Optional[dt.datetime] = None
    last_success: Optional[bool] = None
    last_error: Optional[str] = None
    last_summary: Optional[str] = None


@dataclass
class SchedulerState:
    ebay: JobStatus = field(default_factory=lambda: JobStatus(name="ebay_sync"))
    sheets: JobStatus = field(default_factory=lambda: JobStatus(name="sheets_sync"))
    pricing: JobStatus = field(default_factory=lambda: JobStatus(name="pricing_sync"))


class JobScheduler:
    """Owns the two recurring background jobs (spec section 19/21/29):
    - eBay order sync -> record sales, notify Discord
    - Google Sheets sync -> idempotent push of all entity tables

    Both jobs are wrapped so a failure in one tick (network outage, expired
    token, Sheets API hiccup) is logged, recorded in `self.state`, and
    surfaced to the configured Discord notification channel - it never
    crashes the bot process and never blocks the other job or the core
    Discord command handling (spec section 29: 'If eBay or Google Sheets is
    temporarily unavailable, the core inventory system must continue
    working').
    """

    def __init__(
        self,
        ebay_client: EbayClientInterface,
        sheets_client: SheetsClient,
        notify_fn: Callable[[str], Awaitable[None]],
    ) -> None:
        self.ebay_client = ebay_client
        self.sheets_client = sheets_client
        self.notify_fn = notify_fn
        self.scheduler = AsyncIOScheduler(timezone=settings.timezone)
        self.state = SchedulerState()

    def start(self) -> None:
        if self.ebay_client.configured:
            self.scheduler.add_job(
                self._ebay_tick, IntervalTrigger(minutes=settings.ebay_sync_interval_minutes),
                id="ebay_sync", next_run_time=dt.datetime.now(), max_instances=1, coalesce=True,
            )
        else:
            logger.info("eBay not configured - background eBay sync job disabled.")

        if self.sheets_client.configured:
            self.scheduler.add_job(
                self._sheets_tick, IntervalTrigger(minutes=settings.sheets_sync_interval_minutes),
                id="sheets_sync", next_run_time=dt.datetime.now(), max_instances=1, coalesce=True,
            )
        else:
            logger.info("Google Sheets not configured - background sync job disabled.")

        if self.ebay_client.buy_apis_configured:
            self.scheduler.add_job(
                self._pricing_tick, IntervalTrigger(days=settings.pricing_sync_interval_days),
                id="pricing_sync", next_run_time=dt.datetime.now(), max_instances=1, coalesce=True,
            )
        else:
            logger.info("eBay Buy APIs not configured - background pricing sync job disabled.")

        self.scheduler.start()

    async def shutdown(self) -> None:
        self.scheduler.shutdown(wait=False)

    # ------------------------------------------------------------------
    async def _ebay_tick(self) -> None:
        status = self.state.ebay
        status.last_run_at = dt.datetime.now(dt.timezone.utc)
        try:
            async with session_scope() as session:
                cursor_raw = await config_service.get_setting(session, _EBAY_CURSOR_KEY)
                since = (
                    dt.datetime.fromisoformat(cursor_raw) if cursor_raw
                    else dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=_DEFAULT_LOOKBACK_HOURS)
                )
                results = await sync_ebay_orders(session, self.ebay_client, since=since)
                await config_service.set_setting(
                    session, _EBAY_CURSOR_KEY, dt.datetime.now(dt.timezone.utc).isoformat()
                )

            new_sales = [r for r in results if r.matched and not r.already_recorded]
            status.last_success = True
            status.last_error = None
            status.last_summary = f"{len(results)} order(s) checked, {len(new_sales)} new sale(s)"

            for r in new_sales:
                await self.notify_fn(
                    f"🎉 **PHONE SOLD** — `{r.phone_internal_id}`\n"
                    f"Sale `{r.sale_internal_id}` (eBay order `{r.ebay_order_id}`)\n"
                    f"Net profit: £{r.net_profit}"
                )
        except EbayNotConfiguredError:
            status.last_success = False
            status.last_error = "eBay not configured"
        except EbayApiError as e:
            status.last_success = False
            status.last_error = str(e)
            logger.warning("eBay sync failed: %s", e)
        except Exception as e:  # noqa: BLE001 - background job must never crash the process
            status.last_success = False
            status.last_error = f"Unexpected error: {e}"
            logger.exception("Unexpected error during eBay sync tick")

    async def _sheets_tick(self) -> None:
        status = self.state.sheets
        status.last_run_at = dt.datetime.now(dt.timezone.utc)
        try:
            async with session_scope() as session:
                results = await run_sheets_sync(session, self.sheets_client)
            total_updated = sum(u for u, _ in results.values())
            total_appended = sum(a for _, a in results.values())
            status.last_success = True
            status.last_error = None
            status.last_summary = f"{total_updated} updated, {total_appended} appended across {len(results)} sheets"
        except SheetsNotConfiguredError:
            status.last_success = False
            status.last_error = "Google Sheets not configured"
        except Exception as e:  # noqa: BLE001
            status.last_success = False
            status.last_error = f"Unexpected error: {e}"
            logger.exception("Unexpected error during Sheets sync tick")

    async def _pricing_tick(self) -> None:
        status = self.state.pricing
        status.last_run_at = dt.datetime.now(dt.timezone.utc)
        try:
            async with session_scope() as session:
                results = await run_pricing_refresh(session, self.ebay_client)
            status.last_success = True
            status.last_error = None
            status.last_summary = (
                f"{results['watched']} model(s) watched, {results['observed']} updated, "
                f"{results['no_data']} had no data"
            )
        except EbayNotConfiguredError:
            status.last_success = False
            status.last_error = "eBay not configured"
        except Exception as e:  # noqa: BLE001
            status.last_success = False
            status.last_error = f"Unexpected error: {e}"
            logger.exception("Unexpected error during pricing sync tick")
