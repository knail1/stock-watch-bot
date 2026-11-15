"""Dashboard tests: auth/CSRF, screens, watch CRUD, settings, bind safety. No network."""
from __future__ import annotations

import asyncio
import re
import socket
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from stockwatch.dashboard import app as dash_app
from stockwatch.dashboard import (
    CheckView,
    DashboardControl,
    EventView,
    NotifyChannelView,
    SettingsView,
    TargetStatus,
    WatchSpec,
    WatchStatus,
    create_app,
    resolve_bind_hosts,
    serve,
)
from stockwatch.dashboard.auth import COOKIE_NAME, LoginThrottle, SessionSigner
from stockwatch.dashboard.server import BindError, check_bind_safety
from stockwatch.models import AlertOutcome

PIN = "4821"
NOW = datetime.now(timezone.utc)


class FakeControl:
    def __init__(self) -> None:
        self._watches = [
            WatchStatus(
                id="switch2", name="Nintendo Switch 2", max_price=499.99,
                targets=[
                    TargetStatus(retailer="bestbuy", identifiers={"sku": "6522225"},
                                 product_url="https://www.bestbuy.com/site/6522225.p",
                                 last_check_at=NOW - timedelta(seconds=30), last_result="OUT_OF_STOCK",
                                 last_in_stock=False, last_in_stock_at=NOW - timedelta(days=2)),
                    TargetStatus(retailer="target", identifiers={"tcin": "123"}, blocked=True,
                                 error="blocked", last_check_at=NOW),
                ],
            ),
            WatchStatus(id="paused-one", name="Paused <b>thing</b>", enabled=False,
                        targets=[TargetStatus(retailer="walmart", identifiers={"item_id": "9"}, paused=True)]),
        ]
        self.calls: list[tuple] = []
        self.signin: dict[str, bool | None] = {"bestbuy": True, "target": False}
        self._settings = SettingsView(home_zip="75001", radius_miles=25, quiet_hours=None, channels=[
            NotifyChannelView("pushover", True, True), NotifyChannelView("telegram", True, False),
            NotifyChannelView("mac_sound", False, True)])

    def watches(self) -> list[WatchStatus]:
        return list(self._watches)

    def recent_checks(self, watch_id: str, limit: int = 30) -> list[CheckView]:
        return [CheckView(retailer="bestbuy", checked_at=NOW, in_stock=True, price=449.99, raw_status="IN_STOCK",
                          summary="Pickup: Best Buy Frisco (6.2 mi)")]

    def recent_events(self, limit: int = 100) -> list[EventView]:
        return [EventView(at=NOW, kind="restock", title="IN STOCK at Best Buy", watch_id="switch2",
                          retailer="bestbuy", detail="Added to cart: YES")]

    def settings(self) -> SettingsView:
        return self._settings

    async def signin_status(self) -> dict[str, bool | None]:
        return self.signin

    async def open_signin_browser(self) -> None:
        self.calls.append(("open_signin",))

    async def send_test(self, channel: str | None = None) -> list[AlertOutcome]:
        self.calls.append(("send_test", channel))
        return [AlertOutcome("pushover", True, "receipt"), AlertOutcome("telegram", False, "no token")]

    async def update_settings(self, home_zip: str, radius_miles: float, quiet_hours: str | None) -> None:
        self.calls.append(("update_settings", home_zip, radius_miles, quiet_hours))

    async def add_watch(self, spec: WatchSpec) -> None:
        self.calls.append(("add_watch", spec))
        self._watches.append(WatchStatus(id=spec.id, name=spec.name, targets=[
            TargetStatus(retailer=t.retailer, identifiers=t.identifiers) for t in spec.targets]))

    async def update_watch(self, spec: WatchSpec) -> None:
        self.calls.append(("update_watch", spec))

    async def set_paused(self, watch_id: str, paused: bool) -> None:
        self.calls.append(("set_paused", watch_id, paused))

    async def delete_watch(self, watch_id: str) -> None:
        self.calls.append(("delete_watch", watch_id))

    async def check_now(self, watch_id: str) -> None:
        self.calls.append(("check_now", watch_id))


