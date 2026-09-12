"""Structured logging setup.

Never logs secrets: any logger call that includes token/secret-like data
must redact it first. We keep this simple (stdlib logging + a redaction
filter) rather than pulling in a heavy structured-logging dependency,
which matters on a Raspberry Pi.
"""
from __future__ import annotations

import logging
import re
import sys

from app.config import settings

_SECRET_PATTERNS = [
    re.compile(r"(access_token[\"']?\s*[:=]\s*[\"']?)([^\s\"',}]+)", re.IGNORECASE),
    re.compile(r"(refresh_token[\"']?\s*[:=]\s*[\"']?)([^\s\"',}]+)", re.IGNORECASE),
    re.compile(r"(client_secret[\"']?\s*[:=]\s*[\"']?)([^\s\"',}]+)", re.IGNORECASE),
    re.compile(r"(Bearer\s+)([A-Za-z0-9\-\._~\+\/]+=*)", re.IGNORECASE),
]


class RedactSecretsFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            return True
        redacted = msg
        for pattern in _SECRET_PATTERNS:
            redacted = pattern.sub(r"\1***REDACTED***", redacted)
        if redacted != msg:
            record.msg = redacted
            record.args = ()
        return True


def setup_logging() -> None:
    root = logging.getLogger()
    root.setLevel(settings.log_level.upper())

    handler = logging.StreamHandler(sys.stdout)
    fmt = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    handler.setFormatter(fmt)
    handler.addFilter(RedactSecretsFilter())
    root.handlers = [handler]

    # Quiet noisy libraries a bit
    logging.getLogger("discord.gateway").setLevel(logging.WARNING)
    logging.getLogger("discord.client").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)
