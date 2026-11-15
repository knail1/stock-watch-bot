"""Tests for the Best Buy adapter (API parsing, check(), blocked handling, add-to-cart safety)."""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Callable

import httpx
import pytest
import respx

from stockwatch.adapters import bestbuy
from stockwatch.adapters.base import AdapterError, BlockedError, ParseError
from stockwatch.adapters.bestbuy import (
    ADD_TO_CART_SELECTOR,
    ACCOUNT_BUTTON_SELECTOR,
    CART_COUNT_SELECTOR,
    PICKUP_OPTION_SELECTOR,
    BestBuyAdapter,
    is_signed_in,
    parse_bestbuy,
    parse_product,
    parse_stores,
)
from stockwatch.config import RetailerCfg
from stockwatch.models import ErrorKind, Home, StockResult, Target

FIX = Path(__file__).parent / "fixtures" / "bestbuy"
SKU = "6614313"
KEY = "sekrit-key-123"
STORES_URL = f"https://api.bestbuy.com/v1/products/{SKU}/stores.json"
PRODUCT_URL = f"https://api.bestbuy.com/v1/products/{SKU}.json"
HOME = Home(zip="75034", radius_miles=25)


def load(name: str) -> Any:
    text = (FIX / name).read_text()
    return json.loads(text) if name.endswith(".json") else text


def tgt(**kw: Any) -> Target:
    return Target(retailer="bestbuy", identifiers={"sku": SKU}, watch_id="switch2", **kw)


def adapter(http: httpx.AsyncClient, key: str = KEY) -> BestBuyAdapter:
    return BestBuyAdapter(http=http, settings=RetailerCfg(api_key=key))


# ------------------------------------------------------------------ parsing

def test_parse_in_stock_filters_radius_and_sorts() -> None:
    r = parse_bestbuy(load("in_stock.json"), HOME, tgt())
    assert r.in_stock and r.retailer == "bestbuy"
    assert r.price == 449.99 and r.product_name == "Nintendo Switch 2 System"
    pickups = [c for c in r.channels if c.channel == "store_pickup"]
    assert [c.store_id for c in pickups] == ["1031", "281"]  # Denton (31.4 mi) dropped
    assert pickups[0].store_name == "Best Buy Frisco" and pickups[0].distance_miles == 6.2
    assert pickups[0].low_stock is True and pickups[1].low_stock is False
    ship = [c for c in r.channels if c.channel == "ship"]
    assert len(ship) == 1 and ship[0].available
    assert r.raw_status == "IN_STOCK"


def test_parse_in_stock_store_ids_filter() -> None:
    r = parse_bestbuy(load("in_stock.json"), HOME, tgt(store_ids=["1031"]))
    pickups = [c for c in r.channels if c.channel == "store_pickup"]
    assert [c.store_id for c in pickups] == ["1031"]
    assert r.raw_status == "LIMITED"  # only matched store is low stock


def test_parse_out_of_stock() -> None:
    r = parse_bestbuy(load("out_of_stock.json"), HOME, tgt())
    assert not r.in_stock and r.raw_status == "OUT_OF_STOCK" and r.error is None
    assert [(c.channel, c.available) for c in r.channels] == [("ship", False)]


def test_parse_online_only() -> None:
    r = parse_bestbuy(load("online_only.json"), HOME, tgt())
    assert r.in_stock and r.price == 429.99
    assert [c.channel for c in r.available_channels()] == ["ship"]


def test_parse_far_stores_only_is_out_of_stock() -> None:
    r = parse_bestbuy(load("far_stores_only.json"), HOME, tgt())
    assert not r.in_stock
    wide = parse_bestbuy(load("far_stores_only.json"), Home(zip="75034", radius_miles=50), tgt())
    assert wide.in_stock and wide.available_channels()[0].store_id == "1498"


@pytest.mark.parametrize("payload", [
    None, "garbage", [], {}, {"product": {}, "stores": {}},
    {"product": load("in_stock.json")["product"], "stores": load("api_error.json")},
    {"product": {"sku": 1}, "stores": {"stores": []}},
    {"product": load("in_stock.json")["product"], "stores": {"stores": [{"name": "x"}]}},
])
def test_parse_garbage_raises(payload: Any) -> None:
    with pytest.raises(ParseError):
        parse_bestbuy(payload, HOME, tgt())


def test_parse_parts_direct() -> None:
    assert parse_product({"sku": 1, "onlineAvailability": True, "orderable": "PreOrder"})[2] is True
    assert parse_product({"sku": 1, "onlineAvailability": True, "orderable": "Sold Out"})[2] is False
    assert parse_stores({"ispuEligible": False, "stores": [{"storeID": 1}]}, HOME, tgt()) == []


