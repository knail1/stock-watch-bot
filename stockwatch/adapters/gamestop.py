"""GameStop adapter: product page read through the real Chrome profile (spec section 6).

GameStop has no public stock API. The product page (Salesforce Commerce Cloud)
exposes availability in several places, all of which the parser consults:

* ``data-gtmdata`` JSON on the product container (``productInfo.availability``,
  ``price``, ``name``),
* the ``utag_data`` Tealium object (``product_availability``, ``product_price``,
  ``store_id``, ``user_login_status``),
* JSON-LD ``Product`` offers,
* the fulfillment widget: a "Ship to Home" option and a "Pick Up In Store"
  option carrying the user's set store ("Available at <store>" /
  "Not Available at <store>" / "Only 2 left at <store>").

Local stock is partial: only the store the user picked on gamestop.com is
reported. Akamai "Access Denied" and CAPTCHA interstitials raise BlockedError.
"""
from __future__ import annotations

import json
import re
from typing import Any, ClassVar

from selectolax.lexbor import LexborHTMLParser as HTMLParser
from selectolax.lexbor import LexborNode as Node

from ..cart.safety import ForbiddenClickError, safe_click
from ..models import CartResult, ChannelAvail, ErrorKind, Home, StockResult, Target
from .base import (
    LOCAL_CHANNELS,
    AdapterError,
    BaseAdapter,
    BlockedError,
    BrowserCtx,
    ParseError,
    looks_like_challenge,
    parse_price,
    pickup_store_note,
    resolve_cart_channel,
)

BASE_URL = "https://www.gamestop.com"
CART_URL = f"{BASE_URL}/cart/"

# Something present on every rendered product page (in or out of stock).
WAIT_SELECTOR = "[data-gtmdata], .product-detail, button.add-to-cart"

SHIP_SELECTORS = ('[data-fulfillment="ship"]', ".delivery-option.home-delivery", "#home-delivery-option")
PICKUP_SELECTORS = ('[data-fulfillment="store_pickup"]', ".delivery-option.store-pickup", "#store-pickup-option")

# Click targets used by add_to_cart (tried in order; every click goes through safe_click).
PICKUP_CLICK_SELECTORS = ('label[for="store-pickup"]', '[data-fulfillment="store_pickup"] input[type="radio"]',
                          'input[type="radio"][value="BOPIS"]')
SHIP_CLICK_SELECTORS = ('label[for="home-delivery"]', '[data-fulfillment="ship"] input[type="radio"]',
                        'input[type="radio"][value="HOME_DELIVERY"]')
ADD_TO_CART_SELECTORS = ("button.add-to-cart", 'button[data-buttontext="Add to Cart"]',
                         'button:has-text("Add to Cart")')
CART_COUNT_SELECTORS = (".minicart-quantity", ".cart-quantity", "[data-cart-count]")

_NEGATIVE = ("not available", "notavailable", "unavailable", "out of stock", "outofstock",
             "sold out", "soldout", "discontinued", "backorder", "no longer available")
_POSITIVE = ("in stock", "instock", "available", "pre-order", "preorder", "limited",
             "add to cart", " left", "ready for pickup", "ships")
_STORE_TEXT_RE = re.compile(
    r"(not available|unavailable|available|in stock|limited stock|only\s+\d+\s+left)\s+(?:at|in)\s+(.+)",
    re.IGNORECASE)
_QTY_RE = re.compile(r"only\s+(\d+)\s+left", re.IGNORECASE)
_DIST_RE = re.compile(r"\(?\s*(\d+(?:\.\d+)?)\s*mi(?:les?)?\b\s*\)?", re.IGNORECASE)
_PRODUCT_ID_RE = re.compile(r"gamestop\.com/(?:[^?#]*/)?([A-Za-z0-9-]+)\.html", re.IGNORECASE)


# ------------------------------------------------------------------ pure parsing

def is_challenge_html(html: str | None) -> bool:
    """Akamai "Access Denied" (any size) or a CAPTCHA/challenge interstitial."""
    if not html:
        return False
    if looks_like_challenge(None, html):
        return True
    head = html[:20000].lower()
    return "access denied" in head and ("reference #" in head or "edgesuite" in head or "akamai" in head)


