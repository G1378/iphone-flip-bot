from __future__ import annotations

import datetime as dt
import logging
from decimal import Decimal
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from app.config import settings
from app.db import session_scope
from app.integrations.ebay.interface import EbayClientInterface, EbayApiError, EbayNotConfiguredError
from app.integrations.sheets.client import SheetsClient, SheetsNotConfiguredError
from app.integrations.vinted.interface import VintedClientInterface, VintedNotConfiguredError
from app.jobs.ebay_sync_job import sync_ebay_orders
from app.jobs.pricing_sync_job import run_pricing_refresh
from app.jobs.sheets_sync_job import run_sheets_sync
from app.services import config_service
from app.services import deal_queue
from app.services import deals as deals_service
from app.models.enums import DealSource

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
    deals: JobStatus = field(default_factory=lambda: JobStatus(name="deal_scan"))
    vinted: JobStatus = field(default_factory=lambda: JobStatus(name="vinted_scan"))


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
        vinted_client: Optional[VintedClientInterface] = None,
        deal_post_fn: Optional[Callable[[list[int]], Awaitable[int]]] = None,
        deal_refresh_fn: Optional[Callable[[list[int]], Awaitable[None]]] = None,
    ) -> None:
        self.ebay_client = ebay_client
        self.sheets_client = sheets_client
        self.notify_fn = notify_fn
        self.vinted_client = vinted_client
        # Posts buy-page cards for the given deal ids / greys out cards that expired. Without them (tests,
        # or a bare scheduler) deals fall back to plain text through notify_fn.
        self.deal_post_fn = deal_post_fn
        self.deal_refresh_fn = deal_refresh_fn
        self._vinted_was_blocked = False
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

        if self.ebay_client.buy_apis_configured and settings.deal_scan_interval_minutes > 0:
            self.scheduler.add_job(
                self._deals_tick, IntervalTrigger(minutes=settings.deal_scan_interval_minutes),
                id="deal_scan", max_instances=1, coalesce=True,
            )
        else:
            logger.info("Deal alerts off (eBay Buy APIs not configured, or DEAL_SCAN_INTERVAL_MINUTES=0).")

        if self.vinted_client is not None and self.vinted_client.configured and settings.vinted_scan_interval_minutes > 0:
            # Slow and irregular on purpose: a fixed-period poll is the easiest bot pattern to spot. The first scan
            # waits a full interval (no burst of traffic the moment the bot starts).
            self.scheduler.add_job(
                self._vinted_tick, IntervalTrigger(minutes=settings.vinted_scan_interval_minutes, jitter=120),
                id="vinted_scan", max_instances=1, coalesce=True,
            )
        else:
            logger.info("Vinted scanning off (VINTED_ENABLED / proxy not set, or VINTED_SCAN_INTERVAL_MINUTES=0).")

        # Cards nobody acted on, and auctions that ended, are greyed out so the buy page only shows live deals.
        self.scheduler.add_job(self._expiry_tick, IntervalTrigger(minutes=30), id="deal_expiry",
                               max_instances=1, coalesce=True)

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

    async def _post_new_cards(self, fallback_messages: list[str]) -> None:
        """Put every pending deal that has no card yet on the buy page (this also retries ones an earlier tick
        couldn't post, e.g. because the channel didn't exist yet). With no card poster, plain text instead."""
        if self.deal_post_fn is None:
            for m in fallback_messages:
                await self.notify_fn(m)
            return
        async with session_scope() as session:
            ids = [a.id for a in await deal_queue.unposted_pending(session)]
        if ids:
            await self.deal_post_fn(ids)

    async def _deals_tick(self) -> None:
        status = self.state.deals
        status.last_run_at = dt.datetime.now(dt.timezone.utc)
        try:
            async with deals_service.scan_lock(DealSource.EBAY.value):
                async with session_scope() as session:
                    result = await deals_service.scan_deals(session, self.ebay_client)
                    messages = [deals_service.format_alert(c) for c in result.candidates]
            status.last_success = not result.errors
            status.last_error = "; ".join(result.errors[:2]) or None
            status.last_summary = (f"{result.watched} model(s) watched, {result.checked} listing(s) checked, "
                                   f"{len(messages)} deal(s)")
            await self._post_new_cards(messages)
        except EbayNotConfiguredError:
            status.last_success = False
            status.last_error = "eBay not configured"
        except Exception as e:  # noqa: BLE001
            status.last_success = False
            status.last_error = f"Unexpected error: {e}"
            logger.exception("Unexpected error during deal scan tick")

    async def _vinted_tick(self) -> None:
        status = self.state.vinted
        status.last_run_at = dt.datetime.now(dt.timezone.utc)
        try:
            async with deals_service.scan_lock(DealSource.VINTED.value):
                async with session_scope() as session:
                    result = await deals_service.scan_vinted(session, self.vinted_client)
                    messages = [deals_service.format_alert(c) for c in result.candidates]
            status.last_success = not result.errors and not result.blocked
            status.last_error = "; ".join(result.errors[:2]) or None
            status.last_summary = (f"{result.watched} model(s) watched, {result.checked} listing(s) checked, "
                                   f"{len(messages)} deal(s)" + (" · BLOCKED by Vinted, backing off" if result.blocked else ""))
            await self._post_new_cards(messages)
            if result.blocked and not self._vinted_was_blocked:
                await self.notify_fn("⚠️ **Vinted is blocking the scraper.** It has stopped and will retry on its own "
                                     "with longer and longer pauses. If this keeps happening, the proxy is probably burned: "
                                     "swap it (`VINTED_PROXY_URLS`). Run `/deal-status` for detail.")
            elif not result.blocked and self._vinted_was_blocked:
                await self.notify_fn("✅ Vinted scanning is working again.")
            self._vinted_was_blocked = result.blocked
        except VintedNotConfiguredError:
            status.last_success = False
            status.last_error = "Vinted not configured"
        except Exception as e:  # noqa: BLE001
            status.last_success = False
            status.last_error = f"Unexpected error: {e}"
            logger.exception("Unexpected error during Vinted scan tick")

    async def _expiry_tick(self) -> None:
        try:
            async with session_scope() as session:
                ttl = await config_service.get_setting_decimal(session, "deal_pending_ttl_hours", Decimal("48"))
                expired = await deal_queue.expire_stale(session, ttl_hours=ttl)
                ids = [a.id for a in expired]
            if ids and self.deal_refresh_fn is not None:
                await self.deal_refresh_fn(ids)
        except Exception:  # noqa: BLE001 - housekeeping must never take the bot down
            logger.exception("Unexpected error expiring stale deals")

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