# ------------------------------------------------------------------ URLs

@pytest.mark.parametrize("url", [
    "https://www.bestbuy.com/site/nintendo-switch-2-system-black/6614313.p?skuId=6614313",
    "https://www.bestbuy.com/site/6614313.p?skuId=6614313#anchor",
    "https://bestbuy.com/site/nintendo-switch-2/6614313.p",
])
def test_identifiers_from_url(url: str) -> None:
    assert BestBuyAdapter.matches_url(url)
    ids = BestBuyAdapter.identifiers_from_url(url)
    assert ids["sku"] == SKU and "#" not in ids["url"]


def test_url_mismatch_and_missing_sku() -> None:
    assert not BestBuyAdapter.matches_url("https://www.target.com/p/-/A-12345")
    assert not BestBuyAdapter.matches_url("https://notbestbuy.com/site/6614313.p")
    with pytest.raises(ValueError):
        BestBuyAdapter.identifiers_from_url("https://www.bestbuy.com/site/searchpage.jsp?st=switch")


def test_product_and_cart_url() -> None:
    a = BestBuyAdapter(http=None)  # type: ignore[arg-type]
    assert a.product_url(tgt()) == f"https://www.bestbuy.com/site/{SKU}.p?skuId={SKU}"
    assert a.cart_url() == "https://www.bestbuy.com/cart"


# ------------------------------------------------------------------ check()

@respx.mock
async def test_check_in_stock_end_to_end() -> None:
    fx = load("in_stock.json")
    stores = respx.get(STORES_URL).mock(return_value=httpx.Response(200, json=fx["stores"]))
    product = respx.get(PRODUCT_URL).mock(return_value=httpx.Response(200, json=fx["product"]))
    async with httpx.AsyncClient() as http:
        r = await adapter(http).check(tgt(), HOME)
    assert r.in_stock and r.price == 449.99
    q = stores.calls.last.request.url.params
    assert q["postalCode"] == "75034" and q["apiKey"] == KEY
    assert "onlineAvailability" in product.calls.last.request.url.params["show"]


@respx.mock
async def test_check_out_of_stock_end_to_end() -> None:
    fx = load("out_of_stock.json")
    respx.get(STORES_URL).mock(return_value=httpx.Response(200, json=fx["stores"]))
    respx.get(PRODUCT_URL).mock(return_value=httpx.Response(200, json=fx["product"]))
    async with httpx.AsyncClient() as http:
        r = await adapter(http).check(tgt(), HOME)
    assert not r.in_stock and r.error is None


@pytest.mark.parametrize("status", [403, 429])
@respx.mock
async def test_check_blocked_status_no_retry(status: int) -> None:
    stores = respx.get(STORES_URL).mock(return_value=httpx.Response(status, json={"errorCode": "403"}))
    product = respx.get(PRODUCT_URL).mock(return_value=httpx.Response(200, json={}))
    async with httpx.AsyncClient() as http:
        with pytest.raises(BlockedError) as ei:
            await adapter(http).check(tgt(), HOME)
    assert stores.call_count == 1 and product.call_count == 0  # never retried, stopped early
    assert KEY not in str(ei.value)


@respx.mock
async def test_check_challenge_page_is_blocked() -> None:
    respx.get(STORES_URL).mock(return_value=httpx.Response(
        200, text=load("challenge.html"), headers={"content-type": "text/html"}))
    async with httpx.AsyncClient() as http:
        with pytest.raises(BlockedError):
            await adapter(http).check(tgt(), HOME)


@respx.mock
async def test_check_garbage_raises_parse_error() -> None:
    respx.get(STORES_URL).mock(return_value=httpx.Response(200, text="<html>hello</html>"))
    async with httpx.AsyncClient() as http:
        with pytest.raises(ParseError):
            await adapter(http).check(tgt(), HOME)
    respx.get(STORES_URL).mock(return_value=httpx.Response(200, json={"unexpected": True}))
    respx.get(PRODUCT_URL).mock(return_value=httpx.Response(200, json={"sku": 1}))
    async with httpx.AsyncClient() as http:
        with pytest.raises(ParseError):
            await adapter(http).check(tgt(), HOME)


@respx.mock
async def test_check_timeout_and_404() -> None:
    respx.get(STORES_URL).mock(side_effect=httpx.ConnectTimeout("boom"))
    async with httpx.AsyncClient() as http:
        with pytest.raises(AdapterError) as ei:
            await adapter(http).check(tgt(), HOME)
    assert ei.value.kind == ErrorKind.TIMEOUT
    respx.get(STORES_URL).mock(return_value=httpx.Response(404, json={"errorCode": "404"}))
    async with httpx.AsyncClient() as http:
        with pytest.raises(AdapterError) as ei:
            await adapter(http).check(tgt(), HOME)
    assert ei.value.kind == ErrorKind.NOT_FOUND


