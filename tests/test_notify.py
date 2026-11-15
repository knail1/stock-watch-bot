"""Tests for stockwatch.notify (no network: respx mocks + fakes)."""
from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest
import respx

from stockwatch.config import (
    BrowserCfg, Config, DashboardCfg, Defaults, HealthCfg, MacSoundCfg, NotifyCfg,
    PushoverCfg, TelegramCfg, WatchCfg,
)
from stockwatch.db import Database
from stockwatch.models import Alert, AlertOutcome, CartResult, ChannelAvail, Home, Severity, StockResult, Target
from stockwatch.notify import macsound as macsound_mod
from stockwatch.notify.macsound import FALLBACK_SOUND, MacSound, resolve_sound_file
from stockwatch.notify.notifier import (
    Notifier, format_cart_followup, format_restock_alert, in_quiet_hours, pick_channel,
)
from stockwatch.notify.pushover import PushoverClient
from stockwatch.notify.telegram import TelegramClient, TelegramError, format_alert_html

PO_TOKEN = "apptoken123456"
PO_USER = "userkey123456"
TG_TOKEN = "123456:SECRETBOTTOKEN"
TG_URL = f"https://api.telegram.org/bot{TG_TOKEN}"


def form(request: httpx.Request) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(request.content.decode()).items()}


def make_watch(**kw) -> WatchCfg:
    return WatchCfg(id="switch2", name="Nintendo Switch 2", **kw)


def bb_result(**kw) -> StockResult:
    return StockResult(
        retailer="bestbuy", in_stock=True, price=449.99, raw_status="IN_STOCK",
        channels=[
            ChannelAvail("store_pickup", True, "1", "Plano", 9.0),
            ChannelAvail("store_pickup", True, "2", "Frisco", 6.2, low_stock=True),
            ChannelAvail("ship", True),
        ], **kw,
    )


BB = Target(retailer="bestbuy", identifiers={"sku": "123"}, watch_id="switch2")
PRODUCT = "https://www.bestbuy.com/site/123.p?skuId=123"
CART = "https://www.bestbuy.com/cart"


def make_config(quiet: str | None = None, po=True, tg=True, mac=True) -> Config:
    return Config(
        home=Home("75001"), defaults=Defaults(quiet_hours=quiet),
        notify=NotifyCfg(
            pushover=PushoverCfg(enabled=po, user_key=PO_USER, app_token=PO_TOKEN),
            telegram=TelegramCfg(enabled=tg, bot_token=TG_TOKEN, chat_id="42"),
            mac_sound=MacSoundCfg(enabled=mac),
        ),
        dashboard=DashboardCfg(), health=HealthCfg(), browser=BrowserCfg(),
        retailers={}, watches=[],
    )


# ---------------------------------------------------------------- formatting

def test_restock_alert_matches_spec_example():
    a = format_restock_alert(make_watch(), BB, bb_result(), PRODUCT, CART, added=True)
    assert a.title == "IN STOCK: Nintendo Switch 2 @ Best Buy"
    assert a.body == "Pickup at Best Buy Frisco (6.2 mi), $449.99, low stock. Added to cart: YES"
    assert a.url == CART and a.url_title == "Open cart"
    assert a.extra_url == "https://maps.apple.com/?q=Best%20Buy%20Frisco"
    assert a.severity == Severity.CRITICAL
    assert "Best Buy Frisco" in a.speak and "$449.99" in a.speak


def test_restock_alert_trying_and_omitted():
    a = format_restock_alert(make_watch(), BB, bb_result(), PRODUCT)
    assert a.body.endswith(". Added to cart: trying…")
    assert a.url == PRODUCT  # primary link is product until cart succeeds
    b = format_restock_alert(make_watch(add_to_cart=False), BB, bb_result(), PRODUCT)
    assert b.body == "Pickup at Best Buy Frisco (6.2 mi), $449.99, low stock"
    c = format_restock_alert(make_watch(), BB, bb_result(), PRODUCT, CART, added=False)
    assert c.body.endswith("Added to cart: NO") and c.url == PRODUCT


def test_restock_alert_ship_only_and_store_name_with_retailer():
    w = make_watch(fulfillment=["ship"])
    a = format_restock_alert(w, BB, bb_result(), PRODUCT, added=None)
    assert a.body.startswith("Ships online, $449.99") and a.extra_url is None
    r = StockResult("bestbuy", True, [ChannelAvail("in_store", True, "9", "Best Buy Allen", 3.0)])
    b = format_restock_alert(make_watch(add_to_cart=False), BB, r, PRODUCT)
    assert b.body == "In store at Best Buy Allen (3.0 mi)"


