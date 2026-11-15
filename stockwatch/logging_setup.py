"""Logging: daily-rotated file in ~/StockWatch/logs plus stderr, with secret redaction.

Every handler we install carries a `RedactingFilter`, so secrets are scrubbed no
matter which logger emitted the record (spec section 12: secrets never logged).
"""
from __future__ import annotations

import logging
import re
import sys
from collections.abc import Iterable
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import TYPE_CHECKING

from . import paths

if TYPE_CHECKING:
    from .config import Config

LOG_FILE_NAME = "stockwatch.log"
BACKUP_DAYS = 14
REDACTED = "[REDACTED]"
MIN_SECRET_LEN = 6  # shorter values (e.g. a 4-digit PIN) would mangle unrelated text
_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
_OWNED_ATTR = "_stockwatch_handler"

# Token-looking strings, redacted even if they were never configured.
_TOKEN_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # Telegram bot token: 123456789:AA... (also inside api.telegram.org/bot<token>/)
    (re.compile(r"\d{6,12}:[A-Za-z0-9_-]{30,}"), REDACTED),
    # UUIDs (healthchecks.io ping URLs, API keys)
    (re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"), REDACTED),
    # Authorization headers
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}"), r"\1 " + REDACTED),
    # key=value style secrets in query strings, form bodies and reprs
    (re.compile(r"(?i)\b(\w*(?:token|key|secret|password|passwd|pin))"
                r"(['\"]?\s*[=:]\s*['\"]?)([^&\s'\",}]+)"), r"\1\2" + REDACTED),
    # Long opaque alnum strings mixing letters and digits (Pushover keys, API keys).
    # Hyphens are excluded so product URL slugs survive.
    (re.compile(r"\b(?=[A-Za-z0-9_]*\d)(?=[A-Za-z0-9_]*[A-Za-z])[A-Za-z0-9_]{24,}\b"), REDACTED),
]


def redact_text(text: str, secrets: Iterable[str] = ()) -> str:
    """Replace configured secret values and token-looking strings with a marker."""
    for s in sorted({s for s in secrets if s}, key=len, reverse=True):
        text = text.replace(s, REDACTED)
    for pattern, repl in _TOKEN_PATTERNS:
        text = pattern.sub(repl, text)
    return text


class RedactingFilter(logging.Filter):
    """Rewrites each record's message (and traceback text) with secrets removed."""

    def __init__(self, secrets: Iterable[str] = ()) -> None:
        super().__init__()
        self._secrets: set[str] = set()
        self.add_secrets(secrets)

    def add_secrets(self, secrets: Iterable[str]) -> None:
        self._secrets.update(s for s in secrets if s and len(s) >= MIN_SECRET_LEN)

    @property
    def secrets(self) -> frozenset[str]:
        return frozenset(self._secrets)

    def redact(self, text: str) -> str:
        return redact_text(text, self._secrets)

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:  # bad format args: keep the raw template rather than drop the record
            msg = str(record.msg)
        record.msg = self.redact(msg)
        record.args = None
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = self.redact(record.exc_text)
        if record.stack_info:
            record.stack_info = self.redact(record.stack_info)
        return True


def collect_secrets(config: Config) -> list[str]:
    """Secret values from a resolved Config that must never appear in logs."""
    n, h = config.notify, config.health
    values = [
        n.pushover.user_key, n.pushover.app_token, n.telegram.bot_token,
        config.dashboard.pin, h.heartbeat_url,
        *(rc.api_key for rc in config.retailers.values()),
    ]
    return [v for v in values if v]


def setup_logging(
    level: int | str = logging.INFO,
    secrets: Iterable[str] = (),
    log_dir: Path | None = None,
    console: bool = True,
) -> RedactingFilter:
    """Configure the root logger. Idempotent: re-running replaces our handlers.

    Returns the shared filter so callers can `add_secrets()` once config loads.
    """
    log_dir = log_dir or paths.logs_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    for h in list(root.handlers):
        if getattr(h, _OWNED_ATTR, False):
            root.removeHandler(h)
            h.close()

    redactor = RedactingFilter(secrets)
    formatter = logging.Formatter(_FORMAT)
    handlers: list[logging.Handler] = [
        TimedRotatingFileHandler(log_dir / LOG_FILE_NAME, when="midnight",
                                 backupCount=BACKUP_DAYS, encoding="utf-8"),
    ]
    if console:
        handlers.append(logging.StreamHandler(sys.stderr))
    for h in handlers:
        h.setFormatter(formatter)
        h.addFilter(redactor)
        setattr(h, _OWNED_ATTR, True)
        root.addHandler(h)
    root.setLevel(level)
    # httpx logs full request URLs (which can carry tokens) at INFO.
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    return redactor
