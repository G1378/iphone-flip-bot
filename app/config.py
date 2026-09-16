"""
Environment-driven configuration.

Only DEVELOPER / DEPLOYMENT settings live here (credentials, connection
strings, file paths). Ordinary BUSINESS settings (currency, thresholds,
fee assumptions, etc.) live in the database and are configured entirely
through Discord (see app.services.config_service).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- Discord -----------------------------------------------------
    discord_bot_token: str = ""
    discord_guild_id: Optional[int] = None  # if set, slash commands sync instantly to this guild (dev)

    # A comma separated list of Discord user IDs that are allowed to use the
    # bot *before* any admin has been configured via /config users. This is
    # only used to bootstrap the very first admin. After that, authorized
    # users are managed from the `authorized_users` DB table.
    discord_bootstrap_admin_ids: str = ""

    # --- Database ------------------------------------------------------
    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/iphoneflip"
    # Sync URL used only by Alembic migrations.
    database_url_sync: str = "postgresql+psycopg2://postgres:postgres@localhost:5432/iphoneflip"

    # --- eBay ------------------------------------------------------------
    ebay_env: str = "SANDBOX"  # SANDBOX | PRODUCTION
    ebay_client_id: str = ""
    ebay_client_secret: str = ""
    ebay_redirect_uri: str = ""
    # Refresh token obtained via the one-time OAuth setup script
    # (scripts/ebay_oauth_setup.py). Ongoing operation only needs this.
    ebay_refresh_token: str = ""
    ebay_marketplace_id: str = "EBAY_GB"

    # --- Google Sheets ----------------------------------------------------
    google_service_account_file: str = "/run/secrets/google_service_account.json"
    google_sheets_spreadsheet_id: str = ""

    # --- App behaviour -----------------------------------------------------
    log_level: str = "INFO"
    environment: str = "development"  # development | production
    timezone: str = "Europe/London"

    # Background job intervals (minutes)
    ebay_sync_interval_minutes: int = 15
    sheets_sync_interval_minutes: int = 10
    pricing_sync_interval_days: int = 7

    @property
    def ebay_configured(self) -> bool:
        return bool(self.ebay_client_id and self.ebay_client_secret and self.ebay_refresh_token)

    @property
    def ebay_buy_apis_configured(self) -> bool:
        """eBay's Buy APIs (Browse, Marketplace Insights - used for pricing
        lookups) authenticate with an application-level client-credentials
        token, not the user-consented refresh token that the Sell APIs
        (listings, orders) need. So pricing features can work even before
        the one-time Sell OAuth setup script has been run."""
        return bool(self.ebay_client_id and self.ebay_client_secret)

    @property
    def sheets_configured(self) -> bool:
        if not self.google_sheets_spreadsheet_id:
            return False
        path = Path(self.google_service_account_file)
        # Docker silently creates an empty directory at a bind-mount target
        # when the host file doesn't exist yet, so `.exists()` alone isn't
        # enough - require an actual, parseable service-account JSON file.
        if not path.is_file():
            return False
        try:
            with path.open() as f:
                data = json.load(f)
            return data.get("type") == "service_account" and "private_key" in data
        except (json.JSONDecodeError, OSError):
            return False

    @property
    def bootstrap_admin_id_list(self) -> list[int]:
        raw = self.discord_bootstrap_admin_ids.strip()
        if not raw:
            return []
        return [int(x) for x in raw.split(",") if x.strip()]


settings = Settings()