def fake_identifiers(url: str) -> tuple[str, dict[str, str]]:
    if m := re.search(r"bestbuy\.com/.*?(\d{7})", url):
        return "bestbuy", {"sku": m.group(1), "url": url}
    if m := re.search(r"target\.com/.*A-(\d+)", url):
        return "target", {"tcin": m.group(1), "url": url}
    raise ValueError(f"unsupported retailer URL: {url}")


@pytest.fixture(autouse=True)
def _fake_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dash_app, "identifiers_from_url", fake_identifiers)


@pytest.fixture
def control() -> FakeControl:
    return FakeControl()


def make_client(control: FakeControl, pin: str | None = PIN, base: str = "http://100.64.0.5:8787") -> TestClient:
    return TestClient(create_app(control, pin=pin), base_url=base, follow_redirects=False)


def login(client: TestClient) -> None:
    r = client.post("/login", data={"pin": PIN, "next": "/"})
    assert r.status_code == 303


def csrf(client: TestClient, path: str = "/") -> str:
    r = client.get(path)
    assert r.status_code == 200, r.text
    m = re.search(r'name="csrf" value="([^"]+)"', r.text)
    assert m
    return m.group(1)


# ------------------------------------------------------------------ protocol / views

def test_fake_satisfies_protocol(control: FakeControl) -> None:
    assert isinstance(control, DashboardControl)


def test_status_colors(control: FakeControl) -> None:
    w, paused = control.watches()
    assert [t.status for t in w.targets] == ["green", "yellow"]
    assert w.status == "yellow"
    assert paused.status == "grey"
    w.targets[0].error = "parse_failed"
    assert w.status == "red"
    assert w.last_check_at == NOW and w.last_in_stock_at == NOW - timedelta(days=2)
    assert WatchStatus(id="x", name="x").status == "grey"


def test_watch_spec_to_raw() -> None:
    from stockwatch.models import Target
    spec = WatchSpec(id="a", name="A", targets=[Target("bestbuy", {"sku": "1"}, store_ids=["5"])],
                     fulfillment=["ship"], max_price=10.0)
    raw = spec.to_raw()
    assert raw["targets"] == [{"retailer": "bestbuy", "sku": "1", "store_ids": ["5"]}]
    assert raw["max_price"] == 10.0 and "radius_miles" not in raw


# ------------------------------------------------------------------ auth

def test_requires_login(control: FakeControl) -> None:
    c = make_client(control)
    r = c.get("/settings")
    assert r.status_code == 303 and r.headers["location"].startswith("/login?next=%2Fsettings")
    assert c.post("/watches/switch2/pause").status_code == 401
    assert ("set_paused", "switch2", True) not in control.calls
    # static + manifest are public (iOS fetches them without cookies)
    assert c.get("/manifest.webmanifest").status_code == 200
    assert c.get("/static/style.css").status_code == 200


def test_login_sets_strict_httponly_cookie(control: FakeControl) -> None:
    c = make_client(control)
    assert c.post("/login", data={"pin": "0000"}).status_code == 401
    r = c.post("/login", data={"pin": PIN, "next": "/events"})
    assert r.status_code == 303 and r.headers["location"] == "/events"
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie
    assert c.get("/events").status_code == 200


def test_login_rejects_open_redirect(control: FakeControl) -> None:
    c = make_client(control)
    r = c.post("/login", data={"pin": PIN, "next": "//evil.example/x"})
    assert r.headers["location"] == "/"


def test_login_throttle(control: FakeControl) -> None:
    c = make_client(control)
    for _ in range(5):
        assert c.post("/login", data={"pin": "bad"}).status_code == 401
    assert c.post("/login", data={"pin": PIN}).status_code == 429


def test_tampered_cookie_rejected(control: FakeControl) -> None:
    c = make_client(control)
    login(c)
    good = c.cookies[COOKIE_NAME]
    c.cookies.clear()
    c.cookies.set(COOKIE_NAME, good[:-2] + ("00" if not good.endswith("00") else "11"))
    assert c.get("/").status_code == 303


