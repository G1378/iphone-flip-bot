"""Vinted listings, read the way a browser reads them.

IMPORTANT, read this before relying on it:
  * Vinted has NO public API. This calls the same JSON endpoints Vinted's own website uses.
    That is against Vinted's terms of use and Vinted can change or block it at any time.
  * It only ever reads PUBLIC listings while logged OUT. It never signs in, so there is no
    Vinted account that can be banned. Never give it your own account.
  * The endpoint paths, parameter names and response fields below follow what Vinted's web app
    is known to use, but could not be checked against the live site when this was written. They
    are all in this one file, and `parse_item` reads every field defensively, so if Vinted changes
    something the fix is local. Run ONE real search first (`/deal-status`, then `/deal-scan`).

What "looks like a normal visitor" means here (and what it does not):
  * One consistent identity per route: the same cookies, headers and (with curl_cffi installed)
    the same Chrome TLS/HTTP2 fingerprint for the whole session, as a real browser tab would.
    A plain Python HTTP client is trivially fingerprinted, which is why curl_cffi is recommended.
  * Visit the home page first so Vinted hands us its session cookies, and refresh them
    when they expire, exactly as a returning visitor's browser would.
  * A UK proxy, sticky for the whole scan (not a fresh IP per request, which is itself a bot signal).
  * Slow, irregular pacing (base interval x random jitter) and a hard cap on requests per scan.
  * Sensible Referer / Accept-Language / x-anon-id headers.

What it will NOT do: solve CAPTCHAs, forge anti-bot tokens, or fight a challenge. If Vinted says no
(403 / 429 / a challenge page) the route is put on a growing cool-down (5 min, 15, 45, ...), the next
route is tried, and when none are left `VintedBlockedError` is raised so the scan stops quietly and
tells you. Pushing harder only gets the proxy burned.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import random
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Awaitable, Callable, Optional
from urllib.parse import quote, urlparse

from app.config import settings
from app.integrations.vinted.interface import (
    VintedApiError, VintedBlockedError, VintedClientInterface, VintedHealth, VintedItem, VintedNotConfiguredError,
)

logger = logging.getLogger(__name__)

WARM_TTL_SECONDS = 55 * 60          # Vinted's web token lives about an hour; refresh a little early
REQUEST_TIMEOUT = 20
COOLDOWN_BASE_MINUTES = 5
COOLDOWN_CAP_MINUTES = 6 * 60
MAX_PER_PAGE = 96
CHALLENGE_MARKERS = ("captcha", "datadome", "access denied", "just a moment", "verify you are human",
                     "attention required", "cf-chl", "px-captcha", "are you a robot")

# Used only when curl_cffi is not installed (httpx has no browser TLS fingerprint, so this is weaker).
_FALLBACK_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/124.0.0.0 Safari/537.36"),
    "Accept-Language": "en-GB,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
}


# ---------------------------------------------------------------------------------- routes
@dataclass
class _Route:
    """One way out to the internet (a proxy, or this machine) with its own browser-like session."""
    name: str                                   # host:port only. Never the credentials.
    proxy: Optional[str]
    session: Any = None
    warmed_at: float = float("-inf")      # never: a monotonic clock can be small just after boot, so 0 would not mean 'long ago'
    last_request_at: float = 0.0
    cooldown_until: float = 0.0
    strikes: int = 0
    successes_since_strike: int = 0
    requests_this_session: int = 0


class _Blocked(Exception):
    def __init__(self, why: str, retry_after: Optional[float] = None):
        super().__init__(why)
        self.why, self.retry_after = why, retry_after


def _route_name(proxy: Optional[str]) -> str:
    if not proxy:
        return "direct"
    p = urlparse(proxy if "://" in proxy else f"http://{proxy}")
    return f"{p.hostname or '?'}:{p.port or ''}".rstrip(":")


async def _default_session_factory(route: _Route) -> Any:
    """curl_cffi (Chrome-identical TLS) when installed; plain httpx otherwise."""
    try:
        from curl_cffi.requests import AsyncSession
        proxies = {"http": route.proxy, "https": route.proxy} if route.proxy else None
        return AsyncSession(impersonate="chrome", proxies=proxies, timeout=REQUEST_TIMEOUT)
    except ImportError:
        import httpx
        logger.warning("curl_cffi is not installed: Vinted requests use plain httpx and are easier for Vinted to spot.")
        return httpx.AsyncClient(proxy=route.proxy, headers=_FALLBACK_HEADERS, timeout=REQUEST_TIMEOUT, follow_redirects=True)


async def _close(session: Any) -> None:
    try:
        closer = getattr(session, "aclose", None) or getattr(session, "close", None)
        if closer:
            res = closer()
            if asyncio.iscoroutine(res):
                await res
    except Exception:  # noqa: BLE001
        logger.debug("closing a Vinted session failed", exc_info=True)


# ---------------------------------------------------------------------------------- parsing
def _amount(node: Any) -> Optional[Decimal]:
    """Vinted has sent prices as "12.0", 12.0 and {"amount": "12.0", "currency_code": "GBP"}."""
    if isinstance(node, dict):
        node = node.get("amount")
    if node in (None, ""):
        return None
    try:
        return Decimal(str(node))
    except (InvalidOperation, ValueError):
        return None


def _timestamp(value: Any) -> Optional[dt.datetime]:
    try:
        if isinstance(value, (int, float)) or (isinstance(value, str) and value.isdigit()):
            return dt.datetime.fromtimestamp(int(value), tz=dt.timezone.utc)
        if isinstance(value, str) and value:
            return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, OverflowError, OSError):
        return None
    return None


def parse_item(raw: dict, domain: str) -> Optional[VintedItem]:
    """One catalog entry -> VintedItem. None if it has no usable price/id, isn't in GBP, or is closed."""
    try:
        item_id = str(raw["id"])
    except (KeyError, TypeError):
        return None
    price_node = raw.get("price")
    price = _amount(price_node)
    if price is None or price <= 0:
        return None
    currency = price_node.get("currency_code") if isinstance(price_node, dict) else raw.get("currency")
    if currency and str(currency).upper() != "GBP":
        return None
    if raw.get("is_closed") or raw.get("is_hidden") or raw.get("is_draft") or raw.get("is_reserved"):
        return None     # not buyable right now

    total = _amount(raw.get("total_item_price"))
    buyer_fee = (total - price) if total is not None and total > price else None

    url = raw.get("url") or f"https://{domain}/items/{item_id}"
    if url.startswith("/"):
        url = f"https://{domain}{url}"

    photo = raw.get("photo") or {}
    user = raw.get("user") or {}
    reputation = user.get("feedback_reputation")
    feedback_pct = None
    if isinstance(reputation, (int, float)):
        feedback_pct = Decimal(str(round(reputation * 100 if reputation <= 1 else reputation, 1)))
    reviews = user.get("feedback_count")
    if reviews is None and (user.get("positive_feedback_count") is not None or user.get("negative_feedback_count") is not None):
        reviews = (user.get("positive_feedback_count") or 0) + (user.get("negative_feedback_count") or 0)

    return VintedItem(
        item_id=item_id, title=str(raw.get("title") or ""), price=price, url=url, buyer_fee=buyer_fee,
        condition=raw.get("status") or raw.get("condition"), brand=raw.get("brand_title"),
        description=raw.get("description") or None,
        seller=user.get("login"), seller_id=str(user["id"]) if user.get("id") is not None else None,
        seller_feedback_pct=feedback_pct, seller_reviews=int(reviews) if isinstance(reviews, (int, float)) else None,
        image_url=photo.get("full_size_url") or photo.get("url"),
        listed_at=_timestamp(raw.get("created_at_ts") or (photo.get("high_resolution") or {}).get("timestamp")),
    )


