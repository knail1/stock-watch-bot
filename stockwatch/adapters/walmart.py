"""Walmart adapter (spec section 6).

Walmart has no consumer stock API, so checks load the product page in the
user's real Chrome profile (``BrowserCtx.fetch_html``) and read the
``__NEXT_DATA__`` JSON the page embeds. When that blob is missing or reshaped
we fall back to the visible text ("Pickup, today", "Out of stock", ...).

Walmart only shows availability for the user's *selected* store, so local
stock is partial: at most one pickup store per check.

Walmart runs the most aggressive bot detection of the supported retailers
(PerimeterX "Robot or human?" / "Press & Hold"). Any challenge page raises
``BlockedError`` immediately; the scheduler backs off. We never try to solve
or evade it.
"""
from __future__ import annotations

import json
import re
from typing import Any, ClassVar

from selectolax.lexbor import LexborHTMLParser as HTMLParser

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

BASE_URL = "https://www.walmart.com"
CART_URL = f"{BASE_URL}/cart"

# Walmart / PerimeterX interstitial markers (lower-cased). Any hit -> blocked.
WALMART_CHALLENGE_MARKERS = (
    "robot or human?",
    "px-captcha",
    "press & hold",
    "press and hold",
    "activate and hold the button",
    "/blocked?url=",
)

_AVAILABLE_STATUSES = {"IN_STOCK", "AVAILABLE", "LIMITED_STOCK", "LIMITED", "LOW_STOCK"}
_LOW_STOCK_STATUSES = {"LIMITED_STOCK", "LIMITED", "LOW_STOCK"}
_FULFILLMENT_CHANNEL = {
    "PICKUP": "store_pickup",
    "IN_STORE": "in_store",
    "STORE": "in_store",
    "DELIVERY": "ship",  # delivered to the home (from store); not a pickup option
    "SHIPPING": "ship",
}

_NEXT_DATA_RE = re.compile(
    r'<script[^>]*\bid=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>', re.IGNORECASE | re.DOTALL
)


# ------------------------------------------------------------------ pure parsing

def is_walmart_challenge(status: int | None, html: str | None) -> bool:
    """True for 403/429 or a Walmart/PerimeterX bot-check page.

    Walmart-specific markers always count. The generic heuristic is only trusted
    when the page carries no product data, since real product pages embed large
    scripts that can mention words like "captcha".
    """
    if status in (403, 429):
        return True
    head = (html or "")[:50000].lower()
    if any(m in head for m in WALMART_CHALLENGE_MARKERS):
        return True
    if looks_like_challenge(status, html):
        data = extract_next_data(html or "")
        return _dig(data, "props", "pageProps", "initialData", "data", "product") is None
    return False


def extract_next_data(html: str) -> dict[str, Any] | None:
    """Return the decoded ``__NEXT_DATA__`` JSON, or None if absent/undecodable."""
    m = _NEXT_DATA_RE.search(html)
    if not m:
        return None
    try:
        data = json.loads(m.group(1))
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _dig(obj: Any, *path: str) -> Any:
    for key in path:
        if not isinstance(obj, dict):
            return None
        obj = obj.get(key)
    return obj


def _status_available(status: Any) -> bool:
    return str(status or "").upper() in _AVAILABLE_STATUSES


def _fulfillment_options(product: dict[str, Any]) -> list[dict[str, Any]]:
    for key in ("fulfillmentOptions", "fulfillmentType"):
        opts = product.get(key)
        if isinstance(opts, list):
            return [o for o in opts if isinstance(o, dict)]
    return []


def _channels_from_options(options: list[dict[str, Any]], target: Target) -> list[ChannelAvail]:
    merged: dict[tuple[str, str | None], ChannelAvail] = {}
    for opt in options:
        channel = _FULFILLMENT_CHANNEL.get(str(opt.get("type") or "").upper())
        if channel is None:
            continue
        status = str(opt.get("availabilityStatus") or "").upper()
        store_id = opt.get("storeId")
        store_id = str(store_id) if store_id not in (None, "") else None
        if channel != "ship" and target.store_ids and store_id and store_id not in target.store_ids:
            continue  # Walmart's selected store is not one the user pinned
        avail = ChannelAvail(
            channel=channel,  # type: ignore[arg-type]
            available=_status_available(status),
            store_id=store_id if channel != "ship" else None,
            store_name=(opt.get("storeName") or opt.get("locationText")) if channel != "ship" else None,
            low_stock=True if status in _LOW_STOCK_STATUSES else None,
        )
        key = (channel, avail.store_id)
        prev = merged.get(key)
        if prev is None or (avail.available and not prev.available):
            merged[key] = avail  # e.g. DELIVERY + SHIPPING collapse to one "ship"
    return list(merged.values())


