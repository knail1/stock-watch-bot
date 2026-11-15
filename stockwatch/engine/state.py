"""State and dedupe engine (spec sections 4, 6 and 11).

Turns the stream of per-check `StockResult`s for a target into a short list of
`Decision`s the service acts on (alert, cart attempt, warning). All durable
state lives in the `events` table, so a restart neither re-alerts an ongoing
restock nor repeats a blocked/broken notice:

* ``restock`` event: opens on the first *qualifying* in-stock check (after an
  out-of-stock check, or on the very first check) and closes after 2
  consecutive out-of-stock checks. Exactly one *delivered* critical alert per
  event (the service reports a failed delivery via `critical_delivery_failed`,
  which re-arms it for the next in-stock check, up to 3 attempts); later
  alerts within the same event fire only for a new store or a lower price and
  are downgraded to info once the critical alert has gone out.
* ``blocked`` event: opens on a 403/429/challenge result; one warning per
  episode. Closes (with a ``recovered`` decision) on the next successful check.
* ``error`` event: opens immediately on ``parse_failed`` (adapter broken, fail
  loud) or after N consecutive failures of any other kind; one warning per
  episode; closes on the next successful check.

Error results never count towards the restock in/out-of-stock streaks.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Literal

from ..config import WatchCfg, default_online_only_severity
from ..db import Database
from ..models import ChannelAvail, ErrorKind, Home, Severity, StockResult, Target

log = logging.getLogger(__name__)

DecisionKind = Literal[
    "restock_alert", "price_info", "blocked_notice", "broken_notice", "failure_warning", "recovered",
]

#: Restock event closes after this many consecutive out-of-stock checks.
CLOSE_AFTER_OOS = 2
#: Default consecutive-failure count that triggers one warning (config: health.failure_warn_threshold).
DEFAULT_FAILURE_THRESHOLD = 5
#: A critical alert that failed to deliver is re-sent on the next in-stock check, at most
#: this many attempts per restock event.
MAX_CRITICAL_ATTEMPTS = 3
LOCAL_CHANNELS = ("store_pickup", "in_store")
_PRICE_EPS = 0.005


@dataclass
class Decision:
    """Something the service should act on for one check.

    ``severity`` is the alert severity to use. Only ``restock_alert`` decisions
    with ``Severity.CRITICAL`` should trigger the cart helper (`is_critical`).
    """

    kind: DecisionKind
    severity: Severity
    event_id: int | None
    channel: ChannelAvail | None
    result: StockResult
    reason: str = ""
    realert: bool = False   # True for a follow-up alert within an already-alerted restock event
    channels: list[ChannelAvail] = field(default_factory=list)  # all qualifying channels

    @property
    def is_critical(self) -> bool:
        return self.severity is Severity.CRITICAL


# ------------------------------------------------------------------ pure helpers

def qualifying_channels(result: StockResult, watch: WatchCfg, home: Home,
                        target: Target | None = None) -> list[ChannelAvail]:
    """Available channels the user cares about, in ``watch.fulfillment`` priority order
    (nearest store first within a channel). The first one is the matched channel the
    alert names and the cart helper uses.

    Drops channels not in ``watch.fulfillment``, local channels whose store is
    farther than ``home.radius_miles``, and stores not in ``target.store_ids``
    when that list is configured.
    """
    allowed = set(watch.fulfillment)
    store_ids = set(target.store_ids) if target and target.store_ids else None
    out: list[ChannelAvail] = []
    for c in result.available_channels():
        if c.channel not in allowed:
            continue
        if c.channel in LOCAL_CHANNELS:
            if c.distance_miles is not None and c.distance_miles > home.radius_miles:
                continue
            if store_ids is not None and c.store_id is not None and c.store_id not in store_ids:
                continue
        out.append(c)

    rank = {ch: i for i, ch in enumerate(watch.fulfillment)}

    def order(c: ChannelAvail) -> tuple[int, float]:
        # Watch fulfillment priority first (spec 8: the cart uses this channel), then nearest store.
        dist = c.distance_miles if c.distance_miles is not None else float("inf")
        return (rank.get(c.channel, len(rank)), dist)

    return sorted(out, key=order)


def location_key(c: ChannelAvail) -> str:
    """Identity of where stock is: a store (by id or name) or 'ship' for online."""
    if c.channel == "ship":
        return "ship"
    return f"store:{c.store_id or c.store_name or c.channel}"


def _parse_severity(value: str) -> Severity:
    try:
        return Severity(str(value).lower())
    except ValueError:
        return Severity.CRITICAL


def _is_in_stock_without_detail(result: StockResult) -> bool:
    """Adapter said in stock but gave no channel breakdown; treat as a qualifying hit."""
    return result.in_stock and not result.channels


# ------------------------------------------------------------------ engine

class StateEngine:
    """Applies the spec 11 dedupe rule. One instance per service; safe across restarts.

    ``record_checks``: when True (default) `process` also writes the `checks`
    row for every result, so callers must not record it again.
    """

    def __init__(self, db: Database, *, failure_threshold: int = DEFAULT_FAILURE_THRESHOLD,
                 record_checks: bool = True):
        self.db = db
        self.failure_threshold = max(1, int(failure_threshold))
        self.record_checks = record_checks
        self._failures: dict[int, int] = {}

    # ---- public

    def consecutive_failures(self, target: Target) -> int:
        tid = self._target_id(target)
        return self._failure_count(tid)

    def process(self, target: Target, result: StockResult, watch: WatchCfg,
                max_price: float | None, home: Home, *,
                online_severity: str | None = None) -> list[Decision]:
        """Dedupe one check into decisions. Restock decisions come first (alert-first, spec 8).

        ``online_severity``: severity for ship-only stock, normally
        ``Config.online_only_severity(watch, retailer)``; when None the watch's
        own setting or the spec 7 default (info if a local channel is preferred).
        """
        tid = self._target_id(target)
        failures = self._failure_count(tid)  # seed from history before recording this check
        if self.record_checks:
            self.db.record_check(tid, result)

        if result.error:
            failures += 1
            self._failures[tid] = failures
            return self._on_failure(tid, result, failures)

        self._failures[tid] = 0
        recovered = self._on_success(tid, result)
        qualifying = qualifying_channels(result, watch, home, target)
        restock: list[Decision] = []
        if qualifying or _is_in_stock_without_detail(result):
            online = online_severity or watch.online_only_severity \
                or default_online_only_severity(watch.fulfillment)
            restock = self._on_in_stock(tid, result, watch, max_price, qualifying, online)
        else:
            self._on_out_of_stock(tid, result)
        # The restock alert must never queue behind an INFO "recovered" notice.
        return restock + recovered

    def critical_delivery_failed(self, target: Target, event_id: int | None) -> bool:
        """The critical alert for ``event_id`` did not reach the user: allow one more try.

        Clears ``critical_sent`` so the next qualifying in-stock check in the same
        event emits a CRITICAL decision again (capped by MAX_CRITICAL_ATTEMPTS).
        Returns True when a retry was armed.
        """
        if event_id is None:
            return False
        ev = self.db.get_open_event(self._target_id(target), "restock")
        if not ev or ev["id"] != event_id:
            return False
        details = ev["details"]
        if int(details.get("critical_attempts", 1)) >= MAX_CRITICAL_ATTEMPTS:
            log.warning("critical alert for event %s undelivered after %d attempts; giving up",
                        event_id, MAX_CRITICAL_ATTEMPTS)
            return False
        details["critical_sent"] = False
        details["critical_retry"] = True
        self.db.update_event_details(event_id, details)
        return True

    # ---- bookkeeping

    def _target_id(self, target: Target) -> int:
        if target.db_id is None:
            self.db.ensure_target(target)
        return int(target.db_id)  # type: ignore[arg-type]

    def _failure_count(self, tid: int) -> int:
        """Consecutive failures; after a restart, recomputed from the most recent check rows."""
        if tid not in self._failures:
            n = 0
            for row in self.db.recent_checks(tid, self.failure_threshold + 1):
                if not row.get("error"):
                    break
                n += 1
            self._failures[tid] = n
        return self._failures[tid]

    # ---- failure path

    def _on_failure(self, tid: int, result: StockResult, failures: int) -> list[Decision]:
        details = {"error": result.error, "detail": result.error_detail, "raw_status": result.raw_status}
        if result.error == ErrorKind.BLOCKED:
            if self.db.get_open_event(tid, "blocked"):
                return []
            eid = self.db.open_event(tid, "blocked", details, started_at=result.checked_at)
            return [Decision("blocked_notice", Severity.WARNING, eid, None, result,
                             reason="blocked (403/429/challenge); backing off, check manually")]

        if self.db.get_open_event(tid, "error"):
            return []
        if result.error == ErrorKind.PARSE_FAILED:
            eid = self.db.open_event(tid, "error", {**details, "reason": "parse_failed"},
                                     started_at=result.checked_at)
            return [Decision("broken_notice", Severity.WARNING, eid, None, result,
                             reason="adapter broken: page/API shape changed")]
        if failures == self.failure_threshold and not self.db.get_open_event(tid, "blocked"):
            eid = self.db.open_event(tid, "error", {**details, "reason": "consecutive_failures",
                                                    "failures": failures},
                                     started_at=result.checked_at)
            return [Decision("failure_warning", Severity.WARNING, eid, None, result,
                             reason=f"{failures} consecutive failed checks (last: {result.error})")]
        return []

    def _on_success(self, tid: int, result: StockResult) -> list[Decision]:
        out: list[Decision] = []
        for kind in ("blocked", "error"):
            ev = self.db.get_open_event(tid, kind)
            if ev:
                self.db.close_event(ev["id"], ended_at=result.checked_at)
                out.append(Decision("recovered", Severity.INFO, ev["id"], None, result,
                                    reason=f"{kind} cleared"))
        return out

    # ---- restock path

    def _on_out_of_stock(self, tid: int, result: StockResult) -> None:
        ev = self.db.get_open_event(tid, "restock")
        if not ev:
            return
        details = ev["details"]
        details["oos_streak"] = int(details.get("oos_streak", 0)) + 1
        if details["oos_streak"] >= CLOSE_AFTER_OOS:
            self.db.update_event_details(ev["id"], details)
            self.db.close_event(ev["id"], ended_at=result.checked_at)
            log.info("restock event %s closed after %d out-of-stock checks", ev["id"], details["oos_streak"])
        else:
            self.db.update_event_details(ev["id"], details)

    def _on_in_stock(self, tid: int, result: StockResult, watch: WatchCfg,
                     max_price: float | None, qualifying: list[ChannelAvail],
                     online_severity: str) -> list[Decision]:
        primary = qualifying[0] if qualifying else None
        keys = sorted({location_key(c) for c in qualifying})
        over_max = (max_price is not None and result.price is not None
                    and result.price > max_price + _PRICE_EPS)
        online_only = bool(qualifying) and all(c.channel == "ship" for c in qualifying)
        if over_max:
            kind: DecisionKind = "price_info"
            severity = Severity.INFO
        else:
            kind = "restock_alert"
            severity = _parse_severity(online_severity) if online_only else Severity.CRITICAL

        ev = self.db.get_open_event(tid, "restock")
        if ev is None:
            details: dict[str, Any] = {
                "oos_streak": 0, "seen_locations": keys, "best_price": result.price,
                "first_price": result.price, "critical_sent": severity is Severity.CRITICAL,
                "critical_attempts": 1 if severity is Severity.CRITICAL else 0,
                "alerts": 1, "primary": location_key(primary) if primary else None,
            }
            eid = self.db.open_event(tid, "restock", details, started_at=result.checked_at)
            reason = "over max price" if over_max else ("online only" if online_only else "restock")
            return [Decision(kind, severity, eid, primary, result, reason=reason, channels=qualifying)]

        eid = ev["id"]
        details = ev["details"]
        details["oos_streak"] = 0
        seen = set(details.get("seen_locations") or [])
        new_keys = [k for k in keys if k not in seen]
        best = details.get("best_price")
        lower = result.price is not None and best is not None and result.price < float(best) - _PRICE_EPS
        if result.price is not None and (best is None or result.price < float(best)):
            details["best_price"] = result.price
        details["seen_locations"] = sorted(seen | set(keys))

        decisions: list[Decision] = []
        retry = bool(details.get("critical_retry")) and severity is Severity.CRITICAL
        if new_keys or lower or retry:
            if severity is Severity.CRITICAL and details.get("critical_sent"):
                severity = Severity.INFO  # exactly one (delivered) critical alert per restock event
            if severity is Severity.CRITICAL:
                details["critical_sent"] = True
                details["critical_retry"] = False
                details["critical_attempts"] = int(details.get("critical_attempts", 0)) + 1
            details["alerts"] = int(details.get("alerts", 1)) + 1
            channel = next((c for c in qualifying if location_key(c) in new_keys), primary)
            reason = "new store" if new_keys else ("lower price" if lower else "critical alert retry")
            if over_max:
                reason += ", over max price"
            decisions.append(Decision(kind, severity, eid, channel, result, reason=reason,
                                      realert=True, channels=qualifying))
        self.db.update_event_details(eid, details)
        return decisions