def test_token_query_auth(control: FakeControl) -> None:
    c = make_client(control)
    assert c.get("/?token=nope").status_code == 401
    r = c.get("/events?token=" + PIN)
    assert r.status_code == 303 and r.headers["location"] == "/events"   # token stripped
    assert COOKIE_NAME in r.headers["set-cookie"]
    assert c.get("/events").status_code == 200
    # POST with ?token= works without a CSRF form field (token itself is the secret)
    fresh = make_client(control)
    assert fresh.post(f"/watches/switch2/check?token={PIN}").status_code == 303


def test_csrf_required(control: FakeControl) -> None:
    c = make_client(control)
    login(c)
    assert c.post("/watches/switch2/pause", data={}).status_code == 403
    assert c.post("/watches/switch2/pause", data={"csrf": "x" * 64}).status_code == 403
    assert not control.calls
    token = csrf(c)
    r = c.post("/watches/switch2/pause", data={"csrf": token, "next": "/"})
    assert r.status_code == 303 and r.headers["location"] == "/?ok=paused"
    assert ("set_paused", "switch2", True) in control.calls


def test_csrf_tied_to_session(control: FakeControl) -> None:
    app = create_app(control, pin=PIN, session_secret=b"s" * 32)
    a = TestClient(app, base_url="http://100.64.0.5", follow_redirects=False)
    b = TestClient(app, base_url="http://100.64.0.5", follow_redirects=False)
    login(a)
    login(b)
    assert b.post("/watches/switch2/resume", data={"csrf": csrf(a)}).status_code == 403


def test_logout(control: FakeControl) -> None:
    c = make_client(control)
    login(c)
    r = c.post("/logout", data={"csrf": csrf(c)})
    assert r.status_code == 303
    c.cookies.clear()
    assert c.get("/").status_code == 303


def test_no_pin_localhost_only(control: FakeControl) -> None:
    c = make_client(control, pin=None, base="http://127.0.0.1:8787")
    r = c.get("/")
    assert r.status_code == 200 and COOKIE_NAME in r.headers["set-cookie"]
    # CSRF still enforced in no-PIN mode
    assert c.post("/watches/switch2/pause", data={}).status_code == 403
    assert c.post("/watches/switch2/pause", data={"csrf": csrf(c)}).status_code == 303
    # DNS-rebinding style Host is refused
    other = make_client(control, pin=None, base="http://evil.example:8787")
    assert other.get("/").status_code == 403


def test_security_headers(control: FakeControl) -> None:
    c = make_client(control)
    login(c)
    r = c.get("/")
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["referrer-policy"] == "no-referrer"
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]


def test_signer_expiry() -> None:
    s = SessionSigner("1", b"k" * 32, max_age=10)
    sid, cookie = s.new_session(now=1000)
    assert s.verify(cookie, now=1005) == sid
    assert s.verify(cookie, now=1011) is None
    assert SessionSigner("2", b"k" * 32).verify(cookie, now=1005) is None  # PIN change invalidates


def test_throttle_window() -> None:
    t = LoginThrottle(max_failures=2, window=10)
    t.failure("a", now=0)
    t.failure("a", now=1)
    assert t.locked("a", now=5)
    assert not t.locked("a", now=12)


# ------------------------------------------------------------------ screens

def test_watches_page(control: FakeControl) -> None:
    c = make_client(control)
    login(c)
    html = c.get("/").text
    assert "Nintendo Switch 2" in html
    assert 'class="dot yellow"' in html and 'class="dot grey"' in html
    assert "Paused &lt;b&gt;thing&lt;/b&gt;" in html  # autoescaped
    assert 'action="/watches/paused-one/resume"' in html
    assert 'action="/watches/switch2/pause"' in html
    assert 'name="csrf" value=""' not in html  # macro forms carry the session token


def test_pwa_meta(control: FakeControl) -> None:
    c = make_client(control)
    html = c.get("/login").text
    assert 'name="apple-mobile-web-app-capable"' in html
    assert 'rel="manifest"' in html and 'rel="apple-touch-icon"' in html
    m = c.get("/manifest.webmanifest")
    assert m.headers["content-type"].startswith("application/manifest+json")
    assert m.json()["display"] == "standalone"
    assert c.get("/static/apple-touch-icon.png").content.startswith(b"\x89PNG")