def _classify(text: str | None) -> bool | None:
    """True = purchasable, False = explicitly unavailable, None = no signal."""
    if not text:
        return None
    t = re.sub(r"\s+", " ", str(text)).strip().lower()
    if not t:
        return None
    if any(n in t for n in _NEGATIVE):
        return False
    if any(p in t for p in _POSITIVE):
        return True
    return None


def _first(value: Any) -> Any:
    if isinstance(value, list):
        return value[0] if value else None
    return value


def _main_pid(tree: HTMLParser) -> str | None:
    """The page's own product id: the first ``data-pid`` (later ones belong to promo/recommendation widgets)."""
    node = tree.css_first("[data-pid]")
    return node.attributes.get("data-pid") if node is not None else None


def _gtm_data(tree: HTMLParser, pid: str | None = None, name: str | None = None) -> dict[str, Any]:
    """GTM product data for *this* page's product.

    Real GameStop pages carry dozens of ``data-gtmdata`` blobs (a "GameStop Pro" promo, related
    items...). Only a blob whose sku/productID matches the page's product id, or whose name matches
    the page title, is trusted; a page with a single blob (older layout) is accepted as is.
    """
    blobs: list[dict[str, Any]] = []
    for node in tree.css("[data-gtmdata]"):
        raw = node.attributes.get("data-gtmdata") or ""
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if isinstance(data, dict):
            info = data.get("productInfo")
            blobs.append(info if isinstance(info, dict) else data)
    if len(blobs) == 1:
        return blobs[0]
    want_name = (name or "").strip().lower()
    for b in blobs:
        ids = {str(b.get(k)) for k in ("sku", "productID", "id", "pid") if b.get(k)}
        if pid and pid in ids:
            return b
        if want_name and str(b.get("name") or "").strip().lower() == want_name:
            return b
    return {}


def _main_availability(tree: HTMLParser) -> bool | None:
    """The main product form's availability flags (``.product-availability`` data attributes).

    ``data-ready-to-order``/``data-available`` = "true" -> purchasable; both "false" -> not.
    """
    node = tree.css_first(".product-availability[data-available], .product-availability[data-ready-to-order]")
    if node is None:
        return None
    flags = [node.attributes.get(k) for k in ("data-ready-to-order", "data-available")]
    flags = [str(f).strip().lower() for f in flags if f not in (None, "", "null")]
    if not flags:
        return None
    if "true" in flags:
        return True
    return False if all(f == "false" for f in flags) else None


def _matching_offer(ld: dict[str, Any], pid: str | None) -> dict[str, Any] | None:
    """The JSON-LD offer for this product (offers can point at other SKUs, e.g. a graded-card listing)."""
    offers = ld.get("offers") if ld else None
    offers = offers if isinstance(offers, list) else [offers] if isinstance(offers, dict) else []
    offers = [o for o in offers if isinstance(o, dict)]
    if not offers:
        return None
    if not pid:
        return offers[0] if len(offers) == 1 else None
    for o in offers:
        if str(o.get("sku") or "") == pid or f"/{pid}.html" in str(o.get("url") or ""):
            return o
    return None


def _utag_data(html: str) -> dict[str, Any]:
    m = re.search(r"utag_data\s*=\s*\{", html)
    if not m:
        return {}
    try:
        data, _ = json.JSONDecoder().raw_decode(html, m.end() - 1)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _ld_product(tree: HTMLParser) -> dict[str, Any]:
    for node in tree.css('script[type="application/ld+json"]'):
        try:
            data = json.loads(node.text() or "")
        except ValueError:
            continue
        for item in data if isinstance(data, list) else [data]:
            if isinstance(item, dict) and item.get("@type") == "Product":
                return item
    return {}


def _text(node: Node | None) -> str:
    return re.sub(r"\s+", " ", node.text(separator=" ")).strip() if node is not None else ""


def _first_node(tree: HTMLParser, selectors: tuple[str, ...]) -> Node | None:
    for sel in selectors:
        node = tree.css_first(sel)
        if node is not None:
            return node
    return None


