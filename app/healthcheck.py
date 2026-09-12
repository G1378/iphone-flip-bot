"""Standalone DB connectivity check, used by Docker's HEALTHCHECK.

Deliberately does not check Discord/eBay/Sheets connectivity - those are
external services allowed to be temporarily unavailable (spec section 29);
only the database is load-bearing for container health.
"""
from __future__ import annotations

import asyncio
import sys

from app.db import healthcheck


async def main() -> int:
    ok = await healthcheck()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
