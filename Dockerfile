# Builds cleanly on both linux/amd64 and linux/arm64 (Raspberry Pi 5),
# since it only relies on pure-Python wheels / manylinux wheels already
# published for psycopg2-binary and asyncpg - no custom native builds.
FROM python:3.12-slim-bookworm AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# libpq5 is needed at runtime for psycopg2-binary (used by Alembic).
# postgresql-client provides pg_dump/pg_restore/psql for scripts/backup.sh
# and scripts/restore.sh, run via `docker compose run --rm backup`.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpq5 \
        postgresql-client \
        curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY migrations ./migrations
COPY alembic.ini .
COPY seed ./seed
COPY scripts ./scripts

# Non-root user - the app never needs root inside the container.
RUN useradd --create-home --uid 1000 flipbot \
    && chown -R flipbot:flipbot /app
RUN chmod +x scripts/entrypoint.sh
USER flipbot

HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
    CMD python -m app.healthcheck || exit 1

ENTRYPOINT ["scripts/entrypoint.sh"]
CMD ["python", "-m", "app.main"]
