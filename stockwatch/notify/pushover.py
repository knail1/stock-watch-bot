"""Pushover client: the primary, critical-alert channel (spec section 7).

Emergency priority (2) repeats every `retry_seconds` until acknowledged, up to
`expire_seconds`; with "Critical Alerts" enabled in the iOS app it sounds even
on mute / Focus.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from ..config import PushoverCfg
from ..models import PUSHOVER_PRIORITY, Alert, AlertOutcome, Severity
from ._util import redact, truncate

log = logging.getLogger(__name__)

API = "https://api.pushover.net/1"
CHANNEL = "pushover"

# Pushover API limits.
MAX_TITLE = 250
MAX_MESSAGE = 1024
MAX_URL = 512
MAX_URL_TITLE = 100
MIN_RETRY = 30
MAX_EXPIRE = 10800


class PushoverClient:
    def __init__(self, cfg: PushoverCfg, http: httpx.AsyncClient, *,
                 timeout: float = 15.0, retry_delay: float = 2.0):
        self.cfg = cfg
        self.http = http
        self.timeout = timeout
        self.retry_delay = retry_delay

    # ------------------------------------------------------------ helpers

    def _secrets(self) -> tuple[str, str]:
        return (self.cfg.app_token, self.cfg.user_key)

    def _clean(self, text: str) -> str:
        return redact(text, self._secrets())

    def priority_for(self, severity: Severity) -> int:
        """Severity -> Pushover priority; critical uses the configured priority (default 2)."""
        if severity == Severity.CRITICAL:
            return max(-2, min(2, int(self.cfg.priority)))
        return PUSHOVER_PRIORITY[severity]

    def build_payload(self, alert: Alert, priority: int | None = None) -> dict[str, str]:
        prio = self.priority_for(alert.severity) if priority is None else priority
        data: dict[str, str] = {
            "token": self.cfg.app_token,
            "user": self.cfg.user_key,
            "title": truncate(alert.title, MAX_TITLE),
            "message": truncate(alert.body or alert.title, MAX_MESSAGE),
            "priority": str(prio),
        }
        if prio == 2:
            data["retry"] = str(max(MIN_RETRY, int(self.cfg.retry_seconds)))
            data["expire"] = str(min(MAX_EXPIRE, max(MIN_RETRY, int(self.cfg.expire_seconds))))
        if self.cfg.sound:
            data["sound"] = self.cfg.sound
        if alert.url and len(alert.url) <= MAX_URL:
            data["url"] = alert.url
            if alert.url_title:
                data["url_title"] = truncate(alert.url_title, MAX_URL_TITLE)
        return data

    # ------------------------------------------------------------ API

    async def send(self, alert: Alert, *, priority: int | None = None) -> AlertOutcome:
        """Send one message. Never raises; `detail` holds the receipt for priority 2.

        Transport errors and 5xx responses get one delayed retry; 4xx (bad
        token, rate limit) do not.
        """
        if not self.cfg.app_token or not self.cfg.user_key:
            return AlertOutcome(CHANNEL, False, "pushover app_token/user_key not configured")
        data = self.build_payload(alert, priority)
        last = ""
        for attempt in range(2):
            if attempt:
                await asyncio.sleep(self.retry_delay)
            try:
                resp = await self.http.post(f"{API}/messages.json", data=data, timeout=self.timeout)
            except httpx.HTTPError as e:
                last = self._clean(f"{type(e).__name__}: {e}")
                continue
            if resp.status_code >= 500:
                last = f"HTTP {resp.status_code}"
                continue
            return self._outcome(resp)
        log.warning("pushover send failed: %s", last)
        return AlertOutcome(CHANNEL, False, last)

    def _outcome(self, resp: httpx.Response) -> AlertOutcome:
        try:
            body: dict[str, Any] = resp.json()
        except ValueError:
            body = {}
        if resp.status_code == 200 and body.get("status") == 1:
            return AlertOutcome(CHANNEL, True, str(body.get("receipt") or ""))
        errors = body.get("errors") or [resp.reason_phrase or "error"]
        detail = self._clean(f"HTTP {resp.status_code}: {'; '.join(map(str, errors))}")
        log.warning("pushover rejected message: %s", detail)
        return AlertOutcome(CHANNEL, False, detail)

    async def validate(self) -> tuple[bool, str]:
        """Check the app token / user key pair (users/validate.json)."""
        if not self.cfg.app_token or not self.cfg.user_key:
            return False, "pushover app_token/user_key not configured"
        try:
            resp = await self.http.post(
                f"{API}/users/validate.json",
                data={"token": self.cfg.app_token, "user": self.cfg.user_key},
                timeout=self.timeout,
            )
            body = resp.json()
        except (httpx.HTTPError, ValueError) as e:
            return False, self._clean(f"{type(e).__name__}: {e}")
        if body.get("status") == 1:
            devices = body.get("devices") or []
            return True, "ok" + (f" (devices: {', '.join(devices)})" if devices else "")
        return False, self._clean("; ".join(map(str, body.get("errors") or ["invalid"])))

    async def receipt_status(self, receipt: str) -> dict[str, Any]:
        """Poll an emergency-priority receipt (acknowledged, acknowledged_at, expired, ...).

        Returns {} on any failure.
        """
        try:
            resp = await self.http.get(
                f"{API}/receipts/{receipt}.json",
                params={"token": self.cfg.app_token},
                timeout=self.timeout,
            )
            body = resp.json()
        except (httpx.HTTPError, ValueError) as e:
            log.warning("pushover receipt poll failed: %s", self._clean(f"{type(e).__name__}: {e}"))
            return {}
        return body if isinstance(body, dict) and body.get("status") == 1 else {}
