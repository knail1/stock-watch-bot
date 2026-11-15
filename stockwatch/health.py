"""Operational health (spec section 9): heartbeat, daily summary, pruning, sleep prevention.

- Heartbeat: GET `health.heartbeat_url` every `heartbeat_interval_seconds` so an
  external dead man's switch (e.g. healthchecks.io) alerts the phone if the Mac dies.
  The URL itself is a secret and is never logged.
- Daily summary at `health.daily_summary_time` (local time) via a `send_text`
  callback (Telegram), followed by pruning old `checks` rows.
- `PowerAssertion`: `caffeinate -i -w <pid>` child process so the Mac does not
  idle-sleep while we run; the `-w` flag also makes it exit if we die.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
import subprocess
import sys
from collections.abc import Awaitable, Callable
from datetime import datetime, time, timedelta
from typing import Any

import httpx

from .config import Config
from .db import Database

log = logging.getLogger(__name__)

SendText = Callable[[str], Awaitable[Any]]
Clock = Callable[[], datetime]

DEFAULT_SUMMARY_TIME = time(9, 0)
HEARTBEAT_TIMEOUT_SECONDS = 10.0


def _local_now() -> datetime:
    return datetime.now().astimezone()


def parse_hhmm(value: str | None, default: time = DEFAULT_SUMMARY_TIME) -> time:
    """Parse 'HH:MM' (24h); fall back to `default` on bad input."""
    try:
        hh, mm = str(value).strip().split(":")
        return time(int(hh), int(mm))
    except (ValueError, AttributeError):
        log.warning("invalid health.daily_summary_time %r, using %s", value, default.strftime("%H:%M"))
        return default


def next_run_at(now: datetime, at: time) -> datetime:
    """Next occurrence of local wall-clock time `at` strictly after `now`."""
    candidate = now.replace(hour=at.hour, minute=at.minute, second=0, microsecond=0)
    return candidate if candidate > now else candidate + timedelta(days=1)


#: How often retailer content with a storage limit is purged (Best Buy: 72h max).
CONTENT_PRUNE_INTERVAL_SECONDS = 3600


class HealthMonitor:
    """Background heartbeat + daily summary/prune loops. Never raises from its loops."""

    def __init__(
        self,
        config: Config,
        db: Database,
        http: httpx.AsyncClient,
        send_text: SendText | None = None,
        clock: Clock = _local_now,
    ) -> None:
        self.config = config
        self.db = db
        self.http = http
        self.send_text = send_text
        self.clock = clock
        self.last_heartbeat_at: datetime | None = None
        self.last_heartbeat_ok: bool | None = None
        self.last_summary_at: datetime | None = None
        self._tasks: list[asyncio.Task[None]] = []

    # ------------------------------------------------------------ lifecycle

    def update_config(self, config: Config) -> None:
        """Pick up reloaded config; loops read it on their next iteration."""
        self.config = config

    async def start(self) -> None:
        if self._tasks:
            return
        await self.prune()
        self._tasks = [
            asyncio.create_task(self._heartbeat_loop(), name="health-heartbeat"),
            asyncio.create_task(self._daily_loop(), name="health-daily"),
            asyncio.create_task(self._content_loop(), name="health-content-retention"),
        ]

    async def stop(self) -> None:
        tasks, self._tasks = self._tasks, []
        for t in tasks:
            t.cancel()
        for t in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await t

    # ------------------------------------------------------------ heartbeat

    async def heartbeat_once(self) -> bool:
        """Ping the heartbeat URL once. Returns success; logs failures without the URL."""
        url = self.config.health.heartbeat_url
        if not url:
            return False
        ok = False
        try:
            resp = await self.http.get(url, timeout=HEARTBEAT_TIMEOUT_SECONDS)
            ok = resp.is_success
            if not ok:
                log.warning("heartbeat ping failed: HTTP %s", resp.status_code)
        except httpx.HTTPError as exc:
            # str(exc) may embed the request URL (a secret); log only the type.
            log.warning("heartbeat ping failed: %s", type(exc).__name__)
        self.last_heartbeat_at = self.clock()
        self.last_heartbeat_ok = ok
        return ok

    async def _heartbeat_loop(self) -> None:
        if not self.config.health.heartbeat_url:
            log.info("no health.heartbeat_url configured; heartbeat disabled")
        while True:
            try:
                await self.heartbeat_once()
            except Exception:  # pragma: no cover - defensive, keep the loop alive
                log.exception("heartbeat iteration crashed")
            await asyncio.sleep(max(30, int(self.config.health.heartbeat_interval_seconds or 300)))

    # ------------------------------------------------------------ daily summary

    def build_summary(self, now: datetime | None = None) -> str:
        """Text of the last-24h summary: checks run, errors, watches active, restock events."""
        now = now or self.clock()
        since = now - timedelta(hours=24)
        stats = self.db.check_stats_since(since)
        events = self.db.events_since(since)
        restocks = sum(1 for e in events if e["kind"] == "restock")
        blocked = sum(1 for e in events if e["kind"] == "blocked")
        watches = self.config.watches
        active = sum(1 for w in watches if w.enabled)
        lines = [
            f"StockWatch daily summary ({now:%Y-%m-%d %H:%M})",
            f"Checks run (24h): {stats.get('checks', 0)}",
            f"Errors (24h): {stats.get('errors', 0)}",
            f"Watches active: {active} of {len(watches)}",
            f"Restock events (24h): {restocks}",
        ]
        if blocked:
            lines.append(f"Blocked events (24h): {blocked}")
        if self.last_heartbeat_ok is False:
            lines.append("Warning: last heartbeat ping failed")
        return "\n".join(lines)

    async def send_daily_summary(self) -> bool:
        if self.send_text is None:
            log.info("daily summary skipped: no send_text channel configured")
            return False
        text = self.build_summary()
        try:
            await self.send_text(text)
        except Exception as exc:
            log.warning("daily summary send failed: %s", type(exc).__name__)
            return False
        self.last_summary_at = self.clock()
        return True

    async def prune(self) -> int:
        """Delete check rows past retention. Runs in a thread (may touch many rows)."""
        try:
            removed = await asyncio.to_thread(self.db.prune_checks)
        except Exception:
            log.exception("pruning checks failed")
            return 0
        if removed:
            log.info("pruned %d old check rows", removed)
        return removed

    async def prune_content(self) -> dict[str, int]:
        """Enforce retailer storage limits (Best Buy: 72h). Runs hourly and at startup."""
        try:
            res = await asyncio.to_thread(self.db.prune_restricted_content)
        except Exception:
            log.exception("pruning restricted retailer content failed")
            return {"checks": 0, "events": 0}
        if any(res.values()):
            log.info("retailer content retention: deleted %d checks, scrubbed %d events",
                     res["checks"], res["events"])
        return res

    async def _content_loop(self) -> None:
        while True:
            await self.prune_content()
            await asyncio.sleep(CONTENT_PRUNE_INTERVAL_SECONDS)

    def seconds_until_summary(self, now: datetime | None = None) -> float:
        now = now or self.clock()
        at = parse_hhmm(self.config.health.daily_summary_time)
        return max(1.0, (next_run_at(now, at) - now).total_seconds())

    async def _daily_loop(self) -> None:
        while True:
            await asyncio.sleep(self.seconds_until_summary())
            try:
                await self.send_daily_summary()
                await self.prune()
            except Exception:  # pragma: no cover - defensive
                log.exception("daily maintenance crashed")
            # Step past the scheduled minute so an early wake-up cannot double-send.
            await asyncio.sleep(61)


class PowerAssertion:
    """Keeps the Mac from idle-sleeping via `caffeinate -i -w <our pid>`. No-op off macOS."""

    def __init__(
        self,
        enabled: bool = True,
        platform: str = sys.platform,
        popen: Callable[..., subprocess.Popen[bytes]] = subprocess.Popen,
        which: Callable[[str], str | None] = shutil.which,
    ) -> None:
        self.enabled = enabled
        self.platform = platform
        self._popen = popen
        self._which = which
        self.proc: subprocess.Popen[bytes] | None = None

    @property
    def active(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def command(self) -> list[str]:
        return ["caffeinate", "-i", "-w", str(os.getpid())]

    def start(self) -> bool:
        """Spawn caffeinate if applicable. Returns True when an assertion is held."""
        if self.active:
            return True
        if not self.enabled or self.platform != "darwin":
            return False
        exe = self._which("caffeinate")
        if not exe:
            log.warning("caffeinate not found; Mac may sleep and stop checking")
            return False
        cmd = self.command()
        cmd[0] = exe
        try:
            self.proc = self._popen(cmd, stdin=subprocess.DEVNULL,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError as exc:
            log.warning("could not start caffeinate: %s", exc)
            self.proc = None
            return False
        log.info("power assertion held (caffeinate pid %s)", self.proc.pid)
        return True

    def stop(self) -> None:
        proc, self.proc = self.proc, None
        if proc is None or proc.poll() is not None:
            return
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
        log.info("power assertion released")

    def __enter__(self) -> PowerAssertion:
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()