def parse_next_data(data: dict[str, Any], home: Home, target: Target) -> StockResult:
    """Parse a decoded ``__NEXT_DATA__`` document."""
    data_node = _dig(data, "props", "pageProps", "initialData", "data")
    if not isinstance(data_node, dict) or "product" not in data_node:
        raise ParseError("walmart __NEXT_DATA__ lacks props.pageProps.initialData.data.product")
    product = data_node["product"]
    if product is None:
        raise AdapterError("walmart product not found", kind=ErrorKind.NOT_FOUND)
    if not isinstance(product, dict) or not product.get("availabilityStatus"):
        raise ParseError("walmart product has no availabilityStatus")

    raw_status = str(product["availabilityStatus"]).upper()
    channels = _channels_from_options(_fulfillment_options(product), target)
    if not channels and _status_available(raw_status):
        # Online availability without per-option detail: report it as shippable.
        channels = [ChannelAvail(channel="ship", available=True)]
    in_stock = any(c.available for c in channels)
    price = parse_price(_dig(product, "priceInfo", "currentPrice", "price"))
    return StockResult(
        retailer="walmart",
        in_stock=in_stock,
        channels=channels,
        price=price,
        raw_status=raw_status,
        product_name=product.get("name"),
    )


_PICKUP_RE = re.compile(r"\bpickup,?\s+(today|tomorrow|as soon as|available)", re.IGNORECASE)
_SHIP_RE = re.compile(r"\b(shipping|delivery),?\s+(arrives|today|tomorrow|as soon as)", re.IGNORECASE)
_OOS_RE = re.compile(r"\b(out of stock|currently unavailable|not available)\b", re.IGNORECASE)
_PRICE_RE = re.compile(r"(?:now|current price)\s*\$\s*([\d,]+(?:\.\d{1,2})?)", re.IGNORECASE)


#: The product's own purchase area, when the page marks it.
BUY_BOX_SELECTORS = (
    '[data-testid="add-to-cart-section"]',
    '[data-testid="flex-container-buy-box"]',
    '[data-testid="fulfillment-section"]',
    '[data-seo-id="buy-box"]',
    "#buy-box",
)
#: Page furniture and recommendation carousels whose "Out of stock"/"Pickup today" badges
#: belong to other products.
_NOISE_SELECTORS = "script, style, noscript, header, footer, nav, aside, [role=navigation], [role=banner]"
_CAROUSEL_ATTR_RE = re.compile(r"carousel|recommend|sponsored|similar-items|related|also-viewed", re.IGNORECASE)
_CAROUSEL_HEAD_RE = re.compile(
    r"similar items|related|recommended|sponsored|customers (also|who)|you may also|more (items|to consider)|"
    r"compare|frequently bought|popular items|top picks", re.IGNORECASE)


def _strip_unrelated(tree: HTMLParser) -> None:
    for node in tree.css(_NOISE_SELECTORS):
        node.decompose()
    # Reverse document order: children are removed before their (possibly also removed) parents.
    for node in reversed(tree.css("section, [data-testid]")):
        attrs = " ".join(str(v) for k, v in (node.attributes or {}).items() if k in ("data-testid", "class") and v)
        heading = node.css_first("h2, h3, h4") if node.tag == "section" else None
        if _CAROUSEL_ATTR_RE.search(attrs) or (heading is not None and _CAROUSEL_HEAD_RE.search(heading.text())):
            node.decompose()


