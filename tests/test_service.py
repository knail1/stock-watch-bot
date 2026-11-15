"""End-to-end service tests: fake adapters + real StateEngine/Notifier/CartHelper + in-memory DB."""
from __future__ import annotations

import asyncio
import os
import signal
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

from stockwatch.adapters.base import BlockedError, ParseError
from stockwatch.config import ConfigStore, SecretResolver
from stockwatch.db import Database
from stockwatch.models import AlertOutcome, CartResult, ChannelAvail, Severity, StockResult, Target
from stockwatch.notify.notifier import Notifier
from stockwatch.service import StockWatchService, describe_result

SKU = "6614313"


def write_config(tmp_path: Path, **watch_overrides: Any) -> Path:
    watch = {
        "id": "switch2", "name": "Nintendo Switch 2", "max_price": 499.99,
        "fulfillment": ["store_pickup", "in_store", "ship"], "add_to_cart": True,
        "targets": [{"retailer": "bestbuy", "sku": SKU}],
    }
    watch.update(watch_overrides)
    raw = {
        "home": {"zip": "75001", "radius_miles": 25},
        "defaults": {"interval_seconds": 90, "quiet_hours": None},
        "notify": {
            "pushover": {"enabled": True, "user_key": "u" * 30, "app_token": "a" * 30},
            "telegram": {"enabled": True, "bot_token": "123:abc", "chat_id": "42"},
            "mac_sound": {"enabled": True},
        },
        "retailers": {"bestbuy": {"api_key": "k"}},
        "watches": [watch],
    }
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw, sort_keys=False))
    return path


class EventLog(list):
    pass


class FakeChannel:
    """Stands in for PushoverClient / TelegramClient."""

    def __init__(self, name: str, log: EventLog, delay: float = 0.0):
        self.name, self.log, self.delay = name, log, delay
        self.cfg: Any = None
        self.alerts: list[Any] = []

    def priority_for(self, severity: Severity) -> int:
        return 0

    async def send(self, alert: Any, *, priority: int | None = None) -> AlertOutcome:
        await asyncio.sleep(self.delay)
        self.alerts.append(alert)
        self.log.append(("alert", self.name, alert.title))
        return AlertOutcome(self.name, True, "")

    async def send_text(self, text: str, **_: Any) -> AlertOutcome:
        return AlertOutcome(self.name, True, "")


class FakeSound:
    def __init__(self, log: EventLog):
        self.log = log
        self.cfg: Any = None

    async def play(self, alert: Any) -> AlertOutcome:
        self.log.append(("sound", alert.title))
        return AlertOutcome("mac_sound", True, "")

    async def wait(self) -> None: ...
    async def stop(self) -> None: ...


class FakeBrowser:
    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class FakeAdapter:
    name = "bestbuy"
    min_interval_seconds = 60

    def __init__(self, log: EventLog, results: list[Any], cart: CartResult | None = None,
                 cart_delay: float = 0.0):
        self.log, self.results, self.cart, self.cart_delay = log, list(results), cart, cart_delay
        self.calls = 0
        self.settings = None

    async def check(self, target: Target, home: Any) -> StockResult:
        self.calls += 1
        r = self.results.pop(0) if len(self.results) > 1 else self.results[0]
        if isinstance(r, Exception):
            raise r
        return r

    async def add_to_cart(self, target: Target, result: StockResult, browser: Any,
                          channel: Any = None) -> CartResult:
        self.log.append(("cart_start",))
        await asyncio.sleep(self.cart_delay)
        self.log.append(("cart_done",))
        return self.cart or CartResult("bestbuy", True, "added", cart_url=self.cart_url(),
                                       cart_count_before=0, cart_count_after=1)

    def product_url(self, target: Target) -> str:
        return f"https://www.bestbuy.com/site/{target.get('sku')}.p?skuId={target.get('sku')}"

    def cart_url(self) -> str:
        return "https://www.bestbuy.com/cart"


