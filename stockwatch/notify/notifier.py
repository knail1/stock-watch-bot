"""Alert fan-out and alert text formatting (spec section 7).

`Notifier.send` delivers one Alert to every enabled channel concurrently,
applies quiet hours, records one `alerts` row per channel and never raises.
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, time
from typing import Callable
from urllib.parse import quote

import httpx

from ..config import Config, WatchCfg
from ..db import Database
from ..models import Alert, AlertOutcome, CartResult, ChannelAvail, Severity, StockResult, Target
from .macsound import MacSound
from .pushover import PushoverClient
from .telegram import TelegramClient

log = logging.getLogger(__name__)

RETAILER_DISPLAY: dict[str, str] = {
    "bestbuy": "Best Buy",
    "target": "Target",
    "walmart": "Walmart",
    "gamestop": "GameStop",
    "nintendo": "Nintendo Store",
}

QUIET_PRIORITY = -1  # Pushover "quiet": delivered without sound
CARTING = "trying…"


def retailer_display(retailer: str) -> str:
    return RETAILER_DISPLAY.get(retailer, retailer.title())


# ---------------------------------------------------------------- quiet hours

_QUIET_RE = re.compile(r"^\s*(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})\s*$")


def parse_quiet_hours(spec: str | None) -> tuple[time, time] | None:
    """Parse "23:00-06:00" into (start, end); None/blank/invalid -> None."""
    if not spec:
        return None
    m = _QUIET_RE.match(spec)
    if not m:
        log.warning("ignoring invalid quiet_hours %r (expected HH:MM-HH:MM)", spec)
        return None
    h1, m1, h2, m2 = map(int, m.groups())
    try:
        return time(h1, m1), time(h2, m2)
    except ValueError:
        log.warning("ignoring invalid quiet_hours %r", spec)
        return None


def in_quiet_hours(spec: str | None, now: datetime) -> bool:
    """True if local wall-clock `now` falls in the window (which may wrap midnight)."""
    window = parse_quiet_hours(spec)
    if window is None:
        return False
    start, end = window
    t = now.time().replace(second=0, microsecond=0)
    if start == end:
        return False
    if start < end:
        return start <= t < end
    return t >= start or t < end


# ---------------------------------------------------------------- formatting

def _fmt_price(price: float | None) -> str | None:
    return f"${price:,.2f}" if price is not None else None


def _store_label(retailer: str, store_name: str | None) -> str | None:
    """'Frisco' -> 'Best Buy Frisco'; names already carrying the retailer are kept."""
    if not store_name:
        return None
    disp = retailer_display(retailer)
    return store_name if store_name.lower().startswith(disp.lower()) else f"{disp} {store_name}"


def pick_channel(result: StockResult, fulfillment: list[str] | None = None) -> ChannelAvail | None:
    """Best available channel: watch fulfillment priority order, then nearest store."""
    avail = result.available_channels()
    order = list(fulfillment or []) or ["store_pickup", "in_store", "ship"]
    for ch in order:
        hits = [c for c in avail if c.channel == ch]
        if hits:
            return min(hits, key=lambda c: (c.distance_miles is None, c.distance_miles or 0.0))
    return None


def _where(retailer: str, ch: ChannelAvail | None) -> str:
    if ch is None:
        return "In stock"
    store = _store_label(retailer, ch.store_name)
    dist = f" ({ch.distance_miles:.1f} mi)" if ch.distance_miles is not None else ""
    if ch.channel == "store_pickup":
        return f"Pickup at {store}{dist}" if store else "Pickup available"
    if ch.channel == "in_store":
        return f"In store at {store}{dist}" if store else "In store"
    return "Ships online"


def format_restock_alert(
    watch: WatchCfg,
    target: Target,
    result: StockResult,
    product_url: str,
    cart_url: str | None = None,
    added: bool | None = None,
    *,
    channel: ChannelAvail | None = None,
    severity: Severity = Severity.CRITICAL,
    event_id: int | None = None,
) -> Alert:
    """Build the restock alert exactly as in spec section 7.

    Title: ``IN STOCK: Nintendo Switch 2 @ Best Buy``
    Body:  ``Pickup at Best Buy Frisco (6.2 mi), $449.99, low stock. Added to cart: YES``

    `added`: True/False once the cart attempt finished; None means not yet
    known, which renders ``Added to cart: trying…`` when the watch has
    add_to_cart enabled and omits the clause otherwise. The primary link is
    the cart URL only when the item was added.
    """
    display = retailer_display(target.retailer)
    ch = channel or pick_channel(result, watch.fulfillment)
    parts = [_where(target.retailer, ch)]
    price = _fmt_price(result.price)
    if price:
        parts.append(price)
    if ch is not None and ch.low_stock:
        parts.append("low stock")
    body = ", ".join(parts)
    if added is not None:
        body += f". Added to cart: {'YES' if added else 'NO'}"
    elif watch.add_to_cart:
        body += f". Added to cart: {CARTING}"

    use_cart = bool(added and cart_url)
    store = _store_label(target.retailer, ch.store_name) if ch else None
    speak_where = f" at {store}" if store else f" at {display}"
    speak = f"{watch.name} in stock{speak_where}" + (f" for {price}" if price else "")

    return Alert(
        severity=severity,
        title=f"IN STOCK: {watch.name} @ {display}",
        body=body,
        url=cart_url if use_cart else product_url,
        url_title="Open cart" if use_cart else f"Open at {display}",
        extra_url=f"https://maps.apple.com/?q={quote(store)}" if store else None,
        extra_url_title="Store map" if store else None,
        speak=speak,
        event_id=event_id,
    )


def format_cart_followup(
    watch: WatchCfg,
    target: Target,
    cart: CartResult,
    product_url: str,
    *,
    event_id: int | None = None,
) -> Alert:
    """Follow-up after the cart attempt (spec section 8).

    Success -> primary link becomes the cart URL. Expired session -> warning
    "Sign in needed for <retailer>" with the product link.
    """
    display = retailer_display(target.retailer)
    if cart.signed_out:
        return Alert(
            severity=Severity.WARNING,
            title=f"Sign in needed for {display}",
            body=f"Could not add {watch.name} to cart: signed out of {display}. Added to cart: NO",
            url=product_url, url_title=f"Open at {display}",
            event_id=event_id, play_sound=False,
        )
    if cart.success:
        return Alert(
            severity=Severity.INFO,
            title=f"IN CART: {watch.name} @ {display}",
            body="Added to cart: YES. The cart does not hold inventory; check out fast.",
            url=cart.cart_url or product_url,
            url_title="Open cart" if cart.cart_url else f"Open at {display}",
            event_id=event_id, play_sound=False,
        )
    reason = f" ({cart.message})" if cart.message else ""
    return Alert(
        severity=Severity.INFO,
        title=f"Cart failed: {watch.name} @ {display}",
        body=f"Added to cart: NO{reason}",
        url=product_url, url_title=f"Open at {display}",
        event_id=event_id, play_sound=False,
    )


# ---------------------------------------------------------------- notifier

class Notifier:
    def __init__(
        self,
        config: Config,
        http: httpx.AsyncClient,
        db: Database | None,
        *,
        pushover: PushoverClient | None = None,
        telegram: TelegramClient | None = None,
        mac_sound: MacSound | None = None,
        clock: Callable[[], datetime] = datetime.now,
    ):
        self.http = http
        self.db = db
        self.clock = clock
        self.config = config
        n = config.notify
        self.pushover = pushover or PushoverClient(n.pushover, http)
        self.telegram = telegram or TelegramClient(n.telegram, http)
        self.mac_sound = mac_sound or MacSound(n.mac_sound)

    def update_config(self, config: Config) -> None:
        """Apply a reloaded config (channel settings, quiet hours)."""
        self.config = config
        self.pushover.cfg = config.notify.pushover
        self.telegram.cfg = config.notify.telegram
        self.mac_sound.cfg = config.notify.mac_sound

    def quiet_now(self) -> bool:
        return in_quiet_hours(self.config.defaults.quiet_hours, self.clock())

    async def send(self, alert: Alert) -> list[AlertOutcome]:
        """Fan out to all enabled channels concurrently. Never raises.

        Quiet hours: critical alerts are unaffected; other alerts go to
        Pushover at quiet priority (no sound), skip the Mac sound, and still
        go to Telegram. The Mac sound plays for critical alerts unless
        `alert.play_sound` overrides.
        """
        try:
            return await self._send(alert)
        except Exception as e:  # noqa: BLE001 - alerting must never crash the caller
            log.exception("notifier failure")
            return [AlertOutcome("notifier", False, f"{type(e).__name__}")]

    async def _send(self, alert: Alert) -> list[AlertOutcome]:
        n = self.config.notify
        critical = alert.severity == Severity.CRITICAL
        quiet = not critical and self.quiet_now()
        jobs: list[tuple[str, asyncio.Future | asyncio.Task]] = []

        if n.pushover.enabled:
            prio = None
            if quiet:
                prio = min(self.pushover.priority_for(alert.severity), QUIET_PRIORITY)
            jobs.append(("pushover", asyncio.ensure_future(self.pushover.send(alert, priority=prio))))
        if n.telegram.enabled:
            jobs.append(("telegram", asyncio.ensure_future(self.telegram.send(alert))))
        play = alert.play_sound if alert.play_sound is not None else critical
        if n.mac_sound.enabled and play and not quiet:
            jobs.append(("mac_sound", asyncio.ensure_future(self.mac_sound.play(alert))))

        results = await asyncio.gather(*(j for _, j in jobs), return_exceptions=True)
        outcomes: list[AlertOutcome] = []
        for (name, _), res in zip(jobs, results):
            if isinstance(res, BaseException):
                log.warning("%s channel raised %s", name, type(res).__name__)
                res = AlertOutcome(name, False, f"error: {type(res).__name__}")
            outcomes.append(res)
            self._record(alert, res, quiet)

        log.info("alert %r [%s]%s -> %s", alert.title, alert.severity.value,
                 " (quiet hours)" if quiet else "",
                 ", ".join(f"{o.channel}:{'ok' if o.ok else 'FAILED'}" for o in outcomes) or "no channels")
        return outcomes

    def _record(self, alert: Alert, outcome: AlertOutcome, quiet: bool) -> None:
        if self.db is None:
            return
        status = ("sent_quiet" if quiet and outcome.channel == "pushover" else "sent") if outcome.ok else "failed"
        receipt = (outcome.detail or None) if outcome.channel == "pushover" and outcome.ok else None
        try:
            self.db.record_alert(alert.event_id, outcome.channel, status, receipt)
        except Exception as e:  # noqa: BLE001
            log.warning("could not record alert row: %s", e)

    async def test_all(self) -> list[AlertOutcome]:
        """Fire a critical test alert through every enabled channel (`/test`, `stockwatch test-alert`)."""
        return await self.send(Alert(
            severity=Severity.CRITICAL,
            title="StockWatch test alert",
            body="If you can read this, alerts are working. Acknowledge to stop the repeat.",
            url="https://pushover.net/", url_title="Pushover",
            speak="Stock Watch test alert",
            play_sound=True,
        ))