@pytest.mark.parametrize("key", ["", "${BESTBUY_API_KEY}"])
async def test_check_missing_api_key_is_config_failure(key: str) -> None:
    async with httpx.AsyncClient() as http:
        r = await adapter(http, key=key).check(tgt(), HOME)
    assert r.error == ErrorKind.CONFIG and not r.in_stock
    r2 = await BestBuyAdapter(http=None, settings=None).check(tgt(), HOME)  # type: ignore[arg-type]
    assert r2.error == ErrorKind.CONFIG


async def test_check_missing_sku_is_config_failure() -> None:
    r = await BestBuyAdapter(http=None, settings=RetailerCfg(api_key=KEY)).check(  # type: ignore[arg-type]
        Target(retailer="bestbuy", identifiers={}), HOME)
    assert r.error == ErrorKind.CONFIG


def test_api_key_redacted_from_httpx_logs(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="httpx"):
        logging.getLogger("httpx").info('HTTP Request: %s %s "%s"', "GET",
                                        httpx.URL(STORES_URL, params={"apiKey": KEY}), "HTTP/1.1 200 OK")
    assert caplog.records and KEY not in caplog.text and "apiKey=***" in caplog.text


# ------------------------------------------------------------------ add_to_cart fakes

class FakeLocator:
    """Minimal Playwright Locator stand-in; a locator resolving to zero or more elements."""

    def __init__(self, text: str = "", attrs: dict[str, str] | None = None, *,
                 disabled: bool = False, on_click: Callable[[], None] | None = None,
                 items: list["FakeLocator"] | None = None):
        self.text, self.attrs, self.disabled, self.on_click = text, attrs or {}, disabled, on_click
        self.items = [self] if items is None else items
        self.clicks = 0

    @property
    def first(self) -> "FakeLocator":
        return self.items[0] if self.items else self  # lazy like Playwright: fails on use

    async def count(self) -> int:
        return len(self.items)

    async def inner_text(self, timeout: int = 0) -> str:
        if not self.items:
            raise TimeoutError("no element")
        return self.first.text

    async def get_attribute(self, name: str, timeout: int = 0) -> str | None:
        return self.first.attrs.get(name)

    async def evaluate(self, js: str) -> str:
        return self.first.text

    async def wait_for(self, state: str = "visible", timeout: int = 0) -> None:
        if not self.items:
            raise TimeoutError("not visible")

    async def is_disabled(self) -> bool:
        return self.first.disabled

    async def click(self, timeout: int = 0) -> None:
        self.clicks += 1
        if self.on_click:
            self.on_click()


EMPTY = FakeLocator(items=[])


class FakePage:
    def __init__(self, selectors: dict[str, FakeLocator], html: str = "<html>product</html>", status: int = 200):
        self.selectors, self.html, self.status = selectors, html, status
        self.visited: list[str] = []

    async def goto(self, url: str, **kw: Any) -> Any:
        self.visited.append(url)
        return type("Resp", (), {"status": self.status})()

    async def content(self) -> str:
        return self.html

    def locator(self, selector: str) -> FakeLocator:
        return self.selectors.get(selector, EMPTY)


class FakeBrowser:
    def __init__(self, page: FakePage):
        self.page = page

    async def page_for(self, retailer: str) -> FakePage:
        return self.page

    async def cart_page(self, retailer: str) -> FakePage:
        assert retailer == "bestbuy"
        return self.page

    async def fetch_html(self, retailer: str, url: str, **kw: Any) -> tuple[int, str]:
        return self.page.status, self.page.html


def make_page(atc_text: str = "Add to Cart", signed_in: str = "Hi, Sam", disabled: bool = False,
              atc_attrs: dict[str, str] | None = None) -> tuple[FakePage, dict[str, FakeLocator]]:
    cart = FakeLocator("1")

    def bump() -> None:
        cart.text = str(int(cart.text) + 1)

    els = {
        "atc": FakeLocator(atc_text, atc_attrs or {"class": "add-to-cart-button"}, disabled=disabled, on_click=bump),
        "pickup": FakeLocator("Pickup", {"class": "fulfillment-option"}),
        "cart": cart,
        "account": FakeLocator(signed_in),
    }
    page = FakePage({ADD_TO_CART_SELECTOR: els["atc"], PICKUP_OPTION_SELECTOR: els["pickup"],
                     CART_COUNT_SELECTOR: cart, ACCOUNT_BUTTON_SELECTOR: els["account"]})
    return page, els