def _radio_disabled(container: Node) -> bool:
    radio = container.css_first('input[type="radio"]')
    return radio is not None and "disabled" in radio.attributes


def _ship_channel(tree: HTMLParser, gtm: dict[str, Any], utag: dict[str, Any],
                  offer: dict[str, Any] | None, main: bool | None = None) -> tuple[ChannelAvail | None, str]:
    """Return (ship channel, availability text used).

    ``main`` is the product form's own availability flag; when it says "not available" no other
    signal may turn shipping on (promo widgets and stale JSON-LD often say "InStock").
    """
    container = _first_node(tree, SHIP_SELECTORS)
    if container is not None:
        msg = _text(container.css_first(".availability-msg")) or _text(container)
        verdict = _classify(msg)
        if verdict is not None:
            return ChannelAvail("ship", verdict and main is not False and not _radio_disabled(container)), msg
    if main is not None:
        return ChannelAvail("ship", main), f"product form available={str(main).lower()}"
    candidates = (gtm.get("availability"), _first(utag.get("product_availability")),
                  offer.get("availability") if offer else None)
    for text in candidates:
        verdict = _classify(text)
        if verdict is not None:
            return ChannelAvail("ship", verdict), str(text)
    button = tree.css_first("button.add-to-cart")
    if button is not None:
        label = _text(button) or button.attributes.get("data-buttontext") or ""
        verdict = _classify(label)
        if verdict is not None:
            return ChannelAvail("ship", verdict and "disabled" not in button.attributes), label
    return None, ""


def _pickup_channel(tree: HTMLParser, utag: dict[str, Any]) -> ChannelAvail | None:
    container = _first_node(tree, PICKUP_SELECTORS)
    store_node = (container.css_first(".store-availability") if container is not None else None) \
        or tree.css_first(".store-availability")
    msg = _text(store_node) or _text(container)
    if not msg:
        return None
    m = _STORE_TEXT_RE.search(msg)
    verdict = _classify(m.group(1) if m else msg)
    name_node = (store_node.css_first(".store-name") if store_node is not None else None) \
        or tree.css_first(".store-name")
    store_name = _text(name_node) or (m.group(2).strip() if m else None)
    if store_name:
        store_name = _DIST_RE.sub("", store_name).strip(" -,") or None
    if verdict is None or not store_name:
        return None  # e.g. "Select a store" - no store set, nothing to report locally
    store_id = None
    for node in (store_node, container, tree.css_first("[data-store-id]")):
        if node is not None and node.attributes.get("data-store-id"):
            store_id = node.attributes["data-store-id"]
            break
    store_id = store_id or (str(_first(utag.get("store_id"))) if utag.get("store_id") else None)
    qty_m = _QTY_RE.search(msg)
    dist_m = _DIST_RE.search(msg)
    quantity = int(qty_m.group(1)) if qty_m else None
    low = quantity is not None or "limited" in msg.lower()
    available = verdict and not (container is not None and _radio_disabled(container))
    return ChannelAvail("store_pickup", bool(available), store_id=store_id, store_name=store_name,
                        distance_miles=float(dist_m.group(1)) if dist_m else None,
                        low_stock=low if available else None, quantity=quantity)


