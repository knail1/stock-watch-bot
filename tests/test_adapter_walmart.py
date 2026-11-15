"""Walmart adapter tests: fixtures, URL handling, check() via a fake browser, cart safety."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import httpx
import pytest

from stockwatch.adapters import walmart as wm
from stockwatch.adapters.base import AdapterError, BlockedError, ParseError
from stockwatch.adapters.walmart import WalmartAdapter, parse_cart_count, parse_walmart
from stockwatch.models import ChannelAvail, ErrorKind, Home, StockResult, Target

FIX = Path(__file__).parent / "fixtures" / "walmart"
HOME = Home(zip="75034", radius_miles=25)


def fixture(name: str) -> str:
    return (FIX / name).read_text()


def target(**ids: str) -> Target:
    return Target(retailer="walmart", identifiers=ids or {"item_id": "5113183757"}, watch_id="sw2")


# ------------------------------------------------------------------ parsing

def test_parse_in_stock() -> None:
    r = parse_walmart(fixture("in_stock.html"), HOME, target())
    assert r.in_stock and r.raw_status == "IN_STOCK" and r.error is None
    assert r.price == 449.0
    assert r.product_name == "Nintendo Switch 2 System"
    pickup = [c for c in r.channels if c.channel == "store_pickup"]
    assert len(pickup) == 1 and pickup[0].available
    assert pickup[0].store_id == "1234" and pickup[0].store_name == "Frisco Supercenter"
    ship = [c for c in r.channels if c.channel == "ship"]
    assert len(ship) == 1 and ship[0].available  # DELIVERY + SHIPPING collapse into one


def test_parse_out_of_stock() -> None:
    r = parse_walmart(fixture("out_of_stock.html"), HOME, target())
    assert not r.in_stock and r.raw_status == "OUT_OF_STOCK"
    assert r.channels and not any(c.available for c in r.channels)
    assert r.price == 449.0


def test_parse_ship_only_uses_fulfillment_type_list() -> None:
    r = parse_walmart(fixture("ship_only.html"), HOME, target())
    assert r.in_stock
    assert [c.channel for c in r.available_channels()] == ["ship"]
    assert r.available_channels()[0].low_stock is True


def test_pinned_store_ids_filter_other_selected_store() -> None:
    t = Target(retailer="walmart", identifiers={"item_id": "1"}, store_ids=["9999"])
    r = parse_walmart(fixture("in_stock.html"), HOME, t)
    assert all(c.channel == "ship" for c in r.channels)
    assert r.in_stock  # still shippable


def test_parse_visible_text_fallback() -> None:
    r = parse_walmart(fixture("text_in_stock.html"), HOME, target())
    assert r.in_stock and r.price == 449.0
    assert {c.channel for c in r.available_channels()} == {"store_pickup", "ship"}
    r = parse_walmart(fixture("text_out_of_stock.html"), HOME, target())
    assert not r.in_stock and r.raw_status == "OUT_OF_STOCK" and r.price == 449.0


def test_parse_challenge_raises_blocked() -> None:
    with pytest.raises(BlockedError):
        parse_walmart(fixture("challenge.html"), HOME, target())


@pytest.mark.parametrize("html", ["", "<html><body>???</body></html>", "not html {", None])
def test_parse_garbage_raises_parse_error(html: Any) -> None:
    with pytest.raises(ParseError):
        parse_walmart(html, HOME, target())


def test_parse_garbage_fixture() -> None:
    with pytest.raises(ParseError):
        parse_walmart(fixture("garbage.html"), HOME, target())


def test_parse_missing_product_is_not_found() -> None:
    with pytest.raises(AdapterError) as ei:
        parse_walmart(fixture("product_missing.html"), HOME, target())
    assert ei.value.kind == ErrorKind.NOT_FOUND


def test_parse_cart_count() -> None:
    assert parse_cart_count("Cart contains 3 items Total Amount $10.00") == 3
    assert parse_cart_count("1 item") == 1
    assert parse_cart_count("2\n$12.00") == 2
    assert parse_cart_count("Cart") is None
    assert parse_cart_count(None) is None


# ------------------------------------------------------------------ URLs

@pytest.mark.parametrize("url", [
    "https://www.walmart.com/ip/Nintendo-Switch-2-System/5113183757",
    "https://www.walmart.com/ip/Nintendo-Switch-2-System/5113183757?classType=REGULAR&from=/search",
    "https://walmart.com/ip/5113183757",
    "walmart.com/ip/Some-Thing/5113183757#reviews",
])
def test_identifiers_from_url(url: str) -> None:
    ids = WalmartAdapter.identifiers_from_url(url)
    assert ids["item_id"] == "5113183757"
    assert "#" not in ids["url"]
    assert WalmartAdapter.matches_url(url)


def test_identifiers_from_url_without_id() -> None:
    with pytest.raises(ValueError):
        WalmartAdapter.identifiers_from_url("https://www.walmart.com/browse/video-games/3")


def test_matches_url_rejects_other_hosts() -> None:
    assert not WalmartAdapter.matches_url("https://www.bestbuy.com/site/x/6614313.p")
    assert not WalmartAdapter.matches_url("https://notwalmart.com/ip/1")


def test_urls_and_settings() -> None:
    a = WalmartAdapter(http=httpx.AsyncClient())
    assert a.name == "walmart" and a.min_interval_seconds == 180
    assert a.product_url(target()) == "https://www.walmart.com/ip/5113183757"
    assert a.product_url(target(url="https://www.walmart.com/ip/x/42")) == "https://www.walmart.com/ip/x/42"
    assert a.cart_url() == "https://www.walmart.com/cart"
    with pytest.raises(AdapterError):
        a.product_url(Target(retailer="walmart"))


# ------------------------------------------------------------------ check()

class FakeBrowser:
    """Implements BrowserCtx.fetch_html with canned responses."""

    def __init__(self, status: int = 200, html: str = "", page: Any = None, exc: Exception | None = None):
        self.status, self.html, self.page, self.exc = status, html, page, exc
        self.calls: list[tuple[str, str]] = []

    async def page_for(self, retailer: str) -> Any:
        return self.page

    async def cart_page(self, retailer: str) -> Any:
        return self.page

    async def fetch_html(self, retailer: str, url: str, *, wait_selector: str | None = None,
                         timeout_ms: int = 30_000) -> tuple[int, str]:
        self.calls.append((retailer, url))
        if self.exc:
            raise self.exc
        return self.status, self.html


def adapter(browser: Any) -> WalmartAdapter:
    return WalmartAdapter(http=httpx.AsyncClient(), browser=browser)


async def test_check_in_stock_end_to_end() -> None:
    b = FakeBrowser(200, fixture("in_stock.html"))
    r = await adapter(b).check(target(), HOME)
    assert r.in_stock and r.retailer == "walmart"
    assert b.calls == [("walmart", "https://www.walmart.com/ip/5113183757")]


async def test_check_out_of_stock_end_to_end() -> None:
    r = await adapter(FakeBrowser(200, fixture("out_of_stock.html"))).check(target(), HOME)
    assert not r.in_stock


@pytest.mark.parametrize("status,html", [
    (403, "<html>Access Denied</html>"),
    (429, ""),
    (200, fixture("challenge.html")),
    (200, '<html><body><div id="px-captcha"></div></body></html>'),
])
async def test_check_blocked(status: int, html: str) -> None:
    with pytest.raises(BlockedError):
        await adapter(FakeBrowser(status, html)).check(target(), HOME)


async def test_check_propagates_browser_blocked() -> None:
    with pytest.raises(BlockedError):
        await adapter(FakeBrowser(exc=BlockedError("challenge"))).check(target(), HOME)


async def test_check_http_errors_and_garbage() -> None:
    with pytest.raises(AdapterError) as ei:
        await adapter(FakeBrowser(404, "<html>gone</html>")).check(target(), HOME)
    assert ei.value.kind == ErrorKind.NOT_FOUND
    with pytest.raises(AdapterError) as ei:
        await adapter(FakeBrowser(500, "<html>oops</html>")).check(target(), HOME)
    assert ei.value.kind == ErrorKind.HTTP_ERROR
    with pytest.raises(ParseError):
        await adapter(FakeBrowser(200, fixture("garbage.html"))).check(target(), HOME)


async def test_check_without_browser_is_config_error() -> None:
    with pytest.raises(AdapterError) as ei:
        await adapter(None).check(target(), HOME)
    assert ei.value.kind == ErrorKind.CONFIG


# ------------------------------------------------------------------ add_to_cart

class FakeElement:
    def __init__(self, text: str = "", attrs: dict[str, str] | None = None, visible: bool = True,
                 on_click: Any = None):
        self.text, self.attrs, self.visible, self.on_click = text, attrs or {}, visible, on_click
        self.clicks = 0

    async def inner_text(self, timeout: int | None = None) -> str:
        return self.text

    async def get_attribute(self, name: str, timeout: int | None = None) -> str | None:
        return self.attrs.get(name)

    async def evaluate(self, script: str) -> str:
        return self.text

    async def click(self, timeout: int | None = None) -> None:
        self.clicks += 1
        if self.on_click:
            self.on_click()


class FakeLocator:
    def __init__(self, el: FakeElement | None):
        self.el = el

    @property
    def first(self) -> "FakeLocator":
        return self

    async def count(self) -> int:
        return 1 if self.el else 0

    async def is_visible(self) -> bool:
        return bool(self.el and self.el.visible)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.el, name)


class FakePage:
    def __init__(self, elements: dict[str, FakeElement], html: str = "<html></html>"):
        self.elements, self.html = elements, html
        self.visited: list[str] = []

    async def goto(self, url: str, **kw: Any) -> None:
        self.visited.append(url)

    async def content(self) -> str:
        return self.html

    def locator(self, selector: str) -> FakeLocator:
        return FakeLocator(self.elements.get(selector))

    async def wait_for_timeout(self, ms: int) -> None:
        return None


def walmart_page(*, signed_in: bool = True, atc: FakeElement | None = None,
                 increments: bool = True) -> tuple[FakePage, dict[str, FakeElement]]:
    cart = FakeElement("1 $449.00", {"aria-label": "Cart contains 1 item Total Amount $449.00"})

    def bump() -> None:
        if increments:
            cart.attrs["aria-label"] = "Cart contains 2 items Total Amount $898.00"
            cart.text = "2 $898.00"

    els = {
        '[data-automation-id="cart-button"]': cart,
        '[data-automation-id="headerSignIn"]': FakeElement("Hi, Alex\nAccount" if signed_in else "Sign In\nAccount"),
        '[data-seo-id="fulfillment-Pickup-intent"]': FakeElement("Pickup\nToday"),
        'button[data-automation-id="atc"]': atc or FakeElement("Add to cart", on_click=bump),
        # Present on the page (e.g. the post-add side panel) but must never be clicked.
        'button:has-text("Check out")': FakeElement("Check out", {"data-automation-id": "checkout"}),
    }
    return FakePage(els), els


PICKUP_RESULT = StockResult(retailer="walmart", in_stock=True, channels=[
    ChannelAvail(channel="store_pickup", available=True, store_id="1234", store_name="Frisco Supercenter")])


@pytest.fixture
def click_spy(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Record every click routed through safe_click (while still enforcing it)."""
    seen: list[Any] = []
    real = wm.safe_click

    async def spy(locator: Any, **kw: Any) -> None:
        seen.append(locator.el)
        await real(locator, **kw)

    monkeypatch.setattr(wm, "safe_click", spy)
    return seen


