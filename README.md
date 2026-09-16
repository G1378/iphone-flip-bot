# iPhone Flipping & Repair Business Manager

A production-minded MVP for running a small iPhone buy/repair/resell
business **entirely through Discord**, running on a Raspberry Pi.

There is no web UI, dashboard, or app anywhere in this project. Discord is
the only interface. Google Sheets is a read-only reporting mirror of the
database. eBay is synced through its official REST APIs.

---

## 1. Architecture

```
Discord  ──▶  discord.py bot (slash commands, buttons, modals)
                     │
                     ▼
          Application / service layer  (app/services/*.py)
                     │
                     ▼
              PostgreSQL  (source of truth, SQLAlchemy + Alembic)
                     │
        ┌────────────┴────────────┐
        ▼                         ▼
   eBay REST APIs           Google Sheets API
 (listings + orders)      (idempotent reporting export)
```

- **PostgreSQL is the only source of truth.** Every Discord command writes
  to Postgres first. Nothing is ever derived from eBay or Sheets state.
- **eBay** is a marketplace the app synchronizes *with* — listings are
  created from local data and published; orders are pulled in on a
  schedule and matched back to local phones by SKU.
- **Google Sheets** is a one-way, idempotent export for reporting. Rows
  are matched by the internal database ID stored in column A, so re-running
  a sync never creates duplicate rows.
- Background sync jobs run on **APScheduler** inside the same process —
  no Celery, no Redis, no microservices. If eBay or Sheets is unreachable,
  the job logs the failure and retries on its next tick; core Discord
  commands (buying, testing, repairing, etc.) keep working regardless.

### Project layout

```
app/
  models/          SQLAlchemy models (the schema)
  services/        Business logic (framework-agnostic, fully unit-testable)
  integrations/
    ebay/          Real REST client + interface + test mock
    sheets/        Real gspread client + pure idempotent-upsert logic
  jobs/            APScheduler wiring, eBay/Sheets sync jobs
  bot/
    cogs/          One file per command group
    client.py      Bot setup, cog loading, command sync, error handling
    auth.py        /config-driven authorization checks
    embeds.py      Shared Discord embed builders
migrations/        Alembic migrations
seed/demo_data.py  Realistic demo dataset
scripts/           backup.sh, restore.sh, ebay_oauth_setup.py, entrypoint.sh
tests/             pytest suite against a real Postgres test database
```

---

## 2. Prerequisites

- A Raspberry Pi 5 (8GB recommended) running Raspberry Pi OS (64-bit).
  A microSD card alone is fine to get started (this MVP's database is
  small for a small business); see section 12 if you later want to move
  storage onto an SSD/USB drive for extra reliability.
- Docker + Docker Compose.
- A Discord account and server (guild) you administer.
- (Optional) An eBay developer account, if you want live eBay sync.
- (Optional) A Google Cloud project with the Sheets API enabled, if you
  want Sheets export.

---

## 3. Raspberry Pi setup

```bash
# 1. Flash Raspberry Pi OS (64-bit) to an SD card with Raspberry Pi Imager,
#    enable SSH in the imager's advanced options, boot the Pi, then SSH in.
ssh pi@<your-pi-ip>

# 2. Update the system
sudo apt update && sudo apt full-upgrade -y

# 3. Install Docker
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER
newgrp docker

# 4. Verify Docker Compose plugin is available
docker compose version

# 5. Clone/copy this project onto the Pi
git clone <your-repo-url> iphone-flip-bot   # or scp the folder over
cd iphone-flip-bot
mkdir -p backups   # used by the backup script, section 12
```

No SSD is required to get started — Postgres's data lives in a normal
Docker-managed volume by default, which is fine on the microSD card for a
small business's inventory (realistically tens of MB, maybe low GB after
years of data). If you pick up an SSD or USB drive later, see section 12
for how to move onto it without losing data.

---

## 4. Discord bot setup

1. Go to https://discord.com/developers/applications -> **New Application**.
2. **Bot** tab -> **Reset Token** -> copy it -> this is `DISCORD_BOT_TOKEN`.
3. Still on the **Bot** tab: no privileged gateway intents are required —
   this bot is slash-command only and never reads message content.