def test_watch_detail_and_404(control: FakeControl) -> None:
    c = make_client(control)
    login(c)
    html = c.get("/watches/switch2").text
    assert "Best Buy 6522225" in html and "Best Buy Frisco (6.2 mi)" in html
    assert "$449.99" in html and "$499.99" in html
    assert "backing off" in html
    assert 'name="csrf" value=""' not in html
    assert c.get("/watches/nope").status_code == 404


def test_events_page(control: FakeControl) -> None:
    c = make_client(control)
    login(c)
    html = c.get("/events").text
    assert "IN STOCK at Best Buy" in html and "Added to cart: YES" in html


# ------------------------------------------------------------------ add / edit / ops

def test_add_watch_from_urls(control: FakeControl) -> None:
    c = make_client(control)
    login(c)
    token = csrf(c, "/watches/new")
    urls = ("https://www.bestbuy.com/site/switch/6522225.p\n\n"
            "https://www.target.com/p/switch/-/A-94693225\n"
            "https://www.bestbuy.com/site/switch/6522225.p\n")
    r = c.post("/watches/new", data={"csrf": token, "name": "Nintendo Switch 2", "urls": urls,
                                     "fulfillment": ["store_pickup", "ship"], "max_price": "$499.99",
                                     "radius_miles": "15", "add_to_cart": "1"})
    assert r.status_code == 303, r.text
    assert r.headers["location"] == "/watches/nintendo-switch-2?ok=added"
    spec = next(call[1] for call in control.calls if call[0] == "add_watch")
    assert spec.id == "nintendo-switch-2"
    assert [t.retailer for t in spec.targets] == ["bestbuy", "target"]
    assert spec.targets[1].identifiers["tcin"] == "94693225"
    assert spec.fulfillment == ["store_pickup", "ship"]
    assert spec.max_price == 499.99 and spec.radius_miles == 15 and spec.add_to_cart
    assert all(t.watch_id == spec.id for t in spec.targets)


def test_add_watch_unique_id_and_default_name(control: FakeControl) -> None:
    c = make_client(control)
    login(c)
    token = csrf(c, "/watches/new")
    r = c.post("/watches/new", data={"csrf": token, "urls": "https://www.target.com/p/x/-/A-555",
                                     "fulfillment": "ship"})
    assert r.status_code == 303
    spec = control.calls[0][1]
    assert spec.name == "Target 555" and spec.id == "target-555"
    assert spec.add_to_cart is False


def test_add_watch_validation_errors(control: FakeControl) -> None:
    c = make_client(control)
    login(c)
    token = csrf(c, "/watches/new")
    r = c.post("/watches/new", data={"csrf": token, "urls": "https://www.amazon.com/dp/B0\n",
                                     "max_price": "cheap"})
    assert r.status_code == 400
    assert "unsupported retailer URL" in r.text
    assert "Max price must be a number" in r.text
    assert "Choose at least one fulfillment option" in r.text
    assert "amazon.com/dp/B0" in r.text  # input echoed back
    assert not control.calls
    r = c.post("/watches/new", data={"csrf": token, "urls": "", "fulfillment": "ship"})
    assert "Paste at least one product URL" in r.text


def test_add_watch_control_error_shown(control: FakeControl) -> None:
    async def boom(spec: WatchSpec) -> None:
        raise ValueError("duplicate watch id")
    control.add_watch = boom  # type: ignore[method-assign]
    c = make_client(control)
    login(c)
    r = c.post("/watches/new", data={"csrf": csrf(c, "/watches/new"), "fulfillment": "ship",
                                     "urls": "https://www.target.com/p/x/-/A-1"})
    assert r.status_code == 400 and "duplicate watch id" in r.text


