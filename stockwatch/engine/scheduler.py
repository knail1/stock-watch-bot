"""Per-target polling scheduler (spec sections 4 and 6, "Request etiquette").

* Every target runs in its own asyncio task. The delay to the next check is
  ``interval * uniform(0.8, 1.2)``, never below the retailer's minimum interval.
* ``interval`` starts at ``Config.effective_interval`` (itself floored by the
  retailer minimum and the adapter's ``min_interval_seconds``). A blocked
  result (403/429/challenge) doubles it, capped at 30 minutes; the next
  successful check resets it. Blocked targets are never retried immediately.
* Requests to one retailer go through a `RetailerGate`: at most N in flight
  (default 1) and at least ``spacing`` seconds between request starts.
* Exceptions from adapters become failure `StockResult`s; exceptions from the
  ``on_result`` callback or anywhere else in a loop are logged and the loop keeps
  going.

Clock, sleep and random source are injectable so tests run in virtual time.
"""
from __future__ import annotations

import asyncio
import logging
import random
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Awaitable, Callable, Mapping

from ..adapters.base import AdapterError
from ..config import Config, WatchCfg
from ..models import ErrorKind, StockResult, Target

log = logging.getLogger(__name__)

OnResult = Callable[[Target, WatchCfg, StockResult], Awaitable[None]]
Clock = Callable[[], float]
Sleep = Callable[[float], Awaitable[None]]

JITTER = 0.2
MAX_BACKOFF_SECONDS = 1800.0
DEFAULT_RETAILER_SPACING = 5.0
DEFAULT_CHECK_TIMEOUT = 90.0

_SECRET_PARAM_RE = re.compile(r"(?i)\b((?:api[_-]?)?key|token|apikey|secret|password)=([^&\s\"']+)")


def redact_detail(text: str | None) -> str | None:
    """Strip credential-looking query parameters from exception text (spec 12: secrets never logged)."""
    if not text:
        return text
    return _SECRET_PARAM_RE.sub(lambda m: f"{m.group(1)}=***", text)[:500]


class RetailerGate:
    """Per-retailer request budget: a concurrency limit plus minimum spacing between request starts."""

    def __init__(self, spacing: float, *, concurrency: int = 1, clock: Clock, sleep: Sleep):
        self.spacing = max(0.0, spacing)
        self._sem = asyncio.Semaphore(max(1, concurrency))
        self._lock = asyncio.Lock()
        self._last_start: float | None = None
        self._clock = clock
        self._sleep = sleep

    async def __aenter__(self) -> "RetailerGate":
        await self._sem.acquire()
        try:
            async with self._lock:
                if self._last_start is not None:
                    wait = self._last_start + self.spacing - self._clock()
                    if wait > 0:
                        await self._sleep(wait)
                self._last_start = self._clock()
        except BaseException:
            self._sem.release()
            raise
        return self

    async def __aexit__(self, *exc: object) -> None:
        self._sem.release()


@dataclass
class TargetState:
    """Mutable runtime state for one scheduled target."""

    target: Target
    watch: WatchCfg
    base_interval: float
    floor: float
    interval: float
    paused: bool = False
    blocked: bool = False
    consecutive_failures: int = 0
    last_check_at: datetime | None = None
    last_check_mono: float | None = None
    last_result: StockResult | None = None
    due_at: float | None = None        # monotonic time of next check; None while paused
    checking: bool = False
    task: asyncio.Task[None] | None = None
    wake: asyncio.Event = field(default_factory=asyncio.Event)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    @property
    def key(self) -> str:
        return self.target.key


