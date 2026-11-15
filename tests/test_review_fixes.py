"""Regression tests for the verified review findings (one section per finding)."""
from __future__ import annotations

import asyncio
import os
import stat
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

from stockwatch.adapters import bestbuy as bb
from stockwatch.adapters import target as tgt
from stockwatch.adapters.base import AdapterError, ParseError, parse_price
from stockwatch.adapters.bestbuy import BestBuyAdapter
from stockwatch.adapters.gamestop import GameStopAdapter
from stockwatch.adapters.nintendo import NintendoAdapter, parse_nintendo_page
from stockwatch.adapters.registry import identifiers_from_url
from stockwatch.adapters.target import TargetAdapter, parse_fulfillment
from stockwatch.adapters.walmart import parse_walmart
from stockwatch.browser import BrowserManager
from stockwatch.cart.helper import CartHelper
from stockwatch.config import (
    BrowserCfg,
    ConfigError,
    ConfigStore,
    SecretResolver,
    WatchCfg,
    build_config,
)
from stockwatch.dashboard.server import BindError, check_bind_safety, resolve_bind_hosts, supervise
from stockwatch.db import Database
from stockwatch.engine.scheduler import Scheduler
from stockwatch.engine.state import MAX_CRITICAL_ATTEMPTS, StateEngine, qualifying_channels
from stockwatch.models import AlertOutcome, CartResult, ChannelAvail, Home, Severity, StockResult, Target
from stockwatch.service import critical_delivered
from tests.test_browser import FakeContext, FakePlaywright
from tests.test_state import HOME, Runner, instock, store

FIXTURES = Path(__file__).parent / "fixtures"
NO_SECRETS = SecretResolver(env_file=Path("/nonexistent"), use_keychain=False)


def base_raw(**watch: Any) -> dict[str, Any]:
    w = {"id": "w", "name": "Switch", "targets": [{"retailer": "bestbuy", "sku": "6614313"}]}
    w.update(watch)
    return {"home": {"zip": "75034"}, "watches": [w]}


# ------------------------------------------------------------------ 1. cart uses the matched channel/store

class RecordingAdapter:
    name = "bestbuy"

    def __init__(self) -> None:
        self.channels: list[ChannelAvail | None] = []

    def cart_url(self) -> str:
        return "https://www.bestbuy.com/cart"

    async def add_to_cart(self, target: Target, result: StockResult, browser: Any,
                          channel: ChannelAvail | None = None) -> CartResult:
        self.channels.append(channel)
        return CartResult("bestbuy", True, "added")


def mixed_result() -> StockResult:
    return StockResult("bestbuy", True, channels=[
        ChannelAvail("store_pickup", True, "281", "Frisco", 6.2),
        ChannelAvail("store_pickup", True, "999", "Plano", 3.0),
        ChannelAvail("ship", True),
    ])


async def test_cart_helper_passes_explicit_channel() -> None:
    a = RecordingAdapter()
    chosen = ChannelAvail("store_pickup", True, "281", "Frisco", 6.2)
    await CartHelper(object(), {"bestbuy": a}).try_add(  # type: ignore[arg-type]
        Target("bestbuy", {"sku": "1"}), WatchCfg(id="w", name="w"), mixed_result(), channel=chosen)
    assert a.channels == [chosen]


async def test_cart_helper_honours_fulfillment_and_store_ids() -> None:
    a = RecordingAdapter()
    h = CartHelper(object(), {"bestbuy": a})  # type: ignore[arg-type]
    await h.try_add(Target("bestbuy", {"sku": "1"}), WatchCfg(id="w", name="w", fulfillment=["ship"]),
                    mixed_result())
    assert a.channels[-1].channel == "ship"
    await h.try_add(Target("bestbuy", {"sku": "1"}, store_ids=["281"]), WatchCfg(id="w", name="w"),
                    mixed_result())
    assert a.channels[-1].store_id == "281"  # nearer Plano is not an allowed store


