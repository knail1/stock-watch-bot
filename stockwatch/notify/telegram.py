"""Telegram Bot API client: backup alert channel and command transport (spec section 7).

The bot token is part of every API URL, so all error text is redacted before
it is logged or returned.
"""
from __future__ import annotations

import html
import logging
from typing import Any

import httpx

from ..config import TelegramCfg
from ..models import Alert, AlertOutcome
from ._util import redact, truncate

log = logging.getLogger(__name__)

API = "https://api.telegram.org"
CHANNEL = "telegram"
MAX_TEXT = 4096


class TelegramError(Exception):
    """A Telegram API call failed; the message never contains the bot token."""


def _link(url: str, title: str | None) -> str:
    return f'<a href="{html.escape(url, quote=True)}">{html.escape(title or url)}</a>'


def format_alert_html(alert: Alert) -> str:
    """HTML message with the title in bold, body, primary link and store-map link."""
    lines = [f"<b>{html.escape(alert.title)}</b>"]
    if alert.body:
        lines.append(html.escape(alert.body))
    links = []
    if alert.url:
        links.append(_link(alert.url, alert.url_title or "Open"))
    if alert.extra_url:
        links.append(_link(alert.extra_url, alert.extra_url_title or "Map"))
    if links:
        lines.append("")
        lines.extend(links)
    return "\n".join(lines)


class TelegramClient:
    def __init__(self, cfg: TelegramCfg, http: httpx.AsyncClient, *, timeout: float = 15.0):
        self.cfg = cfg
        self.http = http
        self.timeout = timeout

    def _clean(self, text: str) -> str:
        return redact(text, (self.cfg.bot_token,))

    async def _call(self, method: str, payload: dict[str, Any] | None = None,
                    timeout: float | None = None) -> Any:
        """POST a Bot API method and return its `result`; raises TelegramError."""
        if not self.cfg.bot_token:
            raise TelegramError("telegram bot_token not configured")
        url = f"{API}/bot{self.cfg.bot_token}/{method}"
        try:
            resp = await self.http.post(url, json=payload or {}, timeout=timeout or self.timeout)
        except httpx.HTTPError as e:
            raise TelegramError(self._clean(f"{method}: {type(e).__name__}: {e}")) from None
        try:
            body = resp.json()
        except ValueError:
            body = {}
        if resp.status_code != 200 or not body.get("ok"):
            desc = body.get("description") or resp.reason_phrase or "error"
            raise TelegramError(self._clean(f"{method}: HTTP {resp.status_code}: {desc}"))
        return body.get("result")

    async def send_text(self, text: str, *, parse_mode: str | None = None,
                        chat_id: str | None = None) -> AlertOutcome:
        """Send a message to the configured chat (plain text unless parse_mode given). Never raises."""
        target = chat_id or self.cfg.chat_id
        if not target:
            return AlertOutcome(CHANNEL, False, "telegram chat_id not configured")
        payload: dict[str, Any] = {
            "chat_id": target,
            "text": truncate(text, MAX_TEXT),
            "disable_web_page_preview": True,
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode
        try:
            result = await self._call("sendMessage", payload)
        except TelegramError as e:
            log.warning("telegram send failed: %s", e)
            return AlertOutcome(CHANNEL, False, str(e))
        msg_id = result.get("message_id") if isinstance(result, dict) else None
        return AlertOutcome(CHANNEL, True, str(msg_id or ""))

    async def send(self, alert: Alert) -> AlertOutcome:
        """Send an alert with both links (primary and store map), HTML-escaped."""
        text = format_alert_html(alert)
        if len(text) > MAX_TEXT:  # avoid cutting through an HTML tag
            return await self.send_text(f"{alert.title}\n{alert.body}\n{alert.url or ''}")
        return await self.send_text(text, parse_mode="HTML")

    async def get_updates(self, offset: int | None = None, timeout: int = 30) -> list[dict[str, Any]]:
        """Long-poll for updates. Raises TelegramError on failure."""
        payload: dict[str, Any] = {"timeout": timeout, "allowed_updates": ["message"]}
        if offset is not None:
            payload["offset"] = offset
        result = await self._call("getUpdates", payload, timeout=timeout + 10)
        return list(result or [])

    async def get_me(self) -> dict[str, Any]:
        """Bot identity; raises TelegramError (useful for `stockwatch doctor`)."""
        return dict(await self._call("getMe") or {})
