"""Scheduler tests in virtual time (no real sleeping, no network)."""
from __future__ import annotations

import asyncio
import heapq
import itertools
import random
from pathlib import Path

import pytest

from stockwatch.adapters.base import BlockedError, ParseError
from stockwatch.config import Config, SecretResolver, build_config
from stockwatch.engine.scheduler import MAX_BACKOFF_SECONDS, Scheduler, redact_detail
from stockwatch.models import ErrorKind, StockResult


class VClock:
    """Virtual monotonic clock; `sleep` parks until `advance` moves time past its deadline."""

    def __init__(self) -> None:
        self.now = 0.0
        self._heap: list[tuple[float, int, asyncio.Future[None]]] = []
        self._seq = itertools.count()

    def __call__(self) -> float:
        return self.now

    async def sleep(self, d: float) -> None:
        fut = asyncio.get_running_loop().create_future()
        heapq.heappush(self._heap, (self.now + max(0.0, d), next(self._seq), fut))
        await fut

    @staticmethod
    async def settle() -> None:
        for _ in range(30):
            await asyncio.sleep(0)

    async def advance(self, seconds: float) -> None:
        end = self.now + seconds
        await self.settle()
        while True:
            while self._heap and self._heap[0][2].done():
                heapq.heappop(self._heap)
            if not self._heap or self._heap[0][0] > end:
                break
            when, _, fut = heapq.heappop(self._heap)
            self.now = max(self.now, when)
            fut.set_result(None)
            await self.settle()
        self.now = end
        await self.settle()


class FakeAdapter:
    def __init__(self, name: str, clock: VClock, min_interval_seconds: int = 0) -> None:
        self.name = name
        self.clock = clock
        self.min_interval_seconds = min_interval_seconds
        self.calls: list[tuple[float, str]] = []
        self.script: list[object] = []   # StockResult or Exception, consumed in order
        self.default: object = None

    async def check(self, target, home) -> StockResult:
        self.calls.append((self.clock.now, target.key))
        item = self.script.pop(0) if self.script else self.default
        if isinstance(item, BaseException):
            raise item
        if isinstance(item, StockResult):
            return item
        return StockResult(retailer=self.name, in_stock=False, raw_status="OUT_OF_STOCK")


def make_config(watches: list[dict], retailers: dict | None = None) -> Config:
    raw = {"home": {"zip": "75034"}, "defaults": {"interval_seconds": 90}, "watches": watches,
           "retailers": retailers or {}}
    return build_config(raw, SecretResolver(env_file=Path("/nonexistent/.env"), use_keychain=False))


def bb_watch(wid="sw2", sku="1", interval=None, enabled=True, retailer="bestbuy"):
    ident = {"bestbuy": "sku", "target": "tcin", "walmart": "item_id"}[retailer]
    w = {"id": wid, "name": wid, "enabled": enabled, "targets": [{"retailer": retailer, ident: sku}]}
    if interval is not None:
        w["interval_seconds"] = interval
    return w


def blocked() -> StockResult:
    return StockResult.failure("bestbuy", ErrorKind.BLOCKED, "HTTP 403")


def ok() -> StockResult:
    return StockResult(retailer="bestbuy", in_stock=False, raw_status="OUT_OF_STOCK")


@pytest.fixture
def vclock():
    return VClock()


@pytest.fixture
def env(vclock):
    adapters = {r: FakeAdapter(r, vclock) for r in ("bestbuy", "target", "walmart")}
    seen: list = []

    async def on_result(target, watch, result):
        seen.append((vclock.now, target.key, result))

    def make(cfg: Config, **kw) -> Scheduler:
        kw.setdefault("retailer_spacing", 5.0)
        return Scheduler(cfg, adapters, on_result, clock=vclock, sleep=vclock.sleep,
                         rng=random.Random(1234), **kw)

    return adapters, seen, make


# ------------------------------------------------------------------ intervals

def test_jitter_within_plus_minus_20_percent(env):
    _, _, make = env
    s = make(make_config([bb_watch(interval=300)]))
    st = next(iter(s._build(s.config).values()))
    assert st.base_interval == 300
    delays = [s.next_delay(st) for _ in range(2000)]
    assert all(240 <= d <= 360 for d in delays)
    assert min(delays) < 245 and max(delays) > 355  # actually spans the range
    assert len({round(d, 3) for d in delays}) > 1000


