# Update 2: Vinted's September 2026 API move + `/vinted-test`

CUMULATIVE: includes everything from the earlier "vinted-test-update" zip, so use this one instead of it.
Apply on top of `iphone-flip-bot-buy-page.zip`. Unzip over the repo root, then `docker compose up -d --build`.

Why: your `HTTP 404 for /api/v2/catalog/items` was not your proxy. Vinted retired that endpoint in September 2026.

| Location | Change |
|---|---|
| `app/integrations/vinted/client.py` | Search moved to `api.vinted.<tld>/svc-catalogue/items`; anonymous bearer token + X-Anon-Id + X-Csrf-Token; blank params stripped; `attribute_ids[catalog]`; new item shape; 404 no longer triggers a cookie-refresh retry; descriptions via JSON endpoint then item page; `probe()` |
| `app/integrations/vinted/interface.py` `mock.py` | optional `probe()` |
| `app/bot/cogs/deals.py` | new `/vinted-test` |
| `app/config.py` `.env.example` | `VINTED_API_HOST` (blank = derived); detail lookups per scan 15 -> 8 |
| `tests/test_vinted_client.py` | 59 tests (was 33) |
| `docs/BUY_PAGE.md` | how to read `/vinted-test`, the API change, proxy cost note |

Status: written from other open-source scrapers' reports of the change; NOT tested against live Vinted.
Run `/vinted-test` and send me its output (it never includes credentials).
