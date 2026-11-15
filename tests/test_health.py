"""Tests for heartbeat, daily summary, pruning and the caffeinate power assertion."""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

import httpx
import pytest
import respx

from stockwatch.config import SecretResolver, build_config
from stockwatch.db import Database
from stockwatch.health import HealthMonitor, PowerAssertion, next_run_at, parse_hhmm
from stockwatch.models import StockResult, Target

HB_URL = "https://hc-ping.com/0f3c2a5e-1b2c-4d5e-9f00-123456789abc"


def make_config(**health):
    raw = {
        "home": {"zip": "75034"},
        "health": {"heartbeat_url": HB_URL, **health},
        "watches": [
            {"id": "sw2", "name": "Switch 2", "targets": [{"retailer": "bestbuy", "sku": "1"}]},
            {"id": "old", "name": "Old", "enabled": False},
        ],
    }
    return build_config(raw, SecretResolver(env_file=Path("/nonexistent"), use_keychain=False))


@pytest.fixture
def db():
    d = Database(":memory:")
    yield d
    d.close()


@pytest.fixture
def http():
    return httpx.AsyncClient()


@respx.mock
async def test_heartbeat_success(db, http) -> None:
    route = respx.get(HB_URL).mock(return_value=httpx.Response(200, text="OK"))
    mon = HealthMonitor(make_config(), db, http)
    assert await mon.heartbeat_once() is True
    assert route.called and mon.last_heartbeat_ok is True


@respx.mock
@pytest.mark.parametrize("response", [httpx.Response(500), httpx.ConnectError("conn to " + HB_URL)])
async def test_heartbeat_failure_does_not_log_url(db, http, caplog, response) -> None:
    route = respx.get(HB_URL)
    if isinstance(response, Exception):
        route.mock(side_effect=response)
    else:
        route.mock(return_value=response)
    mon = HealthMonitor(make_config(), db, http)
    with caplog.at_level(logging.WARNING, logger="stockwatch.health"):
        assert await mon.heartbeat_once() is False
    assert "heartbeat ping failed" in caplog.text
    assert "hc-ping" not in caplog.text and "0f3c2a5e" not in caplog.text
    assert mon.last_heartbeat_ok is False


async def test_heartbeat_disabled_without_url(db, http) -> None:
    mon = HealthMonitor(make_config(heartbeat_url=""), db, http)
    assert await mon.heartbeat_once() is False


@respx.mock
async def test_heartbeat_loop_uses_interval(db, http, monkeypatch) -> None:
    respx.get(HB_URL).mock(return_value=httpx.Response(200))
    sleeps: list[float] = []
    real_sleep = asyncio.sleep

    async def fake_sleep(s: float) -> None:
        sleeps.append(s)
        if len(sleeps) >= 2:
            raise asyncio.CancelledError
        await real_sleep(0)

    monkeypatch.setattr("stockwatch.health.asyncio.sleep", fake_sleep)
    mon = HealthMonitor(make_config(), db, http)
    with pytest.raises(asyncio.CancelledError):
        await mon._heartbeat_loop()
    assert sleeps == [300, 300]


def test_summary_counts(db, http) -> None:
    cfg = make_config()
    t = Target(retailer="bestbuy", identifiers={"sku": "1"}, watch_id="sw2")
    db.sync_watch("sw2", "Switch 2", True, {})
    tid = db.ensure_target(t)
    now = datetime.now(timezone.utc)
    db.record_check(tid, StockResult(retailer="bestbuy", in_stock=False, checked_at=now - timedelta(hours=1)))
    db.record_check(tid, StockResult.failure("bestbuy", "network"))
    db.record_check(tid, StockResult(retailer="bestbuy", in_stock=False, checked_at=now - timedelta(days=2)))
    db.open_event(tid, "restock", {}, started_at=now - timedelta(hours=2))
    db.open_event(tid, "restock", {}, started_at=now - timedelta(days=3))
    db.open_event(tid, "blocked", {}, started_at=now - timedelta(hours=3))
    text = HealthMonitor(cfg, db, http).build_summary(now.astimezone())
    assert "Checks run (24h): 2" in text
    assert "Errors (24h): 1" in text
    assert "Watches active: 1 of 2" in text
    assert "Restock events (24h): 1" in text
    assert "Blocked events (24h): 1" in text


