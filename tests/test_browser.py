"""BrowserManager tests using fake Playwright objects (no real browser, no network)."""
from __future__ import annotations

import asyncio
from typing import Any

import pytest
from playwright.async_api import Error as PwError
from playwright.async_api import TimeoutError as PwTimeout

from stockwatch.adapters.base import AdapterError, BlockedError, BrowserCtx
from stockwatch.browser import SIGNIN_URLS, BrowserManager
from stockwatch.config import BrowserCfg
from stockwatch.models import ErrorKind


class FakeResponse:
    def __init__(self, status: int):
        self.status = status


class FakePage:
    def __init__(self, ctx: "FakeContext"):
        self.ctx = ctx
        self.closed = False
        self.visited: list[str] = []
        self.html = "<html><body>ok</body></html>"
        self.status = 200
        self.goto_exc: BaseException | None = None
        self.selector_exc: BaseException | None = None
        self.goto_delay = 0.0
        self.close_on_error = False
        self.active = 0
        self.max_active = 0

    def is_closed(self) -> bool:
        return self.closed

    async def goto(self, url: str, **kw: Any) -> FakeResponse:
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            if self.goto_delay:
                await asyncio.sleep(self.goto_delay)
            if self.goto_exc:
                if self.close_on_error:
                    self.closed = True
                raise self.goto_exc
            self.visited.append(url)
            return FakeResponse(self.status)
        finally:
            self.active -= 1

    async def wait_for_selector(self, selector: str, **kw: Any) -> None:
        if self.selector_exc:
            raise self.selector_exc

    async def content(self) -> str:
        return self.html

    async def close(self) -> None:
        self.closed = True


class FakeContext:
    def __init__(self, kwargs: dict[str, Any]):
        self.kwargs = kwargs
        self.pages: list[FakePage] = []
        self.handlers: dict[str, Any] = {}
        self.closed = False

    def on(self, event: str, cb: Any) -> None:
        self.handlers[event] = cb

    async def new_page(self) -> FakePage:
        if self.closed:
            raise PwError("Target closed")
        p = FakePage(self)
        self.pages.append(p)
        return p

    async def close(self) -> None:
        self.closed = True

    def crash(self) -> None:
        self.closed = True
        for p in self.pages:
            p.closed = True
        self.handlers["close"](self)


class FakeChromium:
    def __init__(self) -> None:
        self.launches: list[FakeContext] = []

    async def launch_persistent_context(self, **kwargs: Any) -> FakeContext:
        ctx = FakeContext(kwargs)
        self.launches.append(ctx)
        return ctx


class FakePlaywright:
    def __init__(self) -> None:
        self.chromium = FakeChromium()
        self.stopped = False

    async def stop(self) -> None:
        self.stopped = True


@pytest.fixture
def pw() -> FakePlaywright:
    return FakePlaywright()


@pytest.fixture
def make(pw: FakePlaywright, tmp_path):
    def _make(headless: bool = True, **kw: Any) -> BrowserManager:
        async def starter() -> FakePlaywright:
            return pw
        return BrowserManager(BrowserCfg(headless=headless), profile_dir=tmp_path / "profile",
                              playwright_starter=starter, **kw)
    return _make


def last_ctx(pw: FakePlaywright) -> FakeContext:
    return pw.chromium.launches[-1]


async def test_implements_protocol_and_lazy_start(make, pw):
    bm = make()
    assert isinstance(bm, BrowserCtx)
    assert not bm.started and pw.chromium.launches == []
    await bm.page_for("walmart")
    assert bm.started
    kw = last_ctx(pw).kwargs
    assert kw["channel"] == "chrome" and kw["headless"] is True
    assert kw["user_data_dir"].endswith("profile")
    # No stealth / spoofing options passed.
    assert set(kw) == {"user_data_dir", "channel", "headless"}


async def test_start_is_idempotent_and_profile_is_private(make, pw, tmp_path):
    bm = make()
    await asyncio.gather(bm.start(), bm.start(), bm.start())
    assert len(pw.chromium.launches) == 1
    assert (tmp_path / "profile").stat().st_mode & 0o777 == 0o700


async def test_pages_reused_and_cart_page_separate(make):
    bm = make()
    p1 = await bm.page_for("walmart")
    p2 = await bm.page_for("walmart")
    c1 = await bm.cart_page("walmart")
    assert p1 is p2
    assert c1 is not p1 and c1 is await bm.cart_page("walmart")
    assert await bm.page_for("gamestop") is not p1


async def test_closed_page_is_recreated(make):
    bm = make()
    p1 = await bm.page_for("walmart")
    p1.closed = True
    assert await bm.page_for("walmart") is not p1


async def test_fetch_html_ok(make):
    bm = make()
    page = await bm.page_for("nintendo")
    page.html = "<html>" + "x" * 100 + "</html>"
    status, html = await bm.fetch_html("nintendo", "https://n/p", wait_selector="#x")
    assert status == 200 and html == page.html and page.visited == ["https://n/p"]


@pytest.mark.parametrize("status", [403, 429])
async def test_fetch_html_blocked_status(make, status):
    bm = make()
    page = await bm.page_for("walmart")
    page.status = status
    with pytest.raises(BlockedError):
        await bm.fetch_html("walmart", "https://w/p")
    assert page.visited == ["https://w/p"]  # exactly one attempt, no retry


