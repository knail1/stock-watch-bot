"""Tests for the Target adapter (no network: respx + fake browser/page)."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

import httpx
import pytest
import respx

from stockwatch.adapters import target as tgt
from stockwatch.adapters.base import AdapterError, BlockedError, ParseError
from stockwatch.adapters.target import (
    FULFILLMENT_URL,
    NEARBY_STORES_URL,
    PDP_URL,
    TargetAdapter,
    is_signed_in,
    parse_fulfillment,
    parse_nearby_stores,
    parse_pdp_price,
    parse_product_page,
)
from stockwatch.cart import safety
from stockwatch.config import RetailerCfg
from stockwatch.models import ErrorKind, Home, StockResult, Target

FIX = Path(__file__).parent / "fixtures" / "target"
TCIN = "94693225"


def load_json(name: str) -> Any:
    return json.loads((FIX / name).read_text())


def load_text(name: str) -> str:
    return (FIX / name).read_text()


@pytest.fixture
def home() -> Home:
    return Home(zip="75034", radius_miles=25)


@pytest.fixture
def target() -> Target:
    return Target(retailer="target", identifiers={"tcin": TCIN}, watch_id="switch2")


@pytest.fixture
def stores() -> dict[str, tgt.NearbyStore]:
    return {s.store_id: s for s in parse_nearby_stores(load_json("nearby_stores.json"))}


def chan(result: StockResult, channel: str, store_id: str | None = None):
    return next(c for c in result.channels if c.channel == channel and c.store_id == store_id)


# ------------------------------------------------------------------ pure parsing

def test_parse_in_stock(home, target, stores):
    r = parse_fulfillment(load_json("in_stock.json"), home, target, stores)
    assert r.retailer == "target" and r.in_stock and r.error is None
    assert r.raw_status == "IN_STOCK"
    assert r.product_name == "Nintendo Switch 2 Console"
    assert chan(r, "ship").available
    frisco = chan(r, "store_pickup", "1923")
    assert frisco.available and frisco.store_name == "Frisco" and frisco.distance_miles == 2.41
    assert frisco.quantity == 7 and frisco.low_stock is False
    assert chan(r, "in_store", "1923").available
    plano = chan(r, "store_pickup", "2088")
    assert plano.available and plano.low_stock is True and plano.quantity == 1 and plano.distance_miles == 6.2
    assert chan(r, "in_store", "2088").available is False  # NOT_SOLD_IN_STORE


def test_parse_out_of_stock(home, target):
    r = parse_fulfillment(load_json("out_of_stock.json"), home, target)
    assert not r.in_stock
    assert r.raw_status == "OUT_OF_STOCK"
    assert r.channels and not any(c.available for c in r.channels)
    assert {c.channel for c in r.channels} == {"ship", "store_pickup", "in_store"}


def test_parse_online_only_with_inline_price(home, target):
    r = parse_fulfillment(load_json("online_only_not_sold_in_store.json"), home, target)
    assert r.in_stock and r.raw_status == "LIMITED_STOCK"
    assert [c.channel for c in r.available_channels()] == ["ship"]
    assert chan(r, "ship").low_stock is True
    assert r.price == 449.99


def test_parse_pinned_stores_filter(home, stores):
    pinned = Target(retailer="target", identifiers={"tcin": TCIN}, store_ids=["2088"])
    r = parse_fulfillment(load_json("in_stock.json"), home, pinned, stores)
    assert {c.store_id for c in r.channels if c.store_id} == {"2088"}


@pytest.mark.parametrize("payload", [
    load_json("unknown_status.json"),
    load_json("shape_changed.json"),
    {}, [], "garbage", None, {"data": {}},
    {"data": {"product": {"fulfillment": {"store_options": "nope"}}}},
    {"data": {"product": {"fulfillment": {"store_options": [{"location_id": "1"}]}}}},
    {"data": {"product": {"fulfillment": {"shipping_options": {}}}}},
    {"errors": [{"message": "internal error"}]},
])
def test_parse_garbage_raises_parse_error(payload, home, target):
    with pytest.raises(ParseError, match="Target adapter broken"):
        parse_fulfillment(payload, home, target)


def test_parse_not_found_is_not_parse_error(home, target):
    with pytest.raises(AdapterError) as ei:
        parse_fulfillment(load_json("not_found.json"), home, target)
    assert not isinstance(ei.value, ParseError)
    assert ei.value.kind == ErrorKind.NOT_FOUND


def test_parse_nearby_and_price():
    stores = parse_nearby_stores(load_json("nearby_stores.json"))
    assert [(s.store_id, s.name, s.distance_miles) for s in stores][:2] == [
        ("1923", "Frisco", 2.41), ("2088", "Plano North", 6.2)]
    assert parse_pdp_price(load_json("pdp_price.json")) == (449.99, "Nintendo Switch 2 Console")
    for bad in ({}, {"data": {"nearby_stores": {"stores": [{"name": "x"}]}}}, "x"):
        with pytest.raises(ParseError):
            parse_nearby_stores(bad)
    for bad in ({}, {"data": {"product": {"price": {}}}}, {"data": {"product": {"price": "449"}}}):
        with pytest.raises(ParseError):
            parse_pdp_price(bad)


def test_parse_product_page_tgt_data(home, target):
    r = parse_product_page(load_text("product_page_in_stock.html"), home, target)
    assert r.in_stock and r.price == 449.99
    assert chan(r, "store_pickup", "1923").available


def test_parse_product_page_buttons_out_of_stock(home, target):
    r = parse_product_page(load_text("product_page_out_of_stock.html"), home, target)
    assert not r.in_stock and r.raw_status == "OUT_OF_STOCK" and r.price == 449.99


@pytest.mark.parametrize("name", ["product_page_garbage.html", "challenge.html"])
def test_parse_product_page_garbage(name, home, target):
    with pytest.raises(ParseError):
        parse_product_page(load_text(name), home, target)


# ------------------------------------------------------------------ URLs

@pytest.mark.parametrize("url", [
    "https://www.target.com/p/nintendo-switch-2-console/-/A-94693225",
    "https://www.target.com/p/nintendo-switch-2-console/-/A-94693225#lnk=sametab",
    "target.com/p/-/A-94693225?preselect=94693225",
    "https://www.target.com/p/x/-/A-94693225?clkid=abc&lnk=snippet",
])
def test_identifiers_from_url(url):
    assert TargetAdapter.matches_url(url)
    ids = TargetAdapter.identifiers_from_url(url)
    assert ids["tcin"] == TCIN and "#" not in ids["url"]


def test_url_rejections():
    assert not TargetAdapter.matches_url("https://www.bestbuy.com/site/x/6614313.p")
    assert not TargetAdapter.matches_url("https://nottarget.com/p/-/A-94693225")
    with pytest.raises(ValueError):
        TargetAdapter.identifiers_from_url("https://www.target.com/c/video-games/-/N-5xtg5")


def test_product_and_cart_urls(target):
    a = TargetAdapter(http=httpx.AsyncClient())
    assert a.product_url(target) == f"https://www.target.com/p/-/A-{TCIN}"
    assert a.cart_url() == "https://www.target.com/cart"
    assert a.name == "target" and a.min_interval_seconds == 90


# ------------------------------------------------------------------ check() end-to-end

class FakeBrowser:
    """Minimal BrowserCtx: fetch_html returns canned (status, html) or raises."""

    def __init__(self, status: int = 200, html: str = "", exc: Exception | None = None, page: Any = None):
        self.status, self.html, self.exc, self.page = status, html, exc, page
        self.fetched: list[str] = []

    async def page_for(self, retailer: str) -> Any:
        return self.page

    async def cart_page(self, retailer: str) -> Any:
        return self.page

    async def fetch_html(self, retailer, url, *, wait_selector=None, timeout_ms=30_000):
        self.fetched.append(url)
        if self.exc:
            raise self.exc
        return self.status, self.html


def _fulfillment_by_store(mapping: dict[str | None, Any]) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        sid = request.url.params.get("store_id")
        return httpx.Response(200, json=mapping[sid])
    return handler


@respx.mock
async def test_check_end_to_end_in_stock(home, target):
    nearby = respx.get(NEARBY_STORES_URL).mock(return_value=httpx.Response(200, json=load_json("nearby_stores.json")))
    ful = respx.get(FULFILLMENT_URL).mock(side_effect=_fulfillment_by_store({"1923": load_json("in_stock.json")}))
    pdp = respx.get(PDP_URL).mock(return_value=httpx.Response(200, json=load_json("pdp_price.json")))
    async with httpx.AsyncClient() as http:
        a = TargetAdapter(http=http, settings=RetailerCfg(extra={"source": "api"}))
        r = await a.check(target, home)
        assert r.in_stock and r.price == 449.99 and r.product_name == "Nintendo Switch 2 Console"
        # 1923 response already covered 2088; Denton (31.7 mi) is outside the 25 mi radius.
        assert ful.call_count == 1
        req = ful.calls[0].request
        assert req.url.params["tcin"] == TCIN and req.url.params["key"] == tgt.DEFAULT_API_KEY
        assert req.url.params["zip"] == "75034"
        await a.check(target, home)
    assert nearby.call_count == 1  # nearby stores cached across checks
    assert pdp.call_count == 2


@respx.mock
async def test_check_out_of_stock_merges_stores(home, target):
    respx.get(NEARBY_STORES_URL).mock(return_value=httpx.Response(200, json=load_json("nearby_stores.json")))
    oos = load_json("out_of_stock.json")
    oos_2088 = json.loads(json.dumps(oos))
    oos_2088["data"]["product"]["fulfillment"]["store_options"][0]["location_id"] = "2088"
    ful = respx.get(FULFILLMENT_URL).mock(side_effect=_fulfillment_by_store({"1923": oos, "2088": oos_2088}))
    respx.get(PDP_URL).mock(return_value=httpx.Response(200, json=load_json("pdp_price.json")))
    async with httpx.AsyncClient() as http:
        r = await TargetAdapter(http=http, settings=RetailerCfg(extra={"source": "api"})).check(target, home)
    assert not r.in_stock and r.raw_status == "OUT_OF_STOCK"
    assert ful.call_count == 2
    assert {c.store_id for c in r.channels if c.store_id} == {"1923", "2088"}
    assert sum(1 for c in r.channels if c.channel == "ship") == 1


@respx.mock
async def test_check_uses_configured_api_key_and_pinned_store(home):
    respx.get(NEARBY_STORES_URL).mock(return_value=httpx.Response(200, json=load_json("nearby_stores.json")))
    ful = respx.get(FULFILLMENT_URL).mock(return_value=httpx.Response(200, json=load_json("online_only_not_sold_in_store.json")))
    pdp = respx.get(PDP_URL).mock(return_value=httpx.Response(200, json=load_json("pdp_price.json")))
    t = Target(retailer="target", identifiers={"tcin": TCIN}, store_ids=["1923"])
    async with httpx.AsyncClient() as http:
        a = TargetAdapter(http=http, settings=RetailerCfg(extra={"api_key": "k-extra", "source": "api"}))
        r = await a.check(t, home)
    assert ful.calls[0].request.url.params["key"] == "k-extra"
    assert ful.calls[0].request.url.params["store_id"] == "1923"
    assert pdp.call_count == 0  # price was inline
    assert r.price == 449.99
    assert TargetAdapter(http=httpx.AsyncClient(), settings=RetailerCfg(api_key="k1")).api_key == "k1"


@pytest.mark.parametrize("status", [403, 429])
@respx.mock
async def test_check_blocked_status(status, home, target):
    respx.get(NEARBY_STORES_URL).mock(return_value=httpx.Response(status, text="denied"))
    browser = FakeBrowser(html=load_text("product_page_in_stock.html"))
    async with httpx.AsyncClient() as http:
        with pytest.raises(BlockedError):
            await TargetAdapter(http=http, browser=browser, settings=RetailerCfg(extra={"source": "api"})).check(target, home)
    assert browser.fetched == []  # never falls back / retries when blocked


@respx.mock
async def test_check_challenge_page_is_blocked(home, target):
    respx.get(NEARBY_STORES_URL).mock(return_value=httpx.Response(200, json=load_json("nearby_stores.json")))
    respx.get(FULFILLMENT_URL).mock(return_value=httpx.Response(
        200, text=load_text("challenge.html"), headers={"content-type": "text/html"}))
    async with httpx.AsyncClient() as http:
        with pytest.raises(BlockedError):
            await TargetAdapter(http=http, settings=RetailerCfg(extra={"source": "api"})).check(target, home)


@respx.mock
async def test_check_garbage_without_browser_is_parse_error(home, target):
    respx.get(NEARBY_STORES_URL).mock(return_value=httpx.Response(200, json=load_json("nearby_stores.json")))
    respx.get(FULFILLMENT_URL).mock(return_value=httpx.Response(200, json=load_json("shape_changed.json")))
    async with httpx.AsyncClient() as http:
        with pytest.raises(ParseError, match="Target adapter broken"):
            await TargetAdapter(http=http, settings=RetailerCfg(extra={"source": "api"})).check(target, home)


@respx.mock
async def test_check_non_json_is_parse_error(home, target):
    respx.get(NEARBY_STORES_URL).mock(return_value=httpx.Response(200, text="<html>" + "x" * 20000 + "</html>",
                                                                   headers={"content-type": "text/html"}))
    async with httpx.AsyncClient() as http:
        with pytest.raises(ParseError):
            await TargetAdapter(http=http, settings=RetailerCfg(extra={"source": "api"})).check(target, home)


@respx.mock
async def test_check_not_found(home, target):
    respx.get(NEARBY_STORES_URL).mock(return_value=httpx.Response(200, json=load_json("nearby_stores.json")))
    respx.get(FULFILLMENT_URL).mock(return_value=httpx.Response(404, json=load_json("not_found.json")))
    async with httpx.AsyncClient() as http:
        with pytest.raises(AdapterError) as ei:
            await TargetAdapter(http=http, settings=RetailerCfg(extra={"source": "api"})).check(target, home)
    assert ei.value.kind == ErrorKind.NOT_FOUND


async def test_check_missing_tcin_is_config_error(home):
    async with httpx.AsyncClient() as http:
        with pytest.raises(AdapterError) as ei:
            await TargetAdapter(http=http, settings=RetailerCfg(extra={"source": "api"})).check(Target(retailer="target"), home)
    assert ei.value.kind == ErrorKind.CONFIG


@respx.mock
async def test_check_falls_back_to_product_page(home, target):
    respx.get(NEARBY_STORES_URL).mock(return_value=httpx.Response(200, json=load_json("nearby_stores.json")))
    respx.get(FULFILLMENT_URL).mock(return_value=httpx.Response(200, json=load_json("shape_changed.json")))
    browser = FakeBrowser(html=load_text("product_page_in_stock.html"))
    async with httpx.AsyncClient() as http:
        r = await TargetAdapter(http=http, browser=browser, settings=RetailerCfg(extra={"source": "api"})).check(target, home)
    assert r.in_stock and browser.fetched == [f"https://www.target.com/p/-/A-{TCIN}"]


@pytest.mark.parametrize("browser, exc", [
    (FakeBrowser(html=load_text("product_page_garbage.html")), ParseError),
    (FakeBrowser(status=200, html=load_text("challenge.html")), BlockedError),
    (FakeBrowser(exc=BlockedError("challenge")), BlockedError),
])
@respx.mock
async def test_check_fallback_failures(browser, exc, home, target):
    respx.get(NEARBY_STORES_URL).mock(return_value=httpx.Response(200, json=load_json("nearby_stores.json")))
    respx.get(FULFILLMENT_URL).mock(return_value=httpx.Response(200, json=load_json("unknown_status.json")))
    async with httpx.AsyncClient() as http:
        with pytest.raises(exc):
            await TargetAdapter(http=http, browser=browser, settings=RetailerCfg(extra={"source": "api"})).check(target, home)


# ------------------------------------------------------------------ add_to_cart with a fake page

class FakeElement:
    def __init__(self, text: str = "", attrs: dict[str, str] | None = None, *, visible: bool = True,
                 enabled: bool = True, on_click: Callable[[], None] | None = None):
        self.text, self.attrs, self.visible, self.enabled = text, dict(attrs or {}), visible, enabled
        self.on_click = on_click
        self.clicks = 0


class FakeLocator:
    def __init__(self, page: "FakePage", selector: str):
        self.page, self.selector = page, selector

    @property
    def el(self) -> FakeElement | None:
        return self.page.elements.get(self.selector)

    @property
    def first(self) -> "FakeLocator":
        return self

    async def count(self) -> int:
        return 1 if self.el else 0

    async def is_visible(self) -> bool:
        return bool(self.el and self.el.visible)

    async def is_enabled(self) -> bool:
        return bool(self.el and self.el.enabled)

    async def inner_text(self, timeout: int | None = None) -> str:
        if not self.el:
            raise TimeoutError(self.selector)
        return self.el.text

    async def get_attribute(self, name: str, timeout: int | None = None) -> str | None:
        if not self.el:
            raise TimeoutError(self.selector)
        return self.el.attrs.get(name)

    async def evaluate(self, script: str) -> str:
        return self.el.text if self.el else ""

    async def click(self, timeout: int | None = None) -> None:
        assert self.el is not None
        self.el.clicks += 1
        self.page.clicked.append(self.selector)
        if self.el.on_click:
            self.el.on_click()


class FakePage:
    def __init__(self, elements: dict[str, FakeElement]):
        self.elements = elements
        self.clicked: list[str] = []
        self.visited: list[str] = []

    def locator(self, selector: str) -> FakeLocator:
        return FakeLocator(self, selector)

    async def goto(self, url: str, **kw: Any) -> Any:
        self.visited.append(url)
        return type("Resp", (), {"status": 200})()

    async def wait_for_timeout(self, ms: int) -> None:
        return None


CHECKOUT_SEL = 'button[data-test="addToCartModalViewCartCheckout"]'


def make_page(*, signed_in: bool = True, button_label: str = "Add to cart",
              pickup_enabled: bool = True, increments: bool = True) -> FakePage:
    cart = FakeElement("", {"aria-label": "cart 0 items"})
    elements: dict[str, FakeElement] = {
        tgt.SEL_CART_LINK: cart,
        tgt.SEL_ACCOUNT_LINK: FakeElement("Hi, Sam" if signed_in else "Sign in"),
        tgt.SEL_SHIPPING_BUTTON: FakeElement("Add to cart", {"data-test": "shippingButton"}),
        tgt.SEL_PICKUP_CELL: FakeElement("Pickup Ready within 2 hours", {"data-test": "fulfillment-cell-pickup"}),
    }

    def added() -> None:
        if increments:
            cart.attrs["aria-label"] = "cart 1 item"
        # Target pops an "added to cart" modal with a checkout button: it must never be clicked.
        elements[tgt.SEL_ADDED_MODAL] = FakeElement("Added to cart")
        elements[CHECKOUT_SEL] = FakeElement("View cart & check out", {"data-test": "addToCartModalViewCartCheckout"})

    elements[tgt.SEL_PICKUP_BUTTON] = FakeElement(button_label, {"data-test": "orderPickupButton"},
                                                  enabled=pickup_enabled, on_click=added)
    return FakePage(elements)


@pytest.fixture
def safe_click_spy(monkeypatch):
    calls: list[str] = []
    real = safety.safe_click

    async def spy(locator, **kw):
        calls.append(locator.selector)
        await real(locator, **kw)

    monkeypatch.setattr(safety, "safe_click", spy)
    return calls


def in_stock_result(channels=("store_pickup",)) -> StockResult:
    from stockwatch.models import ChannelAvail
    return StockResult(retailer="target", in_stock=True,
                       channels=[ChannelAvail(channel=c, available=True, store_id="1923") for c in channels])


async def test_add_to_cart_pickup_via_safe_click(target, safe_click_spy):
    page = make_page()
    a = TargetAdapter(http=httpx.AsyncClient())
    res = await a.add_to_cart(target, in_stock_result(), FakeBrowser(page=page))
    assert res.success and res.cart_count_before == 0 and res.cart_count_after == 1
    assert res.cart_url == "https://www.target.com/cart"
    assert page.visited == [f"https://www.target.com/p/-/A-{TCIN}"]
    assert page.clicked == [tgt.SEL_PICKUP_BUTTON]
    assert safe_click_spy == page.clicked  # every click went through safe_click
    assert page.elements[CHECKOUT_SEL].clicks == 0
    assert not any("cart" in u or "checkout" in u for u in page.visited)


async def test_add_to_cart_falls_back_to_shipping(target, safe_click_spy):
    page = make_page(pickup_enabled=False)
    ship = page.elements[tgt.SEL_SHIPPING_BUTTON]
    ship.on_click = lambda: page.elements[tgt.SEL_CART_LINK].attrs.update({"aria-label": "cart 1 item"})
    res = await TargetAdapter(http=httpx.AsyncClient()).add_to_cart(
        target, in_stock_result(("store_pickup", "ship")), FakeBrowser(page=page))
    assert res.success and "shipping" in res.message
    assert safe_click_spy == page.clicked
    assert tgt.SEL_SHIPPING_BUTTON in page.clicked and tgt.SEL_PICKUP_BUTTON not in page.clicked


async def test_add_to_cart_refuses_checkout_labelled_button(target, safe_click_spy):
    page = make_page(button_label="Check out now")
    res = await TargetAdapter(http=httpx.AsyncClient()).add_to_cart(target, in_stock_result(), FakeBrowser(page=page))
    assert not res.success and "refused" in res.message
    assert page.clicked == []
    assert page.elements[tgt.SEL_PICKUP_BUTTON].clicks == 0


async def test_add_to_cart_signed_out(target, safe_click_spy):
    page = make_page(signed_in=False)
    res = await TargetAdapter(http=httpx.AsyncClient()).add_to_cart(target, in_stock_result(), FakeBrowser(page=page))
    assert not res.success and res.signed_out
    assert page.clicked == [] and safe_click_spy == []


async def test_add_to_cart_count_unchanged_is_failure(target, safe_click_spy):
    page = make_page(increments=False)
    res = await TargetAdapter(http=httpx.AsyncClient()).add_to_cart(target, in_stock_result(), FakeBrowser(page=page))
    assert not res.success and res.cart_count_before == 0 and res.cart_count_after == 0
    assert page.elements[CHECKOUT_SEL].clicks == 0


async def test_add_to_cart_no_button(target, safe_click_spy):
    page = make_page()
    del page.elements[tgt.SEL_PICKUP_BUTTON]
    res = await TargetAdapter(http=httpx.AsyncClient()).add_to_cart(target, in_stock_result(), FakeBrowser(page=page))
    assert not res.success and "no enabled" in res.message
    assert page.clicked == [tgt.SEL_PICKUP_CELL] == safe_click_spy  # only selected the Pickup option


async def test_is_signed_in():
    assert await is_signed_in(make_page(signed_in=True)) is True
    assert await is_signed_in(make_page(signed_in=False)) is False
    assert await is_signed_in(FakePage({})) is None


# ------------------------------------------------------------------ PerimeterX block + page source

PX_BODY = {"appId": "PXGWPp4wUS", "blockScript": "https://captcha.px-cdn.net/PXGWPp4wUS/captcha.js",
           "hostUrl": "https://collector-PXGWPp4wUS.perimeterx.net", "uuid": "x", "vid": ""}


@respx.mock
async def test_perimeterx_435_is_blocked_not_http_error(home: Home, target: Target) -> None:
    route = respx.get(url__startswith=tgt.REDSKY_BASE).mock(return_value=httpx.Response(435, json=PX_BODY))
    async with httpx.AsyncClient() as http:
        with pytest.raises(BlockedError) as ei:
            await TargetAdapter(http=http, settings=RetailerCfg(extra={"source": "api"})).check(target, home)
    assert route.call_count == 1  # never retried
    assert "bot protection" in str(ei.value) and "source: page" in str(ei.value)


def test_page_is_default_source() -> None:
    assert TargetAdapter(http=None).source == "page"  # type: ignore[arg-type]
    assert TargetAdapter(http=None, settings=RetailerCfg(extra={"source": "API"})).source == "api"  # type: ignore[arg-type]
    assert TargetAdapter(http=None, settings=RetailerCfg(extra={"source": "bogus"})).source == "page"  # type: ignore[arg-type]


@respx.mock
async def test_page_source_uses_browser_and_no_api(home: Home, target: Target) -> None:
    api = respx.get(url__startswith=tgt.REDSKY_BASE).mock(return_value=httpx.Response(200, json={}))
    browser = FakeBrowser(200, (FIX / "product_page_in_stock.html").read_text())
    async with httpx.AsyncClient() as http:
        r = await TargetAdapter(http=http, browser=browser).check(target, home)
    assert r.in_stock and api.call_count == 0
    assert browser.fetched == [f"https://www.target.com/p/-/A-{TCIN}"]


async def test_page_source_challenge_is_blocked(home: Home, target: Target) -> None:
    browser = FakeBrowser(200, (FIX / "challenge.html").read_text())
    with pytest.raises(BlockedError):
        await TargetAdapter(http=None, browser=browser).check(target, home)  # type: ignore[arg-type]


async def test_page_source_without_browser_is_config_error(home: Home, target: Target) -> None:
    with pytest.raises(AdapterError) as ei:
        await TargetAdapter(http=None).check(target, home)  # type: ignore[arg-type]
    assert ei.value.kind == ErrorKind.CONFIG


def test_bot_block_detection() -> None:
    from stockwatch.adapters.base import looks_like_bot_block

    assert looks_like_bot_block(435, None)
    assert looks_like_bot_block(200, '{"blockScript":"https://captcha.px-cdn.net/x"}')
    assert not looks_like_bot_block(200, '{"data":{"product":{}}}')