def test_min_interval_floor(env):
    _, _, make = env
    cfg = make_config([bb_watch(interval=10), bb_watch("w2", "2", interval=10, retailer="walmart")])
    s = make(cfg)
    states = s._build(cfg)
    by_r = {st.target.retailer: st for st in states.values()}
    assert by_r["bestbuy"].base_interval == 60 and by_r["walmart"].base_interval == 180
    # jitter never takes the delay below the retailer floor
    assert all(s.next_delay(by_r["bestbuy"]) >= 60 for _ in range(500))
    assert all(s.next_delay(by_r["walmart"]) >= 180 for _ in range(500))


def test_floor_honours_config_and_adapter_minimums(env, vclock):
    adapters, _, make = env
    cfg = make_config([bb_watch(interval=10)], retailers={"bestbuy": {"min_interval_seconds": 120}})
    assert make(cfg).floor_for("bestbuy") == 120
    adapters["bestbuy"].min_interval_seconds = 200
    s = make(cfg)
    assert s.floor_for("bestbuy") == 200
    assert next(iter(s._build(cfg).values())).base_interval == 200


def test_blocked_doubles_capped_and_resets_on_success(env):
    _, _, make = env
    s = make(make_config([bb_watch(interval=100)]))
    st = next(iter(s._build(s.config).values()))
    seen = []
    for _ in range(7):
        s._apply_result(st, blocked())
        seen.append(st.interval)
    assert seen == [200, 400, 800, 1600, 1800, 1800, 1800]
    assert st.blocked and st.consecutive_failures == 7
    s._apply_result(st, StockResult.failure("bestbuy", ErrorKind.TIMEOUT))
    assert st.interval == MAX_BACKOFF_SECONDS  # other failures neither reset nor double
    s._apply_result(st, ok())
    assert st.interval == 100 and not st.blocked and st.consecutive_failures == 0


async def test_blocked_backoff_applies_to_real_schedule(env, vclock):
    adapters, seen, make = env
    bb = adapters["bestbuy"]
    bb.script = [BlockedError("HTTP 429"), BlockedError("challenge"), ok()]
    s = make(make_config([bb_watch(interval=100)]))
    await s.start()
    await vclock.advance(0)
    assert len(bb.calls) == 1 and seen[-1][2].error == ErrorKind.BLOCKED
    # never retried immediately: nothing before 0.8 * 200s
    await vclock.advance(159)
    assert len(bb.calls) == 1
    await vclock.advance(241 - 159)
    assert len(bb.calls) == 2
    t2 = bb.calls[1][0]
    assert 160 <= t2 <= 240
    await vclock.advance(320 - 1)
    assert len(bb.calls) == 2  # second block -> interval 400, >= 320s away
    while len(bb.calls) < 3:
        await vclock.advance(1)
    assert 320 <= bb.calls[2][0] - t2 <= 480
    st = s.status()[0]
    assert st["blocked"] is False and st["interval"] == 100  # reset on success
    assert 80 <= st["next_check_in"] <= 120
    await s.stop()


# ------------------------------------------------------------------ retailer budget

async def test_per_retailer_request_spacing(env, vclock):
    adapters, _, make = env
    cfg = make_config([bb_watch("a", "1"), bb_watch("b", "2"), bb_watch("c", "3"),
                       bb_watch("d", "4", retailer="target")])
    s = make(cfg, retailer_spacing=7.0)
    await s.start()
    await vclock.advance(30)
    times = [t for t, _ in adapters["bestbuy"].calls]
    assert len(times) == 3
    assert all(b - a >= 7.0 for a, b in zip(times, times[1:]))
    assert adapters["target"].calls[0][0] == 0  # other retailers are not held back
    await s.stop()


async def test_check_now_goes_through_retailer_gate(env, vclock):
    adapters, _, make = env
    s = make(make_config([bb_watch("a", "1"), bb_watch("b", "2")]), retailer_spacing=4.0)
    task = asyncio.gather(s.check_now("a"), s.check_now("b"))
    await vclock.advance(10)
    ra, rb = await task
    assert len(ra) == len(rb) == 1
    times = [t for t, _ in adapters["bestbuy"].calls]
    assert times == [0.0, 4.0]


