"""Nintendo Store (US) adapter — online only, page check in the real Chrome profile.

Fetching: the product page is loaded in the shared per-retailer browser page
(``BrowserCtx.fetch_html``). Parsing is pure (:func:`parse_nintendo_page`):

1. Queue / waiting-room pages (Queue-it, ``queue.nintendo``, "You are now in
   line", "waiting room") -> ``raw_status="QUEUE"``.
2. Invite-only sales ("invitation", "invite only", "request an invite") in the
   product's purchase fields, purchase buttons or visible page text ->
   ``raw_status="INVITE_ONLY"``.
3. Otherwise the ``__NEXT_DATA__`` product JSON (price ``finalPrice`` /
   ``regPrice``, availability strings such as "Available now", "Out of stock",
   "Sold out", "Coming soon") decides, with the visible purchase button text as
   a cross-check / fallback.

Design choice for queue / invite-only (spec section 6): these are *not* errors
and *not* stock. They are reported as ``in_stock=False``, ``error=None`` with an
explanatory ``error_detail`` so the service can send an info alert ("Nintendo
is running a queue / invite sale, go look") without opening a blocked/broken
event or a critical restock alert. We never try to get through a queue.

A page with no recognisable product data is either a challenge interstitial
(-> :class:`BlockedError`, back off) or a changed layout (-> :class:`ParseError`,
fail loud). Nintendo is online-only, so the only channel reported is ``ship``.
"""
from __future__ import annotations

import json
import re
from typing import Any, ClassVar, Iterator

from selectolax.lexbor import LexborHTMLParser as HTMLParser

from ..cart.safety import ForbiddenClickError, safe_click
from ..models import CartResult, ChannelAvail, ErrorKind, Home, StockResult, Target
from .base import (
    AdapterError,
    BaseAdapter,
    BlockedError,
    BrowserCtx,
    ParseError,
    looks_like_challenge,
    parse_price,
)

RETAILER = "nintendo"
STORE_BASE = "https://www.nintendo.com/us/store/products/"
CART_URL = "https://www.nintendo.com/us/cart/"

RAW_IN_STOCK = "IN_STOCK"
RAW_PREORDER = "PREORDER"
RAW_OUT_OF_STOCK = "OUT_OF_STOCK"
RAW_COMING_SOON = "COMING_SOON"
RAW_QUEUE = "QUEUE"
RAW_INVITE_ONLY = "INVITE_ONLY"

QUEUE_NOTE = ("Nintendo Store is running a queue / waiting room for this product; "
              "stock cannot be determined until you get through the line. Open the product page.")
INVITE_NOTE = ("Nintendo Store is selling this product by invitation only; "
               "check your email / My Nintendo account for an invite. Open the product page.")

# Waiting-room wording shown to the user (searched in visible text only).
_QUEUE_TEXT_RE = re.compile(
    r"you are now in line|you're now in line|you are in line|your (estimated )?wait(ing)? time|"
    r"waiting room|your place in line|number in line|queue[\s-]it",
    re.IGNORECASE,
)
# Queue infrastructure markers in raw HTML; only trusted when the page has no product data,
# because a normal product page may embed the Queue-it client script.
_QUEUE_INFRA_RE = re.compile(r"queue-it|queueit|queue\.nintendo", re.IGNORECASE)
# Phrases safe to search for in the whole visible page.
_INVITE_PAGE_RE = re.compile(
    r"invite[\s-]only|by invitation|invitation[\s-]only|request (an )?invit(e|ation)|"
    r"invitation (is )?required|(you('ve| have)|you were) been invited|purchase invitation|"
    r"invitation to purchase|sign up for an invitation",
    re.IGNORECASE,
)
# Looser match, only applied to purchase/availability fields and button text.
_INVITE_FIELD_RE = re.compile(r"\binvit(e|es|ed|ation|ations)\b", re.IGNORECASE)

_ADD_TO_CART_RE = re.compile(r"^\s*add\s+to\s+cart\s*$", re.IGNORECASE)

