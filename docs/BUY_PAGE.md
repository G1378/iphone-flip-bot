# The buy page

Deals from **eBay** and **Vinted** that beat your profit rules land in the `🛒-buy-page` channel as cards.
Each card has **Bought**, **Reject** and **Open listing** buttons. Nothing is ever bought for you.

```
iPhone 13 128GB · working                       [thumbnail]
iPhone 13 128GB Blue Unlocked
🟢 Vinted · fixed price · make an offer

You'd pay      Sells for      Profit        Pay up to
£203.70        £300.00        £53.62        £222.01
£190 item +    recent eBay    ROI 26.3%     for the phone
£3.50 post +   sales                        (max all-in £237.32)
£10.20 fee

Seller   sam · 98.0% · 41 reviews
⚠️ Check before buying
• Vinted Buyer Protection estimated at 5% + £0.70: check at checkout
• postage assumed £3.50: Vinted shows the real one at checkout

[ ✅ Bought ]  [ ❌ Reject ]  [ 🔗 Open listing ]
```

## What has to be true for a deal to appear

1. The **title** names exactly the watched model (`iPhone 13` never matches `iPhone 13 Pro`) and storage.
2. It is not iCloud / activation-locked / blacklisted / MDM. ("No iCloud lock" in a description is understood as
   a *claim*: it passes, but the card tells you to verify it.)
3. **Everything you'd pay** (price + postage + Vinted's buyer fee) is under your maximum for that model:
   `resale − selling fees − postage out − packaging − your minimum profit − repair`.
4. The profit is at least your **minimum ROI** (`min_roi_pct`, default 15%), not only `min_profit_gbp`.
5. It is **not a fault we can't price**: water damage, won't power on, boot loop, Face ID / Touch ID, logic board,
   no service. These are gambles, not repairs, and there is deliberately no switch to allow them.
6. It hasn't been announced before.