async def test_fetch_html_challenge_page(make):
    bm = make()
    page = await bm.page_for("walmart")
    page.html = "<html><div id='px-captcha'>Press & Hold</div></html>"
    with pytest.raises(BlockedError) as ei:
        await bm.fetch_html("walmart", "https://w/p")
    assert ei.value.kind == ErrorKind.BLOCKED


async def test_fetch_html_selector_timeout_on_challenge_is_blocked(make):
    bm = make()
    page = await bm.page_for("walmart")
    page.html = "<html>verify you are human</html>"
    page.selector_exc = PwTimeout("timeout")
    with pytest.raises(BlockedError):
        await bm.fetch_html("walmart", "https://w/p", wait_selector="#add")


async def test_fetch_html_timeouts(make):
    bm = make()
    page = await bm.page_for("gamestop")
    page.selector_exc = PwTimeout("Timeout 30000ms exceeded")
    with pytest.raises(AdapterError) as ei:
        await bm.fetch_html("gamestop", "https://g/p", wait_selector="#add")
    assert ei.value.kind == ErrorKind.TIMEOUT
    page.selector_exc = None
    page.goto_exc = PwTimeout("Timeout 30000ms exceeded")
    with pytest.raises(AdapterError) as ei:
        await bm.fetch_html("gamestop", "https://g/p")
    assert ei.value.kind == ErrorKind.TIMEOUT and not isinstance(ei.value, BlockedError)


async def test_fetch_html_serialized_per_retailer(make):
    bm = make()
    page = await bm.page_for("walmart")
    page.goto_delay = 0.01
    await asyncio.gather(*(bm.fetch_html("walmart", f"https://w/{i}") for i in range(4)))
    assert page.max_active == 1 and len(page.visited) == 4


async def test_context_crash_restarts(make, pw):
    bm = make()
    p1 = await bm.page_for("walmart")
    last_ctx(pw).crash()
    assert not bm.started
    p2 = await bm.page_for("walmart")
    assert p2 is not p1 and len(pw.chromium.launches) == 2


async def test_browser_error_on_dead_page_drops_page(make, pw):
    bm = make()
    page = await bm.page_for("walmart")
    page.goto_exc = PwError("Target page, context or browser has been closed")
    page.close_on_error = True
    with pytest.raises(AdapterError) as ei:
        await bm.fetch_html("walmart", "https://w/p")
    assert ei.value.kind == ErrorKind.UNKNOWN
    assert await bm.page_for("walmart") is not page


async def test_open_signin_pages_forces_headed(make, pw):
    bm = make(headless=True)
    await bm.start()
    urls = await bm.open_signin_pages(["bestbuy", "walmart", "https://example.test/login", "bogus"])
    assert urls == [SIGNIN_URLS["bestbuy"], SIGNIN_URLS["walmart"], "https://example.test/login"]
    ctx = last_ctx(pw)
    assert len(pw.chromium.launches) == 2 and ctx.kwargs["headless"] is False
    assert [p.visited[0] for p in ctx.pages] == urls
    await bm.end_signin()
    assert last_ctx(pw).kwargs["headless"] is True


async def test_signin_status_uses_hooks(make):
    async def yes(page: Any) -> bool:
        return True

    async def boom(page: Any) -> bool:
        raise RuntimeError("selector missing")

    bm = make(signin_checks={"bestbuy": yes, "target": boom})
    status = await bm.signin_status(["bestbuy", "target", "walmart"])
    assert status == {"bestbuy": True, "target": None, "walmart": None}


async def test_signin_status_challenge_is_unknown(make, pw):
    called = []

    async def hook(page: Any) -> bool:
        called.append(page)
        return True

    bm = make(signin_checks={"walmart": hook})
    await bm.start()
    orig = FakeContext.new_page

    async def new_page(self: FakeContext) -> FakePage:
        p = await orig(self)
        p.html = "<html>captcha</html>"
        return p

    FakeContext.new_page = new_page  # type: ignore[method-assign]
    try:
        assert await bm.signin_status(["walmart"]) == {"walmart": None}
    finally:
        FakeContext.new_page = orig  # type: ignore[method-assign]
    assert called == []
    assert all(p.closed for p in last_ctx(pw).pages)


async def test_signin_status_module_hook(make, monkeypatch):
    import types

    mod = types.ModuleType("stockwatch.adapters.fakeshop")

    async def is_signed_in(page: Any) -> bool:
        return False

    mod.is_signed_in = is_signed_in  # type: ignore[attr-defined]
    monkeypatch.setitem(__import__("sys").modules, "stockwatch.adapters.fakeshop", mod)
    bm = make()
    assert await bm.signin_status(["fakeshop", "nosuchshop"]) == {"fakeshop": False, "nosuchshop": None}


async def test_close_stops_everything(make, pw):
    bm = make()
    await bm.page_for("walmart")
    ctx = last_ctx(pw)
    await bm.close()
    assert ctx.closed and pw.stopped and not bm.started
    await bm.close()  # idempotent