def test_edit_watch_keeps_and_adds_targets(control: FakeControl) -> None:
    c = make_client(control)
    login(c)
    page = c.get("/watches/switch2/edit").text
    assert 'name="keep" value="0" checked' in page and 'value="499.99"' in page
    token = csrf(c, "/watches/switch2/edit")
    r = c.post("/watches/switch2/edit", data={
        "csrf": token, "name": "Switch 2 bundle", "keep": ["1"],
        "urls": "https://www.bestbuy.com/site/b/7000000.p", "fulfillment": ["in_store"], "max_price": ""})
    assert r.status_code == 303 and r.headers["location"] == "/watches/switch2?ok=saved"
    spec = control.calls[-1][1]
    assert spec.id == "switch2" and spec.name == "Switch 2 bundle"
    assert [(t.retailer, t.get("sku") or t.get("tcin")) for t in spec.targets] == [("target", "123"),
                                                                                    ("bestbuy", "7000000")]
    assert spec.max_price is None and spec.fulfillment == ["in_store"]


def test_edit_requires_a_target(control: FakeControl) -> None:
    c = make_client(control)
    login(c)
    r = c.post("/watches/switch2/edit", data={"csrf": csrf(c), "fulfillment": "ship"})
    assert r.status_code == 400 and "Paste at least one product URL" in r.text


@pytest.mark.parametrize("op,expected", [
    ("pause", ("set_paused", "switch2", True)),
    ("resume", ("set_paused", "switch2", False)),
    ("delete", ("delete_watch", "switch2")),
])
def test_watch_ops(control: FakeControl, op: str, expected: tuple) -> None:
    c = make_client(control)
    login(c)
    r = c.post(f"/watches/switch2/{op}", data={"csrf": csrf(c)})
    assert r.status_code == 303
    assert expected in control.calls
    if op == "delete":
        assert r.headers["location"] == "/?ok=deleted"


def test_check_now_runs_in_background(control: FakeControl) -> None:
    with make_client(control) as c:
        login(c)
        r = c.post("/watches/switch2/check", data={"csrf": csrf(c)})
        assert r.status_code == 303 and r.headers["location"] == "/watches/switch2?ok=checking"
        assert "Check started" in c.get("/watches/switch2?ok=checking").text
    assert ("check_now", "switch2") in control.calls


def test_op_unknown_watch(control: FakeControl) -> None:
    c = make_client(control)
    login(c)
    assert c.post("/watches/ghost/pause", data={"csrf": csrf(c)}).status_code == 404


# ------------------------------------------------------------------ settings

def test_settings_page(control: FakeControl) -> None:
    c = make_client(control)
    login(c)
    html = c.get("/settings").text
    assert 'value="75001"' in html
    assert "secrets missing" in html          # telegram enabled but not configured
    assert "signed in" in html and "signed out" in html and "unknown" in html
    assert "Open sign-in browser" in html


def test_settings_signin_failure_is_unknown(control: FakeControl) -> None:
    async def broken() -> dict[str, bool | None]:
        raise RuntimeError("browser down")
    control.signin_status = broken  # type: ignore[method-assign]
    c = make_client(control)
    login(c)
    r = c.get("/settings")
    assert r.status_code == 200 and "signed in" not in r.text.replace("unknown", "")


def test_save_settings(control: FakeControl) -> None:
    c = make_client(control)
    login(c)
    token = csrf(c, "/settings")
    r = c.post("/settings", data={"csrf": token, "home_zip": "75034", "radius_miles": "30",
                                  "quiet_hours": "23:00-06:00"})
    assert r.status_code == 303
    assert ("update_settings", "75034", 30.0, "23:00-06:00") in control.calls
    r = c.post("/settings", data={"csrf": token, "home_zip": "7503", "radius_miles": "x",
                                  "quiet_hours": "late"})
    assert r.status_code == 400
    assert "ZIP must be 5 digits" in r.text and "Quiet hours must look like" in r.text


def test_send_test(control: FakeControl) -> None:
    c = make_client(control)
    login(c)
    token = csrf(c, "/settings")
    r = c.post("/settings/test", data={"csrf": token, "channel": "pushover"})
    assert r.status_code == 200 and "Test results" in r.text and "no token" in r.text
    assert ("send_test", "pushover") in control.calls
    c.post("/settings/test", data={"csrf": token, "channel": ""})
    assert ("send_test", None) in control.calls
    assert c.post("/settings/test", data={"csrf": token, "channel": "sms"}).status_code == 400


def test_open_signin(control: FakeControl) -> None:
    c = make_client(control)
    login(c)
    r = c.post("/settings/signin", data={"csrf": csrf(c, "/settings")})
    assert r.status_code == 303 and ("open_signin",) in control.calls


