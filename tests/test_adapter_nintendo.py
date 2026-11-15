"""Nintendo Store adapter: pure parsing, URL handling, check() via a fake browser, add-to-cart safety."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import httpx
import pytest

from stockwatch.adapters import nintendo as nmod
from stockwatch.adapters.base import AdapterError, BlockedError, ParseError
from stockwatch.adapters.nintendo import (
    CART_URL,
    NintendoAdapter,
    cart_count_from_html,
    detect_queue,
    is_signed_in,
    parse_nintendo_page,
    signed_in_from_html,
)
from stockwatch.models import ErrorKind, Home, StockResult, Target

FIX = Path(__file__).parent / "fixtures" / "nintendo"
URL = "https://www.nintendo.com/us/store/products/nintendo-switch-2-system-123456/"
HOME = Home(zip="75034")


def fx(name: str) -> str:
    return (FIX / name).read_text()


def target() -> Target:
    return Target(retailer="nintendo", identifiers=NintendoAdapter.identifiers_from_url(URL), watch_id="w")


# ------------------------------------------------------------------ parsing

def test_parse_in_stock() -> None:
    r = parse_nintendo_page(fx("in_stock.html"))
    assert r.in_stock and r.raw_status == "IN_STOCK" and r.error is None
    assert r.price == 449.99 and r.product_name == "Nintendo Switch 2"
    assert [(c.channel, c.available) for c in r.channels] == [("ship", True)]


def test_parse_out_of_stock() -> None:
    r = parse_nintendo_page(fx("out_of_stock.html"))
    assert not r.in_stock and r.raw_status == "OUT_OF_STOCK" and r.error is None
    assert r.price == 449.99
    assert [(c.channel, c.available) for c in r.channels] == [("ship", False)]


def test_parse_apollo_sold_out_uses_prices_minimum() -> None:
    r = parse_nintendo_page(fx("sold_out_apollo.html"))
    assert not r.in_stock and r.raw_status == "OUT_OF_STOCK"
    assert r.price == 499.99 and r.product_name.startswith("Nintendo Switch 2 + Mario Kart")


def test_parse_coming_soon_falls_back_to_reg_price() -> None:
    r = parse_nintendo_page(fx("coming_soon.html"))
    assert not r.in_stock and r.raw_status == "COMING_SOON" and r.price == 84.99


def test_parse_queue_is_info_not_error() -> None:
    r = parse_nintendo_page(fx("queue.html"))
    assert r.raw_status == "QUEUE" and not r.in_stock and r.error is None
    assert "queue" in (r.error_detail or "").lower()
    assert detect_queue(fx("queue.html"))


def test_queue_script_on_product_page_is_not_a_queue() -> None:
    # in_stock.html embeds the Queue-it client script; that alone must not flag a queue.
    assert not detect_queue(fx("in_stock.html"))


def test_parse_invite_only() -> None:
    r = parse_nintendo_page(fx("invite_only.html"))
    assert r.raw_status == "INVITE_ONLY" and not r.in_stock and r.error is None
    assert "invitation" in (r.error_detail or "").lower() and r.price == 449.99


def test_invite_in_availability_field() -> None:
    html = ('<html><body><script id="__NEXT_DATA__" type="application/json">'
            '{"props":{"pageProps":{"product":{"name":"X","price":{"finalPrice":1},'
            '"availability":["Invite required"]}}}}</script></body></html>')
    assert parse_nintendo_page(html).raw_status == "INVITE_ONLY"


def test_parse_challenge_raises_blocked() -> None:
    with pytest.raises(BlockedError):
        parse_nintendo_page(fx("challenge.html"))


def test_parse_garbage_raises_parse_error() -> None:
    with pytest.raises(ParseError):
        parse_nintendo_page(fx("garbage.html"))
    with pytest.raises(ParseError):
        parse_nintendo_page("")
    with pytest.raises(ParseError):
        parse_nintendo_page("<html><body><p>hello</p></body></html>")


def test_parse_unknown_availability_without_buttons_raises() -> None:
    html = ('<html><body><script id="__NEXT_DATA__" type="application/json">'
            '{"props":{"pageProps":{"product":{"name":"X","price":{"finalPrice":1},'
            '"availability":["Mystery state"]}}}}</script></body></html>')
    with pytest.raises(ParseError):
        parse_nintendo_page(html)


def test_button_only_fallback() -> None:
    assert parse_nintendo_page("<html><body><button>Add to cart</button></body></html>").in_stock
    r = parse_nintendo_page("<html><body><button disabled>Add to cart</button></body></html>")
    assert not r.in_stock and r.raw_status == "OUT_OF_STOCK"


def test_json_available_but_button_disabled_trusts_page() -> None:
    html = fx("in_stock.html").replace('<button type="button" data-testid="addToCartButton">',
                                       '<button type="button" disabled data-testid="addToCartButton">')
    assert not parse_nintendo_page(html).in_stock


def test_signed_in_and_cart_count_helpers() -> None:
    assert signed_in_from_html(fx("in_stock.html")) is True
    assert signed_in_from_html(fx("sold_out_apollo.html")) is False
    assert signed_in_from_html(fx("queue.html")) is None
    assert cart_count_from_html(fx("in_stock.html")) == 0
    assert cart_count_from_html('<a href="/us/cart/" aria-label="Cart, 2 items"></a>') == 2
    assert cart_count_from_html('<a href="/us/cart/"><span>3</span></a>') == 3
    assert cart_count_from_html("<p>no cart</p>") is None


# ------------------------------------------------------------------ URLs

def test_identifiers_from_url_and_matches() -> None:
    ids = NintendoAdapter.identifiers_from_url(URL + "?sid=abc#reviews")
    assert ids["product_id"] == "nintendo-switch-2-system-123456"
    assert ids["url"] == URL + "?sid=abc"
    assert NintendoAdapter.matches_url(URL)
    assert NintendoAdapter.matches_url("nintendo.com/us/store/products/x/")
    assert not NintendoAdapter.matches_url("https://www.bestbuy.com/site/x/123.p")
    with pytest.raises(ValueError):
        NintendoAdapter.identifiers_from_url("https://www.nintendo.com/us/games/")
    with pytest.raises(ValueError):
        NintendoAdapter.identifiers_from_url("https://evil.example/store/products/foo/")


def test_product_and_cart_urls() -> None:
    ad = NintendoAdapter(http=httpx.AsyncClient())
    assert ad.product_url(target()) == URL
    assert ad.product_url(Target("nintendo", {"product_id": "abc"})) == \
        "https://www.nintendo.com/us/store/products/abc/"
    assert ad.cart_url() == CART_URL == "https://www.nintendo.com/us/cart/"
    with pytest.raises(AdapterError) as ei:
        ad.product_url(Target("nintendo", {}))
    assert ei.value.kind == ErrorKind.CONFIG
    assert NintendoAdapter.min_interval_seconds == 120 and NintendoAdapter.name == "nintendo"


# ------------------------------------------------------------------ check()

class FakeBrowser:
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


async def test_check_in_and_out_of_stock() -> None:
    async with httpx.AsyncClient() as http:
        b = FakeBrowser(html=fx("in_stock.html"))
        r = await NintendoAdapter(http=http, browser=b).check(target(), HOME)
        assert isinstance(r, StockResult) and r.in_stock and r.price == 449.99
        assert b.calls == [("nintendo", URL)]
        b = FakeBrowser(html=fx("out_of_stock.html"))
        assert not (await NintendoAdapter(http=http, browser=b).check(target(), HOME)).in_stock


async def test_check_queue_returns_result() -> None:
    b = FakeBrowser(html=fx("queue.html"))
    r = await NintendoAdapter(http=httpx.AsyncClient(), browser=b).check(target(), HOME)
    assert r.raw_status == "QUEUE" and r.error is None


@pytest.mark.parametrize("status", [403, 429])
async def test_check_http_blocked(status: int) -> None:
    b = FakeBrowser(status=status, html=fx("in_stock.html"))
    with pytest.raises(BlockedError):
        await NintendoAdapter(http=httpx.AsyncClient(), browser=b).check(target(), HOME)


async def test_check_challenge_page_blocked() -> None:
    b = FakeBrowser(status=200, html=fx("challenge.html"))
    with pytest.raises(BlockedError):
        await NintendoAdapter(http=httpx.AsyncClient(), browser=b).check(target(), HOME)


async def test_check_browser_raises_blocked_propagates() -> None:
    b = FakeBrowser(exc=BlockedError("challenge"))
    with pytest.raises(BlockedError):
        await NintendoAdapter(http=httpx.AsyncClient(), browser=b).check(target(), HOME)


async def test_check_errors() -> None:
    ad = NintendoAdapter(http=httpx.AsyncClient(), browser=FakeBrowser(status=404, html="x"))
    with pytest.raises(AdapterError) as ei:
        await ad.check(target(), HOME)
    assert ei.value.kind == ErrorKind.NOT_FOUND
    ad = NintendoAdapter(http=httpx.AsyncClient(), browser=FakeBrowser(html=fx("garbage.html")))
    with pytest.raises(ParseError):
        await ad.check(target(), HOME)
    with pytest.raises(AdapterError) as ei:
        await NintendoAdapter(http=httpx.AsyncClient(), browser=None).check(target(), HOME)
    assert ei.value.kind == ErrorKind.CONFIG


# ------------------------------------------------------------------ add_to_cart

class FakeLocator:
    def __init__(self, page: "FakePage", text: str | None, attrs: dict[str, str] | None = None,
                 enabled: bool = True):
        self.page, self.text, self.attrs, self.enabled = page, text, attrs or {}, enabled
        self.clicked = 0

    @property
    def first(self) -> "FakeLocator":
        return self

    async def count(self) -> int:
        return 0 if self.text is None else 1

    async def is_enabled(self) -> bool:
        return self.enabled

    async def inner_text(self, timeout: int | None = None) -> str:
        return self.text or ""

    async def get_attribute(self, name: str, timeout: int | None = None) -> str | None:
        return self.attrs.get(name)

    async def evaluate(self, script: str) -> str:
        return self.text or ""

    async def click(self, timeout: int | None = None) -> None:
        self.clicked += 1
        self.page.on_click()


class FakePage:
    def __init__(self, html: str, button_text: str | None = "Add to cart", after_html: str | None = None,
                 button_attrs: dict[str, str] | None = None, enabled: bool = True):
        self.html, self.after_html = html, after_html
        self.url = "about:blank"
        self.gotos: list[str] = []
        self.roles: list[tuple[str, Any]] = []
        self.button = FakeLocator(self, button_text, button_attrs, enabled)

    async def goto(self, url: str, **kw: Any) -> None:
        self.gotos.append(url)
        self.url = url

    async def content(self) -> str:
        return self.html

    def get_by_role(self, role: str, name: Any = None) -> FakeLocator:
        self.roles.append((role, name))
        return self.button

    async def wait_for_timeout(self, ms: int) -> None:
        return None

    def on_click(self) -> None:
        if self.after_html is not None:
            self.html = self.after_html


IN_STOCK_AFTER = fx("in_stock.html").replace('aria-label="Cart, 0 items"', 'aria-label="Cart, 1 item"')


@pytest.fixture
def spy_safe_click(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    calls: list[Any] = []
    real = nmod.safe_click

    async def spy(locator: Any, **kw: Any) -> None:
        calls.append(locator)
        await real(locator, **kw)

    monkeypatch.setattr(nmod, "safe_click", spy)
    return calls


async def test_add_to_cart_success_via_safe_click(spy_safe_click: list[Any]) -> None:
    page = FakePage(fx("in_stock.html"), after_html=IN_STOCK_AFTER)
    res = await NintendoAdapter(http=httpx.AsyncClient()).add_to_cart(
        target(), parse_nintendo_page(fx("in_stock.html")), FakeBrowser(page=page))
    assert res.success and res.cart_url == CART_URL
    assert (res.cart_count_before, res.cart_count_after) == (0, 1)
    assert spy_safe_click == [page.button] and page.button.clicked == 1
    assert page.gotos == [URL]
    role, name = page.roles[0]
    assert role == "button" and isinstance(name, re.Pattern) and name.match("Add to cart")
    assert not name.match("Checkout")
    assert not any("checkout" in u for u in page.gotos)


async def test_add_to_cart_never_clicks_checkout_labelled_button(spy_safe_click: list[Any]) -> None:
    page = FakePage(fx("in_stock.html"), button_text="Checkout", after_html=IN_STOCK_AFTER)
    res = await NintendoAdapter(http=httpx.AsyncClient()).add_to_cart(
        target(), StockResult("nintendo", True), FakeBrowser(page=page))
    assert not res.success and "refused" in res.message
    assert spy_safe_click and page.button.clicked == 0


async def test_add_to_cart_never_clicks_hidden_checkout_attribute(spy_safe_click: list[Any]) -> None:
    page = FakePage(fx("in_stock.html"), button_text="Add to cart",
                    button_attrs={"data-testid": "checkout-now-btn"})
    res = await NintendoAdapter(http=httpx.AsyncClient()).add_to_cart(
        target(), StockResult("nintendo", True), FakeBrowser(page=page))
    assert not res.success and page.button.clicked == 0


async def test_add_to_cart_signed_out() -> None:
    page = FakePage(fx("sold_out_apollo.html").replace("disabled>Sold out", ">Add to cart"))
    res = await NintendoAdapter(http=httpx.AsyncClient()).add_to_cart(
        target(), StockResult("nintendo", True), FakeBrowser(page=page))
    assert not res.success and res.signed_out and page.button.clicked == 0


async def test_add_to_cart_queue_missing_disabled_and_unconfirmed() -> None:
    ad = NintendoAdapter(http=httpx.AsyncClient())
    sr = StockResult("nintendo", True)
    page = FakePage(fx("queue.html"))
    assert "queue" in (await ad.add_to_cart(target(), sr, FakeBrowser(page=page))).message
    page = FakePage(fx("in_stock.html"), button_text=None)
    assert not (await ad.add_to_cart(target(), sr, FakeBrowser(page=page))).success
    page = FakePage(fx("in_stock.html"), enabled=False)
    res = await ad.add_to_cart(target(), sr, FakeBrowser(page=page))
    assert not res.success and page.button.clicked == 0
    page = FakePage(fx("in_stock.html"))  # click works but badge never changes
    res = await ad.add_to_cart(target(), sr, FakeBrowser(page=page))
    assert not res.success and page.button.clicked == 1 and res.cart_count_after == 0


async def test_is_signed_in_hook() -> None:
    assert await is_signed_in(FakePage(fx("in_stock.html"))) is True
    assert await is_signed_in(FakePage(fx("sold_out_apollo.html"))) is False


# ---- 2026 store layout: Apollo cache with argument-suffixed keys and isSalableQty ----

def _apollo_target() -> Target:
    return Target(retailer="nintendo", identifiers={"url": "https://www.nintendo.com/us/store/products/example-console-900001/",
                                                     "product_id": "example-console-900001"})


def test_apollo_sold_out_page() -> None:
    r = parse_nintendo_page(fx("apollo_sold_out.html"), HOME, _apollo_target())
    assert not r.in_stock and r.raw_status == "OUT_OF_STOCK"
    assert r.price == 519.99 and r.product_name == "Example Console Edition"  # not the decoy controller


def test_apollo_in_stock_page() -> None:
    r = parse_nintendo_page(fx("apollo_in_stock.html"), HOME, _apollo_target())
    assert r.in_stock and r.raw_status == "IN_STOCK" and r.price == 519.99