def in_stock(price: float = 449.99, name: str | None = None) -> StockResult:
    return StockResult("bestbuy", True, price=price, raw_status="IN_STOCK", product_name=name, channels=[
        ChannelAvail("store_pickup", True, "281", "Frisco", 6.2, low_stock=True),
    ])


def out_of_stock() -> StockResult:
    return StockResult("bestbuy", False, raw_status="OUT_OF_STOCK", channels=[
        ChannelAvail("store_pickup", False, "281", "Frisco", 6.2)])


@pytest.fixture
async def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("STOCKWATCH_HOME", str(tmp_path / "home"))
    clients: list[httpx.AsyncClient] = []

    def make(results: list[Any], *, cart: CartResult | None = None, cart_delay: float = 0.0,
             push_delay: float = 0.0, adapters: dict[str, Any] | None = None, **overrides: Any):
        log = EventLog()
        path = write_config(tmp_path, **overrides)
        store = ConfigStore(path, resolver=SecretResolver(env_file=tmp_path / ".env", use_keychain=False))
        store.load()
        db = Database(":memory:")
        http = httpx.AsyncClient()
        clients.append(http)
        push, tg, sound = FakeChannel("pushover", log, push_delay), FakeChannel("telegram", log), FakeSound(log)
        notifier = Notifier(store.config, http, db, pushover=push, telegram=tg, mac_sound=sound)
        adapter = FakeAdapter(log, results, cart, cart_delay)
        svc = StockWatchService(
            store, db, http=http, browser=FakeBrowser(), adapters=adapters or {"bestbuy": adapter},
            notifier=notifier, scheduler_kwargs={"retailer_spacing": 0},
            enable_dashboard=False, enable_telegram=False, enable_health=False, enable_power=False,
        )
        return svc, adapter, push, log

    yield make
    for http in clients:  # injected clients are owned by the test, not the service
        await http.aclose()


async def test_restock_alert_sent_before_cart_and_followup(env) -> None:
    svc, adapter, push, log = env([in_stock()], push_delay=0.05, cart_delay=0.05)
    summary = await svc.check_now("switch2")
    assert "IN_STOCK" in summary
    # The check returned without waiting for the cart attempt.
    assert ("cart_done",) not in log
    await svc.wait_background(timeout=5)

    first_alert = next(i for i, e in enumerate(log) if e[0] == "alert")
    cart_start = log.index(("cart_start",))
    assert first_alert < cart_start, log
    # Both push channels delivered the critical alert before the cart started.
    assert {e[1] for e in log[:cart_start] if e[0] == "alert"} == {"pushover", "telegram"}

    critical, followup = push.alerts
    assert critical.severity is Severity.CRITICAL
    assert critical.title == "IN STOCK: Nintendo Switch 2 @ Best Buy"
    assert critical.body == "Pickup at Best Buy Frisco (6.2 mi), $449.99, low stock. Added to cart: trying…"
    assert critical.url.startswith("https://www.bestbuy.com/site/")
    assert followup.severity is Severity.INFO
    assert "Added to cart: YES" in followup.body
    assert followup.url == "https://www.bestbuy.com/cart"

    # One critical per restock event: polling again while in stock sends nothing new.
    await svc.check_now("switch2")
    await svc.wait_background(timeout=5)
    assert len(push.alerts) == 2
    events = svc.recent_events()
    assert [e.kind for e in events] == ["cart", "restock"]
    assert events[0].detail == "Added to cart: YES"
    await svc.stop()


async def test_signed_out_cart_sends_sign_in_warning(env) -> None:
    cart = CartResult("bestbuy", False, "signed out", signed_out=True)
    svc, _, push, _ = env([in_stock()], cart=cart)
    await svc.check_now("switch2")
    await svc.wait_background(timeout=5)
    followup = push.alerts[-1]
    assert followup.title == "Sign in needed for Best Buy"
    assert followup.severity is Severity.WARNING
    assert followup.url.startswith("https://www.bestbuy.com/site/")
    await svc.stop()