def parse_visible_text(html: str, home: Home, target: Target, *, strict: bool = False) -> StockResult:
    """Fallback parser over the product's visible purchase text. Raises ParseError if unrecognisable.

    Only the buy box (or, when the page does not mark one, the main content minus page
    furniture and recommendation carousels) is read, so other products' badges never count.
    ``strict``: the page had ``__NEXT_DATA__`` that no longer parses (shape changed). Then an
    out-of-stock verdict is never taken from text, and in stock needs an Add to cart button;
    anything else raises ParseError so the adapter reports itself broken instead of silently OOS.
    """
    tree = HTMLParser(html)
    _strip_unrelated(tree)
    scope = next((n for sel in BUY_BOX_SELECTORS if (n := tree.css_first(sel)) is not None), None)
    if scope is None:
        scope = tree.css_first("main") or tree.body
    text = re.sub(r"\s+", " ", scope.text(separator=" ") if scope is not None else "")
    pickup = bool(_PICKUP_RE.search(text))
    ship = bool(_SHIP_RE.search(text))
    oos = bool(_OOS_RE.search(text))
    if not (pickup or ship or oos):
        raise ParseError("walmart page has neither __NEXT_DATA__ nor recognisable availability text")
    if strict:
        has_atc = (scope is not None and scope.css_first('button[data-automation-id="atc"]') is not None) \
            or bool(re.search(r"\badd to cart\b", text, re.IGNORECASE))
        if not (pickup or ship) or not has_atc:
            raise ParseError("walmart __NEXT_DATA__ shape changed and the page text is not conclusive")

    channels: list[ChannelAvail] = []
    if pickup:
        channels.append(ChannelAvail(channel="store_pickup", available=True))
    if ship:
        channels.append(ChannelAvail(channel="ship", available=True))
    in_stock = pickup or ship

    price: float | None = None
    price_node = tree.css_first('[itemprop="price"]')
    if price_node is not None:
        price = parse_price(price_node.attributes.get("content") or price_node.text())
    if price is None and (m := _PRICE_RE.search(text)):
        price = parse_price(m.group(1))
    title = tree.css_first("h1")
    return StockResult(
        retailer="walmart",
        in_stock=in_stock,
        channels=channels,
        price=price,
        raw_status="IN_STOCK" if in_stock else "OUT_OF_STOCK",
        product_name=title.text(strip=True) if title is not None else None,
    )


def parse_walmart(html: str, home: Home, target: Target) -> StockResult:
    """Parse a Walmart product page: ``__NEXT_DATA__`` first, visible text as fallback.

    Raises BlockedError on a challenge page and ParseError on an unrecognised page.
    """
    if not isinstance(html, str) or not html.strip():
        raise ParseError("empty walmart page")
    if is_walmart_challenge(None, html):
        raise BlockedError("walmart bot challenge page")
    data = extract_next_data(html)
    if data is not None:
        try:
            return parse_next_data(data, home, target)
        except ParseError as exc:
            # Reshaped JSON: accept only a conclusive in-stock buy box, otherwise fail loud.
            try:
                return parse_visible_text(html, home, target, strict=True)
            except ParseError:
                raise exc from None
    return parse_visible_text(html, home, target)


_CART_COUNT_RE = re.compile(r"(\d+)\s*items?\b", re.IGNORECASE)
_LEADING_INT_RE = re.compile(r"^\s*(\d+)\b")


def parse_cart_count(text: str | None) -> int | None:
    """Read the header cart badge, e.g. aria-label "Cart contains 2 items Total Amount $10.00"."""
    if not text:
        return None
    m = _CART_COUNT_RE.search(text) or _LEADING_INT_RE.search(text)
    return int(m.group(1)) if m else None


# ------------------------------------------------------------------ page helpers (Playwright)

CART_BADGE_SELECTORS = (
    '[data-automation-id="cart-button"]',
    'a[link-identifier="cart"]',
    'a[href$="/cart"]',
)
PICKUP_SELECTORS = (
    '[data-seo-id="fulfillment-Pickup-intent"]',
    '[data-automation-id="fulfillment-PICKUP"]',
    'button:has-text("Pickup")',
)
SHIPPING_SELECTORS = (
    '[data-seo-id="fulfillment-Shipping-intent"]',
    '[data-automation-id="fulfillment-SHIPPING"]',
    'button:has-text("Shipping")',
)
ADD_TO_CART_SELECTORS = (
    'button[data-automation-id="atc"]',
    'button[data-dca-name="ItemBuyBoxAddToCartButton"]',
    'button:has-text("Add to cart")',
)
SIGNIN_SELECTORS = (
    '[data-automation-id="headerSignIn"]',
    'button[aria-label*="Account"]',
    'a[href*="/account"]',
)


async def _first_visible(page: Any, selectors: tuple[str, ...]) -> Any | None:
    for sel in selectors:
        loc = page.locator(sel).first
        try:
            if await loc.count() and await loc.is_visible():
                return loc
        except Exception:
            continue
    return None


async def _element_text(loc: Any) -> str:
    parts: list[str] = []
    for getter in (lambda: loc.get_attribute("aria-label", timeout=2000),
                   lambda: loc.inner_text(timeout=2000)):
        try:
            v = await getter()
        except Exception:
            v = None
        if v:
            parts.append(v)
    return " ".join(parts)


async def read_cart_count(page: Any) -> int | None:
    """Cart item count from the header badge, or None if it cannot be read."""
    for sel in CART_BADGE_SELECTORS:
        loc = page.locator(sel).first
        try:
            if not await loc.count():
                continue
        except Exception:
            continue
        count = parse_cart_count(await _element_text(loc))
        if count is not None:
            return count
    return None