def test_pick_channel_priority_then_distance():
    ch = pick_channel(bb_result(), ["store_pickup", "ship"])
    assert ch.store_name == "Frisco"
    assert pick_channel(bb_result(), ["ship", "store_pickup"]).channel == "ship"
    assert pick_channel(StockResult("bestbuy", False), ["ship"]) is None


def test_cart_followups():
    ok = format_cart_followup(make_watch(), BB, CartResult("bestbuy", True, cart_url=CART), PRODUCT)
    assert ok.url == CART and "Added to cart: YES" in ok.body and ok.play_sound is False
    so = format_cart_followup(make_watch(), BB, CartResult("bestbuy", False, signed_out=True), PRODUCT)
    assert so.title == "Sign in needed for Best Buy" and so.severity == Severity.WARNING
    assert so.url == PRODUCT


@pytest.mark.parametrize("spec,hhmm,expected", [
    ("23:00-06:00", "23:30", True), ("23:00-06:00", "05:59", True),
    ("23:00-06:00", "06:00", False), ("23:00-06:00", "12:00", False),
    ("01:00-05:00", "03:00", True), ("01:00-05:00", "06:00", False),
    (None, "03:00", False), ("garbage", "03:00", False), ("25:00-06:00", "03:00", False),
])
def test_quiet_hours(spec, hhmm, expected):
    h, m = map(int, hhmm.split(":"))
    assert in_quiet_hours(spec, datetime(2026, 1, 1, h, m)) is expected


# ---------------------------------------------------------------- pushover

@respx.mock
async def test_pushover_emergency_payload():
    route = respx.post("https://api.pushover.net/1/messages.json").mock(
        return_value=httpx.Response(200, json={"status": 1, "request": "r", "receipt": "RCPT"}))
    async with httpx.AsyncClient() as http:
        po = PushoverClient(PushoverCfg(enabled=True, user_key=PO_USER, app_token=PO_TOKEN,
                                        retry_seconds=10, expire_seconds=99999), http)
        a = format_restock_alert(make_watch(), BB, bb_result(), PRODUCT)
        out = await po.send(a)
    assert out == AlertOutcome("pushover", True, "RCPT")
    data = form(route.calls.last.request)
    assert data["priority"] == "2" and data["retry"] == "30" and data["expire"] == "10800"
    assert data["sound"] == "siren" and data["url"] == PRODUCT
    assert data["url_title"] == "Open at Best Buy" and data["title"].startswith("IN STOCK")


@respx.mock
async def test_pushover_non_emergency_has_no_retry_and_priority_override():
    route = respx.post("https://api.pushover.net/1/messages.json").mock(
        return_value=httpx.Response(200, json={"status": 1, "request": "r"}))
    async with httpx.AsyncClient() as http:
        po = PushoverClient(PushoverCfg(user_key=PO_USER, app_token=PO_TOKEN), http)
        await po.send(Alert(Severity.WARNING, "t", "b"))
        assert form(route.calls.last.request)["priority"] == "1"
        await po.send(Alert(Severity.CRITICAL, "t", "b"), priority=-1)
    data = form(route.calls.last.request)
    assert data["priority"] == "-1" and "retry" not in data and "expire" not in data


@respx.mock
async def test_pushover_errors_redacted():
    respx.post("https://api.pushover.net/1/messages.json").mock(
        return_value=httpx.Response(400, json={"status": 0, "errors": [f"token {PO_TOKEN} invalid"]}))
    async with httpx.AsyncClient() as http:
        po = PushoverClient(PushoverCfg(user_key=PO_USER, app_token=PO_TOKEN), http, retry_delay=0)
        out = await po.send(Alert(Severity.INFO, "t", "b"))
    assert not out.ok and PO_TOKEN not in out.detail and "HTTP 400" in out.detail


@respx.mock
async def test_pushover_retries_once_on_transport_error():
    route = respx.post("https://api.pushover.net/1/messages.json").mock(side_effect=[
        httpx.ConnectError("boom"), httpx.Response(200, json={"status": 1})])
    async with httpx.AsyncClient() as http:
        po = PushoverClient(PushoverCfg(user_key=PO_USER, app_token=PO_TOKEN), http, retry_delay=0)
        out = await po.send(Alert(Severity.INFO, "t", "b"))
    assert out.ok and route.call_count == 2


@respx.mock
async def test_pushover_validate_and_receipt():
    respx.post("https://api.pushover.net/1/users/validate.json").mock(
        return_value=httpx.Response(200, json={"status": 1, "devices": ["iphone"]}))
    respx.get("https://api.pushover.net/1/receipts/RCPT.json").mock(
        return_value=httpx.Response(200, json={"status": 1, "acknowledged": 1}))
    async with httpx.AsyncClient() as http:
        po = PushoverClient(PushoverCfg(user_key=PO_USER, app_token=PO_TOKEN), http)
        assert await po.validate() == (True, "ok (devices: iphone)")
        assert (await po.receipt_status("RCPT"))["acknowledged"] == 1
    assert await PushoverClient(PushoverCfg(), None).validate() == (False, "pushover app_token/user_key not configured")


