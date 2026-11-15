"""CartHelper: timeout, catch-all, signed-out mapping, per-retailer lock, no secrets in logs."""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest

from stockwatch.adapters.base import AdapterError, BlockedError, SignedOutError
from stockwatch.cart.helper import CartHelper, redact
from stockwatch.cart.safety import ForbiddenClickError
from stockwatch.config import WatchCfg
from stockwatch.models import CartResult, ChannelAvail, ErrorKind, StockResult, Target

CART_URL = "https://www.bestbuy.com/cart"


class FakeBrowser:
    async def page_for(self, retailer: str) -> Any:
        return object()

    async def cart_page(self, retailer: str) -> Any:
        return object()

    async def fetch_html(self, retailer: str, url: str, **kw: Any) -> tuple[int, str]:
        return 200, ""


class FakeAdapter:
    def __init__(self, name: str = "bestbuy", behaviour: Any = None, delay: float = 0.0) -> None:
        self.name = name
        self.behaviour = behaviour
        self.delay = delay
        self.calls = 0
        self.active = 0
        self.max_active = 0
        self.cancelled = False

    def cart_url(self) -> str:
        return CART_URL

    async def add_to_cart(self, target: Target, result: StockResult, browser: Any,
                          channel: Any = None) -> Any:
        self.calls += 1
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
            b = self.behaviour
            if isinstance(b, BaseException):
                raise b
            if callable(b):
                return b()
            if b is not None:
                return b
            return CartResult(retailer=self.name, success=True, message="Added",
                              cart_count_before=0, cart_count_after=1)
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        finally:
            self.active -= 1


def target(retailer: str = "bestbuy") -> Target:
    return Target(retailer=retailer, identifiers={"sku": "6614313"}, watch_id="switch2")


def in_stock(retailer: str = "bestbuy") -> StockResult:
    return StockResult(retailer=retailer, in_stock=True, raw_status="IN_STOCK", price=449.99,
                       channels=[ChannelAvail("store_pickup", True, store_id="1", store_name="Frisco")])


def watch(add: bool = True) -> WatchCfg:
    return WatchCfg(id="switch2", name="Switch 2", add_to_cart=add)


def helper(adapter: FakeAdapter, timeout: float = 25) -> CartHelper:
    return CartHelper(FakeBrowser(), {adapter.name: adapter}, timeout_seconds=timeout)


async def test_success_fills_cart_url() -> None:
    res = await helper(FakeAdapter()).try_add(target(), watch(), in_stock())
    assert res.success and res.cart_url == CART_URL and res.cart_count_after == 1


async def test_adapter_cart_url_preserved() -> None:
    a = FakeAdapter(behaviour=CartResult("bestbuy", True, "ok", cart_url="https://x/cart?id=1"))
    res = await helper(a).try_add(target(), watch(), in_stock())
    assert res.cart_url == "https://x/cart?id=1"


async def test_hard_timeout_cancels_adapter() -> None:
    a = FakeAdapter(delay=5)
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    res = await helper(a, timeout=0.05).try_add(target(), watch(), in_stock())
    assert loop.time() - t0 < 1
    assert not res.success and "timed out" in res.message
    assert a.cancelled and a.active == 0


async def test_default_timeout_is_25s() -> None:
    assert CartHelper(FakeBrowser(), {}).timeout_seconds == 25


@pytest.mark.parametrize("exc", [RuntimeError("boom"), ValueError("bad"), KeyError("k"),
                                 AdapterError("weird", kind=ErrorKind.PARSE_FAILED), OSError("io")])
async def test_catch_all(exc: BaseException) -> None:
    res = await helper(FakeAdapter(behaviour=exc)).try_add(target(), watch(), in_stock())
    assert isinstance(res, CartResult) and not res.success and not res.signed_out
    assert res.cart_url == CART_URL


async def test_blocked_reported_without_retry() -> None:
    a = FakeAdapter(behaviour=BlockedError("HTTP 429"))
    res = await helper(a).try_add(target(), watch(), in_stock())
    assert not res.success and "Blocked" in res.message and a.calls == 1


async def test_forbidden_click_reported() -> None:
    res = await helper(FakeAdapter(behaviour=ForbiddenClickError("checkout"))).try_add(target(), watch(), in_stock())
    assert not res.success and "checkout" in res.message.lower()


