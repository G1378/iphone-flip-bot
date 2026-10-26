from __future__ import annotations

import datetime as dt
import logging
import statistics
from decimal import Decimal
from typing import Optional

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from app.config import settings
from app.integrations.ebay.interface import (
    EbayApiError,
    EbayClientInterface,
    EbayItem,
    EbayOrder,
    EbayOrderLineItem,
    ListingResult,
    PolicyInfo,
    PriceStats,
    PublishingSetup,
)

logger = logging.getLogger(__name__)

SANDBOX_BASE = "https://api.sandbox.ebay.com"
PRODUCTION_BASE = "https://api.ebay.com"
SANDBOX_AUTH = "https://api.sandbox.ebay.com/identity/v1/oauth2/token"
PRODUCTION_AUTH = "https://api.ebay.com/identity/v1/oauth2/token"

BROWSE_SCOPE = "https://api.ebay.com/oauth/api_scope"
INSIGHTS_SCOPE = "https://api.ebay.com/oauth/api_scope/buy.marketplace.insights"

RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class _RetryableHttpError(Exception):
    def __init__(self, response: httpx.Response):
        self.response = response


def _base_url() -> str:
    return PRODUCTION_BASE if settings.ebay_env.upper() == "PRODUCTION" else SANDBOX_BASE


def _auth_url() -> str:
    return PRODUCTION_AUTH if settings.ebay_env.upper() == "PRODUCTION" else SANDBOX_AUTH


