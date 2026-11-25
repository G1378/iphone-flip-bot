"""The Vinted scraper client, with no network: a scripted fake browser session and a fake clock.

What matters here is the behaviour that keeps the proxy alive: warm up before the API, pace requests, back off
when blocked (and generate NO traffic while backing off), never put proxy credentials in a log line."""
from __future__ import annotations

import json
import random
from decimal import Decimal

import pytest

from app.config import Settings
from app.integrations.vinted import client as vc
from app.integrations.vinted.interface import VintedApiError, VintedBlockedError, VintedNotConfiguredError

DOMAIN = "www.vinted.test"
HOME_HTML = "<html>" + "x" * 50000 + " recaptcha config </html>"       # a real page is big and may mention "captcha"


class Resp:
    def __init__(self, status=200, body=None, ctype="application/json", headers=None):
        self.status_code = status
        self._body = body
        self.text = body if isinstance(body, str) else json.dumps(body)
        self.headers = {"content-type": ctype, **(headers or {})}

    def json(self):
        if isinstance(self._body, str):
            raise ValueError("not json")
        return self._body


def item(i=1, price="150.0", **kw):
    return {"id": i, "title": f"iPhone 13 128GB #{i}", "price": {"amount": price, "currency_code": "GBP"},
            "total_item_price": {"amount": str(Decimal(price) + Decimal("8.50")), "currency_code": "GBP"},
            "url": f"https://{DOMAIN}/items/{i}", "status": "Very good", "photo": {"url": "http://img/1.jpg"},
            "user": {"id": 7, "login": "sam", "feedback_reputation": 0.98, "feedback_count": 41}, **kw}


class FakeSession:
    """Answers like Vinted: home page first, then JSON. `api` is a list of responses handed out in order
    (the last one repeats); an Exception in it is raised instead."""
    def __init__(self, api=None, home=None):
        self.api = list(api if api is not None else [Resp(body={"items": [item()]})])
        self.home = home or Resp(body=HOME_HTML, ctype="text/html")
        self.calls: list[dict] = []
        self.cookies = {"anon_id": "anon-123"}
        self.closed = False

    async def get(self, url, params=None, headers=None):
        self.calls.append({"url": url, "params": params, "headers": headers or {}})
        if url.rstrip("/") == f"https://{DOMAIN}":
            return self.home
        r = self.api.pop(0) if len(self.api) > 1 else self.api[0]
        if isinstance(r, Exception):
            raise r
        return r

    async def aclose(self):
        self.closed = True


class Clock:
    def __init__(self):
        self.t = 1000.0
        self.slept: list[float] = []

    def __call__(self):
        return self.t

    async def sleep(self, s):
        self.slept.append(s)
        self.t += s


def make(sessions: dict, proxies=("http://u:secret@10.0.0.1:8000",), **kw):
    clock = Clock()

    async def factory(route):
        return sessions[route.proxy]

    c = vc.VintedClient(domain=DOMAIN, proxies=list(proxies), session_factory=factory, sleep=clock.sleep, clock=clock,
                        rng=random.Random(1), min_interval_seconds=kw.pop("min_interval_seconds", 6), **kw)
    return c, clock


P1, P2 = "http://u:secret@10.0.0.1:8000", "http://u:secret@10.0.0.2:8000"


# ------------------------------------------------------------------ parsing
def test_parse_a_normal_item():
    i = vc.parse_item(item(5, "150.0"), DOMAIN)
    assert (i.item_id, i.price, i.condition, i.seller, i.seller_reviews) == ("5", Decimal("150.0"), "Very good", "sam", 41)
    assert i.buyer_fee == Decimal("8.5")                    # total_item_price - price
    assert i.seller_feedback_pct == Decimal("98.0") and i.image_url == "http://img/1.jpg"


def test_prices_may_arrive_as_plain_strings_or_numbers():
    assert vc.parse_item({"id": 1, "title": "x", "price": "99.5"}, DOMAIN).price == Decimal("99.5")
    assert vc.parse_item({"id": 1, "title": "x", "price": 99.5}, DOMAIN).price == Decimal("99.5")