def parse_product_page(html: str, home: Home, target: Target) -> StockResult:
    """Parse a rendered GameStop product page into a StockResult.

    Raises BlockedError for challenge/Access Denied pages and ParseError when no
    availability signal is recognisable (never silently reports out of stock).
    ``target.store_ids`` (if set) restricts which set-store pickup result is reported.
    """
    if not isinstance(html, str) or not html.strip():
        raise ParseError("empty GameStop page")
    if is_challenge_html(html):
        raise BlockedError("GameStop challenge / Access Denied page")
    tree = HTMLParser(html)
    pid = _main_pid(tree) or target.get("product_id")
    ld = _ld_product(tree)
    page_name = ld.get("name") if ld else None
    page_name = page_name or _text(tree.css_first("h1.product-name")) or _text(tree.css_first("h1")) or None
    gtm, utag = _gtm_data(tree, pid, page_name), _utag_data(html)
    offer = _matching_offer(ld, pid)

    ship, ship_text = _ship_channel(tree, gtm, utag, offer, _main_availability(tree))
    pickup = _pickup_channel(tree, utag)
    if ship is None and pickup is None:
        raise ParseError("no GameStop availability signals found (page layout changed?)")
    if pickup is not None and target.store_ids and pickup.store_id not in {str(s) for s in target.store_ids}:
        pickup = None  # the profile's set store is not one of the pinned stores
    channels = [c for c in (pickup, ship) if c is not None]

    price = None
    for raw in (gtm.get("price"), _first(utag.get("product_price")),
                offer.get("price") if offer else None,
                _text(tree.css_first(".actual-price")) or None):
        price = parse_price(raw)
        if price is not None:
            break
    name = page_name or gtm.get("name") or _first(utag.get("product_name")) or None

    available = [c for c in channels if c.available]
    if not available:
        raw_status = "OUT_OF_STOCK"
    elif any(c.low_stock for c in available):
        raw_status = "LIMITED"
    elif ship is not None and ship.available and re.search(r"pre-?order", ship_text, re.I):
        raw_status = "PREORDER"
    else:
        raw_status = "IN_STOCK"
    return StockResult(retailer="gamestop", in_stock=bool(available), channels=channels, price=price,
                       raw_status=raw_status, product_name=str(name) if name else None)


def signed_in_from_html(html: str | None) -> bool | None:
    """Best-effort sign-in detection from a GameStop page. None = unknown."""
    if not html:
        return None
    status = _first(_utag_data(html).get("user_login_status"))
    if isinstance(status, str):
        s = status.lower()
        if "out" in s or "guest" in s or "anonymous" in s:
            return False
        if "in" in s or "registered" in s:
            return True
    tree = HTMLParser(html)
    marker = tree.css_first("[data-signed-in]")
    if marker is not None:
        return marker.attributes.get("data-signed-in", "").lower() == "true"
    account = _text(tree.css_first(".header-account, .account-header")).lower()
    if "sign out" in account or account.startswith("hi,"):
        return True
    if "sign in" in account:
        return False
    return None


async def is_signed_in(page: Any) -> bool | None:
    """Hook for BrowserManager.signin_status(): inspect the current GameStop page."""
    try:
        return signed_in_from_html(await page.content())
    except Exception:
        return None


# ------------------------------------------------------------------ adapter