# Availability text -> raw status. Matched as substrings of the normalised value.
_AVAIL_MAP: tuple[tuple[str, str], ...] = (
    ("out of stock", RAW_OUT_OF_STOCK),
    ("sold out", RAW_OUT_OF_STOCK),
    ("not available", RAW_OUT_OF_STOCK),
    ("unavailable", RAW_OUT_OF_STOCK),
    ("no longer available", RAW_OUT_OF_STOCK),
    ("discontinued", RAW_OUT_OF_STOCK),
    ("coming soon", RAW_COMING_SOON),
    ("pre-order", RAW_PREORDER),
    ("preorder", RAW_PREORDER),
    ("pre order", RAW_PREORDER),
    ("available now", RAW_IN_STOCK),
    ("in stock", RAW_IN_STOCK),
    ("available", RAW_IN_STOCK),
)
_AVAIL_KEYS = ("availability", "purchaseAvailability", "productAvailability", "stockStatus",
               "inventoryStatus", "purchaseStatus", "purchaseType", "saleStatus", "status")
_BUTTON_OOS = ("out of stock", "sold out", "notify me", "unavailable", "coming soon")


# ------------------------------------------------------------------ pure parsing

def detect_queue(html: str) -> bool:
    """True when the page is a queue / waiting room rather than the product page."""
    tree = HTMLParser(html)
    if _QUEUE_TEXT_RE.search(_visible_text(tree)):
        return True
    data = _next_data(tree)
    return (data is None or _find_product(data) is None) and bool(_QUEUE_INFRA_RE.search(html))


