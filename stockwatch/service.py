"""StockWatch service: wires every component together (spec section 4).

Data flow for one check::

    Scheduler -> adapter.check() -> on_result -> StateEngine.process() -> Decisions
        restock_alert (critical) -> Notifier.send(alert)  [awaited FIRST]
                                 -> cart task: CartHelper.try_add() -> follow-up alert
        blocked_notice           -> one low-priority "paused, check manually" notice
        broken_notice            -> "<Retailer> adapter broken" warning
        failure_warning          -> warning after N consecutive failures
        price_info / recovered   -> info alerts

`StockWatchService` also implements the Telegram bot's `ServiceControl` and the
dashboard's `DashboardControl` protocols, so both front ends drive the same
running instance. Config edits go through `ConfigStore` (config.yaml stays the
source of truth) and are diff-applied to the live scheduler.

The browser is never started eagerly: `BrowserManager` launches Chrome on the
first page-based check, cart attempt, sign-in status or sign-in request.
"""
from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import logging
import os
import secrets
import signal
from datetime import datetime, timezone
from typing import Any, Mapping

import httpx

from . import paths
from .adapters.base import AdapterError
from .adapters.registry import build_adapters, identifiers_from_url
from .cart.helper import CartHelper
from .config import Config, ConfigError, ConfigStore, WatchCfg
from .db import Database
from .engine.scheduler import Scheduler, redact_detail
from .engine.state import Decision, StateEngine
from .models import (
    CHANNELS,
    RETAILERS,
    Alert,
    AlertOutcome,
    CartResult,
    ChannelAvail,
    ErrorKind,
    Home,
    Severity,
    StockResult,
    Target,
)
from .notify.notifier import Notifier, format_cart_followup, format_restock_alert, retailer_display

log = logging.getLogger(__name__)

#: Nintendo queue / invite-only states: informational, alerted once per episode.
QUEUE_STATUSES = ("QUEUE", "INVITE_ONLY")
#: Hard cap on the dry check behind `/add <url>` (name lookup).
ADD_DRY_CHECK_TIMEOUT = 60.0
SESSION_SECRET_FILE = ".dashboard_secret"
HTTP_TIMEOUT = httpx.Timeout(20.0, connect=10.0)
#: getUpdates long-poll length (TelegramClient sets its request timeout 10 s above this).
TELEGRAM_POLL_TIMEOUT = 25

# Identifier key that names a product, per retailer (used for watch ids/names).
PRIMARY_ID_KEYS = ("sku", "tcin", "item_id", "product_id")


# ------------------------------------------------------------------ pure helpers

def primary_identifier(target: Target) -> str | None:
    for k in PRIMARY_ID_KEYS:
        if target.get(k):
            return target.get(k)
    return target.get("url")


def slugify(text: str, limit: int = 40) -> str:
    import re

    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:limit].strip("-") or "watch"


def unique_id(base: str, taken: set[str]) -> str:
    if base not in taken:
        return base
    n = 2
    while f"{base}-{n}" in taken:
        n += 1
    return f"{base}-{n}"


def describe_result(result: StockResult) -> str:
    """One-line human summary of a check, e.g. 'IN_STOCK $449.99 - Pickup at Frisco (6.2 mi)'."""
    if result.error:
        detail = f": {result.error_detail}" if result.error_detail else ""
        return f"{result.error.upper()}{detail}"
    parts = [result.raw_status or ("IN_STOCK" if result.in_stock else "OUT_OF_STOCK")]
    if result.price is not None:
        parts.append(f"${result.price:,.2f}")
    avail = result.available_channels()
    if avail:
        where = []
        for c in avail[:3]:
            label = {"store_pickup": "Pickup", "in_store": "In store", "ship": "Ship"}[c.channel]
            if c.store_name:
                label += f" {c.store_name}"
            if c.distance_miles is not None:
                label += f" ({c.distance_miles:.1f} mi)"
            where.append(label)
        parts.append("- " + "; ".join(where))
    return " ".join(parts)


async def dry_check(adapter: Any, target: Target, home: Home, *, timeout: float = 90.0) -> StockResult:
    """Run one adapter check outside the scheduler; never raises (mirrors Scheduler error mapping)."""
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    try:
        result = await asyncio.wait_for(adapter.check(target, home), timeout)
    except AdapterError as e:
        result = StockResult.failure(target.retailer, e.kind, redact_detail(str(e)) or type(e).__name__)
    except (asyncio.TimeoutError, TimeoutError):
        result = StockResult.failure(target.retailer, ErrorKind.TIMEOUT, f"check exceeded {timeout:.0f}s")
    except Exception as e:  # noqa: BLE001 - a dry check reports, never crashes
        result = StockResult.failure(target.retailer, ErrorKind.UNKNOWN,
                                     redact_detail(f"{type(e).__name__}: {e}"))
    if not isinstance(result, StockResult):
        result = StockResult.failure(target.retailer, ErrorKind.UNKNOWN, "adapter returned no StockResult")
    if result.duration_ms is None:
        result.duration_ms = int((loop.time() - t0) * 1000)
    return result