@pytest.mark.parametrize("raw", [
    {"id": 1, "title": "x", "price": {"amount": "10", "currency_code": "EUR"}},     # not GBP
    {"id": 1, "title": "x", "price": "0"},
    {"id": 1, "title": "x"},                                                           # no price
    {"title": "x", "price": "10"},                                                     # no id
    {"id": 1, "title": "x", "price": "10", "is_closed": True},
    {"id": 1, "title": "x", "price": "10", "is_reserved": True},
])
def test_unusable_or_unbuyable_items_are_dropped(raw):
    assert vc.parse_item(raw, DOMAIN) is None


def test_relative_urls_and_missing_optional_fields_are_fine():
    i = vc.parse_item({"id": 3, "title": "x", "price": "10", "url": "/items/3-x"}, DOMAIN)
    assert i.url == f"https://{DOMAIN}/items/3-x" and i.buyer_fee is None and i.seller is None


@pytest.mark.parametrize("status,ctype,body,expected", [
    (403, "text/html", "", True), (429, "application/json", "", True), (503, "text/html", "", True),
    (200, "application/json", "", False),
    (200, "text/html", "<html>please complete the captcha</html>", True),            # small page = a challenge
    pytest.param(200, "text/html", HOME_HTML, False, id="big-real-page-mentioning-captcha"),
])
def test_challenge_detection(status, ctype, body, expected):
    assert vc._looks_like_challenge(status, ctype, body) is expected


# ------------------------------------------------------------------ behaving like one visitor
@pytest.mark.asyncio
async def test_it_visits_the_home_page_first_then_searches_with_browser_like_headers():
    s = FakeSession()
    c, _ = make({P1: s})
    items = await c.search_items("iPhone 13 128GB", max_price=Decimal("200"), limit=40)
    assert [i.item_id for i in items] == ["1"]
    assert s.calls[0]["url"] == f"https://{DOMAIN}/"                           # warm-up came first
    api = s.calls[1]
    assert api["url"] == f"https://{DOMAIN}/api/v2/catalog/items"
    assert api["params"]["search_text"] == "iPhone 13 128GB" and api["params"]["order"] == "newest_first"
    assert api["params"]["price_to"] == "200"
    assert api["headers"]["x-anon-id"] == "anon-123" and "catalog?search_text=" in api["headers"]["Referer"]
    assert api["headers"]["Accept-Language"].startswith("en-GB")


@pytest.mark.asyncio
async def test_the_session_is_reused_not_re_warmed_for_every_search():
    s = FakeSession()
    c, _ = make({P1: s})
    await c.search_items("a")
    await c.search_items("b")
    assert [x["url"] for x in s.calls].count(f"https://{DOMAIN}/") == 1


@pytest.mark.asyncio
async def test_requests_are_paced_with_jitter():
    s = FakeSession()
    c, clock = make({P1: s}, min_interval_seconds=6)
    await c.search_items("a")
    await c.search_items("b")
    await c.search_items("c")
    waits = [w for w in clock.slept if w > 0]
    assert waits and all(w >= 6 * 0.8 - 1e-9 for w in waits[-2:])               # never faster than 80% of the interval
    assert len({round(w, 3) for w in waits}) > 1                                  # and not a metronome


@pytest.mark.asyncio
async def test_the_session_is_refreshed_when_vinted_says_401():
    s = FakeSession(api=[Resp(401, {"error": "expired"}), Resp(body={"items": [item()]})])
    c, _ = make({P1: s})
    assert len(await c.search_items("a")) == 1
    assert [x["url"] for x in s.calls].count(f"https://{DOMAIN}/") == 2           # warmed again before the retry


# ------------------------------------------------------------------ being blocked
@pytest.mark.asyncio
async def test_a_blocked_route_cools_down_and_the_next_one_is_used():
    bad = FakeSession(api=[Resp(403, "<html>blocked</html>", ctype="text/html")])
    good = FakeSession()
    c, _ = make({P1: bad, P2: good}, proxies=(P1, P2))
    assert len(await c.search_items("a")) == 1
    h = c.health()
    assert h.routes == 2 and h.routes_available == 1 and h.requests_blocked == 1
    assert h.cooling[0][0] == "10.0.0.1:8000"


@pytest.mark.asyncio
async def test_when_every_route_is_blocked_it_raises_and_then_sends_nothing_while_cooling():
    s = FakeSession(api=[Resp(429, "slow down", ctype="text/plain", headers={"retry-after": "120"})])
    c, clock = make({P1: s})
    with pytest.raises(VintedBlockedError):
        await c.search_items("a")
    n = len(s.calls)
    with pytest.raises(VintedBlockedError) as e:
        await c.search_items("b")
    assert len(s.calls) == n                                                       # zero traffic during the cool-down
    assert "cooling down" in str(e.value)


