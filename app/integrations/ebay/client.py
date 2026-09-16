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
    EbayOrder,
    EbayOrderLineItem,
    ListingResult,
    PriceStats,
)

logger = logging.getLogger(__name__)

SANDBOX_BASE = "https://api.sandbox.ebay.com"
PRODUCTION_BASE = "https://api.ebay.com"
SANDBOX_AUTH = "https://api.sandbox.ebay.com/identity/v1/oauth2/token"
PRODUCTION_AUTH = "https://api.ebay.com/identity/v1/oauth2/token"

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
        self._app_access_token: Optional[str] = None
        self._app_token_expires_at: Optional[dt.datetime] = None
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
            # Deliberately do not log resp.text in full - it may contain
            # fragments of the request; log status only.
            logger.error("eBay OAuth token refresh failed with status %s", resp.status_code)
            raise EbayApiError(f"eBay OAuth refresh failed (status {resp.status_code})")

        payload = resp.json()
        self._access_token = payload["access_token"]
        expires_in = int(payload.get("expires_in", 7200))
        self._token_expires_at = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=expires_in - 60)
        return self._access_token

    async def _get_app_access_token(self) -> str:
        """Client-credentials grant for the Buy APIs (Browse, Marketplace
        Insights). No user consent/refresh token needed - just the app's
        own client_id/client_secret - so pricing lookups work even before
        scripts/ebay_oauth_setup.py has been run for the Sell APIs."""
        if self._app_access_token and self._app_token_expires_at and dt.datetime.now(dt.timezone.utc) < self._app_token_expires_at:
            return self._app_access_token

        auth = (settings.ebay_client_id, settings.ebay_client_secret)
        data = {
            "grant_type": "client_credentials",
            "scope": "https://api.ebay.com/oauth/api_scope/buy.marketplace.insights "
                     "https://api.ebay.com/oauth/api_scope",
        }
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(_auth_url(), data=data, auth=auth,
                                      headers={"Content-Type": "application/x-www-form-urlencoded"})
        if resp.status_code != 200:
            logger.error("eBay app token request failed with status %s", resp.status_code)
            raise EbayApiError(f"eBay app token request failed (status {resp.status_code})")

        payload = resp.json()
        self._app_access_token = payload["access_token"]
        expires_in = int(payload.get("expires_in", 7200))
        self._app_token_expires_at = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=expires_in - 60)
        return self._app_access_token

    async def _app_headers(self) -> dict[str, str]:
        token = await self._get_app_access_token()
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
    async def _app_request(self, method: str, path: str, **kwargs) -> httpx.Response:
        """Same as _request but authenticated with the app-level (client
        credentials) token, for the Buy APIs used by pricing lookups."""
        headers = kwargs.pop("headers", {})
        headers.update(await self._app_headers())
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
    ) -> ListingResult:
        self._ensure_configured()

        condition_enum = _map_condition(condition_text)

        item_payload = {
            "product": {"title": title[:80], "description": description},
            "condition": condition_enum,
            "availability": {"shipToLocationAvailability": {"quantity": quantity}},
        }
        resp = await self._request("PUT", f"/sell/inventory/v1/inventory_item/{sku}", json=item_payload)
        if resp.status_code not in (200, 201, 204):
            raise EbayApiError(f"Failed to create inventory item {sku}: HTTP {resp.status_code}")

        offer_payload = {
            "sku": sku,
            "marketplaceId": settings.ebay_marketplace_id,
            "format": "FIXED_PRICE",
            "availableQuantity": quantity,
            "categoryId": category_id or "9355",  # default: Cell Phones & Smartphones
            "listingDescription": description,
            "pricingSummary": {"price": {"value": str(price), "currency": currency}},
            "listingPolicies": {
                "fulfillmentPolicyId": None,
                "paymentPolicyId": None,
                "returnPolicyId": None,
            },
        }
        # Drop policy keys that are None - the seller's account-level default
        # policies apply if omitted (business policies are configured once
        # in the eBay seller account, out of scope for Discord config).
        offer_payload["listingPolicies"] = {
            k: v for k, v in offer_payload["listingPolicies"].items() if v
        }

        resp = await self._request("POST", "/sell/inventory/v1/offer", json=offer_payload)
        if resp.status_code not in (200, 201):
            raise EbayApiError(f"Failed to create offer for {sku}: HTTP {resp.status_code} {resp.text[:200]}")
        offer_id = resp.json()["offerId"]

        resp = await self._request("POST", f"/sell/inventory/v1/offer/{offer_id}/publish/")
        if resp.status_code not in (200, 201):
            raise EbayApiError(f"Failed to publish offer {offer_id}: HTTP {resp.status_code} {resp.text[:200]}")
        listing_id = resp.json()["listingId"]

        url = f"https://www.ebay.co.uk/itm/{listing_id}" if settings.ebay_marketplace_id == "EBAY_GB" \
            else f"https://www.ebay.com/itm/{listing_id}"

        return ListingResult(listing_id=listing_id, offer_id=offer_id, sku=sku, url=url)

    async def end_listing(self, listing_id: str, reason: str = "NotAvailable") -> None:
        self._ensure_configured()
        # eBay's withdraw endpoint operates on the *offer*, not the listing
        # id directly; in this MVP we store offer_id alongside listing_id
        # on the Listing row and callers should pass that where available.
        # Best-effort: attempt withdraw by treating listing_id as offer_id
        # if a dedicated offer id wasn't supplied by the caller.
        resp = await self._request("POST", f"/sell/inventory/v1/offer/{listing_id}/withdraw")
        if resp.status_code not in (200, 204):
            raise EbayApiError(f"Failed to end listing {listing_id}: HTTP {resp.status_code}")

    async def get_listing_status(self, listing_id: str) -> Optional[str]:
        self._ensure_configured()
        resp = await self._request("GET", f"/sell/inventory/v1/offer/{listing_id}")
        if resp.status_code == 404:
            return None
        if resp.status_code != 200:
            raise EbayApiError(f"Failed to fetch offer {listing_id}: HTTP {resp.status_code}")
        return resp.json().get("status")

    # ------------------------------------------------------------------
    # Orders (Sell Fulfillment API)
    # ------------------------------------------------------------------
    async def get_orders_since(self, since: dt.datetime) -> list[EbayOrder]:
        self._ensure_configured()
        since_iso = since.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        params = {"filter": f"creationdate:[{since_iso}..]", "limit": "50"}
        resp = await self._request("GET", "/sell/fulfillment/v1/order", params=params)
        if resp.status_code != 200:
            raise EbayApiError(f"Failed to fetch orders: HTTP {resp.status_code}")

        orders: list[EbayOrder] = []
        for raw in resp.json().get("orders", []):
            line_items = [
                EbayOrderLineItem(
                    sku=li.get("sku", ""),
                    quantity=int(li.get("quantity", 1)),
                    line_item_cost=Decimal(str(li.get("lineItemCost", {}).get("value", "0"))),
                )
                for li in raw.get("lineItems", [])
            ]
            total_fees = Decimal("0")
            for payment in raw.get("paymentSummary", {}).get("payments", []):
                for fee in payment.get("marketplaceFees", []) if "marketplaceFees" in payment else []:
                    total_fees += Decimal(str(fee.get("value", "0")))

            orders.append(
                EbayOrder(
                    order_id=raw["orderId"],
                    line_items=line_items,
                    total_amount=Decimal(str(raw.get("pricingSummary", {}).get("total", {}).get("value", "0"))),
                    total_fees=total_fees,
                    created_at=dt.datetime.fromisoformat(raw["creationDate"].replace("Z", "+00:00")),
                    buyer_username=raw.get("buyer", {}).get("username"),
                    fulfillment_status=raw.get("orderFulfillmentStatus"),
                )
            )
        return orders

    # ------------------------------------------------------------------
    # Pricing (Buy APIs: Marketplace Insights for sold comps, Browse as
    # an always-available fallback for active-listing price estimates)
    # ------------------------------------------------------------------
    async def get_sold_price_stats(self, query: str, condition_ids: list[str], days_back: int = 90) -> Optional[PriceStats]:
        self._ensure_buy_apis_configured()
        params = {
            "q": query,
            "filter": f"conditionIds:{{{'|'.join(condition_ids)}}}",
            "limit": "100",
        }
        try:
            resp = await self._app_request("GET", "/buy/marketplace_insights/v1_beta/item_sales/search", params=params)
        except EbayApiError:
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