class GameStopAdapter(BaseAdapter):
    name: ClassVar[str] = "gamestop"
    min_interval_seconds: ClassVar[int] = 120
    url_hosts: ClassVar[tuple[str, ...]] = ("gamestop.com",)
    url_patterns: ClassVar[list[tuple[str, re.Pattern[str]]]] = [("product_id", _PRODUCT_ID_RE)]

    cart_confirm_seconds: ClassVar[float] = 8.0

    @classmethod
    def identifiers_from_url(cls, url: str) -> dict[str, str]:
        out = super().identifiers_from_url(url)
        if not cls.matches_url(url) or "product_id" not in out:
            raise ValueError(f"not a GameStop product URL (expected .../<id>.html): {url}")
        return out

    def product_url(self, target: Target) -> str:
        url = target.get("url")
        if not url:
            raise AdapterError("GameStop target needs a product 'url'", kind=ErrorKind.CONFIG)
        try:  # re-validate: never navigate the signed-in profile to another host or to checkout
            return self.canonical_url(url)
        except ValueError as e:
            raise AdapterError(str(e), kind=ErrorKind.CONFIG) from None

    def cart_url(self) -> str:
        return CART_URL

    async def check(self, target: Target, home: Home) -> StockResult:
        url = self.product_url(target)
        if self.browser is None:
            raise AdapterError("GameStop checks need the browser profile", kind=ErrorKind.CONFIG)
        status, html = await self.browser.fetch_html(self.name, url, wait_selector=WAIT_SELECTOR)
        if looks_like_challenge(status, html) or is_challenge_html(html):
            raise BlockedError(f"GameStop blocked/challenge (HTTP {status})")
        if status == 404:
            raise AdapterError("HTTP 404", kind=ErrorKind.NOT_FOUND)
        if status and status >= 400:
            raise AdapterError(f"HTTP {status}", kind=ErrorKind.HTTP_ERROR)
        return parse_product_page(html, home, target)

    async def add_to_cart(self, target: Target, result: StockResult, browser: BrowserCtx,
                          channel: ChannelAvail | None = None) -> CartResult:
        """Select the matched option (pickup or ship), click Add to Cart, confirm the count rose.

        Pickup uses the store selected on the page (the account's store); the message says so
        when that is not visibly the matched store.

        Every click goes through safe_click; never navigates to checkout.
        """
        def fail(msg: str, **kw: Any) -> CartResult:
            return CartResult(retailer=self.name, success=False, message=msg, **kw)

        url = self.product_url(target)
        page = await browser.cart_page(self.name)
        await page.goto(url, wait_until="domcontentloaded", timeout=20_000)
        html = await page.content()
        if is_challenge_html(html):
            return fail("GameStop challenge page; not retrying")
        if signed_in_from_html(html) is False:
            return fail("signed out of GameStop", signed_out=True)

        before = await _cart_count(page)
        matched = resolve_cart_channel(result, channel)
        try:
            if matched is not None and matched.channel in LOCAL_CHANNELS:
                if not await _click_first(page, PICKUP_CLICK_SELECTORS):
                    return fail("Store pickup option not found on the product page", cart_count_before=before)
                store_text = await _text_of(page, PICKUP_SELECTORS)
                how = pickup_store_note("GameStop", matched, store_text)
            elif matched is not None and matched.channel == "ship":
                if not await _click_first(page, SHIP_CLICK_SELECTORS):
                    return fail("Ship-to-home option not found on the product page", cart_count_before=before)
                how = "ship to home"
            else:
                how = "default fulfillment"
            button = await _find(page, ADD_TO_CART_SELECTORS)
            if button is None:
                return fail("Add to Cart button not found", cart_count_before=before)
            if not await button.is_enabled():
                return fail("Add to Cart button is disabled", cart_count_before=before)
            await safe_click(button)
        except ForbiddenClickError as exc:
            return fail(f"refused unsafe click: {exc}", cart_count_before=before)

        after = await _wait_cart_increase(page, before, self.cart_confirm_seconds)
        base = before or 0
        if after is not None and after > base:
            return CartResult(retailer=self.name, success=True, message=f"added to cart ({how})",
                              cart_url=CART_URL, cart_count_before=before, cart_count_after=after)
        return fail("clicked Add to Cart but cart count did not change",
                    cart_count_before=before, cart_count_after=after)


# ------------------------------------------------------------------ page helpers

async def _find(page: Any, selectors: tuple[str, ...]) -> Any | None:
    for sel in selectors:
        loc = page.locator(sel).first
        try:
            if await loc.count() > 0:
                return loc
        except Exception:
            continue
    return None


async def _text_of(page: Any, selectors: tuple[str, ...]) -> str | None:
    loc = await _find(page, selectors)
    if loc is None:
        return None
    try:
        return await loc.inner_text(timeout=2000)
    except Exception:
        return None


async def _click_first(page: Any, selectors: tuple[str, ...]) -> bool:
    loc = await _find(page, selectors)
    if loc is None:
        return False
    await safe_click(loc)
    return True


async def _cart_count(page: Any) -> int | None:
    loc = await _find(page, CART_COUNT_SELECTORS)
    if loc is None:
        return None
    try:
        text = (await loc.inner_text(timeout=2000)) or (await loc.get_attribute("data-cart-count")) or ""
    except Exception:
        return None
    m = re.search(r"\d+", text)
    return int(m.group(0)) if m else 0


async def _wait_cart_increase(page: Any, before: int | None, seconds: float) -> int | None:
    base = before or 0
    count = await _cart_count(page)
    waited = 0.0
    while (count is None or count <= base) and waited < seconds:
        await page.wait_for_timeout(500)
        waited += 0.5
        count = await _cart_count(page)
    return count