def _next_data(tree: HTMLParser) -> dict[str, Any] | None:
    node = tree.css_first("script#__NEXT_DATA__")
    if node is None:
        return None
    try:
        data = json.loads(node.text() or "")
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _walk(obj: Any) -> Iterator[dict[str, Any]]:
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from _walk(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk(v)


def _price_of(node: dict[str, Any]) -> float | None:
    """Price from ``price``/``prices`` blocks (``finalPrice`` preferred over ``regPrice``)."""
    for key in ("price", "prices"):
        block = node.get(key)
        if isinstance(block, dict):
            if isinstance(block.get("minimum"), dict):  # prices.minimum.{finalPrice,regPrice}
                block = block["minimum"]
            for pk in ("finalPrice", "salePrice", "regPrice", "amount"):
                val = block.get(pk)
                if isinstance(val, dict):
                    val = val.get("amount") or val.get("value")
                price = parse_price(val) if val not in (None, "") else None
                if price is not None:
                    return price
        elif isinstance(block, (int, float, str)) and block != "":
            return parse_price(block)
    return None


_ID_KEYS = ("urlKey", "slug", "url", "productUrl", "sku", "nsuid", "id")


def _is_product_node(node: dict[str, Any]) -> bool:
    has_name = isinstance(node.get("name") or node.get("title"), str)
    has_price = isinstance(node.get("price"), dict) or isinstance(node.get("prices"), dict)
    return has_name and has_price and any(k in node for k in _AVAIL_KEYS)


def _product_nodes(obj: Any) -> Iterator[dict[str, Any]]:
    """Top-level product-like records (does not descend into a product's own variants)."""
    if isinstance(obj, dict):
        if _is_product_node(obj):
            yield obj
            return
        for v in obj.values():
            yield from _product_nodes(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _product_nodes(v)


def _matches_slug(node: dict[str, Any], slug: str) -> bool:
    slug = slug.strip("/").lower()
    for key in _ID_KEYS:
        val = node.get(key)
        if val in (None, ""):
            continue
        v = str(val).strip().strip("/").lower()
        if v == slug or v.endswith("/" + slug) or (key in ("sku", "nsuid") and slug.endswith(v)):
            return True
    return False


def _find_product(data: dict[str, Any], slug: str | None = None, *,
                  strict: bool = False) -> dict[str, Any] | None:
    """Locate the watched product's record inside __NEXT_DATA__ (pageProps.product, Apollo cache, ...).

    The Apollo cache also holds related/recommended/bundle products, so a record
    matching ``slug`` (the URL's product slug, or the page's ``query.slug``) is
    preferred. ``strict``: with several candidates and none matching, raise
    ParseError rather than report another product's stock and price.
    """
    page_props = data.get("props", {}).get("pageProps", {}) if isinstance(data.get("props"), dict) else {}
    if isinstance(page_props, dict) and isinstance(page_props.get("product"), dict):
        return page_props["product"]
    candidates = list(_product_nodes(data))
    if len(candidates) <= 1:
        return candidates[0] if candidates else None
    query = data.get("query") if isinstance(data.get("query"), dict) else {}
    for want in (slug, query.get("slug")):
        if isinstance(want, str) and want:
            hit = next((c for c in candidates if _matches_slug(c, want)), None)
            if hit is not None:
                return hit
    if strict:
        raise ParseError(f"Nintendo page lists {len(candidates)} products and none matches the watched one")
    return candidates[0]


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [s for v in value for s in _strings(v)]
    if isinstance(value, dict):
        return [s for k in ("label", "text", "value", "status", "name") for s in _strings(value.get(k))]
    return []


def _availability_texts(product: dict[str, Any]) -> list[str]:
    return [s for k in _AVAIL_KEYS for s in _strings(product.get(k))]


def _status_from_texts(texts: list[str]) -> str | None:
    for text in texts:
        norm = re.sub(r"[\s_]+", " ", text).strip().lower()
        for needle, status in _AVAIL_MAP:
            if needle in norm:
                return status
        if norm in ("instock", "available_now"):
            return RAW_IN_STOCK
    return None


def _buttons(tree: HTMLParser | Any) -> list[tuple[str, bool]]:
    """(text, enabled) for every visible-ish purchase button under ``tree``."""
    out: list[tuple[str, bool]] = []
    for node in tree.css("button, [role=button], input[type=submit]"):
        text = (node.text(strip=True) or node.attributes.get("aria-label") or
                node.attributes.get("value") or "").strip()
        if not text:
            continue
        attrs = node.attributes
        disabled = "disabled" in attrs or (attrs.get("aria-disabled") or "").lower() == "true"
        out.append((re.sub(r"\s+", " ", text), not disabled))
    return out


def _visible_text(tree: HTMLParser) -> str:
    body = tree.body
    if body is None:
        return ""
    clone = HTMLParser(body.html or "")
    for tag in ("script", "style", "noscript", "template"):
        for n in clone.css(tag):
            n.decompose()
    return clone.body.text(separator=" ", strip=True) if clone.body else ""


_FURNITURE = "script, style, noscript, template, header, footer, nav, aside, [role=banner], [role=navigation]"
_BANNER_ATTR_RE = re.compile(r"banner|promo|announcement|marquee|alert-bar|site-message|global-message",
                             re.IGNORECASE)


def _purchase_area(tree: HTMLParser) -> HTMLParser:
    """The product's own content: <main> (or <body>) minus header/nav/footer and site banners,
    so a site-wide "Sign up for an invitation" banner never speaks for this product."""
    root = tree.css_first("main") or tree.body
    area = HTMLParser((root.html or "") if root is not None else "")
    for node in reversed(area.css(_FURNITURE)):
        node.decompose()
    for node in reversed(area.css("[class], [id], [data-testid]")):
        attrs = " ".join(str(node.attributes.get(k) or "") for k in ("class", "id", "data-testid"))
        if _BANNER_ATTR_RE.search(attrs):
            node.decompose()
    return area


def _area_text(area: HTMLParser) -> str:
    root = area.body or area.root
    return root.text(separator=" ", strip=True) if root is not None else ""


def parse_nintendo_page(html: str, home: Home | None = None, target: Target | None = None) -> StockResult:
    """Parse a Nintendo Store product page into a StockResult.

    Raises BlockedError for challenge pages and ParseError for unrecognised
    pages. Queue / invite-only states are returned (not raised) with
    ``in_stock=False``, ``error=None`` and a note in ``error_detail``.
    """
    if not isinstance(html, str) or not html.strip():
        raise ParseError("empty Nintendo page")
    tree = HTMLParser(html)
    data = _next_data(tree)
    slug = target.get("product_id") if target is not None else None
    product = _find_product(data, slug, strict=True) if data else None
    visible = _visible_text(tree)
    if _QUEUE_TEXT_RE.search(visible) or (product is None and _QUEUE_INFRA_RE.search(html)):
        return StockResult(retailer=RETAILER, in_stock=False, raw_status=RAW_QUEUE,
                           channels=[ChannelAvail(channel="ship", available=False)],
                           error=None, error_detail=QUEUE_NOTE)
    area = _purchase_area(tree)
    buttons = _buttons(area)
    add_buttons = [enabled for text, enabled in buttons if _ADD_TO_CART_RE.match(text)]

    if product is None and not add_buttons:
        if looks_like_challenge(None, html):
            raise BlockedError("Nintendo challenge / bot-check page")
        raise ParseError("Nintendo page has no __NEXT_DATA__ product or purchase button")

    name: str | None = None
    price: float | None = None
    avail_texts: list[str] = []
    if product is not None:
        raw_name = product.get("name") or product.get("title")
        name = raw_name.strip() if isinstance(raw_name, str) else None
        price = _price_of(product)
        avail_texts = _availability_texts(product)

    button_texts = [t for t, _ in buttons]
    status = _status_from_texts(avail_texts)
    # Invite-only is decided from this product only: its JSON availability, or (when it has
    # no enabled Add to cart) an invite button in its purchase area, or, as a last resort when
    # nothing else is recognisable, invite wording in the purchase area's text.
    purchasable = any(add_buttons)
    invite = (any(_INVITE_FIELD_RE.search(t) for t in avail_texts)
              or (not purchasable and any(_INVITE_FIELD_RE.search(t) for t in button_texts))
              or (not purchasable and status is None and bool(_INVITE_PAGE_RE.search(_area_text(area)))))
    if invite:
        return StockResult(retailer=RETAILER, in_stock=False, raw_status=RAW_INVITE_ONLY,
                           channels=[ChannelAvail(channel="ship", available=False)], price=price,
                           product_name=name, error=None, error_detail=INVITE_NOTE)

    if status is None:
        oos_texts = [t for t in button_texts if any(m in t.lower() for m in _BUTTON_OOS)]
        if any(add_buttons):
            status = RAW_IN_STOCK
        elif oos_texts:
            status = _status_from_texts(oos_texts) or RAW_OUT_OF_STOCK
        elif add_buttons:  # only a disabled Add to cart
            status = RAW_OUT_OF_STOCK
        else:
            raise ParseError(f"unrecognised Nintendo availability: {avail_texts!r}")
    elif status in (RAW_IN_STOCK, RAW_PREORDER) and add_buttons and not any(add_buttons):
        # JSON says purchasable but the only Add to cart button is disabled: trust the page.
        status = RAW_OUT_OF_STOCK

    in_stock = status in (RAW_IN_STOCK, RAW_PREORDER)
    return StockResult(retailer=RETAILER, in_stock=in_stock, raw_status=status, price=price,
                       product_name=name, channels=[ChannelAvail(channel="ship", available=in_stock)])


def signed_in_from_html(html: str) -> bool | None:
    """Best-effort sign-in state from the store header. None when undeterminable."""
    tree = HTMLParser(html)
    labels: list[str] = []
    for node in tree.css("header a, header button, nav a, nav button, [data-testid*=account i]"):
        labels.append(" ".join(filter(None, (node.text(strip=True), node.attributes.get("aria-label")))).lower())
    joined = " | ".join(labels)
    if re.search(r"\b(log ?out|sign ?out|my account|user menu|account menu)\b", joined):
        return True
    if re.search(r"\b(log ?in|sign ?in|create (an )?account)\b", joined):
        return False
    return None


def cart_count_from_html(html: str) -> int | None:
    """Cart badge count from the cart link(s) (aria-label "Cart, 2 items" or badge text)."""
    tree = HTMLParser(html)
    links = tree.css('a[href*="/cart"]')
    counts = [int(m.group(1)) for node in links
              for text in (node.attributes.get("aria-label"), node.text(strip=True))
              if text and (m := re.search(r"(\d+)", text))]
    if counts:
        return max(counts)
    return 0 if links else None  # cart link without a badge -> empty cart


async def is_signed_in(page: Any) -> bool | None:
    """Sign-in hook for BrowserManager.signin_status()."""
    try:
        return signed_in_from_html(await page.content())
    except Exception:
        return None


# ------------------------------------------------------------------ adapter

class NintendoAdapter(BaseAdapter):
    name: ClassVar[str] = RETAILER
    min_interval_seconds: ClassVar[int] = 120
    url_hosts: ClassVar[tuple[str, ...]] = ("nintendo.com",)
    url_patterns: ClassVar[list[tuple[str, re.Pattern[str]]]] = [
        ("product_id", re.compile(r"/store/products/([A-Za-z0-9][A-Za-z0-9._~-]*)", re.IGNORECASE)),
    ]

    #: How long add_to_cart waits for the cart badge to change.
    cart_confirm_polls: ClassVar[int] = 16
    cart_confirm_interval_ms: ClassVar[int] = 500

    @classmethod
    def identifiers_from_url(cls, url: str) -> dict[str, str]:
        ids = super().identifiers_from_url(url)
        if "product_id" not in ids or not cls.matches_url(url):
            raise ValueError(f"not a Nintendo Store product URL: {url}")
        return ids

    def product_url(self, target: Target) -> str:
        url = target.get("url")
        if url:
            try:  # re-validate: never navigate the signed-in profile to another host or to checkout
                return self.canonical_url(url)
            except ValueError as e:
                raise AdapterError(str(e), kind=ErrorKind.CONFIG) from None
        slug = target.get("product_id")
        if slug:
            return f"{STORE_BASE}{slug}/"
        raise AdapterError("nintendo target needs a product url", kind=ErrorKind.CONFIG)

    def cart_url(self) -> str:
        return CART_URL

    async def check(self, target: Target, home: Home) -> StockResult:
        url = self.product_url(target)
        if self.browser is None:
            raise AdapterError("nintendo adapter requires the browser", kind=ErrorKind.CONFIG)
        status, html = await self.browser.fetch_html(RETAILER, url)
        if status in (403, 429):
            raise BlockedError(f"HTTP {status}")
        if status == 404:
            raise AdapterError("HTTP 404", kind=ErrorKind.NOT_FOUND)
        if status >= 400:
            raise AdapterError(f"HTTP {status}", kind=ErrorKind.HTTP_ERROR)
        return parse_nintendo_page(html, home, target)

    async def add_to_cart(self, target: Target, result: StockResult, browser: BrowserCtx,
                          channel: ChannelAvail | None = None) -> CartResult:
        """Open the product page in the cart tab, click "Add to cart" via safe_click, verify the badge.

        The Nintendo Store only ships, so ``channel`` needs no selection here.

        Never navigates to or clicks checkout; a confirmation modal's
        "Checkout" button is left alone.
        """
        def fail(msg: str, **kw: Any) -> CartResult:
            return CartResult(retailer=RETAILER, success=False, message=msg, **kw)

        page = await browser.cart_page(RETAILER)
        await page.goto(self.product_url(target), wait_until="domcontentloaded", timeout=20_000)
        html = await page.content()
        if detect_queue(html):
            return fail("Nintendo queue / waiting room active; cannot add to cart")
        if signed_in_from_html(html) is False:
            return fail("Sign in needed for nintendo", signed_out=True)
        before = cart_count_from_html(html)

        button = page.get_by_role("button", name=_ADD_TO_CART_RE).first
        if await button.count() == 0:
            return fail("Add to cart button not found", cart_count_before=before)
        try:
            if not await button.is_enabled():
                return fail("Add to cart button is disabled", cart_count_before=before)
            await safe_click(button)
        except ForbiddenClickError as exc:
            return fail(f"refused unsafe click: {exc}", cart_count_before=before)

        after = before
        for _ in range(self.cart_confirm_polls):
            await page.wait_for_timeout(self.cart_confirm_interval_ms)
            if "accounts.nintendo.com" in (getattr(page, "url", "") or ""):
                return fail("Sign in needed for nintendo", signed_out=True, cart_count_before=before)
            after = cart_count_from_html(await page.content())
            if after is not None and after > (before or 0):
                return CartResult(retailer=RETAILER, success=True, message="Added to cart",
                                  cart_url=CART_URL, cart_count_before=before, cart_count_after=after)
        return fail("cart count did not change after Add to cart",
                    cart_count_before=before, cart_count_after=after)