async def test_no_cart_when_disabled_or_over_max_price(env) -> None:
    svc, _, push, log = env([in_stock(price=599.0)])
    await svc.check_now("switch2")
    await svc.wait_background(timeout=5)
    assert ("cart_start",) not in log
    (alert,) = push.alerts
    assert alert.severity is Severity.INFO
    assert "Over your max price of $499.99" in alert.body
    assert "Added to cart" not in alert.body
    await svc.stop()

    svc, _, push, log = env([in_stock()], add_to_cart=False)
    await svc.check_now("switch2")
    await svc.wait_background(timeout=5)
    assert ("cart_start",) not in log
    assert "Added to cart" not in push.alerts[0].body
    await svc.stop()


async def test_403_sends_one_low_priority_notice_and_backs_off(env) -> None:
    svc, adapter, push, log = env([BlockedError("HTTP 403")])
    base = svc.scheduler.status()[0]["interval"]
    for _ in range(3):
        await svc.check_now("switch2")
    assert adapter.calls == 3  # one request per check: never retried immediately
    assert len(push.alerts) == 1
    notice = push.alerts[0]
    assert notice.severity is Severity.LOW
    assert "paused" in notice.title and "check manually" in notice.body
    assert notice.url.startswith("https://www.bestbuy.com/site/")
    st = svc.scheduler.status()[0]
    assert st["blocked"] is True
    assert st["interval"] == min(base * 8, 1800)

    # Recovery closes the blocked event, resets the interval and sends one info notice.
    adapter.results = [out_of_stock()]
    await svc.check_now("switch2")
    assert svc.scheduler.status()[0]["interval"] == base
    assert push.alerts[-1].severity is Severity.INFO and "recovered" in push.alerts[-1].title
    await svc.stop()


async def test_parse_error_sends_adapter_broken_once(env) -> None:
    svc, _, push, _ = env([ParseError("shape changed")])
    await svc.check_now("switch2")
    await svc.check_now("switch2")
    assert [a.title for a in push.alerts] == ["Best Buy adapter broken"]
    assert push.alerts[0].severity is Severity.WARNING
    await svc.stop()


async def test_failure_warning_after_threshold(env) -> None:
    from stockwatch.adapters.base import AdapterError
    from stockwatch.models import ErrorKind

    svc, _, push, _ = env([AdapterError("HTTP 500", kind=ErrorKind.HTTP_ERROR)])
    for _ in range(6):
        await svc.check_now("switch2")
    assert len(push.alerts) == 1
    assert push.alerts[0].severity is Severity.WARNING
    assert "checks failing" in push.alerts[0].title
    await svc.stop()


async def test_nintendo_queue_info_once_per_episode(env, tmp_path: Path) -> None:
    log = EventLog()
    queue = StockResult("nintendo", False, raw_status="QUEUE", error_detail="Queue running")
    nin = FakeAdapter(log, [queue, queue, StockResult("nintendo", False, raw_status="OUT_OF_STOCK"), queue])
    nin.name = "nintendo"
    nin.product_url = lambda t: "https://www.nintendo.com/us/store/products/x/"  # type: ignore[method-assign]
    svc, _, push, _ = env([out_of_stock()], adapters={"nintendo": nin},
                          targets=[{"retailer": "nintendo", "url": "https://www.nintendo.com/us/store/products/x/"}])
    for _ in range(4):
        await svc.check_now("switch2")
    titles = [a.title for a in push.alerts]
    assert titles == ["Nintendo Store queue / waiting room: Nintendo Switch 2"] * 2
    assert all(a.severity is Severity.INFO for a in push.alerts)
    assert svc.scheduler.status()[0]["consecutive_failures"] == 0
    await svc.stop()


