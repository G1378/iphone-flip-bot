"""Vinted listings, read the way a browser reads them.

IMPORTANT, read this before relying on it:
  * Vinted has NO public API. This calls the JSON endpoints Vinted's own website uses. That is against Vinted's
    terms of use and Vinted can change or block it at any time.
  * It only ever reads PUBLIC listings while logged OUT. It never signs in, so there is no Vinted account that
    can be banned. Never give it your own account.
  * September 2026: Vinted retired www.vinted.<tld>/api/v2/catalog/items (404 for everyone) and moved search to
    https://api.vinted.<tld>/svc-catalogue/items. It wants an anonymous bearer token (the `access_token_web`
    cookie the home page hands out) plus X-Anon-Id and X-Csrf-Token headers, rejects blank parameters with
    400 INVALID_REQUEST, renamed the id filters (`attribute_ids[catalog]`...), and returns relative URLs with
    brand/condition in an `item_box`. This was written from what other open-source scrapers report after that
    change, NOT from access to the live site: the token/CSRF discovery and parameter names are best-effort.
    `/vinted-test` shows exactly what Vinted answers so any mismatch is a one-line fix, all in this file
    (`_collect_tokens`, `_search_params`, `parse_item`, `get_description`).

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
import json
import logging
import random
import re
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
    bearer: Optional[str] = None      # anonymous access token (the access_token_web cookie)
    anon_id: Optional[str] = None
    csrf: Optional[str] = None
    csrf_source: str = "none"


class _Blocked(Exception):
    def __init__(self, why: str, retry_after: Optional[float] = None):
        super().__init__(why)
        self.why, self.retry_after = why, retry_after


def _route_name(proxy: Optional[str]) -> str:
    if not proxy:
        return "direct"
    p = urlparse(proxy if "://" in proxy else f"http://{proxy}")
    return f"{p.hostname or '?'}:{p.port or ''}".rstrip(":")


_CRED = re.compile(r"(//)[^/@\s]+@")


def _redact(text: str, proxy: Optional[str] = None) -> str:
    """Remove proxy credentials from anything that is about to be shown in Discord or a log."""
    out = str(text)
    if proxy:
        out = out.replace(proxy, "<proxy>")
    return _CRED.sub(r"\1***@", out)


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

    # Since Sept 2026 brand / size / condition sit in a positional "item_box": first_line is the brand (only when it
    # differs from the title), second_line is "size · condition" (only when the listing has a size).
    box = raw.get("item_box") if isinstance(raw.get("item_box"), dict) else {}
    second = str(box.get("second_line") or "")
    box_condition = second.split("·")[-1].strip() if "·" in second else (second.strip() or None)
    return VintedItem(
        item_id=item_id, title=str(raw.get("title") or ""), price=price, url=url, buyer_fee=buyer_fee,
        condition=raw.get("status") or raw.get("condition") or box_condition,
        brand=raw.get("brand_title") or (str(box.get("first_line")) if box.get("first_line") else None),
        description=raw.get("description") or None,
        seller=user.get("login"), seller_id=str(user["id"]) if user.get("id") is not None else None,
        seller_feedback_pct=feedback_pct, seller_reviews=int(reviews) if isinstance(reviews, (int, float)) else None,
        image_url=photo.get("full_size_url") or photo.get("url"),
        listed_at=_timestamp(raw.get("created_at_ts") or (photo.get("high_resolution") or {}).get("timestamp")),
    )


_CSRF_RX = (
    re.compile(r"""<meta[^>]+name=["']csrf-token["'][^>]+content=["']([^"']{10,})"""),
    re.compile(r"""csrf[_-]?token\\*"?\s*[:=]\s*\\*"([A-Za-z0-9._\-=+/]{16,})"""),
)
_ANON_RX = re.compile(r"""anon[_-]?id\\*"?\s*[:=]\s*\\*"([0-9A-Fa-f\-]{16,})""")
_OG_DESC = re.compile(r"""<meta[^>]+property=["']og:description["'][^>]+content=["']([^"']*)""")
_JSONLD_DESC = re.compile(r'''"description"\s*:\s*"((?:[^"\\]|\\.)*)"''')