class EbayClient(EbayClientInterface):
    """Real eBay Sell API client.

    Uses the OAuth2 refresh-token grant (the refresh token is obtained via
    the one-time `scripts/ebay_oauth_setup.py` script - see README) to
    mint short-lived access tokens for the Sell Inventory API (listings)
    and Sell Fulfillment API (orders). The refresh token itself never
    expires (eBay: ~18 months) and lives only in the environment, never in
    Discord or logs.
    """

    def __init__(self) -> None:
        self.configured = settings.ebay_configured
        self.buy_apis_configured = settings.ebay_buy_apis_configured
        self._access_token: Optional[str] = None
        self._token_expires_at: Optional[dt.datetime] = None
        # app-level (client-credentials) tokens, cached per scope set
        self._app_tokens: dict[str, tuple[str, dt.datetime]] = {}
        # Set once eBay says this app isn't granted Marketplace Insights, so
        # we don't burn a failing token request on every lookup.
        self._insights_unavailable_until: Optional[dt.datetime] = None
        self._http = httpx.AsyncClient(base_url=_base_url(), timeout=20.0)

    async def aclose(self) -> None:
        await self._http.aclose()

    # ------------------------------------------------------------------
    # OAuth
    # ------------------------------------------------------------------
    async def _get_access_token(self) -> str:
        if self._access_token and self._token_expires_at and dt.datetime.now(dt.timezone.utc) < self._token_expires_at:
            return self._access_token

        auth = (settings.ebay_client_id, settings.ebay_client_secret)
        data = {
            "grant_type": "refresh_token",
            "refresh_token": settings.ebay_refresh_token,
            "scope": "https://api.ebay.com/oauth/api_scope/sell.inventory "
                     "https://api.ebay.com/oauth/api_scope/sell.fulfillment",
        }
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(_auth_url(), data=data, auth=auth,
                                      headers={"Content-Type": "application/x-www-form-urlencoded"})
        if resp.status_code != 200:
            logger.error(
                "eBay app token request failed: status=%s response=%s",
                resp.status_code,
                resp.text[:1000],
            )
            raise EbayApiError(
                f"eBay app token request failed (status {resp.status_code})"
            )

        payload = resp.json()
        self._access_token = payload["access_token"]
        expires_in = int(payload.get("expires_in", 7200))
        self._token_expires_at = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=expires_in - 60)
        return self._access_token

    async def _get_app_access_token(self, scope: str = BROWSE_SCOPE) -> str:
        """Client-credentials grant for the Buy APIs. No user consent or
        refresh token needed - just the app's own client_id/client_secret.

        Scopes are requested separately: the basic api_scope (enough for the
        Browse API) is available to every app, whereas
        buy.marketplace.insights is restricted. Asking for a scope the app
        isn't approved for makes eBay reject the *whole* request with 400
        invalid_scope, so the two must never be combined in one call."""
        cached = self._app_tokens.get(scope)
        if cached and dt.datetime.now(dt.timezone.utc) < cached[1]:
            return cached[0]

        auth = (settings.ebay_client_id.strip(), settings.ebay_client_secret.strip())
        data = {"grant_type": "client_credentials", "scope": scope}
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(_auth_url(), data=data, auth=auth,
                                      headers={"Content-Type": "application/x-www-form-urlencoded"})
        if resp.status_code != 200:
            log = logger.warning if scope == INSIGHTS_SCOPE else logger.error
            log(
                "eBay app token request failed: status=%s scope=%s response=%s",
                resp.status_code, scope, resp.text[:500],
            )
            raise EbayApiError(f"eBay app token request failed (status {resp.status_code})")

        payload = resp.json()
        expires_in = int(payload.get("expires_in", 7200))
        expires_at = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=expires_in - 60)
        self._app_tokens[scope] = (payload["access_token"], expires_at)
        return payload["access_token"]

    async def _app_headers(self, scope: str = BROWSE_SCOPE) -> dict[str, str]:
        token = await self._get_app_access_token(scope)
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "X-EBAY-C-MARKETPLACE-ID": settings.ebay_marketplace_id,
        }

    async def _headers(self) -> dict[str, str]:
        token = await self._get_access_token()
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "X-EBAY-C-MARKETPLACE-ID": settings.ebay_marketplace_id,
            "Accept-Language": "en-GB",
            "Content-Language": "en-GB",
        }

    # ------------------------------------------------------------------
    # HTTP with retry/backoff for transient failures (rate limits, 5xx)
    # ------------------------------------------------------------------
    @retry(
        reraise=True,
        stop=stop_after_attempt(4),
        wait=wait_exponential(multiplier=1, min=1, max=20),
        retry=retry_if_exception_type(_RetryableHttpError),
    )
    async def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        headers = kwargs.pop("headers", {})
        headers.update(await self._headers())
        resp = await self._http.request(method, path, headers=headers, **kwargs)
        if resp.status_code in RETRYABLE_STATUS:
            raise _RetryableHttpError(resp)
        return resp

    @retry(
        reraise=True,
        stop=stop_after_attempt(4),
        wait=wait_exponential(multiplier=1, min=1, max=20),
        retry=retry_if_exception_type(_RetryableHttpError),
    )
    async def _app_request(self, method: str, path: str, scope: str = BROWSE_SCOPE, **kwargs) -> httpx.Response:
        """Same as _request but authenticated with the app-level (client
        credentials) token, for the Buy APIs used by pricing lookups."""
        headers = kwargs.pop("headers", {})
        headers.update(await self._app_headers(scope))
        resp = await self._http.request(method, path, headers=headers, **kwargs)
        if resp.status_code in RETRYABLE_STATUS:
            raise _RetryableHttpError(resp)
        return resp

    def _ensure_configured(self) -> None:
        if not self.configured:
            from app.integrations.ebay.interface import EbayNotConfiguredError
            raise EbayNotConfiguredError(
                "eBay integration is not configured. Set EBAY_CLIENT_ID, EBAY_CLIENT_SECRET "
                "and EBAY_REFRESH_TOKEN, or run scripts/ebay_oauth_setup.py."
            )

    def _ensure_buy_apis_configured(self) -> None:
        if not self.buy_apis_configured:
            from app.integrations.ebay.interface import EbayNotConfiguredError
            raise EbayNotConfiguredError(
                "eBay pricing lookups need EBAY_CLIENT_ID and EBAY_CLIENT_SECRET to be set."
            )

    # ------------------------------------------------------------------
    # Listings (Sell Inventory API: inventory item -> offer -> publish)
    # ------------------------------------------------------------------
    async def create_listing(
        self, *, sku: str, title: str, description: str, price: Decimal, currency: str,
        quantity: int, condition_text: str, category_id: Optional[str],
        condition_description: Optional[str] = None, aspects: Optional[dict[str, list[str]]] = None,
        image_urls: Optional[list[str]] = None, fulfillment_policy_id: Optional[str] = None,
        payment_policy_id: Optional[str] = None, return_policy_id: Optional[str] = None,
        merchant_location_key: Optional[str] = None, best_offer_enabled: bool = False,
        best_offer_auto_decline: Optional[Decimal] = None, best_offer_auto_accept: Optional[Decimal] = None,
    ) -> ListingResult:
        self._ensure_configured()

        condition_enum = _map_condition(condition_text)
        html = description_to_html(description)

        product: dict = {"title": title[:80], "description": html}
        if aspects:
            product["aspects"] = aspects          # item specifics, e.g. {"Brand": ["Apple"]}
        if image_urls:
            product["imageUrls"] = image_urls[:24]
        item_payload: dict = {
            "product": product,
            "condition": condition_enum,
            "availability": {"shipToLocationAvailability": {"quantity": quantity}},
        }
        if condition_description:
            item_payload["conditionDescription"] = condition_description[:1000]
        resp = await self._request("PUT", f"/sell/inventory/v1/inventory_item/{sku}", json=item_payload)
        if resp.status_code not in (200, 201, 204):
            raise EbayApiError(f"Failed to create inventory item {sku}: HTTP {resp.status_code} {resp.text[:300]}")

        policies = {
            "fulfillmentPolicyId": fulfillment_policy_id,
            "paymentPolicyId": payment_policy_id,
            "returnPolicyId": return_policy_id,
        }
        offer_payload: dict = {
            "sku": sku,
            "marketplaceId": settings.ebay_marketplace_id,
            "format": "FIXED_PRICE",
            "availableQuantity": quantity,
            "categoryId": category_id or "9355",  # default: Cell Phones & Smartphones
            "listingDescription": html,
            "pricingSummary": {"price": {"value": str(price), "currency": currency}},
            "listingPolicies": {k: v for k, v in policies.items() if v},
        }
        if best_offer_enabled:
            terms: dict = {"bestOfferEnabled": True}
            if best_offer_auto_decline:
                terms["autoDeclinePrice"] = {"value": str(best_offer_auto_decline), "currency": currency}
            if best_offer_auto_accept:
                terms["autoAcceptPrice"] = {"value": str(best_offer_auto_accept), "currency": currency}
            offer_payload["listingPolicies"]["bestOfferTerms"] = terms
        if merchant_location_key:
            offer_payload["merchantLocationKey"] = merchant_location_key

        resp = await self._request("POST", "/sell/inventory/v1/offer", json=offer_payload)
        if resp.status_code not in (200, 201):
            raise EbayApiError(f"Failed to create offer for {sku}: HTTP {resp.status_code} {resp.text[:300]}")
        offer_id = resp.json()["offerId"]

        resp = await self._request("POST", f"/sell/inventory/v1/offer/{offer_id}/publish/")
        if resp.status_code not in (200, 201):
            raise EbayApiError(f"Failed to publish offer {offer_id}: HTTP {resp.status_code} {resp.text[:300]}")
        listing_id = resp.json()["listingId"]

        url = f"https://www.ebay.co.uk/itm/{listing_id}" if settings.ebay_marketplace_id == "EBAY_GB" \
            else f"https://www.ebay.com/itm/{listing_id}"

        return ListingResult(listing_id=listing_id, offer_id=offer_id, sku=sku, url=url)

    async def end_listing(self, offer_id: str, reason: str = "NotAvailable") -> None:
        self._ensure_configured()
        # eBay ends a live listing by withdrawing its OFFER (not the public listing id).
        resp = await self._request("POST", f"/sell/inventory/v1/offer/{offer_id}/withdraw")
        if resp.status_code not in (200, 204):
            raise EbayApiError(f"Failed to end offer {offer_id}: HTTP {resp.status_code} {resp.text[:300]}")

    async def get_listing_status(self, offer_id: str) -> Optional[str]:
        self._ensure_configured()
        resp = await self._request("GET", f"/sell/inventory/v1/offer/{offer_id}")
        if resp.status_code == 404:
            return None
        if resp.status_code != 200:
            raise EbayApiError(f"Failed to fetch offer {offer_id}: HTTP {resp.status_code}")
        return resp.json().get("status")

    # ------------------------------------------------------------------
    # Photos, business policies and stock locations
    #
    # NOTE: written from eBay's documentation but not yet exercised against the
    # live sandbox: check these first when connecting a real account.
    # ------------------------------------------------------------------
    async def upload_image(self, data: bytes, filename: str, content_type: str) -> str:
        """eBay Media API: upload a picture, get back its hosted URL."""
        self._ensure_configured()
        base = "https://apim.ebay.com" if settings.ebay_env.upper() == "PRODUCTION" else "https://apim.sandbox.ebay.com"
        headers = await self._headers()
        headers.pop("Content-Type", None)  # httpx sets the multipart boundary itself
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(f"{base}/commerce/media/v1_beta/image/create_image_from_file",
                                     headers=headers, files={"image": (filename, data, content_type)})
            if resp.status_code not in (200, 201):
                raise EbayApiError(f"Photo upload failed: HTTP {resp.status_code} {resp.text[:300]}")
            url = (resp.json() if resp.content else {}).get("imageUrl")
            if not url and resp.headers.get("Location"):   # otherwise follow the new image resource
                got = await client.get(resp.headers["Location"], headers=headers)
                if got.status_code == 200:
                    url = got.json().get("imageUrl")
        if not url:
            raise EbayApiError("eBay accepted the photo but returned no image URL.")
        return url

    async def _scoped_get(self, path: str, scope: str, **kwargs) -> httpx.Response:
        """GET with a user token for one extra scope (so an older refresh token that wasn't
        granted it fails only this call, not every eBay call)."""
        auth = (settings.ebay_client_id, settings.ebay_client_secret)
        data = {"grant_type": "refresh_token", "refresh_token": settings.ebay_refresh_token, "scope": scope}
        async with httpx.AsyncClient(timeout=15.0) as client:
            tok = await client.post(_auth_url(), data=data, auth=auth)
        if tok.status_code != 200:
            raise EbayApiError(
                "eBay didn't grant account access to read your policies. Re-run scripts/ebay_oauth_setup.py "
                f"to approve the extra permission (status {tok.status_code}).")
        headers = {"Authorization": f"Bearer {tok.json()['access_token']}", "Content-Language": "en-GB"}
        return await self._http.get(path, headers=headers, **kwargs)

    async def get_publishing_setup(self) -> PublishingSetup:
        self._ensure_configured()
        scope = "https://api.ebay.com/oauth/api_scope/sell.account.readonly"
        mp = {"marketplace_id": settings.ebay_marketplace_id}
        out = PublishingSetup()

        def parse(resp, listkey, idkey) -> list[PolicyInfo]:
            if resp.status_code != 200:
                raise EbayApiError(f"Couldn't read eBay {listkey}: HTTP {resp.status_code} {resp.text[:200]}")
            infos = []
            for p in resp.json().get(listkey, []):
                is_default = any(c.get("default") for c in p.get("categoryTypes", []))
                infos.append(PolicyInfo(id=p[idkey], name=p.get("name", p[idkey]), is_default=is_default))
            return infos

        out.fulfillment_policies = parse(await self._scoped_get("/sell/account/v1/fulfillment_policy", scope, params=mp),
                                         "fulfillmentPolicies", "fulfillmentPolicyId")
        out.payment_policies = parse(await self._scoped_get("/sell/account/v1/payment_policy", scope, params=mp),
                                     "paymentPolicies", "paymentPolicyId")
        out.return_policies = parse(await self._scoped_get("/sell/account/v1/return_policy", scope, params=mp),
                                    "returnPolicies", "returnPolicyId")
        resp = await self._request("GET", "/sell/inventory/v1/location")
        if resp.status_code == 200:
            out.locations = [PolicyInfo(id=l["merchantLocationKey"], name=l.get("name", l["merchantLocationKey"]),
                                        is_default=False) for l in resp.json().get("locations", [])]
        return out

    async def create_location(self, key: str, name: str, postcode: str, country: str = "GB") -> None:
        self._ensure_configured()
        body = {"name": name, "merchantLocationStatus": "ENABLED", "locationTypes": ["WAREHOUSE"],
                "location": {"address": {"postalCode": postcode, "country": country}}}
        resp = await self._request("POST", f"/sell/inventory/v1/location/{key}", json=body)
        if resp.status_code not in (200, 201, 204):
            raise EbayApiError(f"Failed to create location {key}: HTTP {resp.status_code} {resp.text[:300]}")

    # ------------------------------------------------------------------
    # Orders (Sell Fulfillment API)
    # ------------------------------------------------------------------
    async def get_orders_since(self, since: dt.datetime) -> list[EbayOrder]:
        """Every order created since `since` (all pages, not just the first 50).

        NOTE: field names follow eBay's Fulfillment API docs; check them against a real
        order first (the sync falls back to estimated fees if eBay's fee field is missing)."""
        self._ensure_configured()
        since_iso = since.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        orders: list[EbayOrder] = []
        offset, limit = 0, 200
        for _ in range(50):                       # hard stop: 10,000 orders per sync
            params = {"filter": f"creationdate:[{since_iso}..]", "limit": str(limit), "offset": str(offset)}
            resp = await self._request("GET", "/sell/fulfillment/v1/order", params=params)
            if resp.status_code != 200:
                raise EbayApiError(f"Failed to fetch orders: HTTP {resp.status_code} {resp.text[:200]}")
            body = resp.json()
            page = body.get("orders", [])
            orders.extend(_parse_order(raw) for raw in page)
            offset += len(page)
            if not page or offset >= int(body.get("total", offset)):
                break
        return orders

    # ------------------------------------------------------------------
    # Pricing (Buy APIs: Marketplace Insights for sold comps, Browse as
    # an always-available fallback for active-listing price estimates)
    # ------------------------------------------------------------------
    async def get_sold_price_stats(self, query: str, condition_ids: list[str], days_back: int = 90) -> Optional[PriceStats]:
        self._ensure_buy_apis_configured()
        now = dt.datetime.now(dt.timezone.utc)
        if self._insights_unavailable_until and now < self._insights_unavailable_until:
            return None
        params = {
            "q": query,
            "filter": f"conditionIds:{{{'|'.join(condition_ids)}}}",
            "limit": "100",
        }
        try:
            resp = await self._app_request(
                "GET", "/buy/marketplace_insights/v1_beta/item_sales/search",
                scope=INSIGHTS_SCOPE, params=params,
            )
        except EbayApiError:
            # Token request refused (typically invalid_scope: app not approved
            # for Marketplace Insights). Skip Insights for 24h and fall back.
            self._insights_unavailable_until = now + dt.timedelta(hours=24)
            logger.warning("Marketplace Insights unavailable for this app - using active-listing estimates for 24h.")
            return None

        if resp.status_code == 403:
            # Marketplace Insights is a limited-release API - a 403 here
            # almost always means this developer account isn't approved
            # for it yet. Treat as "unavailable", not an error - the
            # pricing service falls back to active-listing estimates.
            logger.info("Marketplace Insights returned 403 (likely not approved for this app) - falling back.")
            return None
        if resp.status_code != 200:
            logger.warning("Marketplace Insights search failed: HTTP %s", resp.status_code)
            return None

        cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days_back)
        prices: list[Decimal] = []
        for item in resp.json().get("itemSales", []):
            try:
                sold_at_raw = item.get("lastSoldDate") or item.get("soldDate")
                if sold_at_raw:
                    sold_at = dt.datetime.fromisoformat(sold_at_raw.replace("Z", "+00:00"))
                    if sold_at < cutoff:
                        continue
                price_val = item.get("lastSoldPrice", {}).get("value") or item.get("price", {}).get("value")
                if price_val:
                    prices.append(Decimal(str(price_val)))
            except (KeyError, ValueError, TypeError):
                continue

        return _stats_from_prices(prices, source="SOLD")

    async def search_items(self, query: str, *, category_id: Optional[str] = None, max_price: Optional[Decimal] = None,
                           limit: int = 50, newest_first: bool = True) -> list[EbayItem]:
        """Live listings matching `query` (Browse API, application token: no seller login needed).

        NOTE: parameter and field names follow eBay's Browse API docs; check one real search first."""
        self._ensure_buy_apis_configured()
        filters = ["deliveryCountry:GB" if settings.ebay_marketplace_id == "EBAY_GB" else "deliveryCountry:US"]
        if max_price is not None:
            cur = "GBP" if settings.ebay_marketplace_id == "EBAY_GB" else "USD"
            filters += [f"price:[..{max_price}]", f"priceCurrency:{cur}"]
        params = {"q": query, "filter": ",".join(filters), "limit": str(min(limit, 200))}
        if category_id:
            params["category_ids"] = category_id
        if newest_first:
            params["sort"] = "newlyListed"
        resp = await self._app_request("GET", "/buy/browse/v1/item_summary/search", params=params)
        if resp.status_code != 200:
            raise EbayApiError(f"Browse search failed: HTTP {resp.status_code} {resp.text[:200]}")
        return [i for i in (_parse_item(r) for r in resp.json().get("itemSummaries", [])) if i]

    async def get_active_listing_price_stats(self, query: str, condition_ids: list[str]) -> Optional[PriceStats]:
        self._ensure_buy_apis_configured()
        params = {
            "q": query,
            "filter": f"conditionIds:{{{'|'.join(condition_ids)}}}",
            "limit": "100",
        }
        resp = await self._app_request("GET", "/buy/browse/v1/item_summary/search", params=params)
        if resp.status_code != 200:
            raise EbayApiError(f"Browse API search failed: HTTP {resp.status_code}")

        prices: list[Decimal] = []
        for item in resp.json().get("itemSummaries", []):
            try:
                price_val = item.get("price", {}).get("value")
                if price_val:
                    prices.append(Decimal(str(price_val)))
            except (KeyError, ValueError, TypeError):
                continue

        return _stats_from_prices(prices, source="ACTIVE_LISTING_ESTIMATE")