4. **OAuth2 -> URL Generator**: scopes = `bot`, `applications.commands`.
   Permissions: `Send Messages`, `Embed Links`, `Use Slash Commands`,
   `Read Message History`, `Attach Files`, `Manage Channels` (only needed
   for `/setup-channels` — see section 9 — safe to skip if you'd rather
   create channels yourself), `Manage Messages` (used to pin the cheat-sheet
   in each channel `/setup-channels` creates). Open the generated URL and
   invite the bot to your server.
5. Enable Developer Mode in Discord (User Settings -> Advanced), right
   click your server -> **Copy Server ID** -> this is `DISCORD_GUILD_ID`
   (optional, but makes slash commands sync instantly instead of up to an
   hour for a global sync).
6. Right-click your own username -> **Copy User ID** -> put it in
   `DISCORD_BOOTSTRAP_ADMIN_IDS` in `.env` so you can run `/config users
   add` to formally authorize yourself and others once the bot is up.

---

## 5. Environment variables

```bash
cp .env.example .env
nano .env
```

Fill in at minimum: `DISCORD_BOT_TOKEN`, `DISCORD_BOOTSTRAP_ADMIN_IDS`,
`POSTGRES_PASSWORD`. Everything else (eBay, Sheets) can be left blank —
the app runs normally with those integrations reporting as "not
configured" until you fill them in later. See `.env.example` for a full
description of every variable.

**Never** put API keys/tokens in Discord messages, `/config` values, or
logs — they belong in `.env` only, and the app is written to never echo
them back (see `app/logging_conf.py`'s secret-redaction filter and
`app/bot/cogs/config.py`'s `/config ebay` / `/config sheets` status
commands, which show *status*, never the credential itself).

---

## 6. Google Sheets setup (optional)

1. In Google Cloud Console (https://console.cloud.google.com/), create a
   project, enable the **Google Sheets API**.
2. **IAM & Admin -> Service Accounts -> Create Service Account**. No roles
   needed at the project level.
3. Create a JSON key for that service account, download it.
4. Create a new Google Sheet (or use an existing one). Share it with the
   service account's email address (found in the JSON key, looks like
   `xxx@yyy.iam.gserviceaccount.com`) with **Editor** access.
5. Copy the spreadsheet ID from its URL
   (`https://docs.google.com/spreadsheets/d/THIS_PART/edit`).
6. Put the JSON key at the path referenced by
   `GOOGLE_SERVICE_ACCOUNT_HOST_PATH` in `.env` (default
   `./secrets/google_service_account.json`), and set
   `GOOGLE_SHEETS_SPREADSHEET_ID`.
7. Restart the bot. `/config sheets` will confirm it's configured. The
   background sync job (default every 10 minutes) will start creating and
   populating the PHONES, DONORS, PARTS, REPAIRS, PURCHASES, ORDERS,
   LISTINGS, SALES, EXPENSES and DASHBOARD tabs automatically.

---

## 7. eBay setup (optional)

1. Register at https://developer.ebay.com, create a **Keyset** (Sandbox
   first, Production once you're confident).
2. Note the **App ID (Client ID)** and **Cert ID (Client Secret)** ->
   `EBAY_CLIENT_ID` / `EBAY_CLIENT_SECRET`.
3. Under **User Tokens**, set up a **RuName** (your OAuth redirect). It
   doesn't need to point to a real running server for this MVP — you'll
   copy the redirect URL manually in the next step. Put the RuName's
   redirect value in `EBAY_REDIRECT_URI`.