async def test_send_daily_summary(db, http) -> None:
    sent: list[str] = []

    async def send_text(text: str) -> None:
        sent.append(text)

    mon = HealthMonitor(make_config(), db, http, send_text=send_text)
    assert await mon.send_daily_summary() is True
    assert sent and sent[0].startswith("StockWatch daily summary")
    assert await HealthMonitor(make_config(), db, http).send_daily_summary() is False


async def test_send_daily_summary_failure_is_swallowed(db, http) -> None:
    async def boom(text: str) -> None:
        raise RuntimeError("telegram down")

    assert await HealthMonitor(make_config(), db, http, send_text=boom).send_daily_summary() is False


async def test_prune_removes_old_checks(db, http) -> None:
    t = Target(retailer="bestbuy", identifiers={"sku": "1"}, watch_id="sw2")
    db.sync_watch("sw2", "Switch 2", True, {})
    tid = db.ensure_target(t)
    now = datetime.now(timezone.utc)
    db.record_check(tid, StockResult(retailer="bestbuy", in_stock=False, checked_at=now - timedelta(days=30)))
    db.record_check(tid, StockResult(retailer="bestbuy", in_stock=False, checked_at=now))
    assert await HealthMonitor(make_config(), db, http).prune() == 1
    assert len(db.recent_checks(tid)) == 1


def test_schedule_math(db, http) -> None:
    tz = timezone(timedelta(hours=-6))
    assert next_run_at(datetime(2026, 1, 1, 8, 0, tzinfo=tz), time(9)) == datetime(2026, 1, 1, 9, 0, tzinfo=tz)
    assert next_run_at(datetime(2026, 1, 1, 9, 0, tzinfo=tz), time(9)) == datetime(2026, 1, 2, 9, 0, tzinfo=tz)
    assert parse_hhmm("21:30") == time(21, 30)
    assert parse_hhmm("garbage") == time(9, 0)
    mon = HealthMonitor(make_config(daily_summary_time="09:00"), db, http,
                        clock=lambda: datetime(2026, 1, 1, 8, 30, tzinfo=tz))
    assert mon.seconds_until_summary() == 1800


async def test_start_stop(db, http) -> None:
    mon = HealthMonitor(make_config(heartbeat_url=""), db, http)
    await mon.start()
    assert len(mon._tasks) == 2
    await mon.stop()
    assert mon._tasks == []


class FakeProc:
    def __init__(self, cmd, **kw) -> None:
        self.cmd = cmd
        self.pid = 4242
        self.returncode: int | None = None
        self.terminated = False

    def poll(self):
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15

    def wait(self, timeout=None):
        return self.returncode

    def kill(self) -> None:
        self.returncode = -9


def test_power_assertion_spawns_and_terminates_caffeinate() -> None:
    spawned: list[FakeProc] = []

    def popen(cmd, **kw):
        spawned.append(FakeProc(cmd, **kw))
        return spawned[-1]

    pa = PowerAssertion(platform="darwin", popen=popen, which=lambda n: "/usr/bin/caffeinate")
    assert pa.start() is True and pa.active
    assert pa.start() is True and len(spawned) == 1  # idempotent
    assert spawned[0].cmd == ["/usr/bin/caffeinate", "-i", "-w", str(os.getpid())]
    pa.stop()
    assert spawned[0].terminated and not pa.active


def test_power_assertion_noop_off_darwin() -> None:
    def popen(*a, **kw):  # pragma: no cover - must not be called
        raise AssertionError("should not spawn")

    pa = PowerAssertion(platform="linux", popen=popen)
    assert pa.start() is False
    pa.stop()
    assert PowerAssertion(enabled=False, platform="darwin", popen=popen).start() is False


@pytest.mark.skipif(not Path("/usr/bin/caffeinate").exists(), reason="macOS only")
def test_power_assertion_real_caffeinate() -> None:
    with PowerAssertion() as pa:
        assert pa.active
        proc = pa.proc
    assert proc is not None and proc.poll() is not None