# ------------------------------------------------------------------ binding

def _cfg(bind: list[str]) -> SimpleNamespace:
    return SimpleNamespace(dashboard=SimpleNamespace(bind=bind, port=8787, pin=""))


def test_resolve_bind_hosts() -> None:
    assert resolve_bind_hosts(_cfg(["127.0.0.1", "tailscale"]), tailscale_ip=lambda: "100.101.1.2") == [
        "127.0.0.1", "100.101.1.2"]
    assert resolve_bind_hosts(_cfg(["tailscale"]), tailscale_ip=lambda: None) == ["127.0.0.1"]
    assert resolve_bind_hosts(_cfg(["localhost"]), tailscale_ip=lambda: None) == ["127.0.0.1"]
    # DashboardCfg directly is accepted too
    assert resolve_bind_hosts(SimpleNamespace(bind=[]), tailscale_ip=lambda: None) == ["127.0.0.1"]


@pytest.mark.parametrize("bad", ["0.0.0.0", "::", "*", "example.com"])
def test_resolve_bind_hosts_rejects_wildcards(bad: str) -> None:
    with pytest.raises(BindError):
        resolve_bind_hosts(_cfg([bad]), tailscale_ip=lambda: None)


def test_bind_safety_without_pin() -> None:
    check_bind_safety(["127.0.0.1"], pin_configured=False)
    check_bind_safety(["127.0.0.1", "100.64.0.1"], pin_configured=True)
    with pytest.raises(BindError):
        check_bind_safety(["127.0.0.1", "100.64.0.1"], pin_configured=False)
    with pytest.raises(BindError):
        check_bind_safety(["0.0.0.0"], pin_configured=True)


async def test_serve_refuses_unsafe(control: FakeControl) -> None:
    with pytest.raises(BindError):
        await serve(create_app(control, pin=None), ["127.0.0.1", "100.64.0.1"], 0)
    with pytest.raises(BindError):
        await serve(create_app(control, pin=PIN), ["0.0.0.0"], 0)


async def test_serve_localhost(control: FakeControl) -> None:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    task = asyncio.create_task(serve(create_app(control, pin=PIN), ["127.0.0.1"], port))
    try:
        async with httpx.AsyncClient() as client:
            for _ in range(50):
                try:
                    r = await client.get(f"http://127.0.0.1:{port}/login")
                    break
                except httpx.ConnectError:
                    await asyncio.sleep(0.05)
            else:
                pytest.fail("server did not start")
        assert r.status_code == 200 and "Enter PIN" in r.text
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


# ------------------------------------------------------------------ Best Buy branding (API ToS)

def test_bestbuy_logo_and_attribution_shown_when_bestbuy_used(control: FakeControl) -> None:
    from types import SimpleNamespace

    control.config = SimpleNamespace(watches=[SimpleNamespace(targets=[SimpleNamespace(retailer="bestbuy")])])
    c = make_client(control)
    login(c)
    for path in ("/", "/events", "/settings"):
        html = c.get(path).text
        # Best Buy's own hosted asset, exactly as in their branding snippet
        assert 'src="https://developer.bestbuy.com/images/bestbuy-logo.png"' in html, path
        assert "Best Buy Developer API" in html
        assert html.index("bb-logo") > html.index("</main>")  # at the bottom of the page
    csp = c.get("/").headers["content-security-policy"]
    assert "img-src 'self' data: https://developer.bestbuy.com;" in csp


def test_bestbuy_logo_hidden_when_not_used(control: FakeControl) -> None:
    from types import SimpleNamespace

    control.config = SimpleNamespace(watches=[SimpleNamespace(targets=[SimpleNamespace(retailer="target")])])
    c = make_client(control)
    login(c)
    assert "bestbuy-logo" not in c.get("/").text


def test_dashboard_does_not_use_best_buy_blue_as_accent() -> None:
    from pathlib import Path

    css = (Path(__file__).parent.parent / "stockwatch/dashboard/static/style.css").read_text().lower()
    assert "--accent:#2563eb" not in css and "--accent:#0046be" not in css