4. Run the **one-time** OAuth setup script from any machine with a
   browser (doesn't have to be the Pi):
   ```bash
   pip install httpx python-dotenv
   python scripts/ebay_oauth_setup.py
   ```
   Follow the printed instructions: open the URL, log into eBay, grant
   consent, paste the redirect URL you land on back into the terminal.
   The script prints an `EBAY_REFRESH_TOKEN` — put it in `.env`.
5. Restart the bot. `/config ebay` confirms it's configured. This refresh
   token is the **only** long-lived eBay credential the bot needs from
   then on — everything else (creating listings, publishing, checking
   orders) happens automatically from Discord and the background sync job.
6. If you ever revoke access or the refresh token expires (eBay: ~18
   months), just re-run the script.

---

## 8. Running it

```bash
docker compose up -d --build
docker compose logs -f bot
```

On first boot the bot:
1. Waits for Postgres to be healthy.
2. Runs `alembic upgrade head` (creates the schema).
3. Seeds default configuration (statuses, test checklist, part types,
   condition grades, expense categories, business settings) — this only
   *adds what's missing*, it never overwrites anything you've already
   configured.
4. Syncs slash commands to Discord and starts the background jobs.

Then in Discord:
```
/config users add user:@you admin:true
```
You're now fully operational. Try `/demo` (see section 14) to load a
realistic example dataset, or start for real with `/buy`.

---

## 9. Channel setup & control panel

You don't have to organize channels by hand. As an admin, run:
```
/setup-channels
```
This creates (or registers, if they already exist) a standard set of
channels — 🏠 home, 📱 inventory, 🔩 donors-parts, 🔧 repairs, 💷 finance,
🏷️ ebay-listings, ⚙️ admin, 🔔 notifications — and pins a short command
cheat-sheet in each one. (Requires the bot to have the **Manage Channels**
permission; if it doesn't, the command tells you so and you can create the
channels yourself and register each with `/config channels`.)

Then, in the `#home` channel, run:
```
/home
```
This posts an interactive **control panel** — no typing required for the
basics:
- **Jump buttons** to every other channel above.
- **⚙️ Business Settings** — a form for business name, postage/packaging
  defaults, minimum profit/ROI (admin only).
- **📍 Add Location** — a form to add a new storage location (admin only).
- **👥 Authorize User** — pick someone from a member list to grant bot
  access, no need to know their Discord ID (admin only).
- **🔄 Refresh** — updates the live stats (stock value, phones awaiting
  parts, this month's profit, eBay/Sheets status) shown at the top.
- **eBay Status** / **Sheets Status** / **📊 Monthly Report** — read-only
  info, available to anyone authorized.

The panel has no timeout and keeps working after a bot restart — pin the
message in `#home` so it's always the first thing you see.

---

## 10. Automated pricing & buy-price recommendations

The bot can track live eBay pricing for the models you care about and
recommend what to pay for a **used (working)** vs **faulty (repair)**
unit of each one.

### How pricing data is sourced

eBay only exposes actual *sold* prices through the **Marketplace
Insights API**, which is a limited-release endpoint - it needs separate
approval from eBay beyond a normal developer account, and most accounts
won't have it by default. So this feature tries sold data first and
automatically falls back to an estimate from **current active listing**
prices (always available with a standard account) when sold data isn't
available or has too few samples. Every price shown is labeled `sold` or
`est.` so you always know which one you're looking at - active-listing
estimates typically run 10-20% above actual sold prices, since they're
asking prices, not completed sales.

Both lookups only need `EBAY_CLIENT_ID` and `EBAY_CLIENT_SECRET` in
`.env` - unlike listings/orders, pricing doesn't need the Sell API
refresh token, so it works even before you've run
`scripts/ebay_oauth_setup.py`.

### Commands

```
/pricing watch model:"iPhone 13 Pro" storage:128GB     (admin - add to watchlist)
/pricing unwatch model:"iPhone 13 Pro" storage:128GB    (admin - remove from watchlist)
/pricing watchlist                                       (show what's tracked)
/pricing table                                            (show the pricing table + a Refresh button)
/pricing refresh                                          (refresh every watched model right now)
```

Leave `storage` off to track a model broadly instead of per-capacity
(fewer, noisier comps but works for models where storage doesn't move
the price much).

A **weekly background job** refreshes every watched entry automatically
(interval configurable via `PRICING_SYNC_INTERVAL_DAYS` in `.env`,
default 7 days) - `/pricing table` also has a **🔄 Refresh now** button
for an on-demand pull any time.

### How the recommended buy prices are calculated

- **Used (resale-ready):** `median sold/asking price − estimated eBay fees − postage − packaging − your minimum profit`
- **Faulty (bought for repair):** the same used-buy price, minus an
  **estimated repair cost** for that model — pulled from your own
  completed-repair history once you have some (`/pricing table` shows
  which one it's using), falling back to the `default_repair_cost_assumption`
  business setting (default £60, change it with
  `/config business key:default_repair_cost_assumption value:75.00`) until
  you do.

Fee/postage/packaging/profit assumptions are the same ones `/analyse`
uses (`/config business` and `/config thresholds`), so the numbers stay
consistent across the app.

### Feeds into `/analyse` and `/list` automatically

Once a model has live pricing data, `/analyse` and `/list` use it as the
default expected sale price automatically (storage-specific if you're
tracking that storage, otherwise the model's general default) - no need
to manually keep prices up to date. An explicit price you type into
`/analyse` or a per-model override set via `/config pricing-rules` always
wins over the automatic figure.

### A note on eBay condition IDs

The feature filters eBay searches using condition ID `3000` ("Used") for
the used bucket and `7000` ("For parts or not working") for the faulty
bucket - these are stable, well-documented IDs used broadly across
eBay's category tree. If your eBay category defines more granular
sub-grades and the numbers coming back look off once you have real data,
that's the first thing to check (see `app/services/pricing.py`, near the
top, for where these are defined).

---

## 11. Discord command reference

| Group | Commands |
|---|---|
| Home | `/home` `/setup-channels` (admin) |
| Pricing | `/pricing watch` `/pricing unwatch` (admin) `/pricing watchlist` `/pricing table` `/pricing refresh` |
| Inventory | `/buy` `/stock` `/phone` `/edit-phone` `/move` `/status` `/fault` |
| Testing | `/testing` `/test` `/test-result` |
| Donors | `/donor` `/donors` `/teardown` `/donor-parts` `/allocate-cost` |
| Parts | `/parts` `/part` `/find-part` `/reserve-part` `/install-part` `/remove-part` `/scrap-part` `/move-part` `/parts-needed` |
| Repairs | `/repair` `/repair-status` `/analyse` |
| Orders | `/order` `/orders` `/receive` |
| eBay | `/list` `/listing` `/ebay-sync` `/ebay-status` |
| Finance | `/report` `/stock-value` `/profit` `/sales` `/expenses` |
| Search | `/search` |
| Config (admin only) | `/config business` `/config users` `/config channels` `/config locations` `/config tests` `/config part-types` `/config phone-models` `/config expense-types` `/config statuses` `/config thresholds` `/config pricing-rules` `/config ebay` `/config sheets` `/config reset-defaults` |
| Demo | `/demo` (development only) |

Buy/donor/listing flows use **modals** so you never type raw JSON.
Repair planning and donor teardown use **select menus and buttons**.
Destructive/high-stakes actions (publishing a listing, completing a
repair) require an explicit **Confirm/Cancel** button press.

---

## 12. Backups

```bash
# Manual backup
docker compose run --rm backup

# Restore onto a fresh Postgres (overwrites the target database)
docker compose run --rm backup /app/scripts/restore.sh /backups/iphoneflip_20260901T120000Z.sql.gz
```

- Backups are gzip'd `pg_dump` files, timestamped, written to `BACKUP_DIR`
  (default `./backups` — fine on the microSD card; if you have a USB
  drive or SSD, point `BACKUP_DIR` at it in `.env` for extra safety, since
  a corrupted card would otherwise take the backups down with the live data).
- Retention defaults to the newest 14 backups (`RETENTION_COUNT` env var
  on the backup script).
- **Automate it** with a cron entry on the Pi host:
  ```
  0 3 * * * cd /home/pi/iphone-flip-bot && docker compose run --rm backup >> /var/log/iphoneflip-backup.log 2>&1
  ```

### Restoring onto a different Raspberry Pi

1. Set up Docker + this repo on the new Pi (sections 3-5).
2. Copy your `.env` and the backup file over.
3. `docker compose up -d db` (just the database first).
4. `docker compose run --rm backup /app/scripts/restore.sh /backups/<file>.sql.gz`
5. `docker compose up -d --build` — the bot's entrypoint runs any
   migrations newer than the backup automatically.

### Moving onto an SSD/USB drive later

A microSD-only setup is fine to start with — take regular backups (above)
and a card failure just means a short restore, not data loss. If you want
extra day-to-day reliability, the cleanest way to move *all* of Docker's
storage (not just this one volume) onto attached storage is:

```bash
# 1. Back up first (see above), then stop everything
docker compose down
sudo systemctl stop docker

# 2. Mount your drive, e.g.:
lsblk                                    # find your device, e.g. /dev/sda1
sudo mkdir -p /mnt/ssd
sudo mount /dev/sda1 /mnt/ssd
echo '/dev/sda1 /mnt/ssd ext4 defaults,noatime 0 2' | sudo tee -a /etc/fstab

# 3. Move Docker's data there and point Docker at it
sudo rsync -a /var/lib/docker/ /mnt/ssd/docker/
echo '{"data-root": "/mnt/ssd/docker"}' | sudo tee /etc/docker/daemon.json
sudo mv /var/lib/docker /var/lib/docker.old   # keep as a fallback, delete later
sudo systemctl start docker

# 4. Bring the stack back up - the named volume (and everything else
#    Docker manages) now lives on the SSD automatically.
docker compose up -d
```

---

## 13. Updating the application

```bash
git pull            # or copy over your updated files
docker compose up -d --build
```

The entrypoint script runs `alembic upgrade head` on every container
start, so schema changes apply automatically. Take a backup first if the
update includes a major version bump.

To generate a new migration after changing a model:
```bash
docker compose run --rm bot alembic revision --autogenerate -m "describe the change"
```

If you add or change a Discord command, validate it before deploying —
this catches the kind of thing that otherwise only surfaces as a
`CommandSyncFailure` (HTTP 400, error code 50035) when the bot tries to
start (e.g. a description over Discord's 100-character limit, too many
choices, a duplicate command name):
```bash
docker compose run --rm bot python scripts/validate_commands.py
```
The same check also runs as part of `pytest` (`tests/test_command_tree.py`).

---

## 14. Demo data

```
/demo
```

Admin-only, and refuses to run when `ENVIRONMENT=production` in `.env`.
Populates: 4 faulty phones bought for repair, 2 donor phones torn down for
parts (pro-rata cost allocation across recovered parts), a received
purchase order for purchased replacement parts, 4 repairs (3 completed
using a mix of donor and purchased parts, 1 left `AWAITING_PARTS` to show
an in-progress state), 3 phones listed and sold through the mock eBay
client with full profit breakdowns, and a couple of business expenses.
Useful for demoing `/report`, `/profit`, `/search`, and `/stock-value`
without waiting for real data.

---

## 15. Tests

```bash
createdb iphoneflip_test
pip install -r requirements.txt
pytest -q
```

The suite runs against a **real PostgreSQL database**, not sqlite or
mocks (see `tests/conftest.py`), specifically covering the parts of the
spec that are easy to get subtly wrong: donor cost allocation (pro-rata
reconciliation, scaling, manual-allocation rejection, non-destructive
re-allocation history), part reservation/install/removal state
transitions, repair completion gating, true-cost and profit calculation,
idempotent eBay order sync (including duplicate-order handling), and
idempotent Google Sheets sync.

---

## 16. Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| Bot doesn't respond to slash commands | Check `docker compose logs bot` for a sync error; without `DISCORD_GUILD_ID` global sync can take up to an hour after first deploy. |
| "You're not authorized to use this bot" | Your Discord ID isn't in `authorized_users` yet. Put it in `DISCORD_BOOTSTRAP_ADMIN_IDS` in `.env`, restart, then `/config users add`. |
| "eBay is not configured" | Expected until you complete section 7. Check `/config ebay` for status (never shows the secret itself). |
| "Google Sheets is not configured" | Expected until you complete section 6. Check `/config sheets`. |
| Bot container restarts in a loop | Almost always a migration/DB connectivity issue — `docker compose logs bot` will show the Alembic error; check `POSTGRES_PASSWORD` matches between `.env` and what Postgres was initialized with (Postgres only applies `POSTGRES_PASSWORD` on first init of an empty volume). |
| Numbers look wrong after correcting a mistake | By design, the app never deletes historical financial/inventory rows — use `/edit-phone`, `/status`, or `/allocate-cost` again to layer a correction; check `/part` on any part to see its full allocation history, or query `inventory_events` directly for the full audit trail. |
| Need to reset local dev data | `docker compose down -v` removes the Postgres volume entirely (irreversible — only for local dev, never on a Pi with real data). |

---

## 17. Known limitations of this MVP

In the spirit of not overbuilding and being upfront about scope, a few
things are intentionally minimal in this first pass and would be natural
next iterations:

- **Part <-> model compatibility** is optional/best-effort: an untagged
  part is treated as compatible with any model of the same part type,
  which is fine for a small operation but could be tightened with
  stricter tagging if the business scales.
- **eBay business policies** (shipping/payment/return policy IDs) are
  read from your eBay account's account-level defaults rather than being
  configurable per-listing from Discord — set them up once in your eBay
  seller account.
- **`/order`** takes a compact `PartType,Qty,UnitCost;...` line format
  rather than a fully modal-driven multi-row entry, to work around
  Discord's modal field limits — still no raw JSON, but power-user syntax.
