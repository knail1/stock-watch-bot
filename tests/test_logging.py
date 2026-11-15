"""Tests for log setup and secret redaction."""
from __future__ import annotations

import logging
from pathlib import Path

import pytest

from stockwatch.config import SecretResolver, build_config
from stockwatch.logging_setup import (
    REDACTED, RedactingFilter, collect_secrets, redact_text, setup_logging,
)


@pytest.fixture
def restore_root():
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    yield
    for h in list(root.handlers):
        if h not in handlers:
            root.removeHandler(h)
            h.close()
    root.setLevel(level)


def _flush() -> None:
    for h in logging.getLogger().handlers:
        h.flush()


@pytest.mark.parametrize("text", [
    "GET https://api.telegram.org/bot123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsawq/getUpdates",
    "ping https://hc-ping.com/0f3c2a5e-1b2c-4d5e-9f00-123456789abc",
    "Authorization: Bearer abcDEF123456789xyz",
    "posting token=azGDORePK8gMaC0QOYAMyEEuzJnyUi&user=uQiRzpo4DXghDmr9QzzfQu27cmVRsG",
    "{'app_token': 'zz1', 'api_key': 'hunter2!'}",
    "key a1b2c3d4e5f6g7h8i9j0k1l2m3n4",
])
def test_token_patterns_redacted(text: str) -> None:
    out = redact_text(text)
    assert REDACTED in out
    for secret in ("AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsawq", "0f3c2a5e-1b2c", "abcDEF123456789xyz",
                   "azGDORePK8gMaC0QOYAMyEEuzJnyUi", "uQiRzpo4DXghDmr9QzzfQu27cmVRsG",
                   "hunter2!", "a1b2c3d4e5f6g7h8i9j0k1l2m3n4"):
        assert secret not in out


def test_normal_text_untouched() -> None:
    text = ("IN STOCK: Switch 2 @ Best Buy, sku 6614313, $449.99 "
            "https://www.target.com/p/nintendo-switch-2-mario-kart-world-bundle/-/A-94693225")
    assert redact_text(text) == text


def test_configured_secret_redacted_in_args_and_traceback(tmp_path: Path, restore_root) -> None:
    redactor = setup_logging(secrets=["s3cr3t-value", "1234"], log_dir=tmp_path, console=False)
    assert "1234" not in redactor.secrets  # too short to redact safely
    log = logging.getLogger("stockwatch.test")
    log.info("using %s for login", "s3cr3t-value")
    try:
        raise RuntimeError("boom with s3cr3t-value inside")
    except RuntimeError:
        log.exception("failed")
    redactor.add_secrets(["later-added-secret"])
    log.warning("late later-added-secret")
    _flush()
    content = (tmp_path / "stockwatch.log").read_text()
    assert "s3cr3t-value" not in content
    assert "later-added-secret" not in content
    assert content.count(REDACTED) >= 3
    assert "RuntimeError" in content


def test_setup_is_idempotent_and_rotates_daily(tmp_path: Path, restore_root) -> None:
    setup_logging(log_dir=tmp_path, console=True)
    setup_logging(log_dir=tmp_path, console=True)
    ours = [h for h in logging.getLogger().handlers if getattr(h, "_stockwatch_handler", False)]
    assert len(ours) == 2
    file_h = next(h for h in ours if isinstance(h, logging.handlers.TimedRotatingFileHandler))
    assert file_h.when == "MIDNIGHT" and file_h.backupCount == 14
    assert all(any(isinstance(f, RedactingFilter) for f in h.filters) for h in ours)


def test_collect_secrets() -> None:
    raw = {
        "home": {"zip": "75034"},
        "notify": {"pushover": {"user_key": "UK", "app_token": "AT"},
                   "telegram": {"bot_token": "BT", "chat_id": "1"}},
        "dashboard": {"pin": "PIN"},
        "health": {"heartbeat_url": "https://hc-ping.com/x"},
        "retailers": {"bestbuy": {"api_key": "BBK"}},
    }
    cfg = build_config(raw, SecretResolver(env_file=Path("/nonexistent"), use_keychain=False))
    assert set(collect_secrets(cfg)) == {"UK", "AT", "BT", "PIN", "https://hc-ping.com/x", "BBK"}