# ---------------------------------------------------------------- telegram

def test_telegram_html_escapes_and_has_both_links():
    a = Alert(Severity.CRITICAL, "IN STOCK: <Switch> & co", "Pickup at A&B",
              url="https://x.test/p?a=1&b=2", url_title="Open", extra_url="https://maps.apple.com/?q=A%20B",
              extra_url_title="Store map")
    text = format_alert_html(a)
    assert "<b>IN STOCK: &lt;Switch&gt; &amp; co</b>" in text
    assert "Pickup at A&amp;B" in text
    assert '<a href="https://x.test/p?a=1&amp;b=2">Open</a>' in text
    assert '<a href="https://maps.apple.com/?q=A%20B">Store map</a>' in text


@respx.mock
async def test_telegram_send_and_updates():
    send = respx.post(f"{TG_URL}/sendMessage").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": {"message_id": 7}}))
    respx.post(f"{TG_URL}/getUpdates").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": [{"update_id": 1}]}))
    respx.post(f"{TG_URL}/getMe").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": {"username": "bot"}}))
    async with httpx.AsyncClient() as http:
        tg = TelegramClient(TelegramCfg(True, TG_TOKEN, "42"), http)
        out = await tg.send(format_restock_alert(make_watch(), BB, bb_result(), PRODUCT))
        assert out == AlertOutcome("telegram", True, "7")
        import json
        body = json.loads(send.calls.last.request.content)
        assert body["chat_id"] == "42" and body["parse_mode"] == "HTML"
        assert PRODUCT in body["text"] and "maps.apple.com" in body["text"]
        assert await tg.get_updates(offset=5, timeout=1) == [{"update_id": 1}]
        assert (await tg.get_me())["username"] == "bot"


@respx.mock
async def test_telegram_errors_never_leak_token():
    respx.post(f"{TG_URL}/sendMessage").mock(side_effect=httpx.ConnectError(f"failed {TG_URL}"))
    respx.post(f"{TG_URL}/getUpdates").mock(
        return_value=httpx.Response(401, json={"ok": False, "description": "Unauthorized"}))
    async with httpx.AsyncClient() as http:
        tg = TelegramClient(TelegramCfg(True, TG_TOKEN, "42"), http)
        out = await tg.send_text("hi")
        assert not out.ok and TG_TOKEN not in out.detail
        with pytest.raises(TelegramError) as ei:
            await tg.get_updates()
        assert TG_TOKEN not in str(ei.value) and "Unauthorized" in str(ei.value)


# ---------------------------------------------------------------- mac sound

def test_resolve_sound_file(tmp_path, monkeypatch):
    monkeypatch.setenv("STOCKWATCH_HOME", str(tmp_path))
    (tmp_path / "mine.aiff").write_bytes(b"x")
    assert resolve_sound_file("mine.aiff") == tmp_path / "mine.aiff"
    absf = tmp_path / "abs.aiff"
    absf.write_bytes(b"x")
    assert resolve_sound_file(str(absf)) == absf
    monkeypatch.setattr(macsound_mod, "FALLBACK_SOUND", tmp_path / "fallback.aiff")
    (tmp_path / "fallback.aiff").write_bytes(b"x")
    assert resolve_sound_file("missing/nope.aiff") == tmp_path / "fallback.aiff"
    assert FALLBACK_SOUND == Path("/System/Library/Sounds/Sosumi.aiff")


async def test_macsound_noop_off_darwin():
    out = await MacSound(MacSoundCfg(), platform="linux").play(Alert(Severity.CRITICAL, "t", "b"))
    assert not out.ok and "not macOS" in out.detail