def _log_task_failure(task: asyncio.Task[Any]) -> None:
    if not task.cancelled() and task.exception() is not None:
        log.warning("background task %s failed: %r", task.get_name(), task.exception())


def critical_delivered(outcomes: list[AlertOutcome]) -> bool:
    """Did a critical alert reach the user?

    Pushover is the alarm channel: when it was tried it must have succeeded.
    Otherwise any remote channel (Telegram) counts, and with no remote channel
    enabled the Mac sound does. No channels at all means nothing to retry.
    """
    if not outcomes:
        return True
    pushover = [o for o in outcomes if o.channel == "pushover"]
    if pushover:
        return any(o.ok for o in pushover)
    remote = [o for o in outcomes if o.channel != "mac_sound"]
    return any(o.ok for o in (remote or outcomes))


def _session_secret() -> bytes | None:
    """Per-install dashboard session secret (so a service restart doesn't log the phone out)."""
    path = paths.home_dir() / SESSION_SECRET_FILE
    try:
        if path.exists():
            data = path.read_bytes().strip()
            if len(data) >= 32:
                return data
        path.parent.mkdir(parents=True, exist_ok=True)
        data = secrets.token_hex(32).encode()
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        return data
    except OSError as e:
        log.warning("dashboard session secret unavailable (%s); sessions reset on restart", type(e).__name__)
        return None


# ------------------------------------------------------------------ service