async def test_retailer_concurrency_limit(vclock):
    in_flight = 0
    peak = 0

    class Slow(FakeAdapter):
        async def check(self, target, home):
            nonlocal in_flight, peak
            in_flight += 1
            peak = max(peak, in_flight)
            await vclock.sleep(3)
            in_flight -= 1
            return ok()

    async def on_result(*_):
        pass

    cfg = make_config([bb_watch("a", "1"), bb_watch("b", "2"), bb_watch("c", "3")])
    s = Scheduler(cfg, {"bestbuy": Slow("bestbuy", vclock)}, on_result, clock=vclock,
                  sleep=vclock.sleep, rng=random.Random(1), retailer_spacing=0.0)
    await s.start()
    await vclock.advance(20)
    assert peak == 1
    await s.stop()


# ------------------------------------------------------------------ control

async def test_pause_resume(env, vclock):
    adapters, _, make = env
    bb = adapters["bestbuy"]
    s = make(make_config([bb_watch(interval=100)]))
    await s.start()
    await vclock.advance(0)
    assert len(bb.calls) == 1
    s.pause("sw2")
    assert s.status()[0]["paused"] is True and s.status()[0]["next_check_in"] is None
    await vclock.advance(5000)
    assert len(bb.calls) == 1
    s.resume("sw2")
    await vclock.advance(0)
    assert len(bb.calls) == 2  # floor long since passed -> checks promptly
    s.pause("sw2")
    s.resume("sw2")  # resume right after a check must still honour the floor
    await vclock.advance(59)
    assert len(bb.calls) == 2
    await vclock.advance(200)
    assert len(bb.calls) >= 3
    with pytest.raises(KeyError):
        s.pause("nope")
    await s.stop()


async def test_pause_all_resume_all_keeps_individual_pause(env, vclock):
    adapters, _, make = env
    s = make(make_config([bb_watch("a", "1"), bb_watch("b", "2", retailer="target")]))
    await s.start()
    await vclock.advance(0)
    s.pause_all()
    s.pause("a")
    assert all(x["paused"] for x in s.status())
    await vclock.advance(3000)
    assert len(adapters["bestbuy"].calls) == 1 and len(adapters["target"].calls) == 1
    s.resume_all()
    await vclock.advance(200)
    assert len(adapters["bestbuy"].calls) == 1  # still individually paused
    assert len(adapters["target"].calls) >= 2
    await s.stop()


async def test_check_now_runs_immediately_and_reschedules(env, vclock):
    adapters, seen, make = env
    bb = adapters["bestbuy"]
    s = make(make_config([bb_watch(interval=1000)]))
    await s.start()
    await vclock.advance(10)
    assert len(bb.calls) == 1
    results = await s.check_now("sw2")
    assert len(results) == 1 and len(bb.calls) == 2 and len(seen) == 2
    nxt = s.status()[0]["next_check_in"]
    assert 800 <= nxt <= 1200  # timer restarted from the manual check
    await vclock.advance(799)
    assert len(bb.calls) == 2
    with pytest.raises(KeyError):
        await s.check_now("missing")
    await s.stop()


async def test_disabled_watch_is_paused(env, vclock):
    adapters, _, make = env
    s = make(make_config([bb_watch(enabled=False)]))
    await s.start()
    await vclock.advance(1000)
    assert adapters["bestbuy"].calls == []
    assert s.status()[0]["paused"] is True and s.status()[0]["enabled"] is False
    await s.stop()


async def test_reload_diff(env, vclock):
    adapters, _, make = env
    s = make(make_config([bb_watch("a", "1", interval=600), bb_watch("b", "2")]))
    await s.start()
    await vclock.advance(10)
    keys_before = {x["key"] for x in s.status()}
    task_a = next(st.task for st in s._states.values() if st.watch.id == "a")
    task_b = next(st.task for st in s._states.values() if st.watch.id == "b")

    s.reload(make_config([bb_watch("a", "1", interval=120), bb_watch("c", "3", retailer="target")]))
    await vclock.advance(0)
    status = {x["watch_id"]: x for x in s.status()}
    assert set(status) == {"a", "c"}
    assert task_b.cancelled() or task_b.done()
    a_state = next(st for st in s._states.values() if st.watch.id == "a")
    assert a_state.task is task_a  # unchanged target keeps its loop and history
    assert status["a"]["interval"] == 120 and status["a"]["last_result"] is not None
    assert status["a"]["next_check_in"] <= 144  # rescheduled to the shorter interval
    assert len(adapters["target"].calls) == 1  # added target started
    assert keys_before - {x["key"] for x in s.status()} == {"b:bestbuy:sku=2"}
    calls_b = [c for c in adapters["bestbuy"].calls if "sku=2" in c[1]]
    await vclock.advance(2000)
    assert [c for c in adapters["bestbuy"].calls if "sku=2" in c[1]] == calls_b
    # disabling via reload pauses without dropping state
    s.reload(make_config([bb_watch("a", "1", interval=120, enabled=False)]))
    n = len(adapters["bestbuy"].calls)
    await vclock.advance(2000)
    assert len(adapters["bestbuy"].calls) == n
    await s.stop()