async def test_macsound_argv_no_shell(monkeypatch, tmp_path):
    calls: list[tuple[str, ...]] = []

    class Proc:
        returncode = 0

        async def wait(self):
            return 0

    async def fake_exec(*argv, **kw):
        assert "shell" not in kw
        calls.append(argv)
        return Proc()

    sound = tmp_path / "a.aiff"
    sound.write_bytes(b"x")
    monkeypatch.setattr(macsound_mod.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(macsound_mod.shutil, "which", lambda name: f"/usr/bin/{name}")
    ms = MacSound(MacSoundCfg(file=str(sound), repeat=2, speak=True), platform="darwin")
    out = await ms.play(Alert(Severity.CRITICAL, "t", "b", speak="-v Evil; rm -rf ~"))
    assert out.ok
    await ms.wait()
    assert calls == [("afplay", str(sound)), ("afplay", str(sound)), ("say", "--", "-v Evil; rm -rf ~")]


async def test_macsound_is_non_blocking(monkeypatch):
    started = asyncio.Event()
    release = asyncio.Event()

    class Proc:
        returncode = None

        async def wait(self):
            started.set()
            await release.wait()
            return 0

        def kill(self):
            self.returncode = -9

    async def fake_exec(*argv, **kw):
        return Proc()

    monkeypatch.setattr(macsound_mod.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(macsound_mod.shutil, "which", lambda name: name)
    ms = MacSound(MacSoundCfg(repeat=1), platform="darwin")
    out = await asyncio.wait_for(ms.play(Alert(Severity.CRITICAL, "t", "b")), 1)
    assert out.ok
    await asyncio.wait_for(started.wait(), 1)
    await ms.stop()


# ---------------------------------------------------------------- notifier

class FakePushover:
    def __init__(self, fail: bool = False):
        self.sent: list[tuple[Alert, int | None]] = []
        self.fail = fail

    def priority_for(self, sev):
        return PushoverClient(PushoverCfg(), None).priority_for(sev)

    async def send(self, alert, *, priority=None):
        if self.fail:
            raise RuntimeError("kaboom")
        self.sent.append((alert, priority))
        return AlertOutcome("pushover", True, "RCPT" if alert.severity == Severity.CRITICAL else "")


class FakeTelegram:
    def __init__(self):
        self.sent: list[Alert] = []

    async def send(self, alert):
        self.sent.append(alert)
        return AlertOutcome("telegram", True, "1")


class FakeSound:
    def __init__(self):
        self.played: list[Alert] = []

    async def play(self, alert):
        self.played.append(alert)
        return AlertOutcome("mac_sound", True, "playing")


def make_notifier(quiet=None, at="12:00", po_fail=False, **cfg):
    h, m = map(int, at.split(":"))
    db = Database(":memory:")
    po, tg, ms = FakePushover(po_fail), FakeTelegram(), FakeSound()
    n = Notifier(make_config(quiet, **cfg), None, db, pushover=po, telegram=tg, mac_sound=ms,
                 clock=lambda: datetime(2026, 1, 1, h, m))
    return n, db, po, tg, ms


async def test_notifier_fans_out_and_records():
    n, db, po, tg, ms = make_notifier()
    outs = await n.send(Alert(Severity.CRITICAL, "t", "b", event_id=None))
    assert {o.channel for o in outs} == {"pushover", "telegram", "mac_sound"}
    assert po.sent[0][1] is None and len(tg.sent) == 1 and len(ms.played) == 1
    rows = db.recent_alerts()
    assert len(rows) == 3
    assert {r["channel"]: r["receipt"] for r in rows}["pushover"] == "RCPT"


async def test_notifier_sound_only_for_critical_unless_requested():
    n, db, po, tg, ms = make_notifier()
    await n.send(Alert(Severity.INFO, "t", "b"))
    assert ms.played == []
    await n.send(Alert(Severity.INFO, "t", "b", play_sound=True))
    assert len(ms.played) == 1
    await n.send(Alert(Severity.CRITICAL, "t", "b", play_sound=False))
    assert len(ms.played) == 1


async def test_notifier_quiet_hours():
    n, db, po, tg, ms = make_notifier(quiet="23:00-06:00", at="02:00")
    outs = await n.send(Alert(Severity.WARNING, "t", "b", play_sound=True))
    assert po.sent[-1][1] == -1  # delivered without sound
    assert len(tg.sent) == 1 and ms.played == []
    assert {o.channel for o in outs} == {"pushover", "telegram"}
    assert {r["channel"]: r["status"] for r in db.recent_alerts()}["pushover"] == "sent_quiet"
    await n.send(Alert(Severity.CRITICAL, "t", "b"))
    assert po.sent[-1][1] is None and len(ms.played) == 1


async def test_notifier_never_raises_and_respects_disabled():
    n, db, po, tg, ms = make_notifier(po_fail=True, mac=False)
    outs = await n.send(Alert(Severity.CRITICAL, "t", "b"))
    by = {o.channel: o for o in outs}
    assert not by["pushover"].ok and "RuntimeError" in by["pushover"].detail
    assert by["telegram"].ok and "mac_sound" not in by
    assert {r["channel"]: r["status"] for r in db.recent_alerts()}["pushover"] == "failed"


async def test_notifier_test_all():
    n, db, po, tg, ms = make_notifier(quiet="00:00-23:59", at="12:00")
    outs = await n.test_all()
    assert len(outs) == 3 and all(o.ok for o in outs)
    assert po.sent[0][0].severity == Severity.CRITICAL