def _parse_item(raw: dict) -> Optional[EbayItem]:
    """One Browse API item summary -> EbayItem (None if it has no usable price or link)."""
    price_node = raw.get("price") or (raw.get("currentBidPrice") if "AUCTION" in raw.get("buyingOptions", []) else None)
    if not price_node or not raw.get("itemWebUrl"):
        return None
    ship = None
    options = raw.get("shippingOptions") or []
    if options and options[0].get("shippingCost") is not None:
        ship = _money(options[0]["shippingCost"])
    seller = raw.get("seller") or {}
    fb = seller.get("feedbackPercentage")
    ends = raw.get("itemEndDate")
    return EbayItem(
        item_id=raw["itemId"], title=raw.get("title", ""), price=_money(price_node), url=raw["itemWebUrl"],
        shipping_cost=ship, condition=raw.get("condition"), condition_id=raw.get("conditionId"),
        buying_options=list(raw.get("buyingOptions", [])), seller=seller.get("username"),
        seller_feedback_pct=Decimal(str(fb)) if fb not in (None, "") else None,
        seller_feedback_score=int(seller["feedbackScore"]) if seller.get("feedbackScore") is not None else None,
        image_url=(raw.get("image") or {}).get("imageUrl"),
        ends_at=dt.datetime.fromisoformat(ends.replace("Z", "+00:00")) if ends else None,
    )