def test_engine_primary_channel_follows_fulfillment_priority() -> None:
    watch = WatchCfg(id="w", name="w", fulfillment=["ship", "store_pickup"])
    q = qualifying_channels(mixed_result(), watch, HOME)
    assert q[0].channel == "ship"
    q = qualifying_channels(mixed_result(), WatchCfg(id="w", name="w"), HOME)
    assert (q[0].channel, q[0].store_name) == ("store_pickup", "Plano")  # nearest store first


async def test_target_explicit_ship_channel_never_adds_pickup() -> None:
    from tests.test_adapter_target import FakeBrowser, make_page

    page = make_page()
    page.elements[tgt.SEL_SHIPPING_BUTTON].on_click = \
        lambda: page.elements[tgt.SEL_CART_LINK].attrs.update({"aria-label": "cart 1 item"})
    res = await TargetAdapter(http=httpx.AsyncClient()).add_to_cart(
        Target("target", {"tcin": "94693225"}), mixed_result(), FakeBrowser(page=page),
        channel=ChannelAvail("ship", True))
    assert res.success and "shipping" in res.message
    assert tgt.SEL_PICKUP_BUTTON not in page.clicked


async def test_bestbuy_ship_channel_and_store_mismatch_note(monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.test_adapter_bestbuy import FakeBrowser, make_page

    monkeypatch.setattr(bb, "CART_POLL_INTERVAL_S", 0)
    page, els = make_page()
    res = await BestBuyAdapter(http=None).add_to_cart(  # type: ignore[arg-type]
        Target("bestbuy", {"sku": "6614313"}), mixed_result(), FakeBrowser(page), channel=ChannelAvail("ship", True))
    assert res.success and els["pickup"].clicks == 0 and "shipping" in res.message

    page, els = make_page()
    res = await BestBuyAdapter(http=None).add_to_cart(  # type: ignore[arg-type]
        Target("bestbuy", {"sku": "6614313"}), mixed_result(), FakeBrowser(page),
        channel=ChannelAvail("store_pickup", True, "281", "Frisco", 6.2))
    # The page does not show Frisco: the message must say the account's store was used.
    assert res.success and els["pickup"].clicks == 1
    assert "may not be Frisco" in res.message


# ------------------------------------------------------------------ 2. online-only severity

def test_online_only_is_info_by_default_but_nintendo_critical() -> None:
    cfg = build_config(base_raw(), NO_SECRETS)
    w = cfg.watches[0]
    assert cfg.online_only_severity(w, "bestbuy") == "info"
    assert cfg.online_only_severity(w, "nintendo") == "critical"
    w.fulfillment = ["ship", "store_pickup"]
    assert cfg.online_only_severity(w, "walmart") == "critical"


def test_online_only_overrides() -> None:
    raw = base_raw()
    raw["retailers"] = {"nintendo": {"online_severity": "info"}}
    cfg = build_config(raw, NO_SECRETS)
    assert cfg.online_only_severity(cfg.watches[0], "nintendo") == "info"
    raw = base_raw(online_only_severity="critical")
    cfg = build_config(raw, NO_SECRETS)
    assert cfg.online_only_severity(cfg.watches[0], "target") == "critical"
    with pytest.raises(ConfigError):
        build_config(base_raw(online_only_severity="loud"), NO_SECRETS)


# ------------------------------------------------------------------ 3. dashboard supervisor

async def test_supervisor_retries_until_tailscale_appears_and_rebinds() -> None:
    answers = [[], [], ["100.64.0.5"], ["100.64.0.5"], ["100.64.0.9"]]
    binds: list[list[str]] = []
    rebound = asyncio.Event()

    async def resolve() -> list[str]:
        return answers.pop(0) if len(answers) > 1 else answers[0]

    async def fake_serve(app: Any, hosts: list[str], port: int) -> None:
        binds.append(hosts)
        if hosts == ["100.64.0.9"]:
            rebound.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(supervise(None, resolve, 8787, retry_seconds=0.001, refresh_seconds=0.001,
                                         serve_fn=fake_serve))
    await asyncio.wait_for(rebound.wait(), 2)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert binds == [["100.64.0.5"], ["100.64.0.9"]]


async def test_supervisor_retries_after_bind_failure() -> None:
    calls = 0
    ok = asyncio.Event()

    async def resolve() -> list[str]:
        return ["127.0.0.1"]

    async def flaky_serve(app: Any, hosts: list[str], port: int) -> None:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise OSError("address not available")
        ok.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(supervise(None, resolve, 8787, retry_seconds=0.001, refresh_seconds=1,
                                         serve_fn=flaky_serve))
    await asyncio.wait_for(ok.wait(), 2)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert calls == 3


# ------------------------------------------------------------------ 4/5. alert ordering and critical retry

@pytest.fixture
def engine_env():
    db = Database(":memory:")
    db.sync_watch("sw2", "Switch 2", True, {})
    t = Target(retailer="bestbuy", identifiers={"sku": "123"}, watch_id="sw2")
    db.ensure_target(t)
    yield db, t, WatchCfg(id="sw2", name="Switch 2", targets=[t])
    db.close()


def test_failed_critical_is_resent_on_next_check(engine_env) -> None:
    db, t, w = engine_env
    engine = StateEngine(db)
    run = Runner(engine, t, w, max_price=500)
    first = run(instock(store()))
    assert first[0].is_critical
    assert engine.critical_delivery_failed(t, first[0].event_id)
    again = run(instock(store()))  # same store, same price: still re-sent as CRITICAL
    assert [d.severity for d in again] == [Severity.CRITICAL]
    assert again[0].reason == "critical alert retry"
    assert run(instock(store())) == []  # delivered now: no repeat


def test_critical_retry_is_capped(engine_env) -> None:
    db, t, w = engine_env
    engine = StateEngine(db)
    run = Runner(engine, t, w, max_price=500)
    d = run(instock(store()))[0]
    for _ in range(MAX_CRITICAL_ATTEMPTS - 1):
        assert engine.critical_delivery_failed(t, d.event_id)
        d = run(instock(store()))[0]
        assert d.is_critical
    assert not engine.critical_delivery_failed(t, d.event_id)
    assert run(instock(store())) == []


def test_critical_delivered_rules() -> None:
    ok, bad = (lambda c: AlertOutcome(c, True)), (lambda c: AlertOutcome(c, False))
    assert critical_delivered([])
    assert not critical_delivered([bad("pushover"), ok("telegram"), ok("mac_sound")])
    assert critical_delivered([ok("pushover"), bad("telegram")])
    assert critical_delivered([ok("telegram"), bad("mac_sound")])
    assert not critical_delivered([bad("telegram"), ok("mac_sound")])
    assert critical_delivered([ok("mac_sound")])
    assert not critical_delivered([bad("notifier")])


async def test_service_resends_critical_when_pushover_failed(tmp_path: Path, monkeypatch) -> None:
    from stockwatch.notify.notifier import Notifier
    from stockwatch.service import StockWatchService
    from tests.test_service import EventLog, FakeAdapter, FakeBrowser, FakeChannel, FakeSound, in_stock, write_config

    monkeypatch.setenv("STOCKWATCH_HOME", str(tmp_path / "home"))
    log = EventLog()
    store_ = ConfigStore(write_config(tmp_path, add_to_cart=False),
                         resolver=SecretResolver(env_file=tmp_path / ".env", use_keychain=False))
    store_.load()
    db = Database(":memory:")
    async with httpx.AsyncClient() as http:
        push = FakeChannel("pushover", log)
        failures = [True]
        real_send = push.send

        async def flaky(alert: Any, *, priority: int | None = None) -> AlertOutcome:
            if failures and alert.severity is Severity.CRITICAL:
                failures.pop()
                return AlertOutcome("pushover", False, "HTTP 500")
            return await real_send(alert, priority=priority)

        push.send = flaky  # type: ignore[method-assign]
        notifier = Notifier(store_.config, http, db, pushover=push, telegram=FakeChannel("telegram", log),
                            mac_sound=FakeSound(log))
        svc = StockWatchService(store_, db, http=http, browser=FakeBrowser(),
                                adapters={"bestbuy": FakeAdapter(log, [in_stock()])}, notifier=notifier,
                                scheduler_kwargs={"retailer_spacing": 0}, enable_dashboard=False,
                                enable_telegram=False, enable_health=False, enable_power=False)
        await svc.check_now("switch2")
        assert push.alerts == []  # first critical failed on Pushover
        await svc.check_now("switch2")
        assert [a.severity for a in push.alerts] == [Severity.CRITICAL]
        await svc.check_now("switch2")
        assert len(push.alerts) == 1
        await svc.stop()


# ------------------------------------------------------------------ 6/8/9. browser lifecycle

class SlowChromium:
    def __init__(self, delay: float) -> None:
        self.delay = delay
        self.launches: list[FakeContext] = []

    async def launch_persistent_context(self, **kw: Any) -> FakeContext:
        await asyncio.sleep(self.delay)
        ctx = FakeContext(kw)
        self.launches.append(ctx)
        return ctx


def manager(tmp_path: Path, pw: Any, **kw: Any) -> BrowserManager:
    async def starter() -> Any:
        return pw
    return BrowserManager(BrowserCfg(headless=True), profile_dir=tmp_path / "profile",
                          playwright_starter=starter, **kw)


async def test_cancelled_start_does_not_orphan_launch(tmp_path: Path) -> None:
    pw = FakePlaywright()
    pw.chromium = SlowChromium(0.05)  # type: ignore[assignment]
    bm = manager(tmp_path, pw)
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(bm.start(), 0.01)
    await asyncio.sleep(0.1)
    assert bm.started  # the launch finished and was recorded although its caller timed out
    await bm.start()
    assert len(pw.chromium.launches) == 1  # no second Chrome on the same profile


async def test_restart_with_stale_context_keeps_newer_one(tmp_path: Path) -> None:
    pw = FakePlaywright()
    bm = manager(tmp_path, pw)
    old = await bm.start()
    new = await bm.restart(stale=old)
    assert new is not old and old.closed
    again = await bm.restart(stale=old)  # a second task that saw the same dead context
    assert again is new and not new.closed
    assert len(pw.chromium.launches) == 2


async def test_closed_manager_never_relaunches(tmp_path: Path) -> None:
    pw = FakePlaywright()
    bm = manager(tmp_path, pw)
    await bm.page_for("walmart")
    await bm.close()
    with pytest.raises(AdapterError):
        await bm.page_for("walmart")
    assert len(pw.chromium.launches) == 1


async def test_close_during_launch_closes_the_new_context(tmp_path: Path) -> None:
    pw = FakePlaywright()
    pw.chromium = SlowChromium(0.05)  # type: ignore[assignment]
    bm = manager(tmp_path, pw)
    starter = asyncio.create_task(bm.start())
    await asyncio.sleep(0.01)
    await bm.close()
    with pytest.raises(AdapterError):
        await starter
    assert all(c.closed for c in pw.chromium.launches) and not bm.started


async def test_scheduler_check_after_stop_is_discarded() -> None:
    cfg = build_config(base_raw(), NO_SECRETS)

    class Adapter:
        calls = 0

        async def check(self, target: Target, home: Home) -> StockResult:
            Adapter.calls += 1
            return StockResult("bestbuy", False)

    seen: list[Any] = []

    async def on_result(*a: Any) -> None:
        seen.append(a)

    s = Scheduler(cfg, {"bestbuy": Adapter()}, on_result, retailer_spacing=0)
    await s.stop()
    assert await s.check_now("w") == []
    assert Adapter.calls == 0 and seen == []


async def test_dashboard_spawn_goes_through_service(monkeypatch) -> None:
    from tests.test_dashboard import FakeControl, csrf, login, make_client

    control = FakeControl()
    spawned: list[str] = []

    def spawn_background(coro: Any, name: str = "") -> None:
        spawned.append(name)
        coro.close()

    control.spawn_background = spawn_background  # type: ignore[attr-defined]
    c = make_client(control)
    login(c)
    wid = control.watches()[0].id
    r = c.post(f"/watches/{wid}/check", data={"csrf": csrf(c, f"/watches/{wid}")})
    assert r.status_code == 303 and spawned == ["dashboard-task"]


# ------------------------------------------------------------------ 7. sign-in status: cached, gated, no launch

async def test_signin_status_cached_gated_and_never_launches(tmp_path: Path) -> None:
    pw = FakePlaywright()
    now = [0.0]
    calls: list[str] = []
    gated: list[str] = []

    async def hook(page: Any) -> bool:
        calls.append("probe")
        return True

    bm = manager(tmp_path, pw, signin_checks={"bestbuy": hook}, clock=lambda: now[0])

    class Gate:
        def __init__(self, name: str) -> None:
            self.name = name

        async def __aenter__(self) -> None:
            gated.append(self.name)

        async def __aexit__(self, *a: Any) -> None:
            return None

    bm.request_gate = Gate
    assert await bm.signin_status(["bestbuy"], launch=False) == {"bestbuy": None}
    assert pw.chromium.launches == []  # not started by a status request
    await bm.start()
    assert await bm.signin_status(["bestbuy"]) == {"bestbuy": True}
    assert await bm.signin_status(["bestbuy"]) == {"bestbuy": True}
    assert calls == ["probe"] and gated == ["bestbuy"]  # second call served from cache
    now[0] += 601
    assert bm.signin_stale(["bestbuy"]) == ["bestbuy"]
    await bm.signin_status(["bestbuy"])
    assert calls == ["probe", "probe"]


# ------------------------------------------------------------------ 10. ${VAR} never expanded in watches

def test_var_refs_rejected_in_watches_but_expanded_in_settings(monkeypatch) -> None:
    monkeypatch.setenv("FOO_SECRET", "hunter2")
    raw = base_raw(name="leak ${FOO_SECRET}")
    with pytest.raises(ConfigError):
        build_config(raw, SecretResolver(env_file=Path("/nonexistent"), use_keychain=False))
    raw = base_raw()
    raw["notify"] = {"telegram": {"enabled": True, "bot_token": "${FOO_SECRET}", "chat_id": "1"}}
    cfg = build_config(raw, SecretResolver(env_file=Path("/nonexistent"), use_keychain=False))
    assert cfg.notify.telegram.bot_token == "hunter2"


def test_rejected_watch_edit_rolls_back(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(base_raw()))
    st = ConfigStore(path, resolver=NO_SECRETS)
    st.load()
    with pytest.raises(ConfigError):
        st.upsert_watch({"id": "x", "name": "${TELEGRAM_BOT_TOKEN}",
                         "targets": [{"retailer": "bestbuy", "sku": "1"}]})
    assert [w["id"] for w in st.raw_watches()] == ["w"]
    with pytest.raises(ValueError):
        identifiers_from_url("https://www.gamestop.com/${TELEGRAM_BOT_TOKEN}/1.html")


# ------------------------------------------------------------------ 11. ?token= is throttled

def test_token_query_is_throttled() -> None:
    from tests.test_dashboard import PIN, FakeControl, make_client

    c = make_client(FakeControl())
    for i in range(5):
        assert c.get(f"/?token=wrong{i}").status_code == 401
    assert c.get(f"/?token={PIN}").status_code == 429


# ------------------------------------------------------------------ 12. URL host spoofing

@pytest.mark.parametrize("url", [
    "https://evil.example?.gamestop.com/x/1.html",
    "https://evil.example#.gamestop.com/x/1.html",
    "https://evil.example\\.gamestop.com/x/1.html",
    "https://user@evil.example/.gamestop.com/x/1.html",
    "https://www.gamestop.com/checkout/a.html",
    "https://evil.example?.nintendo.com/store/products/abc/",
    "javascript:alert(1)//www.target.com/p/-/A-12345678",
])
def test_spoofed_or_checkout_urls_rejected(url: str) -> None:
    with pytest.raises(ValueError):
        identifiers_from_url(url)


def test_product_url_revalidates_stored_url() -> None:
    a = GameStopAdapter(http=None)  # type: ignore[arg-type]
    with pytest.raises(AdapterError):
        a.product_url(Target("gamestop", {"url": "https://evil.example?.gamestop.com/x/1.html"}))
    n = NintendoAdapter(http=None)  # type: ignore[arg-type]
    with pytest.raises(AdapterError):
        n.product_url(Target("nintendo", {"url": "https://evil.example#.nintendo.com/store/products/a/"}))
    ok = "https://www.gamestop.com/video-games/switch/products/x/12345.html"
    assert a.product_url(Target("gamestop", {"url": ok})) == ok


# ------------------------------------------------------------------ 13. bind addresses

@pytest.mark.parametrize("host", ["192.168.1.20", "10.0.0.5", "8.8.8.8", "0.0.0.0"])
def test_lan_and_public_bind_refused(host: str) -> None:
    with pytest.raises(BindError):
        check_bind_safety(["127.0.0.1", host], True)


def test_loopback_and_tailscale_bind_allowed() -> None:
    check_bind_safety(["127.0.0.1", "100.64.0.5", "fd7a:115c:a1e0::1"], True)
    cfg = build_config(base_raw(), NO_SECRETS).dashboard
    assert resolve_bind_hosts(cfg, tailscale_ip=lambda: "100.101.102.103") == ["127.0.0.1", "100.101.102.103"]


# ------------------------------------------------------------------ 14. config.yaml permissions survive saves

def test_config_save_keeps_permissions(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(base_raw()))
    os.chmod(path, 0o600)
    st = ConfigStore(path, resolver=NO_SECRETS)
    st.load()
    st.set_enabled("w", False)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert not path.with_suffix(".yaml.tmp").exists()


# ------------------------------------------------------------------ 15. prices >= 1000

@pytest.mark.parametrize("text,expected", [
    ("1299.99", 1299.99), ("$1299.99", 1299.99), ("$1,299.99", 1299.99), ("1099", 1099.0),
    ("49.99", 49.99), ("Now $12,345", 12345.0), ("$1,299.99 was $1,499.99", 1299.99),
])
def test_parse_price_large_values(text: str, expected: float) -> None:
    assert parse_price(text) == expected


# ------------------------------------------------------------------ 16. Walmart reshaped JSON fails loud

def test_walmart_reshaped_json_with_carousel_oos_is_parse_error() -> None:
    html = (FIXTURES / "walmart" / "in_stock.html").read_text()
    html = html.replace("initialData", "initialDataV2").replace("Pickup, today", "Pick up today")
    with pytest.raises(ParseError):
        parse_walmart(html, HOME, Target("walmart", {"item_id": "1"}))


def test_walmart_text_fallback_ignores_carousel_badges() -> None:
    html = ("<html><body><main><h1>Switch</h1><div>Pickup, today</div>"
            "<button data-automation-id='atc'>Add to cart</button>"
            "<section><h2>Similar items</h2><span>Out of stock</span></section></main></body></html>")
    assert parse_walmart(html, HOME, Target("walmart", {"item_id": "1"})).in_stock
    oos_only_in_carousel = ("<html><body><main><h1>Switch</h1><div>Get in-stock alert</div>"
                            "<section><h2>Similar items</h2><span>Pickup, today</span></section>"
                            "</main></body></html>")
    with pytest.raises(ParseError):  # nothing about *this* product: fail loud, never a false IN_STOCK
        parse_walmart(oos_only_in_carousel, HOME, Target("walmart", {"item_id": "1"}))


# ------------------------------------------------------------------ 17. Target ?preselect= child wins

def test_target_preselect_wins_over_parent_tcin() -> None:
    ids = TargetAdapter.identifiers_from_url("https://www.target.com/p/x/-/A-89981366?preselect=89474495")
    assert ids["tcin"] == "89474495"


# ------------------------------------------------------------------ 18. Target stores outside the radius

def test_target_unknown_distance_store_outside_lookup_is_dropped() -> None:
    payload = {"data": {"product": {"fulfillment": {"store_options": [
        {"location_id": "1", "order_pickup": {"availability_status": "IN_STOCK"}},
        {"location_id": "2", "order_pickup": {"availability_status": "IN_STOCK"}},
        {"location_id": "3", "distance": 40.0, "order_pickup": {"availability_status": "IN_STOCK"}},
    ]}}}}
    nearby = {"1": tgt.NearbyStore(store_id="1", name="Frisco", distance_miles=5.0)}
    r = parse_fulfillment(payload, HOME, Target("target", {"tcin": "1"}), nearby)
    assert [c.store_id for c in r.channels] == ["1"]


# ------------------------------------------------------------------ 19. Best Buy /product/.../sku/<n> URLs

def test_bestbuy_new_pdp_url() -> None:
    url = "https://www.bestbuy.com/product/nintendo-switch-2/JJGCQ8RK3Q/sku/6614313"
    assert BestBuyAdapter.identifiers_from_url(url)["sku"] == "6614313"
    with pytest.raises(ValueError, match="no numeric SKU"):
        BestBuyAdapter.identifiers_from_url("https://www.bestbuy.com/product/nintendo-switch-2/JJGCQ8RK3Q")


# ------------------------------------------------------------------ 20/21. Nintendo invite banner and product match

def test_nintendo_site_invite_banner_does_not_mark_product_invite_only() -> None:
    html = (FIXTURES / "nintendo" / "in_stock.html").read_text()
    html = html.replace("<header>", "<header><a href='/switch2'>Switch 2: Sign up for an invitation</a>"
                                    "<button>Request an invite</button>", 1)
    html = html.replace("<main>", "<main><div class='site-banner'><button>Request an invite</button></div>", 1)
    r = parse_nintendo_page(html)
    assert r.raw_status == "IN_STOCK" and r.in_stock


def _apollo_page(products: dict[str, dict[str, Any]], slug: str | None = None) -> str:
    import json

    data: dict[str, Any] = {"props": {"pageProps": {"initialApolloState": products}}}
    if slug:
        data["query"] = {"slug": slug}
    return (f"<html><body><main><h1>x</h1></main><script id='__NEXT_DATA__' type='application/json'>"
            f"{json.dumps(data)}</script></body></html>")


def _node(sku: str, url_key: str, status: str, price: float) -> dict[str, Any]:
    return {"sku": sku, "urlKey": url_key, "name": url_key, "prices": {"minimum": {"finalPrice": price}},
            "purchaseStatus": status}


def test_nintendo_apollo_picks_the_watched_product() -> None:
    page = _apollo_page({
        "StoreProduct:1": _node("1", "mario-kart-bundle", "Available now", 499.99),
        "StoreProduct:2": _node("2", "nintendo-switch-2-system", "Sold out", 449.99),
    })
    r = parse_nintendo_page(page, HOME, Target("nintendo", {"product_id": "nintendo-switch-2-system"}))
    assert not r.in_stock and r.price == 449.99
    with pytest.raises(ParseError):
        parse_nintendo_page(page, HOME, Target("nintendo", {"product_id": "something-else"}))
