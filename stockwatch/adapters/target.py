"""Target adapter (spec section 6).

Primary source: the redsky aggregation JSON that target.com itself calls:

* ``nearby_stores_v1``  -> stores near the home ZIP (cached; stores do not move)
* ``product_fulfillment_and_variation_hierarchy_v1`` -> shipping + per-store
  pickup / in-store availability for a TCIN at a store
* ``pdp_client_v1`` -> current price and title (only when fulfillment lacks a price)

These endpoints are unofficial and can change without notice, so every parser
validates the shape and raises :class:`ParseError` ("Target adapter broken")
instead of silently reporting out of stock. When the JSON path fails to parse
and a browser is available, the product page is read as a fallback
(``__TGT_DATA__`` preloaded state, then the add-to-cart buttons).
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass
from typing import Any, ClassVar, Iterable

import httpx
from selectolax.lexbor import LexborHTMLParser as HTMLParser

from ..cart import safety
from ..models import CartResult, ChannelAvail, ErrorKind, Home, StockResult, Target
from .base import (
    LOCAL_CHANNELS,
    AdapterError,
    BaseAdapter,
    BlockedError,
    BrowserCtx,
    ParseError,
    looks_like_bot_block,
    looks_like_challenge,
    parse_price,
    pickup_store_note,
    raise_for_http,
    resolve_cart_channel,
)

REDSKY_BASE = "https://redsky.target.com/redsky_aggregations/v1/web"
FULFILLMENT_URL = f"{REDSKY_BASE}/product_fulfillment_and_variation_hierarchy_v1"
NEARBY_STORES_URL = f"{REDSKY_BASE}/nearby_stores_v1"
PDP_URL = f"{REDSKY_BASE}/pdp_client_v1"

# Public web key embedded in target.com's own JS bundle (not a secret, but it
# rotates occasionally): override with `retailers.target.api_key` in config.yaml.
DEFAULT_API_KEY = "9f36aeafbe60771e321a7cc95a78140772ab3e96"

AVAILABLE_STATUSES = frozenset({"IN_STOCK", "LIMITED_STOCK", "PRE_ORDER_SELLABLE"})
UNAVAILABLE_STATUSES = frozenset({
    "OUT_OF_STOCK", "UNAVAILABLE", "NOT_SOLD_IN_STORE", "PRE_ORDER_UNSELLABLE", "DISCONTINUED",
})
KNOWN_STATUSES = AVAILABLE_STATUSES | UNAVAILABLE_STATUSES
# Most-informative first; used to summarise raw_status.
_STATUS_RANK = ("IN_STOCK", "PRE_ORDER_SELLABLE", "LIMITED_STOCK", "OUT_OF_STOCK",
                "PRE_ORDER_UNSELLABLE", "UNAVAILABLE", "NOT_SOLD_IN_STORE", "DISCONTINUED")

LOW_STOCK_QTY = 2
NEARBY_CACHE_TTL_S = 12 * 3600
DEFAULT_MAX_STORES = 3

BROKEN = "Target adapter broken"

TCIN_RE = re.compile(r"/A-(\d{6,10})(?:\b|$)", re.IGNORECASE)
TCIN_QUERY_RE = re.compile(r"[?&](?:tcin|preselect)=(\d{6,10})", re.IGNORECASE)

# ---- product-page selectors (target.com uses stable data-test attributes) ----
SEL_PICKUP_CELL = '[data-test="fulfillment-cell-pickup"]'
SEL_SHIPPING_CELL = '[data-test="fulfillment-cell-shipping"]'
SEL_PICKUP_BUTTON = 'button[data-test="orderPickupButton"]'
SEL_SHIPPING_BUTTON = 'button[data-test="shippingButton"]'
SEL_CART_LINK = '[data-test="@web/CartLink"]'
SEL_ACCOUNT_LINK = '[data-test="@web/AccountLink"]'
SEL_ADDED_MODAL = '[data-test="addToCartModal"]'


def _broken(detail: str) -> ParseError:
    return ParseError(f"{BROKEN}: {detail}")


# ------------------------------------------------------------------ pure parsers

@dataclass(frozen=True)
class NearbyStore:
    store_id: str
    name: str | None
    distance_miles: float | None


def parse_nearby_stores(payload: Any) -> list[NearbyStore]:
    """Parse ``nearby_stores_v1``: data.nearby_stores.stores[{store_id, location_name, distance}]."""
    try:
        stores = payload["data"]["nearby_stores"]["stores"]
    except (KeyError, TypeError) as exc:
        raise _broken(f"nearby_stores shape changed ({exc!r})") from None
    if not isinstance(stores, list):
        raise _broken("nearby_stores.stores is not a list")
    out: list[NearbyStore] = []
    for s in stores:
        if not isinstance(s, dict) or s.get("store_id") in (None, ""):
            raise _broken(f"nearby store entry without store_id: {s!r:.200}")
        dist = s.get("distance")
        out.append(NearbyStore(
            store_id=str(s["store_id"]),
            name=s.get("location_name"),
            distance_miles=float(dist) if isinstance(dist, (int, float)) else None,
        ))
    return out


def parse_pdp_price(payload: Any) -> tuple[float | None, str | None]:
    """Parse ``pdp_client_v1`` -> (current price, product title)."""
    try:
        product = payload["data"]["product"]
        price = product["price"]
    except (KeyError, TypeError) as exc:
        raise _broken(f"pdp price shape changed ({exc!r})") from None
    if not isinstance(price, dict):
        raise _broken("pdp product.price is not an object")
    value = _price_from_obj(price)
    if value is None:
        raise _broken(f"pdp price has no current_retail: {price!r:.200}")
    return value, _title(product)


def parse_fulfillment(payload: Any, home: Home, target: Target,
                      stores: dict[str, NearbyStore] | None = None) -> StockResult:
    """Parse ``product_fulfillment_and_variation_hierarchy_v1`` into a StockResult.

    ``stores`` (from :func:`parse_nearby_stores`) supplies names/distances when the
    payload lacks them. When ``target.store_ids`` is set only those stores are kept.
    Raises AdapterError(NOT_FOUND) for an unknown TCIN and ParseError on any
    unrecognised shape or availability status.
    """
    if not isinstance(payload, dict):
        raise _broken(f"fulfillment payload is {type(payload).__name__}, not an object")
    product = (payload.get("data") or {}).get("product") if isinstance(payload.get("data"), dict) else None
    if not isinstance(product, dict):
        errors = payload.get("errors")
        if isinstance(errors, list) and errors:
            msg = "; ".join(str(e.get("message", e)) if isinstance(e, dict) else str(e) for e in errors)
            if re.search(r"not\s+found|no product", msg, re.IGNORECASE):
                raise AdapterError(f"Target TCIN {target.get('tcin')} not found: {msg}", kind=ErrorKind.NOT_FOUND)
            raise _broken(f"redsky errors: {msg[:300]}")
        raise _broken("missing data.product")
    ful = product.get("fulfillment")
    if not isinstance(ful, dict):
        raise _broken("missing data.product.fulfillment")
    has_ship = "shipping_options" in ful
    has_stores = "store_options" in ful
    if not (has_ship or has_stores):
        raise _broken("fulfillment has neither shipping_options nor store_options")

    stores = stores or {}
    pinned = set(target.store_ids or [])
    channels: list[ChannelAvail] = []
    statuses: list[str] = []

    if has_ship:
        ship = ful["shipping_options"]
        status = _status(ship, "shipping_options")
        statuses.append(status)
        channels.append(ChannelAvail(
            channel="ship", available=status in AVAILABLE_STATUSES,
            low_stock=(status == "LIMITED_STOCK") if status in AVAILABLE_STATUSES else None,
            quantity=_int(ship.get("available_to_promise_quantity")),
        ))

    store_opts = ful.get("store_options") if has_stores else []
    if not isinstance(store_opts, list):
        raise _broken("fulfillment.store_options is not a list")
    for opt in store_opts:
        if not isinstance(opt, dict) or opt.get("location_id") in (None, ""):
            raise _broken(f"store option without location_id: {opt!r:.200}")
        sid = str(opt["location_id"])
        if pinned and sid not in pinned:
            continue
        known = stores.get(sid)
        name = opt.get("location_name") or (known.name if known else None)
        dist = _float(opt.get("distance"))
        if dist is None and known:
            dist = known.distance_miles
        if not pinned:
            # Only stores within the radius count as local. The nearby-stores lookup is made
            # `within` the radius, so a NEARBY store missing from it (and with no distance of
            # its own) is outside the radius; never let an unknown distance pass as local.
            if dist is not None and dist > home.radius_miles:
                continue
            if dist is None and stores and sid not in stores:
                continue
        qty = _int(opt.get("location_available_to_promise_quantity"))
        found = False
        for key, channel in (("order_pickup", "store_pickup"), ("in_store_only", "in_store")):
            if key not in opt:
                continue
            found = True
            status = _status(opt[key], f"store {sid} {key}")
            statuses.append(status)
            avail = status in AVAILABLE_STATUSES
            low = (status == "LIMITED_STOCK" or (qty is not None and 0 < qty <= LOW_STOCK_QTY)) if avail else None
            channels.append(ChannelAvail(
                channel=channel, available=avail, store_id=sid, store_name=name,
                distance_miles=dist, low_stock=low, quantity=qty,
            ))
        if not found:
            raise _broken(f"store {sid} has neither order_pickup nor in_store_only")

    price = None
    if isinstance(product.get("price"), dict):
        price = _price_from_obj(product["price"])
    return StockResult(
        retailer="target",
        in_stock=any(c.available for c in channels),
        channels=channels,
        price=price,
        raw_status=_summarise(statuses),
        product_name=_title(product),
    )


def parse_product_page(html: str, home: Home, target: Target) -> StockResult:
    """Fallback: parse a target.com product page.

    Tries the ``__TGT_DATA__`` preloaded redsky state first (same shape as the
    JSON endpoint), then the visible fulfillment buttons. Raises ParseError if
    neither yields a recognisable state.
    """
    if not isinstance(html, str) or not html.strip():
        raise _broken("empty product page")
    tcin = target.get("tcin")
    state = _extract_tgt_data(html)
    if state is not None:
        product = _find_product(state, tcin)
        if product is not None:
            result = parse_fulfillment({"data": {"product": product}}, home, target)
            if result.price is None:
                price_obj = _find_key(state, "price", lambda v: isinstance(v, dict) and _price_from_obj(v) is not None)
                result.price = _price_from_obj(price_obj) if price_obj else None
            return result
    return _parse_buttons(html)


# ------------------------------------------------------------------ parse helpers

def _status(obj: Any, where: str) -> str:
    if not isinstance(obj, dict):
        raise _broken(f"{where} is not an object")
    status = obj.get("availability_status")
    if not isinstance(status, str):
        raise _broken(f"{where} missing availability_status")
    if status not in KNOWN_STATUSES:
        raise _broken(f"{where} has unknown availability_status {status!r}")
    return status


def _summarise(statuses: Iterable[str]) -> str:
    seen = set(statuses)
    return next((s for s in _STATUS_RANK if s in seen), "UNKNOWN")


def _int(v: Any) -> int | None:
    return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _float(v: Any) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _price_from_obj(price: dict[str, Any]) -> float | None:
    for key in ("current_retail", "current_retail_min", "reg_retail"):
        v = price.get(key)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return float(v)
    fmt = price.get("formatted_current_price")
    return parse_price(fmt) if isinstance(fmt, str) else None


def _title(product: dict[str, Any]) -> str | None:
    try:
        t = product["item"]["product_description"]["title"]
    except (KeyError, TypeError):
        return None
    return t if isinstance(t, str) else None


_TGT_JSON_PARSE_RE = re.compile(r"__TGT_DATA__.{0,400}?JSON\.parse\((\"(?:[^\"\\]|\\.)*\")\)", re.DOTALL)
_TGT_ASSIGN_RE = re.compile(r"__TGT_DATA__\s*=\s*(\{.*?\})\s*;?\s*</script>", re.DOTALL)


def _extract_tgt_data(html: str) -> Any | None:
    m = _TGT_JSON_PARSE_RE.search(html)
    try:
        if m:
            return json.loads(json.loads(m.group(1)))
        m = _TGT_ASSIGN_RE.search(html)
        if m:
            return json.loads(m.group(1))
    except (json.JSONDecodeError, TypeError):
        raise _broken("__TGT_DATA__ present but not valid JSON") from None
    return None


def _walk(node: Any) -> Iterable[dict[str, Any]]:
    stack = [node]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            yield cur
            stack.extend(cur.values())
        elif isinstance(cur, list):
            stack.extend(cur)


def _find_product(state: Any, tcin: str | None) -> dict[str, Any] | None:
    """Find a product object carrying a fulfillment block (matching tcin when present)."""
    for d in _walk(state):
        if isinstance(d.get("fulfillment"), dict) and (
            tcin is None or str(d.get("tcin", tcin)) == tcin
        ):
            return d
    return None


def _find_key(state: Any, key: str, pred) -> Any:
    for d in _walk(state):
        if key in d and pred(d[key]):
            return d[key]
    return None


def _parse_buttons(html: str) -> StockResult:
    tree = HTMLParser(html)

    def enabled(sel: str) -> bool:
        n = tree.css_first(sel)
        return n is not None and "disabled" not in n.attributes and n.attributes.get("aria-disabled") != "true"

    pickup = enabled(SEL_PICKUP_BUTTON)
    ship = enabled(SEL_SHIPPING_BUTTON)
    oos_node = tree.css_first('[data-test="outOfStockMessage"], [data-test="soldOutBlock"], '
                              '[data-test="outOfStockButton"], [data-test="notAvailableMessage"]')
    price_node = tree.css_first('[data-test="product-price"]')
    title_node = tree.css_first('h1[data-test="product-title"]')
    if not (pickup or ship or oos_node is not None):
        raise _broken("product page has no __TGT_DATA__ and no recognisable fulfillment buttons")
    channels = [ChannelAvail(channel="ship", available=ship)]
    if pickup or tree.css_first(SEL_PICKUP_BUTTON) is not None:
        channels.append(ChannelAvail(channel="store_pickup", available=pickup))
    return StockResult(
        retailer="target", in_stock=pickup or ship, channels=channels,
        price=parse_price(price_node.text(strip=True)) if price_node else None,
        raw_status="IN_STOCK" if (pickup or ship) else "OUT_OF_STOCK",
        product_name=title_node.text(strip=True) if title_node else None,
    )


# ------------------------------------------------------------------ adapter

class TargetAdapter(BaseAdapter):
    name: ClassVar[str] = "target"
    min_interval_seconds: ClassVar[int] = 90
    url_hosts: ClassVar[tuple[str, ...]] = ("target.com",)
    # ?preselect= names the variation child the user picked; the /A-<id> path is the parent.
    url_patterns: ClassVar[list[tuple[str, re.Pattern[str]]]] = [("tcin", TCIN_QUERY_RE), ("tcin", TCIN_RE)]

    def __init__(self, http: httpx.AsyncClient, browser: BrowserCtx | None = None, settings: Any = None):
        super().__init__(http, browser, settings)
        self._nearby_cache: dict[tuple[str, float], tuple[float, list[NearbyStore]]] = {}

    # ---- URLs / identifiers ----
    @classmethod
    def identifiers_from_url(cls, url: str) -> dict[str, str]:
        out = super().identifiers_from_url(url)
        if "tcin" not in out:
            raise ValueError(f"no Target TCIN (/A-12345678) in URL: {url}")
        return out

    def product_url(self, target: Target) -> str:
        tcin = target.get("tcin")
        return f"https://www.target.com/p/-/A-{tcin}" if tcin else (target.get("url") or "https://www.target.com/")

    def cart_url(self) -> str:
        return "https://www.target.com/cart"

    # ---- settings ----
    @property
    def api_key(self) -> str:
        s = self.settings
        if s is not None:
            if getattr(s, "api_key", ""):
                return str(s.api_key)
            extra = getattr(s, "extra", None) or {}
            if extra.get("api_key"):
                return str(extra["api_key"])
        return DEFAULT_API_KEY

    def _extra(self, key: str, default: Any) -> Any:
        extra = getattr(self.settings, "extra", None) or {}
        return extra.get(key, default)

    # ---- check ----
    async def check(self, target: Target, home: Home) -> StockResult:
        tcin = target.get("tcin")
        if not tcin:
            if target.get("url"):
                try:
                    tcin = self.identifiers_from_url(target.get("url") or "")["tcin"]
                except ValueError:
                    tcin = None
            if not tcin:
                raise AdapterError("Target target has no tcin", kind=ErrorKind.CONFIG)
            target = Target(retailer=target.retailer, identifiers={**target.identifiers, "tcin": tcin},
                            store_ids=target.store_ids, watch_id=target.watch_id, db_id=target.db_id)
        if self.source == "page":
            if self.browser is None:
                raise AdapterError("Target page check needs the Chrome profile (browser unavailable)",
                                   kind=ErrorKind.CONFIG)
            return await self._check_page(target, home)
        try:
            return await self._check_json(target, home, tcin)
        except ParseError as json_err:
            if self.browser is None:
                raise
            try:
                status, html = await self.browser.fetch_html(self.name, self.product_url(target),
                                                             wait_selector="h1")
            except ParseError as page_err:
                raise ParseError(f"{json_err}; page fallback: {page_err}") from None
            if looks_like_challenge(status, html):
                raise BlockedError(f"challenge page on product-page fallback (HTTP {status})") from None
            if status >= 400:
                raise ParseError(f"{json_err}; page fallback HTTP {status}") from None
            try:
                return parse_product_page(html, home, target)
            except ParseError as page_err:
                raise ParseError(f"{json_err}; page fallback: {page_err}") from None

    @property
    def source(self) -> str:
        """`retailers.target.source`: "page" (default) or "api".

        Target puts its redsky JSON behind PerimeterX bot protection (HTTP 435 for non-browser
        clients), so the default reads the product page in the real Chrome profile, the spec's
        documented fallback. "api" keeps the JSON path for when it works.
        """
        v = str(self._extra("source", "page")).strip().lower()
        return v if v in ("page", "api") else "page"

    async def _check_page(self, target: Target, home: Home) -> StockResult:
        """Product page in the shared Chrome profile (shows the store selected in your Target account)."""
        assert self.browser is not None
        status, html = await self.browser.fetch_html(self.name, self.product_url(target), wait_selector="h1")
        if looks_like_challenge(status, html) or looks_like_bot_block(status if status >= 400 else None, None):
            raise BlockedError(f"Target product page challenged (HTTP {status})")
        if status == 404:
            raise AdapterError("Target product page HTTP 404", kind=ErrorKind.NOT_FOUND)
        if status >= 400:
            raise AdapterError(f"Target product page HTTP {status}", kind=ErrorKind.HTTP_ERROR)
        return parse_product_page(html, home, target)

    async def _check_json(self, target: Target, home: Home, tcin: str) -> StockResult:
        nearby = await self._nearby_stores(home)
        by_id = {s.store_id: s for s in nearby}
        if target.store_ids:
            candidates = list(target.store_ids)
        else:
            candidates = [s.store_id for s in sorted(
                nearby, key=lambda s: s.distance_miles if s.distance_miles is not None else 1e9)
                if s.distance_miles is not None and s.distance_miles <= home.radius_miles]
        max_stores = int(self._extra("max_stores", DEFAULT_MAX_STORES))
        candidates = candidates[:max(1, max_stores)]

        merged: StockResult | None = None
        seen_stores: set[str] = set()
        for store_id in candidates or [None]:
            if store_id is not None and store_id in seen_stores:
                continue
            payload = await self._get_json(FULFILLMENT_URL, self._fulfillment_params(tcin, store_id, home))
            res = parse_fulfillment(payload, home, target, by_id)
            merged = res if merged is None else _merge(merged, res)
            seen_stores.update(c.store_id for c in res.channels if c.store_id)
            if store_id is not None:
                seen_stores.add(store_id)
        assert merged is not None

        if merged.price is None:
            price, title = parse_pdp_price(await self._get_json(PDP_URL, self._pdp_params(tcin, candidates)))
            merged.price = price
            merged.product_name = merged.product_name or title
        return merged

    async def _nearby_stores(self, home: Home) -> list[NearbyStore]:
        key = (home.zip, float(home.radius_miles))
        hit = self._nearby_cache.get(key)
        if hit and time.monotonic() - hit[0] < NEARBY_CACHE_TTL_S:
            return hit[1]
        params = {"key": self.api_key, "limit": 20, "within": int(round(home.radius_miles)) or 1,
                  "place": home.zip, "channel": "WEB", "page": "/s"}
        stores = parse_nearby_stores(await self._get_json(NEARBY_STORES_URL, params))
        self._nearby_cache[key] = (time.monotonic(), stores)
        return stores

    def _fulfillment_params(self, tcin: str, store_id: str | None, home: Home) -> dict[str, Any]:
        p: dict[str, Any] = {"key": self.api_key, "tcin": tcin, "zip": home.zip, "channel": "WEB",
                             "page": f"/p/A-{tcin}", "required_store_id": "", "paid_membership": "false",
                             "base_membership": "false", "card_membership": "false"}
        if store_id:
            p.update(store_id=store_id, scheduled_delivery_store_id=store_id, required_store_id=store_id)
        return p

    def _pdp_params(self, tcin: str, stores: list[str]) -> dict[str, Any]:
        p: dict[str, Any] = {"key": self.api_key, "tcin": tcin, "channel": "WEB", "page": f"/p/A-{tcin}"}
        if stores:
            p.update(store_id=stores[0], pricing_store_id=stores[0])
        return p

    async def _get_json(self, url: str, params: dict[str, Any]) -> Any:
        """GET a redsky endpoint. 403/429/challenge -> BlockedError; non-JSON -> ParseError.

        Note: params include the public web key; we never log full URLs.
        """
        try:
            resp = await self.http.get(url, params=params, headers={"Accept": "application/json"},
                                       timeout=20.0)
        except httpx.TimeoutException as exc:
            raise AdapterError(f"timeout fetching {url.rsplit('/', 1)[-1]}", kind=ErrorKind.TIMEOUT) from exc
        except httpx.HTTPError as exc:
            raise AdapterError(f"{type(exc).__name__} fetching {url.rsplit('/', 1)[-1]}",
                               kind=ErrorKind.HTTP_ERROR) from exc
        endpoint = url.rsplit("/", 1)[-1]
        ctype = resp.headers.get("content-type", "")
        if looks_like_bot_block(resp.status_code, resp.text if resp.status_code >= 400 else None):
            raise BlockedError(f"Target {endpoint}: bot protection (HTTP {resp.status_code}); "
                               "Target blocks direct API requests, use retailers.target.source: page")
        if "html" in ctype and looks_like_challenge(resp.status_code, resp.text):
            raise BlockedError(f"Target {endpoint}: challenge page (HTTP {resp.status_code})")
        if resp.status_code in (400, 404) and "json" in ctype:
            # redsky reports unknown TCIN as a JSON error body; let the parser classify it.
            try:
                return resp.json()
            except ValueError:
                pass
        raise_for_http(resp)
        try:
            return resp.json()
        except ValueError:
            if looks_like_challenge(resp.status_code, resp.text):
                raise BlockedError(f"Target {endpoint}: challenge page") from None
            raise _broken(f"{endpoint} returned non-JSON ({ctype or 'no content-type'})") from None

    # ---- cart ----
    async def add_to_cart(self, target: Target, result: StockResult, browser: BrowserCtx,
                          channel: ChannelAvail | None = None) -> CartResult:
        """Add the item to the signed-in cart via the matched channel (pickup or shipping).

        Never checks out; every click goes through safe_click. Target picks up at the
        store saved in the account ("My Store"); when the page does not show the
        matched store the message says so.
        """
        cart_url = self.cart_url()
        page = await browser.cart_page(self.name)
        resp = await page.goto(self.product_url(target), wait_until="domcontentloaded")
        status = getattr(resp, "status", None) if resp is not None else None
        if status in (403, 429):
            return CartResult(self.name, False, f"blocked (HTTP {status})", cart_url=cart_url)

        signed_in = await is_signed_in(page)
        if signed_in is False:
            return CartResult(self.name, False, "signed out of Target in the browser profile",
                              cart_url=cart_url, signed_out=True)

        before = await _cart_count(page)
        # An explicit matched channel is honoured strictly (a ship-only watch never gets a
        # pickup add); without one, try every available option, pickup first.
        matched = resolve_cart_channel(result, channel)
        wanted = {channel.channel} if channel is not None else {c.channel for c in result.available_channels()}
        plan: list[tuple[str, str, str]] = []
        if wanted & set(LOCAL_CHANNELS) or not wanted:
            plan.append(("pickup", SEL_PICKUP_CELL, SEL_PICKUP_BUTTON))
        if "ship" in wanted or not wanted:
            plan.append(("shipping", SEL_SHIPPING_CELL, SEL_SHIPPING_BUTTON))

        try:
            for label, cell_sel, button_sel in plan:
                button = page.locator(button_sel).first
                if not await _usable(button):
                    cell = page.locator(cell_sel).first
                    if await _usable(cell):
                        await safety.safe_click(cell)
                    if not await _usable(button):
                        continue
                if label == "pickup" and matched is not None and matched.channel in LOCAL_CHANNELS:
                    label = pickup_store_note("Target", matched, await _text(page, SEL_PICKUP_CELL))
                await safety.safe_click(button)
                after = await _wait_cart_increase(page, before)
                if after is not None and (before is None or after > before):
                    return CartResult(self.name, True, f"added to cart ({label})", cart_url=cart_url,
                                      cart_count_before=before, cart_count_after=after)
                if before is None and await _usable(page.locator(SEL_ADDED_MODAL).first):
                    return CartResult(self.name, True, f"added to cart ({label}); cart count unreadable",
                                      cart_url=cart_url, cart_count_before=before, cart_count_after=after)
                return CartResult(self.name, False, f"clicked add to cart ({label}) but cart count did not change",
                                  cart_url=cart_url, cart_count_before=before, cart_count_after=after)
        except safety.ForbiddenClickError as exc:
            return CartResult(self.name, False, f"refused unsafe click: {exc}", cart_url=cart_url,
                              cart_count_before=before)
        wanted = "Pickup" if plan and plan[0][0] == "pickup" and len(plan) == 1 else \
            "Shipping" if len(plan) == 1 else "Pickup/Shipping"
        return CartResult(self.name, False, f"no enabled {wanted} add-to-cart button on page",
                          cart_url=cart_url, cart_count_before=before)


def _merge(a: StockResult, b: StockResult) -> StockResult:
    """Combine results from several store queries, de-duplicating channels."""
    keys = {(c.channel, c.store_id) for c in a.channels}
    a.channels.extend(c for c in b.channels if (c.channel, c.store_id) not in keys)
    a.in_stock = any(c.available for c in a.channels)
    a.raw_status = _summarise([a.raw_status, b.raw_status])
    a.price = a.price if a.price is not None else b.price
    a.product_name = a.product_name or b.product_name
    return a


# ------------------------------------------------------------------ page helpers

async def _usable(locator: Any) -> bool:
    try:
        return bool(await locator.count()) and await locator.is_visible() and await locator.is_enabled()
    except Exception:
        return False


async def _text(page: Any, selector: str) -> str | None:
    loc = page.locator(selector).first
    try:
        if not await loc.count():
            return None
        return await loc.inner_text(timeout=2000)
    except Exception:
        return None


async def _cart_count(page: Any) -> int | None:
    """Read the header cart badge ("cart 2 items" aria-label or badge text)."""
    loc = page.locator(SEL_CART_LINK).first
    try:
        if not await loc.count():
            return None
        texts = [await loc.get_attribute("aria-label", timeout=2000) or "",
                 await loc.inner_text(timeout=2000) or ""]
    except Exception:
        return None
    for t in texts:
        m = re.search(r"(\d+)", t)
        if m:
            return int(m.group(1))
    return 0 if any("cart" in t.lower() for t in texts) else None


async def _wait_cart_increase(page: Any, before: int | None, attempts: int = 16,
                              interval_ms: int = 500) -> int | None:
    after = None
    for _ in range(attempts):
        after = await _cart_count(page)
        if after is not None and (before is None or after > before):
            return after
        try:
            await page.wait_for_timeout(interval_ms)
        except Exception:
            await asyncio.sleep(interval_ms / 1000)
    return after


async def is_signed_in(page: Any) -> bool | None:
    """Sign-in check for the browser manager: True/False, or None when unknown.

    target.com's header account link reads "Sign in" when signed out and
    "Hi, <name>" when signed in.
    """
    loc = page.locator(SEL_ACCOUNT_LINK).first
    try:
        if not await loc.count():
            return None
        text = " ".join(filter(None, [await loc.inner_text(timeout=2000),
                                      await loc.get_attribute("aria-label", timeout=2000)])).lower()
    except Exception:
        return None
    if "sign in" in text or "signin" in text:
        return False
    return True if text.strip() else None