def _stats_from_prices(prices: list[Decimal], source: str) -> Optional[PriceStats]:
    if not prices:
        return None
    return PriceStats(
        source=source,
        sample_count=len(prices),
        avg_price=(sum(prices) / len(prices)).quantize(Decimal("0.01")),
        median_price=Decimal(str(statistics.median(prices))).quantize(Decimal("0.01")),
        min_price=min(prices),
        max_price=max(prices),
    )


def _money(node) -> Decimal:
    try:
        return Decimal(str((node or {}).get("value", "0")))
    except Exception:
        return Decimal("0")


def _parse_order(raw: dict) -> EbayOrder:
    line_items = [
        EbayOrderLineItem(sku=li.get("sku", ""), quantity=int(li.get("quantity", 1)),
                          line_item_cost=_money(li.get("lineItemCost")))
        for li in raw.get("lineItems", [])
    ]
    # eBay reports the order's selling fees as totalMarketplaceFee. Older code looked inside the
    # payments instead, so keep that as a fallback. 0 means "eBay didn't tell us".
    fees = _money(raw.get("totalMarketplaceFee"))
    if fees == 0:
        for payment in raw.get("paymentSummary", {}).get("payments", []):
            for fee in payment.get("marketplaceFees", []):
                fees += _money(fee)
    pricing = raw.get("pricingSummary", {})
    return EbayOrder(
        order_id=raw["orderId"], line_items=line_items, total_amount=_money(pricing.get("total")),
        total_fees=fees, created_at=dt.datetime.fromisoformat(raw["creationDate"].replace("Z", "+00:00")),
        buyer_username=raw.get("buyer", {}).get("username"), fulfillment_status=raw.get("orderFulfillmentStatus"),
        delivery_cost=_money(pricing.get("deliveryCost")), payment_status=raw.get("orderPaymentStatus"),
        cancel_state=(raw.get("cancelStatus") or {}).get("cancelState"),
    )