class StockWatchService:
    """The running bot. See module docstring.

    Every collaborator can be injected for tests; anything not injected is
    built from the config. Optional subsystems (dashboard, Telegram bot,
    health monitor, power assertion) can be switched off, e.g. for one-shot
    CLI checks.
    """

    def __init__(
        self,
        store: ConfigStore,
        db: Database,
        *,
        http: httpx.AsyncClient | None = None,
        browser: Any | None = None,
        adapters: Mapping[str, Any] | None = None,
        notifier: Any | None = None,
        cart_helper: CartHelper | None = None,
        scheduler_kwargs: dict[str, Any] | None = None,
        enable_dashboard: bool = True,
        enable_telegram: bool = True,
        enable_health: bool = True,
        enable_power: bool = True,
        redactor: Any | None = None,
    ) -> None:
        self.store = store
        self.db = db
        self.config: Config = store.config or store.load()
        self.redactor = redactor
        self._owns_http = http is None
        self.http = http or httpx.AsyncClient(timeout=HTTP_TIMEOUT, follow_redirects=True)

        if browser is None:
            from .browser import BrowserManager  # Playwright is imported lazily inside

            browser = BrowserManager(self.config.browser)
        self.browser = browser
        self.adapters: dict[str, Any] = dict(adapters) if adapters is not None else \
            build_adapters(self.config, self.http, self.browser)
        self.engine = StateEngine(db, failure_threshold=self.config.health.failure_warn_threshold)
        self.notifier = notifier or Notifier(self.config, self.http, db)
        self.cart = cart_helper or CartHelper(self.browser, self.adapters,
                                              timeout_seconds=self.config.browser.cart_timeout_seconds)
        self.scheduler = Scheduler(self.config, self.adapters, self._on_result, **(scheduler_kwargs or {}))

        self.enable_dashboard = enable_dashboard
        self.enable_telegram = enable_telegram
        self.enable_health = enable_health
        self.enable_power = enable_power
        self.bot: Any | None = None
        self.health: Any | None = None
        self.power: Any | None = None

        self._queue_state: dict[str, str] = {}      # target key -> QUEUE / INVITE_ONLY while ongoing
        self._product_names: dict[str, str] = {}    # target key -> name seen in a check
        self._bg: set[asyncio.Task[Any]] = set()     # cart attempts and other fire-and-forget work
        self._tasks: list[asyncio.Task[Any]] = []    # long-running subsystems (bot, dashboard)
        self._stop_event: asyncio.Event | None = None
        self._started = False
        self._stopping = False
        self._signin_refresh: asyncio.Task[Any] | None = None
        #: Retry delay for a dashboard bind that is missing/failed (Tailscale not up yet, port busy).
        self.dashboard_retry_seconds = 30.0
        if hasattr(self.browser, "request_gate"):
            # Sign-in probes load retailer home pages: same per-retailer spacing as stock checks.
            self.browser.request_gate = self.scheduler.gate
        self._sync_db(self.config)

    # ------------------------------------------------------------ config

    def _sync_db(self, config: Config) -> None:
        """Mirror watches/targets into SQLite and attach target db ids."""
        for w in config.watches:
            raw = self.store.raw_watch(w.id) or {"id": w.id, "name": w.name}
            self.db.sync_watch(w.id, w.name, w.enabled, raw)
            for t in w.targets:
                t.watch_id = t.watch_id or w.id
                self.db.ensure_target(t)
        self.db.delete_watches_not_in([w.id for w in config.watches])

    def apply_config(self, config: Config) -> None:
        """Push a (re)loaded config into every running component."""
        self.config = config
        self._sync_db(config)
        for name, adapter in self.adapters.items():
            if hasattr(adapter, "settings"):
                adapter.settings = config.retailers.get(name)
        self.engine.failure_threshold = max(1, int(config.health.failure_warn_threshold))
        self.cart.timeout_seconds = float(config.browser.cart_timeout_seconds)
        if hasattr(self.notifier, "update_config"):
            self.notifier.update_config(config)
        if self.health is not None:
            self.health.update_config(config)
        if self.redactor is not None:
            from .logging_setup import collect_secrets

            self.redactor.add_secrets(collect_secrets(config))
        self.scheduler.reload(config)
        live = {t.key for w in config.watches for t in w.targets}
        for key in [k for k in self._queue_state if k not in live]:
            self._queue_state.pop(key, None)

    def reload_config(self) -> Config:
        """Re-read config.yaml from disk and apply it (raises ConfigError on invalid YAML/values)."""
        config = self.store.load()
        self.apply_config(config)
        log.info("config reloaded: %d watches", len(config.watches))
        return config

    def _watch(self, watch_id: str) -> WatchCfg:
        w = self.config.watch(watch_id)
        if w is None:
            raise KeyError(watch_id)
        return w

    # ------------------------------------------------------------ helpers

    def product_url(self, target: Target) -> str:
        adapter = self.adapters.get(target.retailer)
        try:
            if adapter is not None:
                return adapter.product_url(target)
        except Exception:  # noqa: BLE001 - fall back to the configured URL
            pass
        return target.get("url") or ""

    def _spawn(self, coro: Any, name: str) -> asyncio.Task[Any] | None:
        """Run ``coro`` in the background, tracked so `stop()` cancels and awaits it.

        Refused (returns None) once shutdown has begun.
        """
        if self._stopping:
            coro.close()
            log.info("not starting %s: service is stopping", name)
            return None
        task = asyncio.create_task(coro, name=name)
        self._bg.add(task)
        task.add_done_callback(self._bg.discard)
        task.add_done_callback(_log_task_failure)
        return task

    def spawn_background(self, coro: Any, name: str = "dashboard-task") -> asyncio.Task[Any] | None:
        """Fire-and-forget work from the dashboard (e.g. check now), owned by the service."""
        return self._spawn(coro, name)

    async def wait_background(self, timeout: float | None = None) -> None:
        """Wait for pending cart attempts / follow-ups (used by one-shot CLI checks and tests)."""
        pending = [t for t in self._bg if not t.done()]
        if pending:
            await asyncio.wait(pending, timeout=timeout)

    async def _send(self, alert: Alert) -> list[AlertOutcome]:
        try:
            return await self.notifier.send(alert)
        except Exception as e:  # noqa: BLE001 - Notifier never raises, but a fake/replacement might
            log.exception("alert send failed: %s", alert.title)
            return [AlertOutcome("notifier", False, type(e).__name__)]

    # ------------------------------------------------------------ check pipeline

    async def _on_result(self, target: Target, watch: WatchCfg, result: StockResult) -> None:
        """Scheduler callback: dedupe via the state engine, then act on each decision."""
        if result.product_name:
            self._product_names[target.key] = result.product_name
        decisions = self.engine.process(
            target, result, watch, self.config.effective_max_price(watch), self.config.effective_home(watch),
            online_severity=self.config.online_only_severity(watch, target.retailer))
        for d in decisions:
            try:
                await self._handle(target, watch, d)
            except Exception:  # noqa: BLE001 - one bad decision must not drop the others
                log.exception("handling %s for %s failed", d.kind, target.key)
        await self._handle_queue(target, watch, result)

    async def _handle(self, target: Target, watch: WatchCfg, d: Decision) -> None:
        display = retailer_display(target.retailer)
        url = self.product_url(target)
        result = d.result

        if d.kind in ("restock_alert", "price_info"):
            will_cart = d.kind == "restock_alert" and d.is_critical and watch.add_to_cart
            alert = format_restock_alert(
                dataclasses.replace(watch, add_to_cart=will_cart), target, result, url,
                channel=d.channel, severity=d.severity, event_id=d.event_id,
            )
            if d.kind == "price_info":
                max_price = self.config.effective_max_price(watch)
                alert.body += f". Over your max price of ${max_price:,.2f}" if max_price is not None else ""
            elif d.realert:
                alert.body += f" ({d.reason})"
            # Spec 8: the alert goes out first and never waits on the cart.
            outcomes = await self._send(alert)
            if d.is_critical and not critical_delivered(outcomes):
                retry = self.engine.critical_delivery_failed(target, d.event_id)
                log.warning("critical alert for %s not delivered (%s)%s", target.key,
                            ", ".join(f"{o.channel}:{'ok' if o.ok else 'FAILED'}" for o in outcomes),
                            "; will re-send on the next in-stock check" if retry else "")
            if will_cart and not self._cart_succeeded(target, d.event_id):
                self._spawn(self._cart_flow(target, watch, result, d.event_id, d.channel),
                            f"cart:{target.key}")
            return

        if d.kind == "blocked_notice":
            interval = self._interval_for(target)
            wait = f" Next try in about {interval / 60:.0f} min." if interval else ""
            await self._send(Alert(
                severity=Severity.LOW,
                title=f"{display} paused: {watch.name}",
                body=(f"{display} blocked or challenged the check (403/429/CAPTCHA). Checks are paused with "
                      f"backoff; check manually.{wait}"),
                url=url or None, url_title=f"Open at {display}", event_id=d.event_id, play_sound=False,
            ))
        elif d.kind == "broken_notice":
            detail = result.error_detail or "page or API shape changed"
            await self._send(Alert(
                severity=Severity.WARNING,
                title=f"{display} adapter broken",
                body=f"{watch.name}: {detail}. Stock is NOT being tracked at {display} until this is fixed.",
                url=url or None, url_title=f"Open at {display}", event_id=d.event_id, play_sound=False,
            ))
        elif d.kind == "failure_warning":
            await self._send(Alert(
                severity=Severity.WARNING,
                title=f"{display} checks failing: {watch.name}",
                body=f"{d.reason}. {result.error_detail or ''}".strip(),
                url=url or None, url_title=f"Open at {display}", event_id=d.event_id, play_sound=False,
            ))
        elif d.kind == "recovered":
            await self._send(Alert(
                severity=Severity.INFO,
                title=f"{display} checks recovered: {watch.name}",
                body=f"{d.reason}; checking normally again.",
                url=url or None, url_title=f"Open at {display}", event_id=d.event_id, play_sound=False,
            ))

    def _interval_for(self, target: Target) -> float | None:
        for st in self.scheduler.status():
            if st["key"] == target.key:
                return float(st["interval"])
        return None

    async def _handle_queue(self, target: Target, watch: WatchCfg, result: StockResult) -> None:
        """Nintendo queue / invite-only: one info alert per episode, never counted as a failure."""
        if result.error:
            return
        status = (result.raw_status or "").upper()
        if status not in QUEUE_STATUSES:
            self._queue_state.pop(target.key, None)
            return
        if self._queue_state.get(target.key) == status:
            return
        self._queue_state[target.key] = status
        display = retailer_display(target.retailer)
        what = "queue / waiting room" if status == "QUEUE" else "invite-only sale"
        url = self.product_url(target)
        await self._send(Alert(
            severity=Severity.INFO,
            title=f"{display} {what}: {watch.name}",
            body=result.error_detail or f"{display} is running a {what} for this product.",
            url=url or None, url_title=f"Open at {display}", play_sound=False,
        ))

    async def _cart_flow(self, target: Target, watch: WatchCfg, result: StockResult,
                         event_id: int | None, channel: ChannelAvail | None = None) -> CartResult:
        """Cart attempt (after the alert) for the matched channel/store, then the follow-up message."""
        cart = await self.cart.try_add(target, watch, result, channel=channel)
        self._record_cart(target, event_id, cart)
        await self._send(format_cart_followup(watch, target, cart, self.product_url(target),
                                              event_id=event_id))
        return cart

    def _cart_succeeded(self, target: Target, event_id: int | None) -> bool:
        """True if this restock event already put the item in the cart (a re-sent alert must not re-add)."""
        if target.db_id is None or event_id is None:
            return False
        ev = self.db.get_open_event(target.db_id, "restock")
        return bool(ev and ev["id"] == event_id and (ev["details"].get("cart") or {}).get("success"))

    def _record_cart(self, target: Target, event_id: int | None, cart: CartResult) -> None:
        if target.db_id is None or event_id is None:
            return
        try:
            ev = self.db.get_open_event(target.db_id, "restock")
            if ev and ev["id"] == event_id:
                details = ev["details"]
                details["cart"] = {"success": cart.success, "signed_out": cart.signed_out,
                                   "message": cart.message,
                                   "at": datetime.now(timezone.utc).isoformat()}
                self.db.update_event_details(event_id, details)
        except Exception:  # noqa: BLE001 - bookkeeping only
            log.exception("could not record cart result")

    # ------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        """Start every subsystem. Does not launch the browser."""
        if self._started:
            return
        self._started = True
        if self.enable_power:
            from .health import PowerAssertion

            self.power = PowerAssertion(enabled=self.config.health.prevent_sleep)
            self.power.start()
        await self.scheduler.start()
        tg = self.config.notify.telegram
        telegram_client = getattr(self.notifier, "telegram", None)
        telegram_ready = tg.enabled and bool(tg.bot_token) and bool(tg.chat_id) and telegram_client is not None
        if self.enable_health:
            from .health import HealthMonitor

            self.health = HealthMonitor(self.config, self.db, self.http,
                                        send_text=telegram_client.send_text if telegram_ready else None)
            await self.health.start()
        if self.enable_telegram and telegram_ready:
            from .telegram_bot import TelegramBot

            self.bot = TelegramBot(self, telegram_client, tg.chat_id, poll_timeout=TELEGRAM_POLL_TIMEOUT)
            self._tasks.append(asyncio.create_task(self.bot.run(), name="telegram-bot"))
        if self.enable_dashboard:
            self._tasks.append(asyncio.create_task(self._serve_dashboard(), name="dashboard"))
        log.info("StockWatch started: %d watches, %d targets", len(self.config.watches),
                 sum(len(w.targets) for w in self.config.watches))

    async def _serve_dashboard(self) -> None:
        """Serve the dashboard under two independent supervisors (spec 10, AC8).

        127.0.0.1 is bound on its own, so a Tailscale problem can never take localhost
        down. The Tailscale/remote addresses are re-resolved periodically and (re)bound
        with retry, so a login-time race with Tailscale heals without a restart.
        """
        from .dashboard import LOOPBACK, BindError, check_bind_safety, create_app, resolve_bind_hosts, supervise

        cfg = self.config.dashboard
        pin = cfg.pin or None
        app = create_app(self, pin=pin, session_secret=_session_secret())
        last_problem: list[str | None] = [None]

        async def local_hosts() -> list[str]:
            return [LOOPBACK]

        async def remote_hosts() -> list[str]:
            problem: str | None = None
            try:
                hosts = await asyncio.to_thread(resolve_bind_hosts, self.config)
                remote = [h for h in hosts if h != LOOPBACK]
                check_bind_safety(remote, bool(pin))
            except BindError as e:
                remote, problem = [], f"{e}; listening on 127.0.0.1 only"
            else:
                wants_remote = any(str(b).strip().lower() not in ("", LOOPBACK, "localhost")
                                   for b in self.config.dashboard.bind or [])
                if wants_remote and not remote:
                    problem = "Tailscale address not available yet; will keep retrying"
            if problem != last_problem[0]:
                if problem:
                    log.warning("dashboard: %s", problem)
                elif last_problem[0]:
                    log.info("dashboard: remote bind address available again")
                last_problem[0] = problem
            return remote

        await asyncio.gather(
            supervise(app, local_hosts, cfg.port, label="dashboard (localhost)",
                      retry_seconds=self.dashboard_retry_seconds),
            supervise(app, remote_hosts, cfg.port, label="dashboard (tailscale)",
                      retry_seconds=self.dashboard_retry_seconds),
        )

    def request_stop(self) -> None:
        if self._stop_event is not None:
            self._stop_event.set()

    async def run(self, *, install_signal_handlers: bool = True) -> None:
        """Start, run until SIGTERM/SIGINT (or `request_stop`), then shut down cleanly."""
        self._stop_event = asyncio.Event()
        loop = asyncio.get_running_loop()
        installed: list[signal.Signals] = []
        if install_signal_handlers:
            for sig in (signal.SIGTERM, signal.SIGINT):
                with contextlib.suppress(NotImplementedError, RuntimeError):
                    loop.add_signal_handler(sig, self.request_stop)
                    installed.append(sig)
        try:
            await self.start()
            await self._stop_event.wait()
            log.info("shutdown requested")
        finally:
            for sig in installed:
                loop.remove_signal_handler(sig)
            await self.stop()

    async def stop(self) -> None:
        """Stop everything; safe to call more than once."""
        self._stopping = True
        await self.scheduler.stop()
        if self.bot is not None:
            self.bot.stop()
        for t in list(self._tasks) + list(self._bg):
            t.cancel()
        await asyncio.gather(*self._tasks, *self._bg, return_exceptions=True)
        self._tasks.clear()
        if self.health is not None:
            await self.health.stop()
        mac = getattr(self.notifier, "mac_sound", None)
        if mac is not None and hasattr(mac, "stop"):
            with contextlib.suppress(Exception):
                await mac.stop()
        if self.browser is not None and hasattr(self.browser, "close"):
            with contextlib.suppress(Exception):
                await self.browser.close()
        if self._owns_http:
            await self.http.aclose()
        if self.power is not None:
            self.power.stop()
        self._started = False
        self._stopping = False
        log.info("StockWatch stopped")

    # ============================================================ Telegram ServiceControl

    async def status_text(self) -> str:
        rows = self.scheduler.status()
        if not rows:
            return "No watches configured. Add one with /add <product url>."
        head = "StockWatch status" + (" (ALL PAUSED)" if self.scheduler.paused_all else "")
        lines = [head]
        by_watch: dict[str, list[dict[str, Any]]] = {}
        for r in rows:
            by_watch.setdefault(r["watch_id"], []).append(r)
        now = datetime.now(timezone.utc)
        for wid, trs in by_watch.items():
            paused = all(r["paused"] for r in trs)
            lines.append(f"\n{wid} ({trs[0]['watch_name']}){' [paused]' if paused else ''}")
            for r in trs:
                res: StockResult | None = r["last_result"]
                if res is None:
                    state = "not checked yet"
                else:
                    ago = int((now - r["last_check_at"]).total_seconds()) if r["last_check_at"] else None
                    state = describe_result(res) + (f", {_ago(ago)}" if ago is not None else "")
                if r["blocked"]:
                    state += f" [blocked, backoff {r['interval'] / 60:.0f} min]"
                elif r["consecutive_failures"]:
                    state += f" [{r['consecutive_failures']} failures in a row]"
                lines.append(f"  {retailer_display(r['retailer'])}: {state}")
        return "\n".join(lines)

    async def pause(self, watch_id: str) -> None:
        await self.set_paused(watch_id, True)

    async def resume(self, watch_id: str) -> None:
        await self.set_paused(watch_id, False)

    async def pause_all(self) -> None:
        self.scheduler.pause_all()

    async def resume_all(self) -> None:
        self.scheduler.resume_all()

    async def check_now(self, watch_id: str) -> str:
        """Immediate check of every target in the watch (KeyError if unknown); returns a summary."""
        self._watch(watch_id)
        results = await self.scheduler.check_now(watch_id)
        if not results:
            return f"{watch_id}: no targets"
        return "\n".join(f"{retailer_display(r.retailer)}: {describe_result(r)}" for r in results)

    async def add_watch_from_url(self, url: str) -> str:
        """Create a watch with defaults from a product URL; returns the new watch id.

        A dry check (through the retailer's request gate) supplies the product
        name and counts as the target's first check. Raises ValueError for an
        unsupported or incomplete URL.
        """
        retailer, idents = identifiers_from_url(url.strip())
        if retailer not in RETAILERS:
            raise ValueError(f"unsupported retailer {retailer}")
        idents = {k: v for k, v in idents.items() if v}
        probe = Target(retailer=retailer, identifiers=idents)
        main = primary_identifier(probe) or retailer
        taken = {w.id for w in self.config.watches}
        if any(t.retailer == retailer and primary_identifier(t) == main
               for w in self.config.watches for t in w.targets):
            raise ValueError("that product is already being watched")
        watch_id = unique_id(slugify(f"{retailer}-{main}" if main != probe.get("url") else retailer), taken)
        probe.watch_id = watch_id

        result: StockResult | None = None
        adapter = self.adapters.get(retailer)
        if adapter is not None:
            home = Home(zip=self.config.home.zip, radius_miles=self.config.home.radius_miles)
            async with self.scheduler.gate(retailer):
                result = await dry_check(adapter, probe, home, timeout=ADD_DRY_CHECK_TIMEOUT)
        name = (result.product_name if result and result.product_name else None) \
            or f"{retailer_display(retailer)} {main if main != probe.get('url') else 'item'}"

        raw: dict[str, Any] = {
            "id": watch_id, "name": name, "enabled": True,
            "fulfillment": list(CHANNELS), "add_to_cart": True,
            "targets": [{"retailer": retailer, **idents}],
        }
        config = self.store.upsert_watch(raw)
        self.apply_config(config)
        if result is not None:
            target = next(t for t in self._watch(watch_id).targets)
            self.scheduler.seed_result(target.key, result)
            await self._on_result(target, self._watch(watch_id), result)
        log.info("added watch %s from URL (%s)", watch_id, retailer)
        return watch_id

    async def test_alert(self) -> str:
        outcomes = await self.send_test(None)
        if not outcomes:
            return "No notification channels are enabled."
        return "\n".join(f"{o.channel}: {'ok' if o.ok else 'FAILED'}" for o in outcomes)

    # ============================================================ Dashboard DashboardControl

    def watches(self) -> list[Any]:
        from .dashboard import TargetStatus, WatchStatus

        status = {r["key"]: r for r in self.scheduler.status()}
        out = []
        for w in self.config.watches:
            targets = []
            for t in w.targets:
                st = status.get(t.key, {})
                res: StockResult | None = st.get("last_result")
                last_row = self.db.last_check(t.db_id) if (res is None and t.db_id) else None
                in_stock_row = self.db.last_in_stock(t.db_id) if t.db_id else None
                targets.append(TargetStatus(
                    retailer=t.retailer,
                    identifiers=dict(t.identifiers),
                    store_ids=t.store_ids,
                    product_url=self.product_url(t) or None,
                    last_check_at=st.get("last_check_at") or _parse_ts(last_row and last_row["checked_at"]),
                    last_result=(res.raw_status if res else (last_row or {}).get("raw_status")),
                    last_in_stock=(res.in_stock if res else
                                   (bool(last_row["in_stock"]) if last_row else None)),
                    last_price=(res.price if res else (last_row or {}).get("price")),
                    last_in_stock_at=_parse_ts(in_stock_row and in_stock_row["checked_at"]),
                    interval_seconds=st.get("interval"),
                    blocked=bool(st.get("blocked")),
                    error=(res.error if res else (last_row or {}).get("error")),
                    consecutive_failures=int(st.get("consecutive_failures") or 0),
                    paused=bool(st.get("paused", not w.enabled)),
                ))
            out.append(WatchStatus(
                id=w.id, name=w.name, enabled=w.enabled, targets=targets, max_price=w.max_price,
                fulfillment=list(w.fulfillment), radius_miles=w.radius_miles,
                interval_seconds=w.interval_seconds, add_to_cart=w.add_to_cart,
            ))
        return out

    def recent_checks(self, watch_id: str, limit: int = 30) -> list[Any]:
        from .dashboard import CheckView

        w = self._watch(watch_id)
        rows: list[tuple[dict[str, Any], str]] = []
        for t in w.targets:
            if t.db_id:
                rows += [(r, t.retailer) for r in self.db.recent_checks(t.db_id, limit)]
        rows.sort(key=lambda p: p[0]["checked_at"], reverse=True)
        out = []
        for r, retailer in rows[:limit]:
            out.append(CheckView(
                retailer=retailer, checked_at=_parse_ts(r["checked_at"]) or datetime.now(timezone.utc),
                in_stock=bool(r["in_stock"]), price=r["price"], raw_status=r.get("raw_status"),
                error=r["error"], duration_ms=r["duration_ms"], summary=_channels_summary(r),
            ))
        return out

    def recent_events(self, limit: int = 100) -> list[Any]:
        from .dashboard import EventView

        names = {w.id: w.name for w in self.config.watches}
        out = []
        for e in self.db.recent_events(limit):
            disp = retailer_display(e["retailer"])
            name = names.get(e["watch_id"], e["watch_id"])
            d = e["details"]
            if e["kind"] == "restock":
                title = f"In stock: {name} @ {disp}"
                price = d.get("best_price")
                detail = ", ".join(x for x in (
                    f"${price:,.2f}" if isinstance(price, (int, float)) else "",
                    f"{len(d.get('seen_locations') or [])} location(s)",
                ) if x)
            elif e["kind"] == "blocked":
                title = f"Blocked: {name} @ {disp}"
                detail = d.get("detail") or "403/429/challenge"
            else:
                title = f"Error: {name} @ {disp}"
                detail = d.get("detail") or d.get("reason") or d.get("error") or ""
            out.append(EventView(at=_parse_ts(e["started_at"]) or datetime.now(timezone.utc), kind=e["kind"],
                                 title=title, detail=str(detail), watch_id=e["watch_id"],
                                 retailer=e["retailer"], ended_at=_parse_ts(e["ended_at"])))
            cart = d.get("cart")
            if isinstance(cart, dict):
                msg = "Added to cart: YES" if cart.get("success") else \
                    f"Added to cart: NO ({cart.get('message') or 'failed'})"
                out.append(EventView(at=_parse_ts(cart.get("at")) or out[-1].at, kind="cart",
                                     title=f"Cart: {name} @ {disp}", detail=msg,
                                     watch_id=e["watch_id"], retailer=e["retailer"]))
        out.sort(key=lambda v: v.at, reverse=True)
        return out[:limit]

    def settings(self) -> Any:
        from .dashboard import NotifyChannelView, SettingsView

        n = self.config.notify
        channels = [
            NotifyChannelView("pushover", n.pushover.enabled, bool(n.pushover.user_key and n.pushover.app_token)),
            NotifyChannelView("telegram", n.telegram.enabled, bool(n.telegram.bot_token and n.telegram.chat_id)),
            NotifyChannelView("mac_sound", n.mac_sound.enabled, True),
        ]
        return SettingsView(home_zip=self.config.home.zip, radius_miles=self.config.home.radius_miles,
                            quiet_hours=self.config.defaults.quiet_hours, channels=channels)

    async def signin_status(self) -> dict[str, bool | None]:
        """Cached sign-in state for the Settings page; never launches Chrome or waits on navigation.

        When the cache is stale and the browser is already running, one background
        refresh is started (each probe goes through the retailer's request gate).
        """
        used = sorted({t.retailer for w in self.config.watches for t in w.targets}) or list(RETAILERS)
        cached = getattr(self.browser, "cached_signin_status", None)
        if cached is None:  # a minimal browser without a cache
            return await self.browser.signin_status(used)
        stale = self.browser.signin_stale(used)
        refreshing = self._signin_refresh is not None and not self._signin_refresh.done()
        if stale and self.browser.started and not refreshing:
            self._signin_refresh = self._spawn(self.browser.signin_status(stale, launch=False), "signin-status")
        return cached(used)

    async def open_signin_browser(self) -> None:
        used = sorted({t.retailer for w in self.config.watches for t in w.targets}) or list(RETAILERS)
        await self.browser.open_signin_pages(used)

    async def send_test(self, channel: str | None = None) -> list[AlertOutcome]:
        if channel is None:
            return await self.notifier.test_all()
        alert = Alert(severity=Severity.CRITICAL, title="StockWatch test alert",
                      body=f"Test of the {channel} channel.", url="https://pushover.net/",
                      url_title="Pushover", speak="Stock Watch test alert", play_sound=True)
        if channel == "pushover":
            return [await self.notifier.pushover.send(alert)]
        if channel == "telegram":
            return [await self.notifier.telegram.send(alert)]
        if channel == "mac_sound":
            return [await self.notifier.mac_sound.play(alert)]
        raise ValueError(f"unknown channel {channel!r}")

    async def update_settings(self, home_zip: str, radius_miles: float, quiet_hours: str | None) -> None:
        zip_code = str(home_zip).strip()
        if not (zip_code.isdigit() and len(zip_code) == 5):
            raise ValueError("ZIP must be 5 digits")
        if radius_miles <= 0:
            raise ValueError("radius must be greater than 0")
        self.apply_config(self.store.set_values({
            "home.zip": zip_code,
            "home.radius_miles": float(radius_miles),
            "defaults.quiet_hours": quiet_hours or None,
        }))

    async def add_watch(self, spec: Any) -> None:
        if self.config.watch(spec.id) is not None:
            raise ValueError(f"a watch with id {spec.id!r} already exists")
        self.apply_config(self.store.upsert_watch(_spec_raw(spec)))

    async def update_watch(self, spec: Any) -> None:
        self._watch(spec.id)
        raw = _spec_raw(spec)
        old = self.store.raw_watch(spec.id) or {}
        if "online_only_severity" in old:  # keep YAML-only settings the form doesn't edit
            raw["online_only_severity"] = old["online_only_severity"]
        self.apply_config(self.store.upsert_watch(raw))

    async def set_paused(self, watch_id: str, paused: bool) -> None:
        """Pause/resume a watch; persisted as `enabled` in config.yaml so it survives restarts."""
        self._watch(watch_id)
        try:
            config = self.store.set_enabled(watch_id, not paused)
        except ConfigError as e:
            raise KeyError(watch_id) from e
        self.apply_config(config)
        if paused:
            self.scheduler.pause(watch_id)
        else:
            self.scheduler.resume(watch_id)

    async def delete_watch(self, watch_id: str) -> None:
        self._watch(watch_id)
        self.apply_config(self.store.delete_watch(watch_id))


# ------------------------------------------------------------------ small utilities

def _spec_raw(spec: Any) -> dict[str, Any]:
    return spec.to_raw()


def _parse_ts(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def _ago(seconds: int) -> str:
    if seconds < 90:
        return f"{seconds}s ago"
    if seconds < 5400:
        return f"{seconds // 60} min ago"
    return f"{seconds // 3600} h ago"


def _channels_summary(row: dict[str, Any]) -> str:
    import json

    try:
        chans = json.loads(row.get("channels_json") or "[]")
    except ValueError:
        return ""
    parts = []
    for c in chans:
        if not c.get("available"):
            continue
        label = {"store_pickup": "Pickup", "in_store": "In store", "ship": "Ship"}.get(c.get("channel"), "?")
        if c.get("store_name"):
            label += f": {c['store_name']}"
        if c.get("distance_miles") is not None:
            label += f" ({c['distance_miles']:.1f} mi)"
        parts.append(label)
    return "; ".join(parts[:3])