def extract_page_tokens(html: str) -> tuple[Optional[str], Optional[str]]:
    """(csrf, anon_id) found in the home page's HTML, when they are not in cookies. Best effort."""
    csrf = next((m.group(1) for rx in _CSRF_RX if (m := rx.search(html or ""))), None)
    anon = (m.group(1) if (m := _ANON_RX.search(html or "")) else None)
    return csrf, anon


def extract_page_description(html: str) -> Optional[str]:
    """The seller's text from a server-rendered item page: JSON-LD first, og:description as a fallback."""
    import html as htmllib
    for rx in (_JSONLD_DESC, _OG_DESC):
        m = rx.search(html or "")
        if m and m.group(1).strip():
            raw = m.group(1)
            try:
                raw = json.loads(f'"{raw}"') if rx is _JSONLD_DESC else raw
            except ValueError:
                pass
            return htmllib.unescape(str(raw)).strip() or None
    return None


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
        self, *, domain: str, proxies: list[str], catalog_ids: Optional[list[str]] = None, api_host: str = "",
        min_interval_seconds: float = 6.0, require_proxy: bool = True, rotate_after_requests: int = 60,
        session_factory: Optional[Callable[[_Route], Awaitable[Any]]] = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep, clock: Callable[[], float] = time.monotonic,
        rng: Optional[random.Random] = None,
    ) -> None:
        if require_proxy and not proxies:
            raise VintedNotConfiguredError("Vinted scanning needs a proxy (VINTED_PROXY_URLS).")
        self.domain = domain
        self.base = f"https://{domain}"
        host = api_host or ("api." + domain[4:] if domain.startswith("www.") else "api." + domain)
        self.api_base = f"https://{host}"
        self._items_api_ok: Optional[bool] = None     # does www /api/v2/items/<id> still exist? learned on first use
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
        return cls(domain=settings.vinted_domain, api_host=settings.vinted_api_host, proxies=settings.vinted_proxy_list,
                   catalog_ids=settings.vinted_catalog_id_list,
                   min_interval_seconds=settings.vinted_min_request_interval_seconds,
                   require_proxy=settings.vinted_require_proxy)

    # ------------------------------------------------------------------ public API
    async def search_items(self, query: str, *, max_price: Optional[Decimal] = None, limit: int = 40) -> list[VintedItem]:
        params = self._search_params(query, max_price, limit)
        data = await self._get_json(f"{self.api_base}/svc-catalogue/items", params,
                                    referer=f"{self.base}/catalog?search_text={quote(query)}", api=True)
        rows = data.get("items")
        if not isinstance(rows, list):
            keys = ", ".join(list(data.keys())[:10])
            raise VintedApiError(f"Vinted search returned an unexpected shape (no 'items' list; keys: {keys}): its API may have changed.")
        items = [i for i in (parse_item(r, self.domain) for r in rows if isinstance(r, dict)) if i]
        return items[:limit]

    def _search_params(self, query: str, max_price: Optional[Decimal], limit: int) -> dict[str, str]:
        params: dict[str, Optional[str]] = {
            "search_text": query, "order": "newest_first", "page": "1",
            "per_page": str(min(max(limit, 1), MAX_PER_PAGE)),
            "price_to": str(max_price) if max_price is not None else None,
            # the id filters were renamed in Sept 2026; the old names are silently IGNORED (results come back unfiltered)
            "attribute_ids[catalog]": ",".join(self.catalog_ids) or None,
        }
        # the new endpoint rejects blank parameters with 400 INVALID_REQUEST, where the old one ignored them
        return {k: v for k, v in params.items() if v not in (None, "")}

    async def get_description(self, item_id: str) -> Optional[str]:
        """The seller's text. Tries the cheap JSON item endpoint first (remembering if it is gone), then the
        server-rendered item page. A block is not swallowed; anything else just means "couldn't read it"."""
        if self._items_api_ok is not False:
            try:
                data = await self._get_json(f"{self.base}/api/v2/items/{item_id}", {}, referer=f"{self.base}/items/{item_id}", api=True)
                node = data.get("item") if isinstance(data.get("item"), dict) else data
                text = node.get("description") if isinstance(node, dict) else None
                self._items_api_ok = True
                if text:
                    return str(text).strip() or None
                return None
            except VintedApiError as e:
                if "HTTP 404" in str(e):
                    self._items_api_ok = False       # gone: stop wasting a request on it every time
        return await self._description_from_page(item_id)

    async def _description_from_page(self, item_id: str) -> Optional[str]:
        async with self._lock:
            try:
                route = self._pick_route()
                if route.session is None or self._clock() - route.warmed_at > WARM_TTL_SECONDS:
                    await self._warm(route)
                await self._pace(route)
                resp = await route.session.get(f"{self.base}/items/{item_id}", headers={**self._home_headers(), "Sec-Fetch-Site": "same-origin"})
            except _Blocked as b:
                self._penalise(route, b.why, b.retry_after)
                raise VintedBlockedError(f"Vinted is blocking ({b.why}); backing off") from None
            except (VintedApiError, VintedBlockedError):
                raise
            except Exception:  # noqa: BLE001
                return None
            if _looks_like_challenge(resp.status_code, resp.headers.get("content-type", ""), resp.text or "" ):
                self._penalise(route, f"item page answered HTTP {resp.status_code} / a challenge", self._retry_after(resp))
                raise VintedBlockedError("Vinted is blocking the item pages; backing off")
            if resp.status_code >= 400:
                return None
            self._credit(route)
            return extract_page_description(resp.text or "")

    def health(self) -> VintedHealth:
        now = self._clock()
        cooling = [(r.name, max(1, int((r.cooldown_until - now) // 60) + 1)) for r in self._routes if r.cooldown_until > now]
        return VintedHealth(configured=True, routes=len(self._routes), routes_available=len(self._routes) - len(cooling),
                            cooling=cooling, requests_ok=self._ok, requests_blocked=self._blocked,
                            last_ok_at=self._last_ok_at, last_error=self._last_error)

    @staticmethod
    def _describe(resp: Any) -> str:
        """Status line for a diagnostic: never includes cookie values or credentials."""
        h = {str(k).lower(): str(v) for k, v in dict(resp.headers).items()}
        seen = [f"{k}={v[:40]}" for k, v in h.items() if k == "server" or k.startswith(("cf-ray", "x-datadome", "x-dd", "x-px"))]
        return (f"HTTP {resp.status_code} · {h.get('content-type', '?').split(';')[0]} · {len(resp.text or '')} bytes"
                + (" · " + ", ".join(seen) if seen else ""))

    async def probe(self, query: str = "iPhone 13") -> list[str]:
        """One home-page request then one search, reported step by step, so you can see WHICH request Vinted
        refuses. It respects cool-downs (sends nothing while cooling) and uses the same headers real scans do."""
        out: list[str] = []
        async with self._lock:
            avail = self._available()
            if not avail:
                return ["Every route is cooling down, so nothing was sent." + (f" Last problem: {_redact(self._last_error)}" if self._last_error else "")]
            route = self._routes[avail[0]]
            if route.session is not None:
                await _close(route.session)
                route.session = None
            try:
                route.session = await self._factory(route)
                out.append(f"Route {route.name} · client: {type(route.session).__module__.split('.')[0]}"
                           + ("" if "curl_cffi" in type(route.session).__module__ else "  ⚠️ not curl_cffi: weaker browser fingerprint"))
                await self._pace(route)
                home = await route.session.get(self.base + "/", headers=self._home_headers())
            except Exception as e:  # noqa: BLE001
                return out + [f"1. Home page: could not connect ({type(e).__name__}: {_redact(e, route.proxy)[:160]})",
                              "→ The proxy itself failed (wrong host/port/login, or it is down). Vinted never saw a request."]
            out.append(f"1. Home page: {self._describe(home)}")
            try:
                cookies = sorted(str(k) for k in route.session.cookies.keys())
            except Exception:  # noqa: BLE001
                cookies = []
            out.append(f"   cookies received: {', '.join(cookies) if cookies else 'none'}")
            if not _looks_like_challenge(home.status_code, home.headers.get("content-type", ""), home.text or ""):
                self._collect_tokens(route, home.text or "")
                out.append(f"   session token: {'yes' if route.bearer else 'NO'} · anon id: {'yes' if route.anon_id else 'NO'}"
                           f" · csrf: {route.csrf_source}")
            if _looks_like_challenge(home.status_code, home.headers.get("content-type", ""), home.text or ""):
                self._penalise(route, f"home page answered HTTP {home.status_code} / a challenge", self._retry_after(home))
                return out + ["→ Vinted refused this IP at the front door, before any search. This is IP reputation (datacentre or "
                              "flagged range), not something the bot can fix. Use a UK residential/mobile proxy."]
            route.warmed_at = self._clock()
            route.requests_this_session = 0
            params = self._search_params(query, None, 5)
            try:
                await self._pace(route)
                api = await route.session.get(f"{self.api_base}/svc-catalogue/items", params=params,
                                              headers=self._api_headers(route, f"{self.base}/catalog?search_text={quote(query)}"))
            except Exception as e:  # noqa: BLE001
                return out + [f"2. Search: network error ({type(e).__name__}: {_redact(e, route.proxy)[:160]})"]
            out.append(f"2. Search ({urlparse(self.api_base).hostname}/svc-catalogue/items): {self._describe(api)}")
            ctype = api.headers.get("content-type", "")
            if _looks_like_challenge(api.status_code, ctype, "" if "json" in ctype.lower() else (api.text or "")):
                self._penalise(route, f"HTTP {api.status_code} / a challenge page", self._retry_after(api))
                return out + ["→ The home page loaded but the search call was challenged. That is Vinted's bot detection, and "
                              "retrying harder makes it worse. A residential IP may be treated differently; if it is still "
                              "challenged there, stop: don't escalate (no CAPTCHA solving / stealth browsers)."]
            if api.status_code >= 400:
                body = re.sub(r"\s+", " ", _redact(api.text or "", route.proxy))[:220]
                hint = {400: "Vinted rejected the request parameters (INVALID_REQUEST usually means a param name/blank value it doesn't accept).",
                        401: "The bearer token was not accepted: the session token may be wrong or expired.",
                        404: "That path doesn't exist on this host: Vinted may have moved the catalogue again."}.get(api.status_code, "Unexpected status.")
                return out + [f"   body: {body}", f"→ {hint} Send me these lines."]
            try:
                data = api.json()
            except Exception:  # noqa: BLE001
                snippet = re.sub(r"\s+", " ", _redact(api.text or "", route.proxy))[:200]
                return out + [f"→ Reached Vinted but the reply isn't JSON. Starts: {snippet}"]
            rows = data.get("items") if isinstance(data, dict) else None
            if not isinstance(rows, list):
                keys = ", ".join(list(data.keys())[:12]) if isinstance(data, dict) else type(data).__name__
                return out + [f"→ JSON came back but without an 'items' list (keys: {keys}). Vinted's API shape differs from what "
                              "this client expects: send me this line and I'll adjust parse_item/search_items."]
            parsed = [i for i in (parse_item(r, self.domain) for r in rows if isinstance(r, dict)) if i]
            self._credit(route)
            first = f" First: “{parsed[0].title[:50]}” £{parsed[0].price}" if parsed else ""
            return out + [f"→ ✅ Working: {len(rows)} results, {len(parsed)} usable.{first}"]

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
            why = f" (last problem: {self._last_error})" if self._last_error else ""
            raise VintedBlockedError(f"every Vinted route is cooling down after being blocked; next try in ~{max(1, int(soonest // 60) + 1)} min{why}")
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
        resp = await route.session.get(self.base + "/", headers=self._home_headers())
        status, ctype, body = resp.status_code, resp.headers.get("content-type", ""), resp.text or ""
        if _looks_like_challenge(status, ctype, body):
            raise _Blocked(f"home page answered HTTP {status} / a challenge", self._retry_after(resp))
        if status >= 400:
            raise VintedApiError(f"Vinted home page returned HTTP {status}")
        self._collect_tokens(route, body)
        if not route.bearer:
            raise VintedApiError("Vinted's home page did not hand out a session token (the access_token_web cookie): "
                                 "its session handling may have changed. Run /vinted-test.")
        route.warmed_at = self._clock()
        route.requests_this_session = 0

    def _collect_tokens(self, route: _Route, html: str) -> None:
        """The anonymous session tokens the search API wants: bearer (cookie), anon id and CSRF (cookie, else page)."""
        route.bearer = self._cookie(route.session, "access_token_web")
        page_csrf, page_anon = extract_page_tokens(html)
        route.anon_id = self._cookie(route.session, "anon_id") or page_anon
        cookie_csrf = self._cookie(route.session, "csrf_token") or self._cookie(route.session, "_csrf")
        route.csrf, route.csrf_source = (cookie_csrf, "cookie") if cookie_csrf else ((page_csrf, "page") if page_csrf else (None, "not found"))

    @staticmethod
    def _home_headers() -> dict[str, str]:
        return {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "en-GB,en;q=0.9",
            "Upgrade-Insecure-Requests": "1", "Sec-Fetch-Dest": "document", "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none", "Sec-Fetch-User": "?1",
        }

    def _api_headers(self, route: _Route, referer: str) -> dict[str, str]:
        headers = {
            "Accept": "application/json, text/plain, */*", "Accept-Language": "en-GB,en;q=0.9",
            "Origin": self.base, "Referer": referer, "Sec-Fetch-Dest": "empty", "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-site",
        }
        if route.bearer:
            headers["Authorization"] = f"Bearer {route.bearer}"
        if route.anon_id:
            headers["X-Anon-Id"] = route.anon_id
        if route.csrf:
            headers["X-Csrf-Token"] = route.csrf
        return headers

    @staticmethod
    def _retry_after(resp: Any) -> Optional[float]:
        try:
            return float(resp.headers.get("retry-after"))
        except (TypeError, ValueError):
            return None

    async def _get_json(self, url: str, params: dict[str, str], *, referer: str, api: bool = True) -> dict:
        async with self._lock:
            refreshed_auth = False
            tried_blocked = 0
            while True:
                route = self._pick_route()
                try:
                    if route.session is None or self._clock() - route.warmed_at > WARM_TTL_SECONDS:
                        await self._warm(route)
                    await self._pace(route)
                    resp = await route.session.get(url, params=params, headers=self._api_headers(route, referer))
                    route.requests_this_session += 1
                    status, ctype = resp.status_code, resp.headers.get("content-type", "")
                    body = resp.text if "json" not in ctype.lower() else ""
                    if _looks_like_challenge(status, ctype, body):
                        raise _Blocked(f"HTTP {status} / a challenge page", self._retry_after(resp))
                    if status == 401 and not refreshed_auth:
                        refreshed_auth = True            # token expired: visit the home page again and retry once
                        route.warmed_at = float("-inf")
                        continue
                    if status >= 400:
                        # 404 is a real answer (wrong path), NOT a reason to refresh cookies and retry: that hid the cause before.
                        snippet = re.sub(r"\s+", " ", _redact(resp.text or "", route.proxy))[:160]
                        raise VintedApiError(f"Vinted returned HTTP {status} for {urlparse(url).path}" + (f": {snippet}" if snippet else ""))
                    try:
                        data = resp.json()
                    except Exception as e:  # noqa: BLE001
                        raise VintedApiError(f"Vinted returned something that isn't JSON for {urlparse(url).path}") from e
                    if not isinstance(data, dict):
                        raise VintedApiError(f"Vinted returned an unexpected response for {urlparse(url).path}")
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