async def is_signed_in(page: Any) -> bool | None:
    """Sign-in state from the header account button ("Hi, Name" vs "Sign In"); None if unknown."""
    loc = await _first_visible(page, SIGNIN_SELECTORS)
    if loc is None:
        return None
    text = (await _element_text(loc)).lower()
    if re.search(r"\bhi,", text):
        return True
    if "sign in" in text or "sign-in" in text:
        return False
    return None


# ------------------------------------------------------------------ adapter

class WalmartAdapter(BaseAdapter):
    name: ClassVar[str] = "walmart"
    min_interval_seconds: ClassVar[int] = 180
    url_hosts: ClassVar[tuple[str, ...]] = ("walmart.com",)
    url_patterns: ClassVar[list[tuple[str, re.Pattern[str]]]] = [
        ("item_id", re.compile(r"walmart\.com/ip/(?:[^/?#]+/)?(\d+)(?:[/?#]|$)", re.IGNORECASE)),
    ]

    cart_count_wait_seconds: ClassVar[float] = 8.0
    cart_poll_ms: ClassVar[int] = 500

    @classmethod
    def identifiers_from_url(cls, url: str) -> dict[str, str]:
        ids = super().identifiers_from_url(url)
        if "item_id" not in ids:
            raise ValueError(f"no Walmart item id in URL: {url}")
        return ids

    def product_url(self, target: Target) -> str:
        item_id = target.get("item_id")
        if item_id:
            return f"{BASE_URL}/ip/{item_id}"
        url = target.get("url")
        if url:
            return url
        raise AdapterError("walmart target needs item_id or url", kind=ErrorKind.CONFIG)

    def cart_url(self) -> str:
        return CART_URL

    async def check(self, target: Target, home: Home) -> StockResult:
        if self.browser is None:
            raise AdapterError("walmart checks need the browser profile", kind=ErrorKind.CONFIG)
        status, html = await self.browser.fetch_html(self.name, self.product_url(target))
        if is_walmart_challenge(status, html):
            raise BlockedError(f"walmart challenge (HTTP {status})")
        if status == 404:
            raise AdapterError("HTTP 404", kind=ErrorKind.NOT_FOUND)
        if status >= 400:
            raise AdapterError(f"HTTP {status}", kind=ErrorKind.HTTP_ERROR)
        return parse_walmart(html, home, target)

    async def add_to_cart(self, target: Target, result: StockResult, browser: BrowserCtx,
                          channel: ChannelAvail | None = None) -> CartResult:
        """Select the matched option (pickup or shipping), click "Add to cart" via safe_click,
        confirm the cart count rose. Pickup uses the store saved in the Walmart account."""
        def fail(msg: str, **kw: Any) -> CartResult:
            return CartResult(retailer=self.name, success=False, message=msg, cart_url=CART_URL, **kw)

        page = await browser.cart_page(self.name)
        await page.goto(self.product_url(target), wait_until="domcontentloaded", timeout=15_000)
        if is_walmart_challenge(None, await page.content()):
            return fail("Walmart showed a bot challenge; add to cart manually")
        if await is_signed_in(page) is False:
            return fail("Sign in needed for walmart", signed_out=True)

        before = await read_cart_count(page)
        matched = resolve_cart_channel(result, channel)
        how = ""
        try:
            if matched is None or matched.channel in LOCAL_CHANNELS:
                pickup = await _first_visible(page, PICKUP_SELECTORS)
                if pickup is not None:
                    await safe_click(pickup)
                    if matched is not None:
                        how = f" ({pickup_store_note('Walmart', matched, await _element_text(pickup))})"
                elif matched is not None:
                    return fail("Pickup option not found on the product page", cart_count_before=before)
            else:
                shipping = await _first_visible(page, SHIPPING_SELECTORS)
                if shipping is not None:
                    await safe_click(shipping)
                how = " (shipping)"
            button = await _first_visible(page, ADD_TO_CART_SELECTORS)
            if button is None:
                return fail("Add to cart button not found", cart_count_before=before)
            await safe_click(button)
        except ForbiddenClickError as exc:
            return fail(f"refused unsafe click: {exc}", cart_count_before=before)

        after = before
        loops = max(1, int(self.cart_count_wait_seconds * 1000 / self.cart_poll_ms))
        for _ in range(loops):
            await page.wait_for_timeout(self.cart_poll_ms)
            after = await read_cart_count(page)
            if after is not None and (after > before if before is not None else after > 0):
                return CartResult(retailer=self.name, success=True, message=f"Added to cart{how}",
                                  cart_url=CART_URL, cart_count_before=before, cart_count_after=after)
        return fail("Clicked add to cart but the cart count did not change",
                    cart_count_before=before, cart_count_after=after)