@pytest.fixture
def safe_calls(monkeypatch: pytest.MonkeyPatch) -> list[FakeLocator]:
    """Record every click routed through safe_click (the real one still runs)."""
    calls: list[FakeLocator] = []
    real = bestbuy.safe_click

    async def spy(locator: Any, **kw: Any) -> None:
        calls.append(locator)
        await real(locator, **kw)

    monkeypatch.setattr(bestbuy, "safe_click", spy)
    monkeypatch.setattr(bestbuy, "CART_POLL_INTERVAL_S", 0)
    return calls


def in_stock_result() -> StockResult:
    return parse_bestbuy(load("in_stock.json"), HOME, tgt())


async def test_add_to_cart_pickup_success(safe_calls: list[FakeLocator]) -> None:
    page, els = make_page()
    res = await BestBuyAdapter(http=None).add_to_cart(tgt(), in_stock_result(), FakeBrowser(page))  # type: ignore[arg-type]
    assert res.success, res.message
    assert res.cart_count_before == 1 and res.cart_count_after == 2
    assert res.cart_url == "https://www.bestbuy.com/cart" and "Frisco" in res.message
    assert page.visited == [f"https://www.bestbuy.com/site/{SKU}.p?skuId={SKU}"]
    assert safe_calls == [els["pickup"], els["atc"]]  # every click went through safe_click
    assert els["pickup"].clicks == 1 and els["atc"].clicks == 1


async def test_add_to_cart_ship_only_skips_pickup(safe_calls: list[FakeLocator]) -> None:
    page, els = make_page()
    result = parse_bestbuy(load("online_only.json"), HOME, tgt())
    res = await BestBuyAdapter(http=None).add_to_cart(tgt(), result, FakeBrowser(page))  # type: ignore[arg-type]
    assert res.success and els["pickup"].clicks == 0 and safe_calls == [els["atc"]]


@pytest.mark.parametrize("text,attrs", [
    ("Checkout", {"class": "btn"}),
    ("Add to Cart", {"class": "btn", "data-track": "checkout-button"}),
    ("Buy Now", {}),
])
async def test_add_to_cart_never_clicks_checkout(safe_calls: list[FakeLocator], text: str,
                                                 attrs: dict[str, str]) -> None:
    page, els = make_page(atc_text=text, atc_attrs=attrs)
    res = await BestBuyAdapter(http=None).add_to_cart(tgt(), in_stock_result(), FakeBrowser(page))  # type: ignore[arg-type]
    assert not res.success and "checkout" in res.message
    assert els["atc"].clicks == 0 and els["atc"] in safe_calls


async def test_add_to_cart_signed_out(safe_calls: list[FakeLocator]) -> None:
    page, els = make_page(signed_in="Account")
    res = await BestBuyAdapter(http=None).add_to_cart(tgt(), in_stock_result(), FakeBrowser(page))  # type: ignore[arg-type]
    assert not res.success and res.signed_out and safe_calls == []


async def test_add_to_cart_challenge_page(safe_calls: list[FakeLocator]) -> None:
    page, _ = make_page()
    page.html = load("challenge.html")
    res = await BestBuyAdapter(http=None).add_to_cart(tgt(), in_stock_result(), FakeBrowser(page))  # type: ignore[arg-type]
    assert not res.success and "challenge" in res.message and safe_calls == []


async def test_add_to_cart_disabled_and_count_unchanged(safe_calls: list[FakeLocator]) -> None:
    page, els = make_page(disabled=True)
    res = await BestBuyAdapter(http=None).add_to_cart(tgt(), in_stock_result(), FakeBrowser(page))  # type: ignore[arg-type]
    assert not res.success and els["atc"].clicks == 0

    page, els = make_page()
    els["atc"].on_click = None  # click happens but the badge never moves
    res = await BestBuyAdapter(http=None).add_to_cart(tgt(), in_stock_result(), FakeBrowser(page))  # type: ignore[arg-type]
    assert not res.success and res.cart_count_after == 1 and els["atc"].clicks == 1


async def test_add_to_cart_missing_button(safe_calls: list[FakeLocator]) -> None:
    page = FakePage({})
    res = await BestBuyAdapter(http=None).add_to_cart(tgt(), in_stock_result(), FakeBrowser(page))  # type: ignore[arg-type]
    assert not res.success and "not found" in res.message and safe_calls == []


@pytest.mark.parametrize("text,expected", [("Hi, Sam", True), ("Account", False), ("Sign In", False), ("??", None)])
async def test_is_signed_in(text: str, expected: bool | None) -> None:
    assert await is_signed_in(FakePage({ACCOUNT_BUTTON_SELECTOR: FakeLocator(text)})) is expected
    assert await is_signed_in(FakePage({})) is None