Anything priced under 50% of its resale value is *kept but flagged* ("scam, lock or hidden damage until proven
otherwise"): a bargain and a trap look identical, so you get told rather than shielded.

### Repairs

For a faulty listing the scanner reads the title (and, on Vinted, the description) and names the parts:

| It says | Priced as |
|---|---|
| cracked / smashed screen, display lines, dead pixels | Screen |
| cracked back (glass) | Back Glass |
| battery health under 80%, "needs replacing" | Battery |
| not charging, charging port | Charging Port / Flex |
| rear / front camera, speaker, mic, power/volume button | Rear Camera / Front Camera / Speaker / Housing |
| just "cracked" | Screen |
| "faulty", "spares or repair", "untested" with nothing named | your flat `default_repair_cost_assumption` |

Each part is priced from, in order: **what you actually paid** for that part for exactly that model (last 10 buys),
then `repair_part_cost_defaults`, then built-in placeholders (screen £55, battery £20, back glass £30, charging
£20, rear camera £35, front camera £20, speaker £15, housing £35). Then `repair_contingency_pct` (default 10%) and
`repair_labour_gbp` (default £0) are added. The card shows the workings.

A working-condition listing that *admits* a fault ("great phone, battery health 74%") is priced as a repair too.
The text matching knows "no cracks", "never been repaired" and "screen protector cracked" are not faults.

## Setup

1. **Rebuild and restart**: `docker compose up -d --build`. The container entrypoint runs `alembic upgrade head`
   on every start, so the database migration (new columns on `deal_alerts`; alerts announced before this feature
   are marked `ARCHIVED`) happens by itself. The rebuild is needed for the new `curl_cffi` dependency.
2. **`/setup-channels`** creates `🛒-buy-page` (existing channels are left alone). Until it exists, cards go to the
   notifications channel, or to the channel where you ran `/deal-scan`.
3. Watch some models and price them: `/pricing watch`, `/pricing refresh`. A model with no price data is never scanned.
4. **Recommended:** `/config business key:asking_price_haircut_pct value:10`. Resale values from eBay *asking*
   prices run 10–20% above what phones sell for. It defaults to 0 so nothing changes until you choose.
5. eBay scanning needs no new setup (it already used your eBay Buy API keys).
   `DEAL_SCAN_INTERVAL_MINUTES` (default 30) controls how often.

### Settings (`/config business`)

| Key | Default | |
|---|---|---|
| `min_roi_pct` | 15 | deals below this return are skipped (existing setting, now enforced) |
| `min_profit_gbp` | 20 | existing |
| `asking_price_haircut_pct` | 0 | shave this % off resale values based on asking prices |
| `deal_suspicious_pct` | 50 | flag working phones priced below this % of resale |
| `deal_pending_ttl_hours` | 48 | un-actioned cards expire after this long |
| `vinted_buyer_fee_pct` / `_fixed` | 5 / 0.70 | Vinted's Buyer Protection fee, used when Vinted doesn't state it. **Check against a real checkout.** |
| `repair_part_cost_defaults` | blank | e.g. `screen=70, battery=25` |
| `repair_contingency_pct` / `repair_labour_gbp` | 10 / 0 | |

## Vinted

Vinted has **no public API**. This reads the same JSON endpoints Vinted's website uses, logged **out**, through a
proxy. It is against Vinted's terms of use, Vinted can block it or change it at any time, and **it was written
without access to the live site**: the endpoint paths and field names are the ones Vinted's web app is known to use
but have not been verified here. Treat the first run as a test (see below).

> **September 2026:** Vinted retired its old search endpoint (`/api/v2/catalog/items` now returns **404 for everyone**) and moved
> it to `api.vinted.co.uk/svc-catalogue/items`, which needs an anonymous bearer token and two extra headers. The client
> follows that change as other open-source scrapers have documented it, but I could not test it against the live site, so
> expect `/vinted-test` to need one or two rounds before it says ✅.

### Switching it on

```env
VINTED_ENABLED=true
VINTED_PROXY_URLS=http://user:pass@uk-residential-proxy.example:8000
```

* Use a **UK residential or mobile** proxy. Datacentre IPs are flagged almost immediately.
* It refuses to start without a proxy (`VINTED_REQUIRE_PROXY=true`) so your home IP is never the one that gets
  flagged. Several proxies can be comma-separated; it sticks to one and rotates when blocked.
* **Never** put your own Vinted account anywhere near this. It never logs in, so there is no account to ban.
* `curl_cffi` (in `requirements.txt`) gives requests a real Chrome TLS fingerprint. Without it, it falls back to
  plain `httpx`, which is much easier for Vinted to spot (a warning is logged).

### How it tries to look like one ordinary visitor

One consistent browser identity per proxy (same cookies/headers/fingerprint for the whole session); visits the home
page first to get Vinted's session cookies and refreshes them when they expire; a sticky proxy for the whole scan;
slow, irregular pacing (`VINTED_MIN_REQUEST_INTERVAL_SECONDS` × random 0.8–1.7); a different model order each scan;
scans every 15 minutes with ±2 minutes of jitter and no burst at start-up; description pages are fetched only for
listings that already look like deals, capped at 8 per scan (set `VINTED_MAX_DETAIL_LOOKUPS_PER_SCAN`). Reading a description can mean loading the whole
item page, which is heavy: on a per-GB residential proxy this is what costs money, so keep the cap low.

### What it will not do

It will not solve CAPTCHAs or forge anti-bot tokens. If Vinted says no (403 / 429 / a challenge page) the route
cools down for 5 min, then 15, 45, 2h15, 6h; the next proxy is tried; if none are left the scan stops and you get
**one** Discord message ("Vinted is blocking the scraper"), and one when it recovers. While cooling down it sends
**zero** requests. If it keeps happening the proxy is burned: swap it. `/deal-status` shows which routes are cooling.

### Verify it on day one

1. `VINTED_ENABLED=true`, restart, run **`/deal-status`**: Vinted should show `1/1 routes available`.
1b. Run **`/vinted-test`**. It sends exactly one home-page request and one search and reports each step. Read it first:
    * *"refused this IP at the front door"* (home page 403): Vinted rejects the proxy's IP before any search. The proxy
      type is the problem (datacentre / flagged range). Only a UK **residential or mobile** proxy can help.
    * *"home page loaded but the search call was challenged"*: Vinted's bot detection. Retrying makes it worse.
      Try a residential IP; if it is still challenged there, stop. Don't escalate to CAPTCHA solving or stealth browsers.
    * *"could not connect"*: the proxy itself failed (host/port/login). Vinted never saw a request.
    * *"HTTP 404 for /svc-catalogue/items"*: the catalogue moved again. *"HTTP 400 … INVALID_REQUEST"*: a parameter name
      isn't accepted. *"session token: NO"*: Vinted's home page didn't hand out the anonymous token. Send those lines over.
    * *"without an 'items' list"*: Vinted's API shape differs from what this client expects. Send that line over.
    * *"✅ Working"*: carry on to the next step.
2. Run **`/deal-scan source:Vinted only`**. Read the summary line: *checked N listings* means the search worked.
   `0 checked` with no error means Vinted answered but nothing matched; an error mentioning *"API may have
   changed"* means a field or path changed. The fix is local to `app/integrations/vinted/client.py`
   (`parse_item`, and the two paths in `search_items` / `get_description`).
3. Open a card's **Open listing** and compare postage and the Buyer Protection fee with the card. Correct
   `vinted_buyer_fee_pct` / `vinted_buyer_fee_fixed` if they differ.

## Commands

| | |
|---|---|
| `/deal-scan [source]` | scan now (eBay, Vinted, or both) and post cards |
| `/deals [status]` | counts, and the last 10 waiting / bought / rejected / expired |
| `/buy-page` | post any waiting deals that don't have a card yet |
| `/deal-status` | last scan results, Vinted route health, the queue |
| `/vinted-test` | one home-page request + one search to Vinted, reported step by step (shows why it is blocked) |
| `/ebay-parts` | unchanged |

**Bought** opens a form pre-filled with the all-in price: change it to what you *actually* paid (postage and fees
included: that is the phone's cost basis) and optionally add colour / IMEI / notes. It creates the phone in
inventory (`REPAIR` if the deal was faulty, otherwise `RESALE`; source "eBay"/"Vinted"; notes with the link and the
numbers at the time). Pressing it twice can only ever create one phone. **Reject** is one tap and has **Undo**.
Cards nobody acted on expire after 48 hours, and ended auctions immediately, and are greyed out.

## Known limits

* Resale value comes from your existing `/pricing` data: if that is thin or stale, so is every deal. Check
  `/pricing table` first.
* Listing text is read by pattern matching. It errs towards *more* repair or a skip, but it can be fooled; the card
  shows its workings so you make the final call. Always check IMEI / iCloud before paying.
* Vinted postage isn't in search results, so it is assumed (`default_shipping_in_cost`); the card says so.
* A listing already seen is never re-announced, even if the seller later cuts the price.
* Whether a listing is still available is not re-checked when you open a card (only the 48h / auction expiry).
