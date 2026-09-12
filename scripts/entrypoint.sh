#!/usr/bin/env bash
set -euo pipefail

echo "[entrypoint] Waiting for Postgres..."
python - <<'PYEOF'
import asyncio
import sys
import time

from app.db import healthcheck

async def wait():
    for attempt in range(30):
        if await healthcheck():
            print("[entrypoint] Postgres is ready.")
            return True
        print(f"[entrypoint] Postgres not ready yet (attempt {attempt + 1}/30)...")
        await asyncio.sleep(2)
    return False

ok = asyncio.run(wait())
sys.exit(0 if ok else 1)
PYEOF

echo "[entrypoint] Running database migrations..."
alembic upgrade head

echo "[entrypoint] Starting application: $*"
exec "$@"