@pytest.mark.asyncio
async def test_the_cool_down_grows_each_time_and_the_route_comes_back():
    s = FakeSession(api=[Resp(403, "no", ctype="text/html")])
    c, clock = make({P1: s})
    for expected_minutes in (5, 15, 45):
        with pytest.raises(VintedBlockedError):
            await c.search_items("a")
        route = c._routes[0]
        assert round((route.cooldown_until - clock.t) / 60) == expected_minutes
        clock.t = route.cooldown_until + 1                                         # time passes
    s.api = [Resp(body={"items": [item()]})]
    assert len(await c.search_items("a")) == 1                                    # healed


@pytest.mark.asyncio
async def test_a_flaky_proxy_is_not_treated_as_a_block():
    s = FakeSession(api=[ConnectionError("proxy reset")])
    c, _ = make({P1: s})
    with pytest.raises(VintedApiError):
        await c.search_items("a")
    assert c.health().requests_blocked == 0 and s.closed                            # session dropped, strike-free


@pytest.mark.asyncio
async def test_a_changed_response_shape_is_reported_not_swallowed():
    c, _ = make({P1: FakeSession(api=[Resp(body={"unexpected": True})])})
    with pytest.raises(VintedApiError, match="may have changed"):
        await c.search_items("a")


# ------------------------------------------------------------------ descriptions
@pytest.mark.asyncio
async def test_description_comes_from_the_item_page():
    s = FakeSession(api=[Resp(body={"item": {"description": "  No cracks, battery 92%  "}})])
    c, _ = make({P1: s})
    assert await c.get_description("123") == "No cracks, battery 92%"
    assert s.calls[-1]["url"].endswith("/api/v2/items/123")


@pytest.mark.asyncio
async def test_a_missing_item_page_just_means_no_description():
    c, _ = make({P1: FakeSession(api=[Resp(404, {"error": "gone"})])})
    assert await c.get_description("123") is None


@pytest.mark.asyncio
async def test_a_block_while_reading_a_description_is_not_swallowed():
    c, _ = make({P1: FakeSession(api=[Resp(403, "no", ctype="text/html")])})
    with pytest.raises(VintedBlockedError):
        await c.get_description("123")


# ------------------------------------------------------------------ safety
@pytest.mark.asyncio
async def test_a_proxy_is_required_by_default():
    with pytest.raises(VintedNotConfiguredError):
        vc.VintedClient(domain=DOMAIN, proxies=[], require_proxy=True)
    assert vc.VintedClient(domain=DOMAIN, proxies=[], require_proxy=False)._routes[0].name == "direct"


def test_credentials_never_appear_in_a_route_name():
    assert vc._route_name("http://user:hunter2@10.1.2.3:9000") == "10.1.2.3:9000"
    assert vc._route_name("user:hunter2@proxy.example:8080") == "proxy.example:8080"
    assert vc._route_name(None) == "direct"


@pytest.mark.asyncio
async def test_closing_closes_every_session():
    s1, s2 = FakeSession(), FakeSession()
    c, _ = make({P1: s1, P2: s2}, proxies=(P1, P2))
    await c.search_items("a")
    await c.aclose()
    assert s1.closed


def test_settings_decide_whether_vinted_is_on():
    assert not Settings(vinted_enabled=False).vinted_configured
    assert not Settings(vinted_enabled=True, vinted_proxy_urls="").vinted_configured            # no proxy: refuse
    assert Settings(vinted_enabled=True, vinted_proxy_urls="http://a:1, http://b:2").vinted_configured
    assert Settings(vinted_enabled=True, vinted_require_proxy=False).vinted_configured
    assert Settings(vinted_proxy_urls="http://a:1, http://b:2").vinted_proxy_list == ["http://a:1", "http://b:2"]


def test_the_factory_returns_a_disabled_mock_that_says_why():
    from app.integrations.vinted import get_vinted_client
    c = get_vinted_client()                 # the test environment has VINTED_ENABLED unset
    assert not c.configured and "VINTED_ENABLED" in c.health().reason