@pytest.mark.parametrize("behaviour", [
    SignedOutError("login page"),
    AdapterError("login", kind=ErrorKind.SIGNED_OUT),
    CartResult("bestbuy", success=True, message="whatever", signed_out=True),
    CartResult("bestbuy", success=False, message="Sign in needed for bestbuy", signed_out=True),
])
async def test_signed_out_mapping(behaviour: Any) -> None:
    res = await helper(FakeAdapter(behaviour=behaviour)).try_add(target(), watch(), in_stock())
    assert res.signed_out and not res.success
    assert res.message == "Sign in needed for Best Buy"


async def test_non_cartresult_return_is_failure() -> None:
    res = await helper(FakeAdapter(behaviour=lambda: {"ok": True})).try_add(target(), watch(), in_stock())
    assert not res.success


async def test_skips_when_disabled_or_not_in_stock_or_no_adapter() -> None:
    a = FakeAdapter()
    h = helper(a)
    assert not (await h.try_add(target(), watch(add=False), in_stock())).success
    assert not (await h.try_add(target(), watch(), StockResult("bestbuy", in_stock=False))).success
    assert not (await h.try_add(target(), watch(), None)).success
    assert not (await h.try_add(target("walmart"), watch(), in_stock("walmart"))).success
    assert a.calls == 0
    no_browser = CartHelper(None, {"bestbuy": a})
    assert not (await no_browser.try_add(target(), watch(), in_stock())).success
    assert a.calls == 0


async def test_watch_optional() -> None:
    assert (await helper(FakeAdapter()).try_add(target(), None, in_stock())).success


async def test_same_retailer_serialized_other_retailers_parallel() -> None:
    bb = FakeAdapter("bestbuy", delay=0.05)
    tg = FakeAdapter("target", delay=0.05)
    h = CartHelper(FakeBrowser(), {"bestbuy": bb, "target": tg})
    results = await asyncio.gather(
        h.try_add(target(), watch(), in_stock()),
        h.try_add(target(), watch(), in_stock()),
        h.try_add(target("target"), watch(), in_stock("target")),
        h.try_add(target("target"), watch(), in_stock("target")),
    )
    assert all(r.success for r in results)
    assert bb.max_active == 1 and tg.max_active == 1 and bb.calls == 2


async def test_lock_wait_counts_against_timeout_and_lock_released() -> None:
    a = FakeAdapter(delay=0.3)
    h = helper(a, timeout=0.1)
    r1, r2 = await asyncio.gather(h.try_add(target(), watch(), in_stock()),
                                  h.try_add(target(), watch(), in_stock()))
    assert not r1.success and not r2.success
    assert not h.lock_for("bestbuy").locked()
    a.delay = 0
    assert (await h.try_add(target(), watch(), in_stock())).success


async def test_outer_cancellation_propagates() -> None:
    h = helper(FakeAdapter(delay=5))
    task = asyncio.create_task(h.try_add(target(), watch(), in_stock()))
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not h.lock_for("bestbuy").locked()


async def test_secrets_not_logged_or_returned(caplog: pytest.LogCaptureFixture) -> None:
    secret = "SuperSecretKey123"
    exc = RuntimeError(f"GET https://api.bestbuy.com/v1/products?apiKey={secret}&format=json "
                       f"Authorization: Bearer {secret}abcdef token={secret}")
    caplog.set_level(logging.DEBUG, logger="stockwatch")
    res = await helper(FakeAdapter(behaviour=exc)).try_add(target(), watch(), in_stock())
    assert secret not in res.message
    assert secret not in caplog.text
    assert "cart result" in caplog.text


def test_redact() -> None:
    assert redact("x?apiKey=abc123&y=1") == "x?apiKey=***&y=1"
    assert redact("token: abcdef") == "token: ***"
    assert redact("Authorization: Bearer abcdefghijkl") == "Authorization: Bearer ***"
    assert redact("Added to cart (1 item)") == "Added to cart (1 item)"


async def test_long_messages_truncated() -> None:
    res = await helper(FakeAdapter(behaviour=RuntimeError("x" * 5000))).try_add(target(), watch(), in_stock())
    assert len(res.message) <= 300
