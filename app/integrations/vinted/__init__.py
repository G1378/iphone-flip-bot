from app.config import settings
from app.integrations.vinted.interface import VintedClientInterface


def get_vinted_client() -> VintedClientInterface:
    """Real scraper only when VINTED_ENABLED=true AND (a proxy is set OR VINTED_REQUIRE_PROXY=false).
    Otherwise a disabled mock that says why, so the rest of the app runs normally."""
    if settings.vinted_configured:
        from app.integrations.vinted.client import VintedClient
        return VintedClient.from_settings()
    from app.integrations.vinted.mock import MockVintedClient
    if not settings.vinted_enabled:
        return MockVintedClient(configured=False, reason="Vinted scanning is off (VINTED_ENABLED=false).")
    return MockVintedClient(
        configured=False,
        reason="Vinted is enabled but no proxy is set (VINTED_PROXY_URLS). Set one, or VINTED_REQUIRE_PROXY=false to scrape from this machine's own IP.",
    )
