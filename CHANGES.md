# Buy page: eBay + Vinted deals with Bought / Reject

Unzip over the root of your `iphone-flip-bot` repo (paths below are relative to it), then:

    docker compose up -d --build

The entrypoint runs `alembic upgrade head` itself. Then `/setup-channels` in Discord (creates `🛒-buy-page`).
Read `docs/BUY_PAGE.md` before enabling Vinted. `buy-page.patch` is the same change as a diff (`git apply buy-page.patch`).

Tests: 297 existing + 136 new = 433 passing against real Postgres. Migration verified on a fresh DB, both directions,
and with a pre-existing deal row (becomes ARCHIVED).

## New files
| Location | What it is |
|---|---|
| `app/integrations/vinted/interface.py` | `VintedItem`, `VintedClientInterface`, errors, `VintedHealth` |
| `app/integrations/vinted/client.py` | The scraper: proxy routes, session warm-up, pacing, block cool-downs, tolerant parsing |
| `app/integrations/vinted/mock.py` | In-memory Vinted for tests / when switched off |
| `app/integrations/vinted/__init__.py` | `get_vinted_client()` factory (disabled unless enabled AND proxied) |
| `app/services/repair_estimate.py` | Reads listing text, names the parts, prices them from YOUR purchase history first |
| `app/services/deal_queue.py` | Bought / Reject / Undo / Expire state machine (row-locked, audited) |
| `app/bot/deal_cards.py` | Card embed, persistent buttons, Bought form, posting helpers |
| `migrations/versions/d4a6b8c0e235_buy_page.py` | Adds columns to `deal_alerts` (down_revision `c3f5a7b9d124`) |
| `docs/BUY_PAGE.md` | Rules, settings, Vinted setup, verify-on-day-one steps, known limits |
| `tests/test_buy_page.py` `tests/test_repair_estimate.py` `tests/test_vinted_client.py` | 136 new tests |

## Modified files
| Location | Change |
|---|---|
| `app/services/deals.py` | One shared decision pipeline for eBay + Vinted; ROI gate; per-fault repair; negation-aware text reading; `scan_vinted`; `scan_lock`. Old public functions and skip reasons kept |
| `app/services/pricing.py` | `asking_price_haircut_pct`; `PriceRecommendation` now exposes `resale_price`, `sale_costs` (new fields have defaults) |
| `app/models/pricing_models.py` | `DealAlert`: source, status, economics, buyer fee, card message ids, `phone_id` |
| `app/models/enums.py` | `DealSource`, `DealStatus`, event types `DEAL_BOUGHT/REJECTED/RESTORED` |
| `app/bot/cogs/deals.py` | `/deal-scan [source]`, `/deals [status]`, `/buy-page`, `/deal-status` (`/ebay-parts` unchanged) |
| `app/bot/cogs/home.py` | `🛒-buy-page` channel in the setup plan + home jump buttons |
| `app/bot/client.py` | Creates the Vinted client, posts / refreshes cards, closes the client |
| `app/jobs/scheduler.py` | `_vinted_tick` (own interval + jitter), `_expiry_tick`; deals post as cards (text fallback kept) |
| `app/config.py` | `VINTED_*` settings |
| `app/services/defaults.py` | New business settings (all have the same default in code, so existing DBs behave identically) |
| `requirements.txt` | `curl_cffi>=0.7,<0.8` (Chrome TLS fingerprint; optional, falls back to httpx with a warning) |
| `.env.example`, `README.md` | Documented |
