"""GameStop adapter tests: fixture parsing, URL handling, check() via a fake browser, cart safety."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from stockwatch.adapters import gamestop as gs
from stockwatch.adapters.base import AdapterError, BlockedError, ParseError
from stockwatch.adapters.gamestop import (GameStopAdapter, is_challenge_html, is_signed_in,
                                          parse_product_page, signed_in_from_html)
from stockwatch.cart import safety
from stockwatch.models import ChannelAvail, ErrorKind, Home, StockResult, Target

FIX = Path(__file__).parent / "fixtures" / "gamestop"
URL = ("https://www.gamestop.com/video-games/nintendo-switch-2/consoles/products/"
       "nintendo-switch-2-console/20018872.html")
HOME = Home(zip="75034", radius_miles=25)


def fx(name: str) -> str:
    return (FIX / name).read_text()


def target(**kw: Any) -> Target:
    return Target(retailer="gamestop", identifiers={"url": URL, "product_id": "20018872"}, **kw)


# ------------------------------------------------------------------ parsing

def test_parse_in_stock() -> None:
    r = parse_product_page(fx("in_stock.html"), HOME, target())
    assert r.in_stock and r.raw_status == "IN_STOCK" and r.error is None
    assert r.price == 449.99
    assert r.product_name == "Nintendo Switch 2 Console"
    by = {c.channel: c for c in r.channels}
    assert by["ship"].available
    pickup = by["store_pickup"]
    assert pickup.available and pickup.store_id == "6512"
    assert pickup.store_name == "Frisco - Stonebriar Centre"
    assert pickup.low_stock is False and pickup.quantity is None


def test_parse_out_of_stock() -> None:
    r = parse_product_page(fx("out_of_stock.html"), HOME, target())
    assert not r.in_stock and r.raw_status == "OUT_OF_STOCK"
    assert {c.channel for c in r.channels} == {"ship", "store_pickup"}
    assert not any(c.available for c in r.channels)
    assert r.price == 449.99


def test_parse_limited_pickup_only() -> None:
    r = parse_product_page(fx("limited_pickup.html"), HOME, target())
    assert r.in_stock and r.raw_status == "LIMITED"
    by = {c.channel: c for c in r.channels}
    assert not by["ship"].available
    p = by["store_pickup"]
    assert p.available and p.low_stock and p.quantity == 2
    assert p.distance_miles == 6.2 and p.store_name == "Frisco - Stonebriar Centre"


def test_parse_preorder_from_gtm_only_and_no_store_set() -> None:
    r = parse_product_page(fx("preorder_gtm_only.html"), HOME, target())
    assert r.in_stock and r.raw_status == "PREORDER"
    assert [c.channel for c in r.channels] == ["ship"]  # "Select a store" -> no pickup channel
    assert r.price == 79.99 and r.product_name == "Mario Kart World"


def test_parse_pinned_store_mismatch_drops_pickup() -> None:
    r = parse_product_page(fx("limited_pickup.html"), HOME, target(store_ids=["9999"]))
    assert not r.in_stock
    assert [c.channel for c in r.channels] == ["ship"]
    r2 = parse_product_page(fx("limited_pickup.html"), HOME, target(store_ids=["6512"]))
    assert r2.in_stock


def test_parse_challenge_raises_blocked() -> None:
    assert is_challenge_html(fx("challenge.html"))
    with pytest.raises(BlockedError):
        parse_product_page(fx("challenge.html"), HOME, target())


def test_large_akamai_access_denied_is_blocked() -> None:
    html = ("<html><title>Access Denied</title><body><h1>Access Denied</h1>Reference #18.abc"
            + "<!-- pad -->" * 3000 + "</body></html>")
    assert len(html) > 15000
    with pytest.raises(BlockedError):
        parse_product_page(html, HOME, target())


@pytest.mark.parametrize("payload", ["", "   ", "<html><body>hello</body></html>", "{garbage"])
def test_parse_garbage_raises_parse_error(payload: str) -> None:
    with pytest.raises(ParseError):
        parse_product_page(payload, HOME, target())


def test_parse_layout_changed_raises_parse_error() -> None:
    with pytest.raises(ParseError):
        parse_product_page(fx("layout_changed.html"), HOME, target())


def test_signed_in_detection() -> None:
    assert signed_in_from_html(fx("in_stock.html")) is True
    assert signed_in_from_html(fx("preorder_gtm_only.html")) is False
    assert signed_in_from_html("<html></html>") is None


async def test_is_signed_in_hook() -> None:
    page = FakePage(fx("in_stock.html"))
    assert await is_signed_in(page) is True


# ------------------------------------------------------------------ URLs

def test_identifiers_from_url() -> None:
    ids = GameStopAdapter.identifiers_from_url(URL + "#reviews")
    assert ids == {"product_id": "20018872", "url": URL}
    assert GameStopAdapter.matches_url(URL)
    assert GameStopAdapter.matches_url("gamestop.com/x/y/123.html")
    assert not GameStopAdapter.matches_url("https://www.bestbuy.com/site/x/123.p")
    with pytest.raises(ValueError):
        GameStopAdapter.identifiers_from_url("https://www.gamestop.com/search/?q=switch")
    with pytest.raises(ValueError):
        GameStopAdapter.identifiers_from_url("https://example.com/a/123.html")


def test_product_and_cart_url() -> None:
    a = GameStopAdapter(http=httpx.AsyncClient())
    assert a.product_url(target()) == URL
    assert a.cart_url() == "https://www.gamestop.com/cart/"
    with pytest.raises(AdapterError) as ei:
        a.product_url(Target(retailer="gamestop"))
    assert ei.value.kind == ErrorKind.CONFIG


# ------------------------------------------------------------------ check()

class FakeBrowser:
    """Implements BrowserCtx.fetch_html (and cart_page for add_to_cart tests)."""

    def __init__(self, status: int = 200, html: str = "", page: Any = None):
        self.status, self.html, self.page = status, html, page
        self.calls: list[tuple[str, str, str | None]] = []

    async def page_for(self, retailer: str) -> Any:
        return self.page

    async def cart_page(self, retailer: str) -> Any:
        return self.page

    async def fetch_html(self, retailer: str, url: str, *, wait_selector: str | None = None,
                         timeout_ms: int = 30_000) -> tuple[int, str]:
        self.calls.append((retailer, url, wait_selector))
        return self.status, self.html


def adapter(browser: Any) -> GameStopAdapter:
    return GameStopAdapter(http=httpx.AsyncClient(), browser=browser)


async def test_check_in_stock() -> None:
    b = FakeBrowser(200, fx("in_stock.html"))
    r = await adapter(b).check(target(), HOME)
    assert r.in_stock and r.retailer == "gamestop"
    assert b.calls == [("gamestop", URL, gs.WAIT_SELECTOR)]


async def test_check_out_of_stock() -> None:
    r = await adapter(FakeBrowser(200, fx("out_of_stock.html"))).check(target(), HOME)
    assert not r.in_stock and r.raw_status == "OUT_OF_STOCK"


@pytest.mark.parametrize("status", [403, 429])
async def test_check_http_block(status: int) -> None:
    with pytest.raises(BlockedError):
        await adapter(FakeBrowser(status, fx("in_stock.html"))).check(target(), HOME)


async def test_check_challenge_page_200() -> None:
    b = FakeBrowser(200, fx("challenge.html"))
    with pytest.raises(BlockedError):
        await adapter(b).check(target(), HOME)
    assert len(b.calls) == 1  # never retried immediately


async def test_check_404_and_500() -> None:
    with pytest.raises(AdapterError) as ei:
        await adapter(FakeBrowser(404, "<html></html>")).check(target(), HOME)
    assert ei.value.kind == ErrorKind.NOT_FOUND
    with pytest.raises(AdapterError) as ei:
        await adapter(FakeBrowser(503, "<html>oops</html>")).check(target(), HOME)
    assert ei.value.kind == ErrorKind.HTTP_ERROR


async def test_check_garbage_raises_parse_error() -> None:
    with pytest.raises(ParseError):
        await adapter(FakeBrowser(200, fx("layout_changed.html"))).check(target(), HOME)


async def test_check_config_errors() -> None:
    with pytest.raises(AdapterError) as ei:
        await adapter(None).check(target(), HOME)
    assert ei.value.kind == ErrorKind.CONFIG
    with pytest.raises(AdapterError) as ei:
        await adapter(FakeBrowser()).check(Target(retailer="gamestop"), HOME)
    assert ei.value.kind == ErrorKind.CONFIG


# ------------------------------------------------------------------ add_to_cart

class FakeLocator:
    def __init__(self, page: "FakePage", text: str = "", attrs: dict[str, str] | None = None,
                 enabled: bool = True, on_click: Any = None, exists: bool = True):
        self.page, self.text, self.attrs = page, text, attrs or {}
        self.enabled, self.on_click, self.exists = enabled, on_click, exists
        self.clicks = 0

    @property
    def first(self) -> "FakeLocator":
        return self

    async def count(self) -> int:
        return 1 if self.exists else 0

    async def inner_text(self, timeout: int | None = None) -> str:
        return self.text() if callable(self.text) else self.text

    async def get_attribute(self, name: str, timeout: int | None = None) -> str | None:
        return self.attrs.get(name)

    async def evaluate(self, script: str) -> str:
        return ""

    async def is_enabled(self) -> bool:
        return self.enabled

    async def click(self, timeout: int | None = None) -> None:
        self.clicks += 1
        self.page.clicked.append(self)
        if self.on_click:
            self.on_click()


class FakePage:
    def __init__(self, html: str):
        self.html = html
        self.cart = 0
        self.locators: dict[str, FakeLocator] = {}
        self.clicked: list[FakeLocator] = []
        self.gotos: list[str] = []
        self.waits = 0

    def add(self, selector: str, **kw: Any) -> FakeLocator:
        loc = FakeLocator(self, **kw)
        self.locators[selector] = loc
        return loc

    def locator(self, selector: str) -> FakeLocator:
        return self.locators.get(selector) or FakeLocator(self, exists=False)

    async def goto(self, url: str, **kw: Any) -> None:
        self.gotos.append(url)

    async def content(self) -> str:
        return self.html

    async def wait_for_timeout(self, ms: int) -> None:
        self.waits += 1


def product_page(html: str = "", add_label: str = "Add to Cart", increments: bool = True) -> FakePage:
    page = FakePage(html or fx("in_stock.html"))
    page.add(".minicart-quantity", text=lambda: str(page.cart))
    page.add('label[for="store-pickup"]', text="Pick Up In Store")
    page.add('label[for="home-delivery"]', text="Ship to Home")

    def bump() -> None:
        if increments:
            page.cart += 1

    page.add("button.add-to-cart", text=add_label, attrs={"class": "add-to-cart btn btn-primary"},
             on_click=bump)
    return page


def pickup_result() -> StockResult:
    return StockResult(retailer="gamestop", in_stock=True, channels=[
        ChannelAvail("store_pickup", True, store_id="6512", store_name="Frisco - Stonebriar Centre"),
        ChannelAvail("ship", True)])


@pytest.fixture
def spy_safe_click(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    seen: list[Any] = []
    real = safety.safe_click

    async def spy(locator: Any, **kw: Any) -> None:
        seen.append(locator)
        await real(locator, **kw)

    monkeypatch.setattr(gs, "safe_click", spy)
    return seen


async def test_add_to_cart_pickup_success(spy_safe_click: list[Any]) -> None:
    page = product_page()
    res = await adapter(None).add_to_cart(target(), pickup_result(), FakeBrowser(page=page))
    assert res.success, res.message
    assert "Frisco" in res.message
    assert res.cart_url == "https://www.gamestop.com/cart/"
    assert (res.cart_count_before, res.cart_count_after) == (0, 1)
    assert page.gotos == [URL]
    pickup, add = page.locators['label[for="store-pickup"]'], page.locators["button.add-to-cart"]
    assert page.clicked == [pickup, add]
    # every click went through safe_click
    assert spy_safe_click == page.clicked


async def test_add_to_cart_ship_when_no_pickup(spy_safe_click: list[Any]) -> None:
    page = product_page()
    result = StockResult(retailer="gamestop", in_stock=True, channels=[ChannelAvail("ship", True)])
    res = await adapter(None).add_to_cart(target(), result, FakeBrowser(page=page))
    assert res.success and "ship" in res.message
    assert page.clicked == [page.locators['label[for="home-delivery"]'], page.locators["button.add-to-cart"]]
    assert spy_safe_click == page.clicked


@pytest.mark.parametrize("label", ["Checkout", "Proceed to Checkout", "Buy Now", "Place Order"])
async def test_add_to_cart_never_clicks_checkout(label: str, spy_safe_click: list[Any]) -> None:
    page = product_page(add_label=label)
    res = await adapter(None).add_to_cart(target(), pickup_result(), FakeBrowser(page=page))
    assert not res.success and "refused" in res.message
    assert page.locators["button.add-to-cart"].clicks == 0
    assert page.cart == 0


async def test_add_to_cart_checkout_in_attribute_refused(spy_safe_click: list[Any]) -> None:
    page = product_page()
    page.locators["button.add-to-cart"].attrs["data-testid"] = "checkout-button"
    res = await adapter(None).add_to_cart(target(), pickup_result(), FakeBrowser(page=page))
    assert not res.success
    assert page.locators["button.add-to-cart"].clicks == 0


async def test_add_to_cart_count_unchanged() -> None:
    page = product_page(increments=False)
    a = adapter(None)
    a.cart_confirm_seconds = 1.0
    res = await a.add_to_cart(target(), pickup_result(), FakeBrowser(page=page))
    assert not res.success and "did not change" in res.message
    assert page.waits == 2


async def test_add_to_cart_signed_out() -> None:
    page = product_page(html=fx("preorder_gtm_only.html"))
    res = await adapter(None).add_to_cart(target(), pickup_result(), FakeBrowser(page=page))
    assert not res.success and res.signed_out
    assert page.clicked == []


async def test_add_to_cart_challenge_and_disabled() -> None:
    page = product_page(html=fx("challenge.html"))
    res = await adapter(None).add_to_cart(target(), pickup_result(), FakeBrowser(page=page))
    assert not res.success and page.clicked == []

    page = product_page()
    page.locators["button.add-to-cart"].enabled = False
    res = await adapter(None).add_to_cart(target(), StockResult("gamestop", True), FakeBrowser(page=page))
    assert not res.success and "disabled" in res.message
    assert page.locators["button.add-to-cart"].clicks == 0


def test_promo_widgets_and_foreign_offer_do_not_fake_in_stock() -> None:
    """Regression (live page 2026-11-14): a 'GameStop Pro' promo blob said Available and the JSON-LD
    offer for another SKU said InStock, while the product form said data-available=false."""
    t = Target(retailer="gamestop", identifiers={"url": URL, "product_id": "20037854"})
    r = parse_product_page(fx("promo_widgets_unavailable.html"), HOME, t)
    assert not r.in_stock and r.raw_status == "OUT_OF_STOCK"
    assert r.product_name == "Nintendo Switch 2 Example Edition"  # not "GameStop Pro"
    assert r.price == 519.99