async def test_add_to_cart_success_selects_pickup(click_spy: list[Any]) -> None:
    page, els = walmart_page()
    res = await adapter(None).add_to_cart(target(), PICKUP_RESULT, FakeBrowser(page=page))
    assert res.success, res.message
    assert (res.cart_count_before, res.cart_count_after) == (1, 2)
    assert res.cart_url == "https://www.walmart.com/cart"
    assert page.visited == ["https://www.walmart.com/ip/5113183757"]
    pickup, atc = els['[data-seo-id="fulfillment-Pickup-intent"]'], els['button[data-automation-id="atc"]']
    assert click_spy == [pickup, atc]  # every click went through safe_click
    assert pickup.clicks == 1 and atc.clicks == 1
    assert els['button:has-text("Check out")'].clicks == 0


async def test_add_to_cart_never_clicks_checkout_button(click_spy: list[Any]) -> None:
    trap = FakeElement("Check out", {"aria-label": "Continue to checkout"})
    page, els = walmart_page(atc=trap)
    res = await adapter(None).add_to_cart(target(), PICKUP_RESULT, FakeBrowser(page=page))
    assert not res.success and "refused" in res.message
    assert trap in click_spy and trap.clicks == 0
    assert els['button:has-text("Check out")'].clicks == 0


@pytest.mark.parametrize("label", ["Buy now", "Place order", "Pay with PayPal", "checkout-btn"])
async def test_add_to_cart_refuses_purchase_labels(label: str) -> None:
    trap = FakeElement("Add", {"data-automation-id": label})
    page, _ = walmart_page(atc=trap)
    res = await adapter(None).add_to_cart(target(), PICKUP_RESULT, FakeBrowser(page=page))
    assert not res.success and trap.clicks == 0