def description_to_html(text: str) -> str:
    """Our descriptions are plain text; eBay shows the description as HTML, where bare line
    breaks vanish. Blank lines become paragraphs, runs of '• ' lines become bullet lists, and
    a line wholly in CAPITALS becomes a heading."""
    import html as _html
    out, bullets = [], []

    def flush() -> None:
        if bullets:
            out.append("<ul>" + "".join(f"<li>{b}</li>" for b in bullets) + "</ul>")
            bullets.clear()

    for block in (text or "").replace("\r\n", "\n").split("\n\n"):
        for line in block.split("\n"):
            line = line.strip()
            if not line:
                continue
            esc = _html.escape(line)
            if line.startswith("• "):
                bullets.append(_html.escape(line[2:].strip()))
                continue
            flush()
            if line.isupper() and len(line) < 40:
                out.append(f"<h3>{esc}</h3>")
            else:
                out.append(f"<p>{esc}</p>")
        flush()
    flush()
    return "".join(out)


def _map_condition(condition_text: str) -> str:
    """Map our free-text condition to eBay's condition enum. Falls back to
    USED_EXCELLENT which is a safe default for tested/working secondhand phones."""
    t = (condition_text or "").lower()
    if "new" in t and "open" not in t:
        return "NEW"
    if "open box" in t:
        return "NEW_OTHER"
    if "excellent" in t or "grade a" in t:
        return "USED_EXCELLENT"
    if "good" in t or "grade b" in t:
        return "USED_GOOD"
    if "fair" in t or "grade c" in t:
        return "USED_ACCEPTABLE"
    if "faulty" in t or "parts" in t or "not working" in t:
        return "FOR_PARTS_OR_NOT_WORKING"
    return "USED_EXCELLENT"