async def test_add_watch_from_url_writes_config(env) -> None:
    svc, adapter, push, _ = env([out_of_stock()])
    adapter.results = [StockResult("bestbuy", False, raw_status="OUT_OF_STOCK", product_name="Switch 2 Mario Kart Bundle")]
    wid = await svc.add_watch_from_url("https://www.bestbuy.com/site/nintendo-switch-2/6620000.p?skuId=6620000")
    assert wid == "bestbuy-6620000"
    on_disk = yaml.safe_load(svc.store.path.read_text())
    w = next(w for w in on_disk["watches"] if w["id"] == wid)
    assert w["name"] == "Switch 2 Mario Kart Bundle"
    assert w["targets"][0]["retailer"] == "bestbuy" and w["targets"][0]["sku"] == "6620000"
    # Live: scheduled, recorded in the DB, and the dry check counts as the first check.
    st = next(s for s in svc.scheduler.status() if s["watch_id"] == wid)
    assert st["last_result"] is not None and st["next_check_in"] > 0
    assert svc.recent_checks(wid)[0].raw_status == "OUT_OF_STOCK"

    with pytest.raises(ValueError):
        await svc.add_watch_from_url("https://www.bestbuy.com/site/x/6620000.p?skuId=6620000")
    with pytest.raises(ValueError):
        await svc.add_watch_from_url("https://example.com/item/1")
    await svc.stop()


async def test_add_watch_from_url_keeps_secret_refs(env, tmp_path: Path) -> None:
    svc, _, _, _ = env([out_of_stock()])
    raw = yaml.safe_load(svc.store.path.read_text())
    raw["notify"]["pushover"]["user_key"] = "${PUSHOVER_USER_KEY}"
    svc.store.path.write_text(yaml.safe_dump(raw))
    svc.reload_config()
    await svc.add_watch_from_url("https://www.bestbuy.com/site/x/6620001.p?skuId=6620001")
    assert "${PUSHOVER_USER_KEY}" in svc.store.path.read_text()
    await svc.stop()


async def test_dashboard_control_pause_and_views(env) -> None:
    svc, _, _, _ = env([out_of_stock()])
    await svc.check_now("switch2")
    (w,) = svc.watches()
    assert w.id == "switch2" and w.targets[0].last_result == "OUT_OF_STOCK"
    assert w.status == "green"

    await svc.set_paused("switch2", True)
    assert yaml.safe_load(svc.store.path.read_text())["watches"][0]["enabled"] is False
    assert svc.watches()[0].status == "grey"
    await svc.resume("switch2")
    assert svc.config.watch("switch2").enabled is True
    with pytest.raises(KeyError):
        await svc.pause("nope")
    with pytest.raises(KeyError):
        await svc.check_now("nope")

    await svc.update_settings("75002", 10, "23:00-06:00")
    assert svc.config.home.zip == "75002" and svc.config.defaults.quiet_hours == "23:00-06:00"
    with pytest.raises(ValueError):
        await svc.update_settings("abc", 10, None)

    text = await svc.status_text()
    assert "switch2" in text and "Best Buy" in text
    s = svc.settings()
    assert [c.name for c in s.channels] == ["pushover", "telegram", "mac_sound"]

    await svc.delete_watch("switch2")
    assert svc.config.watches == [] and svc.scheduler.status() == []
    await svc.stop()


async def test_test_alert_summary(env) -> None:
    svc, _, push, _ = env([out_of_stock()])
    text = await svc.test_alert()
    assert "pushover: ok" in text and "telegram: ok" in text
    assert push.alerts[0].severity is Severity.CRITICAL
    await svc.stop()


async def test_run_stops_on_sigterm(env) -> None:
    svc, _, _, _ = env([out_of_stock()])
    task = asyncio.create_task(svc.run())
    await asyncio.sleep(0.05)
    assert svc.scheduler._running
    os.kill(os.getpid(), signal.SIGTERM)
    await asyncio.wait_for(task, 5)
    assert not svc.scheduler._running
    assert svc.browser.closed


