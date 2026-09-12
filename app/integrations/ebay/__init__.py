from app.config import settings
from app.integrations.ebay.interface import EbayClientInterface


def get_ebay_client() -> EbayClientInterface:
    """Factory: real client if credentials are configured, otherwise a
    disabled mock (configured=False) so callers get a clean
    EbayNotConfiguredError instead of the app crashing at import time."""
    if settings.ebay_configured:
        from app.integrations.ebay.client import EbayClient
        return EbayClient()
    from app.integrations.ebay.mock import MockEbayClient
    return MockEbayClient(configured=False)