class Scheduler:
    """Runs every enabled watch's targets on jittered, backed-off intervals.

    ``adapters`` maps retailer name to an adapter exposing
    ``async check(target, home) -> StockResult``. ``on_result`` is awaited after
    every check (scheduled or `check_now`); its exceptions are logged, never raised.
    """

    def __init__(self, config: Config, adapters: Mapping[str, Any], on_result: OnResult, *,
                 clock: Clock = time.monotonic, sleep: Sleep = asyncio.sleep,
                 rng: random.Random | None = None,
                 retailer_spacing: float | Mapping[str, float] = DEFAULT_RETAILER_SPACING,
                 retailer_concurrency: int = 1,
                 check_timeout: float = DEFAULT_CHECK_TIMEOUT,
                 max_backoff: float = MAX_BACKOFF_SECONDS):
        self.config = config
        self.adapters = adapters
        self.on_result = on_result
        self.clock = clock
        self.sleep = sleep
        self.rng = rng or random.Random()
        self.retailer_spacing = retailer_spacing
        self.retailer_concurrency = retailer_concurrency
        self.check_timeout = check_timeout
        self.max_backoff = max_backoff
        self.paused_all = False
        self._gates: dict[str, RetailerGate] = {}
        self._running = False
        self._stopped = False
        self._states: dict[str, TargetState] = self._build(config)

    # ------------------------------------------------------------ intervals

    def floor_for(self, retailer: str) -> float:
        adapter = self.adapters.get(retailer)
        adapter_min = getattr(adapter, "min_interval_seconds", 0) or 0
        return float(max(self.config.min_interval(retailer), adapter_min))

    def base_interval_for(self, watch: WatchCfg, retailer: str) -> float:
        return float(max(self.config.effective_interval(watch, retailer), self.floor_for(retailer)))

    def next_delay(self, st: TargetState) -> float:
        """Jittered delay (interval * U(0.8, 1.2)), never below the retailer floor."""
        jittered = st.interval * self.rng.uniform(1 - JITTER, 1 + JITTER)
        return max(jittered, st.floor)

    def _apply_result(self, st: TargetState, result: StockResult) -> None:
        if result.error == ErrorKind.BLOCKED:
            st.blocked = True
            cap = max(self.max_backoff, st.base_interval)
            st.interval = min(max(st.interval, st.base_interval) * 2, cap)
        elif result.error is None:
            st.blocked = False
            st.interval = st.base_interval
        st.consecutive_failures = st.consecutive_failures + 1 if result.error else 0

    def gate(self, retailer: str) -> RetailerGate:
        g = self._gates.get(retailer)
        if g is None:
            sp = self.retailer_spacing
            spacing = sp.get(retailer, DEFAULT_RETAILER_SPACING) if isinstance(sp, Mapping) else sp
            g = RetailerGate(float(spacing), concurrency=self.retailer_concurrency,
                             clock=self.clock, sleep=self.sleep)
            self._gates[retailer] = g
        return g

    # ------------------------------------------------------------ lifecycle

    def _make_state(self, watch: WatchCfg, target: Target) -> TargetState:
        base = self.base_interval_for(watch, target.retailer)
        st = TargetState(target=target, watch=watch, base_interval=base,
                         floor=self.floor_for(target.retailer), interval=base)
        st.due_at = self.clock()
        return st

    def _spawn(self, st: TargetState) -> None:
        if self._running and st.task is None:
            st.task = asyncio.create_task(self._loop(st), name=f"watch:{st.key}")

    def _build(self, config: Config) -> dict[str, TargetState]:
        out: dict[str, TargetState] = {}
        for w in config.watches:
            for t in w.targets:
                t.watch_id = t.watch_id or w.id
                out.setdefault(t.key, self._make_state(w, t))
        return out

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._stopped = False
        for st in self._states.values():
            self._spawn(st)

    async def stop(self) -> None:
        self._running = False
        self._stopped = True  # stragglers (dashboard check_now) must not reach adapters any more
        tasks = [st.task for st in self._states.values() if st.task]
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for st in self._states.values():
            st.task = None

    def reload(self, config: Config) -> None:
        """Diff-apply a new config: start added targets, stop removed ones, update the rest in place."""
        self.config = config
        new: dict[str, tuple[WatchCfg, Target]] = {}
        for w in config.watches:
            for t in w.targets:
                t.watch_id = t.watch_id or w.id
                new.setdefault(t.key, (w, t))
        for key in [k for k in self._states if k not in new]:
            st = self._states.pop(key)
            if st.task:
                st.task.cancel()
        for key, (w, t) in new.items():
            st = self._states.get(key)
            if st is None:
                st = self._make_state(w, t)
                self._states[key] = st
                self._spawn(st)
                continue
            t.db_id = t.db_id if t.db_id is not None else st.target.db_id
            st.watch, st.target = w, t
            st.floor = self.floor_for(t.retailer)
            new_base = self.base_interval_for(w, t.retailer)
            if new_base != st.base_interval:
                st.base_interval = new_base
                if not st.blocked:
                    st.interval = new_base
                if st.last_check_mono is not None and st.due_at is not None:
                    st.due_at = min(st.due_at, st.last_check_mono + self.next_delay(st))
            else:
                st.interval = max(st.interval, new_base)
            st.wake.set()

    # ------------------------------------------------------------ control

    def _paused(self, st: TargetState) -> bool:
        return self.paused_all or st.paused or not st.watch.enabled

    def _states_for(self, watch_id: str) -> list[TargetState]:
        sts = [st for st in self._states.values() if st.watch.id == watch_id]
        if not sts:
            raise KeyError(watch_id)
        return sts

    def _resume_due(self, st: TargetState) -> float:
        """Earliest polite time to check after a resume: honour the floor, or the backoff when blocked."""
        now = self.clock()
        if st.last_check_mono is None:
            return now
        return max(now, st.last_check_mono + (st.interval if st.blocked else st.floor))

    def pause(self, watch_id: str) -> None:
        for st in self._states_for(watch_id):
            st.paused = True
            st.wake.set()

    def resume(self, watch_id: str) -> None:
        for st in self._states_for(watch_id):
            st.paused = False
            st.due_at = max(st.due_at or 0.0, self._resume_due(st))
            st.wake.set()

    def pause_all(self) -> None:
        self.paused_all = True
        for st in self._states.values():
            st.wake.set()

    def resume_all(self) -> None:
        """Lift the global pause; per-watch pauses stay in effect."""
        self.paused_all = False
        for st in self._states.values():
            st.due_at = max(st.due_at or 0.0, self._resume_due(st))
            st.wake.set()

    async def check_now(self, watch_id: str) -> list[StockResult]:
        """Check every target of a watch now (still through the retailer gate); reschedules each."""
        sts = self._states_for(watch_id)
        results = await asyncio.gather(*(self._check(st) for st in sts))
        for st in sts:
            st.wake.set()
        return [r for r in results if r is not None]

    def seed_result(self, target_key: str, result: StockResult) -> bool:
        """Count a check made outside the scheduler (e.g. the dry check behind `/add`) as the
        target's latest check, so the next scheduled request waits a full jittered interval.

        Does not call ``on_result``. Returns False for an unknown target key.
        """
        st = self._states.get(target_key)
        if st is None:
            return False
        st.last_result = result
        st.last_check_at = result.checked_at
        st.last_check_mono = self.clock()
        self._apply_result(st, result)
        st.due_at = st.last_check_mono + self.next_delay(st)
        st.wake.set()
        return True

    def status(self) -> list[dict[str, Any]]:
        """Snapshot for dashboard / Telegram /status."""
        now = self.clock()
        out = []
        for st in self._states.values():
            out.append({
                "key": st.key,
                "watch_id": st.watch.id,
                "watch_name": st.watch.name,
                "retailer": st.target.retailer,
                "target": st.target,
                "last_check_at": st.last_check_at,
                "last_result": st.last_result,
                "interval": st.interval,
                "base_interval": st.base_interval,
                "next_check_in": (max(0.0, st.due_at - now)
                                  if st.due_at is not None and not self._paused(st) else None),
                "blocked": st.blocked,
                "consecutive_failures": st.consecutive_failures,
                "paused": self._paused(st),
                "enabled": st.watch.enabled,
                "checking": st.checking,
            })
        return out

    # ------------------------------------------------------------ core

    async def _run_adapter(self, st: TargetState) -> StockResult:
        retailer = st.target.retailer
        adapter = self.adapters.get(retailer)
        if adapter is None:
            return StockResult.failure(retailer, ErrorKind.CONFIG, f"no adapter for {retailer}")
        home = self.config.effective_home(st.watch)
        t0 = self.clock()
        try:
            async with self.gate(retailer):
                if self._stopped:
                    return StockResult.failure(retailer, ErrorKind.UNKNOWN, "scheduler stopped")
                t0 = self.clock()
                result = await asyncio.wait_for(adapter.check(st.target, home), self.check_timeout)
        except AdapterError as e:
            result = StockResult.failure(retailer, e.kind, redact_detail(str(e)) or type(e).__name__)
        except asyncio.TimeoutError:
            result = StockResult.failure(retailer, ErrorKind.TIMEOUT, f"check exceeded {self.check_timeout:.0f}s")
        except asyncio.CancelledError:
            raise
        except Exception as e:  # adapter bug or network error: never kill the loop
            result = StockResult.failure(retailer, ErrorKind.UNKNOWN,
                                         redact_detail(f"{type(e).__name__}: {e}"))
        if not isinstance(result, StockResult):
            result = StockResult.failure(retailer, ErrorKind.UNKNOWN, "adapter returned no StockResult")
        if result.duration_ms is None:
            result.duration_ms = int(max(0.0, self.clock() - t0) * 1000)
        return result

    async def _check(self, st: TargetState, *, scheduled: bool = False) -> StockResult | None:
        """One check: adapter -> backoff bookkeeping -> on_result; schedules the next due time.

        A ``scheduled`` check is skipped (returns None) if a `check_now` ran while
        it waited for the target lock and pushed the due time out.
        """
        async with st.lock:
            if self._stopped or (scheduled and (st.due_at is None or self.clock() + 1e-9 < st.due_at)):
                return None
            st.checking = True
            try:
                result = await self._run_adapter(st)
            finally:
                st.checking = False
            if self._stopped:
                return None  # stopped while waiting on the gate/adapter: discard, no callbacks
            st.last_result = result
            st.last_check_at = result.checked_at
            st.last_check_mono = self.clock()
            self._apply_result(st, result)
            st.due_at = st.last_check_mono + self.next_delay(st)
            if result.error:
                log.warning("check %s failed: %s (%s)", st.key, result.error, result.error_detail or "")
            try:
                await self.on_result(st.target, st.watch, result)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("on_result callback failed for %s", st.key)
            return result

    async def _wait(self, st: TargetState) -> None:
        """Sleep until due (or forever while paused), returning early when woken."""
        st.wake.clear()
        if self._paused(st) or st.due_at is None:
            await st.wake.wait()
            return
        delay = st.due_at - self.clock()
        if delay <= 0:
            return
        sleeper = asyncio.ensure_future(self.sleep(delay))
        waker = asyncio.ensure_future(st.wake.wait())
        try:
            await asyncio.wait({sleeper, waker}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for f in (sleeper, waker):
                f.cancel()

    async def _loop(self, st: TargetState) -> None:
        while self._running:
            try:
                await self._wait(st)
                if not self._running or self._states.get(st.key) is not st:
                    return
                if self._paused(st):
                    continue
                if st.due_at is None:
                    st.due_at = self._resume_due(st)
                if self.clock() + 1e-9 < st.due_at:
                    continue  # woken early (reschedule/check_now); recompute the wait
                await self._check(st, scheduled=True)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("scheduler loop error for %s; continuing", st.key)
                st.due_at = self.clock() + max(st.interval, st.floor)