async def test_add_to_cart_signed_out() -> None:
    page, els = walmart_page(signed_in=False)
    res = await adapter(None).add_to_cart(target(), PICKUP_RESULT, FakeBrowser(page=page))
    assert not res.success and res.signed_out
    assert els['button[data-automation-id="atc"]'].clicks == 0


async def test_add_to_cart_count_unchanged_is_failure() -> None:
    page, _ = walmart_page(increments=False)
    a = adapter(None)
    a.cart_count_wait_seconds = 0.01
    res = await a.add_to_cart(target(), PICKUP_RESULT, FakeBrowser(page=page))
    assert not res.success and res.cart_count_before == 1 and res.cart_count_after == 1


async def test_add_to_cart_challenge_page() -> None:
    page, els = walmart_page()
    page.html = fixture("challenge.html")
    res = await adapter(None).add_to_cart(target(), PICKUP_RESULT, FakeBrowser(page=page))
    assert not res.success and re.search("challenge", res.message)
    assert all(e.clicks == 0 for e in els.values())


async def test_is_signed_in() -> None:
    assert await wm.is_signed_in(walmart_page(signed_in=True)[0]) is True
    assert await wm.is_signed_in(walmart_page(signed_in=False)[0]) is False
    assert await wm.is_signed_in(FakePage({})) is None