def _looks_like_challenge(status: int, content_type: str, body: str) -> bool:
    """403/429/503 are refusals. A 200 that is HTML when we asked for JSON, and is small enough to be a
    challenge page (a real Vinted page is hundreds of KB and may mention 'captcha' in its scripts), is one too."""
    if status in (403, 429, 503):
        return True
    if "json" in (content_type or "").lower():
        return False
    body = body or ""
    return len(body) < 30000 and any(m in body[:6000].lower() for m in CHALLENGE_MARKERS)


# ---------------------------------------------------------------------------------- the client
class VintedClient(VintedClientInterface):
    configured = True

    def __init__(
        self, *, domain: str, proxies: list[str], catalog_ids: Optional[list[str]] = None,
        min_interval_seconds: float = 6.0, require_proxy: bool = True, rotate_after_requests: int = 60,
        session_factory: Optional[Callable[[_Route], Awaitable[Any]]] = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep, clock: Callable[[], float] = time.monotonic,
        rng: Optional[random.Random] = None,
    ) -> None:
        if require_proxy and not proxies:
            raise VintedNotConfiguredError("Vinted scanning needs a proxy (VINTED_PROXY_URLS).")
        self.domain = domain
        self.base = f"https://{domain}"
        self.catalog_ids = catalog_ids or []
        self.min_interval = max(1.0, float(min_interval_seconds))
        self.rotate_after = rotate_after_requests
        self._routes = [_Route(_route_name(p), p) for p in proxies] or [_Route("direct", None)]
        self._current = 0
        self._factory = session_factory or _default_session_factory
        self._sleep, self._clock = sleep, clock
        self._rng = rng or random.Random()
        self._lock = asyncio.Lock()          # one request at a time: that is how a person browses
        self._ok = self._blocked = 0
        self._last_ok_at: Optional[dt.datetime] = None
        self._last_error: Optional[str] = None

    @classmethod
    def from_settings(cls) -> "VintedClient":
        return cls(domain=settings.vinted_domain, proxies=settings.vinted_proxy_list,
                   catalog_ids=settings.vinted_catalog_id_list,
                   min_interval_seconds=settings.vinted_min_request_interval_seconds,
                   require_proxy=settings.vinted_require_proxy)

    # ------------------------------------------------------------------ public API
    async def search_items(self, query: str, *, max_price: Optional[Decimal] = None, limit: int = 40) -> list[VintedItem]:
        params: dict[str, str] = {
            "search_text": query, "order": "newest_first",
            "per_page": str(min(max(limit, 1), MAX_PER_PAGE)), "page": "1",
        }
        if max_price is not None:
            params["price_to"] = str(max_price)
            params["currency"] = "GBP"
        if self.catalog_ids:
            params["catalog_ids"] = ",".join(self.catalog_ids)
        data = await self._get_json("/api/v2/catalog/items", params, referer=f"{self.base}/catalog?search_text={quote(query)}")
        rows = data.get("items")
        if not isinstance(rows, list):
            raise VintedApiError("Vinted search returned an unexpected shape (no 'items' list): its API may have changed.")
        items = [i for i in (parse_item(r, self.domain) for r in rows if isinstance(r, dict)) if i]
        return items[:limit]

    async def get_description(self, item_id: str) -> Optional[str]:
        try:
            data = await self._get_json(f"/api/v2/items/{item_id}", {}, referer=f"{self.base}/items/{item_id}")
        except VintedApiError:
            return None      # sold/removed/odd response: the caller treats the description as unread
        node = data.get("item") if isinstance(data.get("item"), dict) else data
        text = node.get("description") if isinstance(node, dict) else None
        return str(text).strip() or None if text else None

    def health(self) -> VintedHealth:
        now = self._clock()
        cooling = [(r.name, max(1, int((r.cooldown_until - now) // 60) + 1)) for r in self._routes if r.cooldown_until > now]
        return VintedHealth(configured=True, routes=len(self._routes), routes_available=len(self._routes) - len(cooling),
                            cooling=cooling, requests_ok=self._ok, requests_blocked=self._blocked,
                            last_ok_at=self._last_ok_at, last_error=self._last_error)

    async def aclose(self) -> None:
        for r in self._routes:
            if r.session is not None:
                await _close(r.session)
                r.session = None

    # ------------------------------------------------------------------ internals
    def _available(self) -> list[int]:
        now = self._clock()
        return [i for i, r in enumerate(self._routes) if r.cooldown_until <= now]

    def _pick_route(self) -> _Route:
        avail = self._available()
        if not avail:
            soonest = min(r.cooldown_until for r in self._routes) - self._clock()
            raise VintedBlockedError(f"every Vinted route is cooling down after being blocked; next try in ~{max(1, int(soonest // 60) + 1)} min")
        if self._current not in avail:
            self._current = avail[0]
        route = self._routes[self._current]
        if route.requests_this_session >= self.rotate_after and len(avail) > 1:
            # a long browsing session from one IP gets noticed: move on, and warm the next route freshly
            self._current = avail[(avail.index(self._current) + 1) % len(avail)]
            route = self._routes[self._current]
        return route

    def _penalise(self, route: _Route, why: str, retry_after: Optional[float]) -> None:
        route.strikes += 1
        route.successes_since_strike = 0
        minutes = min(COOLDOWN_CAP_MINUTES, COOLDOWN_BASE_MINUTES * (3 ** (route.strikes - 1)))
        seconds = max(minutes * 60, retry_after or 0)
        route.cooldown_until = self._clock() + seconds
        route.warmed_at = float("-inf")            # new session, new cookies, when it comes back
        self._blocked += 1
        self._last_error = f"{route.name}: {why}"
        logger.warning("Vinted route %s blocked (%s): cooling down %d min", route.name, why, int(seconds // 60))

    def _credit(self, route: _Route) -> None:
        route.successes_since_strike += 1
        if route.successes_since_strike >= 20:
            route.strikes = max(0, route.strikes - 1)
            route.successes_since_strike = 0
        self._ok += 1
        self._last_ok_at = dt.datetime.now(dt.timezone.utc)

    async def _pace(self, route: _Route) -> None:
        wait = route.last_request_at + self.min_interval * self._rng.uniform(0.8, 1.7) - self._clock()
        if wait > 0:
            await self._sleep(wait)
        route.last_request_at = self._clock()

    @staticmethod
    def _cookie(session: Any, name: str) -> Optional[str]:
        try:
            value = session.cookies.get(name)
            return str(value) if value else None
        except Exception:  # noqa: BLE001
            return None

    async def _warm(self, route: _Route) -> None:
        """Open the home page like a visitor arriving: it hands out the session cookies."""
        if route.session is None:
            route.session = await self._factory(route)
        await self._pace(route)
        resp = await route.session.get(self.base + "/", headers={
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "en-GB,en;q=0.9",
            "Upgrade-Insecure-Requests": "1", "Sec-Fetch-Dest": "document", "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none", "Sec-Fetch-User": "?1",
        })
        status, ctype, body = resp.status_code, resp.headers.get("content-type", ""), resp.text or ""
        if _looks_like_challenge(status, ctype, body):
            raise _Blocked(f"home page answered HTTP {status} / a challenge", self._retry_after(resp))
        if status >= 400:
            raise VintedApiError(f"Vinted home page returned HTTP {status}")
        route.warmed_at = self._clock()
        route.requests_this_session = 0

    @staticmethod
    def _retry_after(resp: Any) -> Optional[float]:
        try:
            return float(resp.headers.get("retry-after"))
        except (TypeError, ValueError):
            return None

    async def _get_json(self, path: str, params: dict[str, str], *, referer: str) -> dict:
        async with self._lock:
            refreshed_auth = False
            tried_blocked = 0
            while True:
                route = self._pick_route()
                try:
                    if route.session is None or self._clock() - route.warmed_at > WARM_TTL_SECONDS:
                        await self._warm(route)
                    await self._pace(route)
                    headers = {
                        "Accept": "application/json, text/plain, */*", "Accept-Language": "en-GB,en;q=0.9",
                        "Referer": referer, "Sec-Fetch-Dest": "empty", "Sec-Fetch-Mode": "cors", "Sec-Fetch-Site": "same-origin",
                    }
                    anon = self._cookie(route.session, "anon_id")
                    if anon:
                        headers["x-anon-id"] = anon
                    resp = await route.session.get(self.base + path, params=params, headers=headers)
                    route.requests_this_session += 1
                    status, ctype = resp.status_code, resp.headers.get("content-type", "")
                    body = resp.text if "json" not in ctype.lower() else ""
                    if _looks_like_challenge(status, ctype, body):
                        raise _Blocked(f"HTTP {status} / a challenge page", self._retry_after(resp))
                    if status == 401 and not refreshed_auth:
                        refreshed_auth = True            # session expired: visit the home page again and retry once
                        route.warmed_at = float("-inf")
                        continue
                    if status >= 400:
                        raise VintedApiError(f"Vinted returned HTTP {status} for {path}")
                    try:
                        data = resp.json()
                    except Exception as e:  # noqa: BLE001
                        raise VintedApiError(f"Vinted returned something that isn't JSON for {path}") from e
                    if not isinstance(data, dict):
                        raise VintedApiError(f"Vinted returned an unexpected response for {path}")
                    self._credit(route)
                    return data
                except _Blocked as b:
                    self._penalise(route, b.why, b.retry_after)
                    tried_blocked += 1
                    if tried_blocked >= len(self._routes):
                        raise VintedBlockedError(f"Vinted is blocking every route ({b.why}); backing off") from None
                    continue
                except VintedApiError as e:
                    self._last_error = str(e)
                    raise
                except (VintedBlockedError, asyncio.CancelledError):
                    raise
                except Exception as e:  # noqa: BLE001 - network errors differ between curl_cffi and httpx
                    # A flaky proxy is not a block: short cool-down, close the session, try another route if any.
                    route.cooldown_until = self._clock() + 60
                    route.warmed_at = float("-inf")
                    if route.session is not None:
                        await _close(route.session)
                        route.session = None
                    self._last_error = f"{route.name}: {type(e).__name__}"
                    tried_blocked += 1
                    if tried_blocked >= len(self._routes):
                        raise VintedApiError(f"Vinted request failed ({type(e).__name__}); check the proxy") from e