# ------------------------------------------------------------------ resilience

async def test_loop_survives_adapter_exceptions(env, vclock):
    adapters, seen, make = env
    bb = adapters["bestbuy"]
    bb.script = [RuntimeError("boom"), ParseError("shape changed"), ValueError("x"), ok()]
    s = make(make_config([bb_watch(interval=60)]))
    await s.start()
    await vclock.advance(400)
    errors = [r.error for _, _, r in seen[:4]]
    assert errors == [ErrorKind.UNKNOWN, ErrorKind.PARSE_FAILED, ErrorKind.UNKNOWN, None]
    assert len(bb.calls) >= 5
    assert all(r.duration_ms is not None for _, _, r in seen)
    await s.stop()


async def test_loop_survives_on_result_exception(vclock):
    adapter = FakeAdapter("bestbuy", vclock)
    calls = 0

    async def bad_on_result(target, watch, result):
        nonlocal calls
        calls += 1
        raise RuntimeError("callback bug")

    s = Scheduler(make_config([bb_watch(interval=60)]), {"bestbuy": adapter}, bad_on_result,
                  clock=vclock, sleep=vclock.sleep, rng=random.Random(5))
    await s.start()
    await vclock.advance(400)
    assert calls >= 5 and len(adapter.calls) == calls
    assert all(st.task and not st.task.done() for st in s._states.values())
    await s.stop()


async def test_loop_survives_internal_error(env, vclock, monkeypatch):
    adapters, _, make = env
    s = make(make_config([bb_watch(interval=60)]))
    original = s.next_delay
    state = {"n": 0}

    def flaky(st):
        state["n"] += 1
        if state["n"] == 1:
            raise RuntimeError("bug")
        return original(st)

    monkeypatch.setattr(s, "next_delay", flaky)
    await s.start()
    await vclock.advance(300)
    assert len(adapters["bestbuy"].calls) >= 3
    await s.stop()


async def test_missing_adapter_and_timeout(vclock):
    seen = []

    async def on_result(t, w, r):
        seen.append(r)

    class Hang(FakeAdapter):
        async def check(self, target, home):
            await asyncio.Event().wait()

    cfg = make_config([bb_watch("a", "1"), bb_watch("b", "2", retailer="target")])
    s = Scheduler(cfg, {"target": Hang("target", vclock)}, on_result, clock=vclock,
                  sleep=vclock.sleep, rng=random.Random(2), check_timeout=0.01)
    (ra,) = await s.check_now("a")
    (rb,) = await s.check_now("b")
    assert ra.error == ErrorKind.CONFIG
    assert rb.error == ErrorKind.TIMEOUT


async def test_error_detail_redacts_secrets(env):
    adapters, seen, make = env
    adapters["bestbuy"].script = [RuntimeError(
        "ConnectError for url https://api.bestbuy.com/v1/products/1/stores.json?apiKey=SEKRET123&postalCode=1")]
    s = make(make_config([bb_watch()]))
    (r,) = await s.check_now("sw2")
    assert "SEKRET123" not in (r.error_detail or "")
    assert "apiKey=***" in r.error_detail
    assert redact_detail("token=abc&x=1") == "token=***&x=1"


async def test_status_snapshot(env, vclock):
    _, _, make = env
    s = make(make_config([bb_watch(interval=100)]))
    assert s.status()[0]["last_result"] is None and s.status()[0]["checking"] is False
    await s.start()
    await vclock.advance(0)
    (st,) = s.status()
    for k in ("last_check_at", "last_result", "interval", "blocked", "consecutive_failures", "paused",
              "watch_id", "retailer", "next_check_in"):
        assert k in st
    assert st["last_result"].raw_status == "OUT_OF_STOCK" and st["interval"] == 100
    await s.stop()
    assert all(x.task is None for x in s._states.values())