def test_describe_result() -> None:
    assert describe_result(in_stock()) == "IN_STOCK $449.99 - Pickup Frisco (6.2 mi)"
    assert describe_result(StockResult.failure("bestbuy", "blocked", "HTTP 403")) == "BLOCKED: HTTP 403"


async def test_telegram_bot_drives_service(env) -> None:
    from stockwatch.telegram_bot import TelegramBot

    svc, adapter, push, _ = env([out_of_stock()])

    class Client:
        async def get_updates(self, offset=None, timeout=30):  # pragma: no cover - not polled here
            return []

        async def send_text(self, text: str) -> None: ...

    bot = TelegramBot(svc, Client(), "42")
    assert "Best Buy: OUT_OF_STOCK" in await bot.execute("/check switch2")
    assert "switch2" in await bot.execute("/status")
    await bot.execute("/pause switch2")
    assert svc.config.watch("switch2").enabled is False
    await bot.execute("/resume switch2")
    assert svc.config.watch("switch2").enabled is True
    assert "Unknown watch" in await bot.execute("/pause nope")
    adapter.results = [StockResult("bestbuy", False, raw_status="OUT_OF_STOCK", product_name="Zelda")]
    reply = await bot.execute("/add https://www.bestbuy.com/site/zelda/6630000.p?skuId=6630000")
    assert "bestbuy-6630000" in reply
    assert svc.config.watch("bestbuy-6630000").name == "Zelda"
    await svc.stop()


def test_dashboard_renders_real_service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi.testclient import TestClient

    from stockwatch.dashboard import create_app

    monkeypatch.setenv("STOCKWATCH_HOME", str(tmp_path / "home"))
    log = EventLog()
    path = write_config(tmp_path)
    store = ConfigStore(path, resolver=SecretResolver(env_file=tmp_path / ".env", use_keychain=False))
    store.load()
    svc = StockWatchService(store, Database(":memory:"), http=httpx.AsyncClient(), browser=FakeBrowser(),
                            adapters={"bestbuy": FakeAdapter(log, [out_of_stock()])},
                            notifier=Notifier(store.config, httpx.AsyncClient(), None,
                                              pushover=FakeChannel("pushover", log),
                                              telegram=FakeChannel("telegram", log), mac_sound=FakeSound(log)),
                            enable_dashboard=False, enable_telegram=False, enable_health=False, enable_power=False)
    client = TestClient(create_app(svc, pin="246810"))
    for page in ("/", "/watches/switch2", "/events", "/watches/new"):
        r = client.get(f"{page}?token=246810")
        assert r.status_code == 200, page
    assert "Nintendo Switch 2" in client.get("/?token=246810").text


async def test_status_notices_off_keeps_paused_and_recovered_off_the_phone(env) -> None:
    svc, adapter, push, _ = env([BlockedError("HTTP 403")])
    svc.config.notify.status_notices = False
    await svc.check_now("switch2")
    assert svc.scheduler.status()[0]["blocked"] is True  # still tracked (dashboard shows it)
    adapter.results = [out_of_stock()]
    await svc.check_now("switch2")
    assert push.alerts == []
    # Real stock still alerts.
    adapter.results = [in_stock()]
    await svc.check_now("switch2")
    await svc.wait_background(timeout=5)
    assert push.alerts and push.alerts[0].severity is Severity.CRITICAL
    await svc.stop()


def test_status_notices_config_parses(tmp_path) -> None:
    import yaml as _yaml

    path = write_config(tmp_path)
    raw = _yaml.safe_load(path.read_text())
    store = ConfigStore(path, resolver=SecretResolver(env_file=tmp_path / ".env", use_keychain=False))
    assert store.load().notify.status_notices is True
    raw["notify"]["status_notices"] = False
    path.write_text(_yaml.safe_dump(raw))
    assert store.load().notify.status_notices is False
