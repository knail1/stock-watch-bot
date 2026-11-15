"""Best Buy adapter (spec section 6): official Products API for stock, browser profile for cart.

Stock comes from two documented endpoints:

* ``GET /v1/products/{sku}/stores.json?postalCode=ZIP`` - stores near ZIP that have
  the SKU for in-store pickup, each with ``distance`` (miles) and ``lowStock``.
* ``GET /v1/products/{sku}.json?show=...`` - name, sale price and online (ship)
  availability.

Parsing is pure (``parse_bestbuy`` / ``parse_stores`` / ``parse_product``) and runs
over recorded fixtures in ``tests/fixtures/bestbuy/``. The API key travels as a
query parameter, so it is never placed in error details and is redacted from
httpx's request log lines.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections import deque
from typing import Any, Callable, ClassVar

import httpx

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
    raise_for_http,
    resolve_cart_channel,
)

log = logging.getLogger(__name__)

API_BASE = "https://api.bestbuy.com/v1"
PRODUCT_FIELDS = "sku,name,type,salePrice,onlineAvailability,inStoreAvailability,orderable,url"

# ---- Best Buy API Terms of Service (docs/policies/bestbuy-api-terms.md) ----
#: Published limits are 50,000 calls/day and 5 calls/second per key. We stay under both
#: with headroom: a rolling 24h window (so no calendar day can exceed it either).
DAILY_CALL_LIMIT = 50_000
DEFAULT_DAILY_CALL_BUDGET = 45_000
MAX_CALLS_PER_SECOND = 4
#: "these Terms do not grant you any rights to Content specific to Games, CDs, DVDs, or
#: Blu-ray titles" -> refuse to track those product types through the API.
UNLICENSED_PRODUCT_TYPES = {"game", "music", "movie"}
SITE_BASE = "https://www.bestbuy.com"
CART_URL = f"{SITE_BASE}/cart"

# `orderable` values that mean the item cannot be shipped right now.
_NOT_ORDERABLE = {"soldout", "notavailable", "comingsoon"}

# ---- cart-page selectors (Playwright CSS; kept as constants so tests can fake them) ----
ADD_TO_CART_SELECTOR = 'button.add-to-cart-button, button[data-button-state="ADD_TO_CART"]'
PICKUP_OPTION_SELECTOR = (
    'button[data-test-id="fulfillment-pickup"], '
    '.fulfillment-fulfillment-summary button:has-text("Pickup")'
)
SHIPPING_OPTION_SELECTOR = (
    'button[data-test-id="fulfillment-shipping"], '
    '.fulfillment-fulfillment-summary button:has-text("Shipping")'
)
#: Text naming the pickup store currently selected on the product page.
PICKUP_STORE_SELECTOR = '.fulfillment-fulfillment-summary .store-name, [data-test-id="fulfillment-store-name"]'
CART_COUNT_SELECTOR = '.cart-link .dot, .cart-icon .dot, [data-testid="cart-count"]'
ACCOUNT_BUTTON_SELECTOR = '#account-menu-account-button, [data-testid="account-button"], .account-button'

CART_POLL_INTERVAL_S = 0.5
CART_POLL_ATTEMPTS = 16


# ------------------------------------------------------------------ secret hygiene

_APIKEY_RE = re.compile(r"(apiKey=)[^&\s\"']+", re.IGNORECASE)


def redact(text: str) -> str:
    """Strip API key values from any string that may contain a request URL."""
    return _APIKEY_RE.sub(r"\1***", text)


class _RedactApiKeyFilter(logging.Filter):
    """httpx logs every request URL at INFO; make sure the Best Buy key never reaches a log."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple) and record.args:
            record.args = tuple(redact(str(a)) if "apikey=" in str(a).lower() else a for a in record.args)
        elif isinstance(record.msg, str) and "apikey=" in record.msg.lower():
            record.msg = redact(record.msg)
        return True


def _install_log_redaction() -> None:
    for name in ("httpx", "httpcore"):
        lg = logging.getLogger(name)
        if not any(isinstance(f, _RedactApiKeyFilter) for f in lg.filters):
            lg.addFilter(_RedactApiKeyFilter())


_install_log_redaction()


# ------------------------------------------------------------------ API rate limiting (ToS)

class BudgetExceeded(BlockedError):
    """The rolling 24h call budget is used up; treated like a block (backoff, one notice)."""


class ApiRateLimiter:
    """Client-side enforcement of Best Buy's per-key limits, shared by every Best Buy target.

    * at most ``per_second`` request starts in any 1-second window (spaced evenly);
    * at most ``daily_budget`` requests in any rolling 24h window; beyond that requests are
      refused (``BudgetExceeded``) instead of being sent and earning 403s.
    """

    def __init__(self, daily_budget: int = DEFAULT_DAILY_CALL_BUDGET, per_second: int = MAX_CALLS_PER_SECOND,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], Any] = asyncio.sleep) -> None:
        self.daily_budget = max(1, min(int(daily_budget), DAILY_CALL_LIMIT))
        self.spacing = 1.0 / max(1, min(int(per_second), 5))
        self._clock = clock
        self._sleep = sleep
        self._calls: deque[float] = deque()
        self._last = float("-inf")
        self._lock = asyncio.Lock()

    def calls_last_24h(self) -> int:
        self._expire(self._clock())
        return len(self._calls)

    def _expire(self, now: float) -> None:
        while self._calls and now - self._calls[0] >= 86_400:
            self._calls.popleft()

    async def acquire(self) -> None:
        async with self._lock:
            now = self._clock()
            self._expire(now)
            if len(self._calls) >= self.daily_budget:
                raise BudgetExceeded(
                    f"Best Buy API daily budget reached ({self.daily_budget} calls in 24h; "
                    f"Best Buy's limit is {DAILY_CALL_LIMIT}). Pausing until calls age out.")
            wait = self._last + self.spacing - now
            if wait > 0:
                await self._sleep(wait)
                now = self._clock()
            self._last = now
            self._calls.append(now)


#: One limiter per process: the limits are per API key, not per target.
_LIMITER: ApiRateLimiter | None = None


def shared_limiter(daily_budget: int = DEFAULT_DAILY_CALL_BUDGET) -> ApiRateLimiter:
    global _LIMITER
    if _LIMITER is None:
        _LIMITER = ApiRateLimiter(daily_budget)
    else:
        _LIMITER.daily_budget = max(1, min(int(daily_budget), DAILY_CALL_LIMIT))
    return _LIMITER


def estimated_daily_calls(interval_seconds: float, targets: int = 1) -> int:
    """Two API calls per check (product, then stores) at the given average interval."""
    return int(2 * targets * 86_400 / max(1.0, interval_seconds))


def _error_message(body: str) -> str:
    """Best Buy's own error text from a 4xx body (quota vs bad key), with any key removed."""
    try:
        data = json.loads(body)
    except ValueError:
        return ""
    if isinstance(data, dict):
        err = data.get("error") if isinstance(data.get("error"), dict) else data
        for k in ("message", "errorMessage", "error"):
            v = err.get(k) if isinstance(err, dict) else None
            if isinstance(v, str) and v.strip():
                return redact(v.strip())[:160]
    return ""


# ------------------------------------------------------------------ pure parsing

def _as_bool(v: Any) -> bool | None:
    if isinstance(v, bool):
        return v
    if isinstance(v, str) and v.lower() in ("true", "false"):
        return v.lower() == "true"
    return None


def _as_float(v: Any) -> float | None:
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def parse_stores(payload: Any, home: Home, target: Target) -> list[ChannelAvail]:
    """Parse the stores.json payload into available store_pickup channels.

    Only stores within ``home.radius_miles`` (and in ``target.store_ids`` when set)
    are returned; stores the API lists are the ones that have the SKU for pickup.
    """
    if isinstance(payload, dict) and "errorCode" in payload:
        raise ParseError(f"bestbuy stores API error {payload.get('errorCode')}")
    if not isinstance(payload, dict) or not isinstance(payload.get("stores"), list):
        raise ParseError("bestbuy stores payload missing 'stores' list")
    ispu = _as_bool(payload.get("ispuEligible"))
    if ispu is False:
        return []
    wanted = {str(s) for s in target.store_ids} if target.store_ids else None
    out: list[ChannelAvail] = []
    for store in payload["stores"]:
        if not isinstance(store, dict) or store.get("storeID") in (None, ""):
            raise ParseError("bestbuy store entry missing storeID")
        store_id = str(store["storeID"])
        distance = _as_float(store.get("distance"))
        if wanted is not None and store_id not in wanted:
            continue
        if distance is not None and distance > home.radius_miles:
            continue
        name = store.get("name") or store.get("longName") or f"Store {store_id}"
        out.append(ChannelAvail(
            channel="store_pickup",
            available=True,
            store_id=store_id,
            store_name=f"Best Buy {name}",
            distance_miles=distance,
            low_stock=_as_bool(store.get("lowStock")),
        ))
    out.sort(key=lambda c: (c.distance_miles is None, c.distance_miles or 0.0))
    return out


def unlicensed_type(payload: Any) -> str | None:
    """The product's `type` if the API terms do not license its content (games, music, movies)."""
    t = payload.get("type") if isinstance(payload, dict) else None
    return t if isinstance(t, str) and t.strip().lower() in UNLICENSED_PRODUCT_TYPES else None


def parse_product(payload: Any) -> tuple[str | None, float | None, bool]:
    """Parse the product payload -> (name, sale price, ship available)."""
    if not isinstance(payload, dict) or "sku" not in payload or "onlineAvailability" not in payload:
        raise ParseError("bestbuy product payload missing sku/onlineAvailability")
    online = _as_bool(payload.get("onlineAvailability"))
    if online is None:
        raise ParseError("bestbuy product payload has non-boolean onlineAvailability")
    orderable = str(payload.get("orderable") or "").replace(" ", "").lower()
    ship = online and orderable not in _NOT_ORDERABLE
    price = parse_price(payload.get("salePrice"))
    if price is None:
        price = parse_price(payload.get("regularPrice"))
    name = payload.get("name") if isinstance(payload.get("name"), str) else None
    return name, price, ship


def parse_bestbuy(payload: dict[str, Any], home: Home, target: Target) -> StockResult:
    """Combine ``{"product": <product.json>, "stores": <stores.json>}`` into a StockResult."""
    if not isinstance(payload, dict) or "product" not in payload or "stores" not in payload:
        raise ParseError("bestbuy payload must contain 'product' and 'stores'")
    name, price, ship = parse_product(payload["product"])
    channels = parse_stores(payload["stores"], home, target)
    channels.append(ChannelAvail(channel="ship", available=ship))
    pickups = [c for c in channels if c.channel == "store_pickup"]
    in_stock = any(c.available for c in channels)
    if not in_stock:
        status = "OUT_OF_STOCK"
    elif pickups and all(c.low_stock for c in pickups):
        status = "LIMITED"
    else:
        status = "IN_STOCK"
    return StockResult(retailer="bestbuy", in_stock=in_stock, channels=channels, price=price,
                       raw_status=status, product_name=name)


# ------------------------------------------------------------------ browser helpers

async def _first_text(page: Any, selector: str) -> str | None:
    loc = page.locator(selector)
    try:
        if await loc.count() == 0:
            return None
        return (await loc.first.inner_text(timeout=2000)).strip()
    except Exception:
        return None


async def is_signed_in(page: Any) -> bool | None:
    """Best Buy header shows "Hi, <name>" when signed in and "Account"/"Sign In" otherwise."""
    text = await _first_text(page, ACCOUNT_BUTTON_SELECTOR)
    if text is None:
        return None
    low = text.lower()
    if re.match(r"\s*hi\b", low):
        return True
    if "sign in" in low or low == "account":
        return False
    return None


async def _cart_count(page: Any) -> int:
    text = await _first_text(page, CART_COUNT_SELECTOR)
    m = re.search(r"\d+", text or "")
    return int(m.group()) if m else 0


# ------------------------------------------------------------------ adapter

class BestBuyAdapter(BaseAdapter):
    name: ClassVar[str] = "bestbuy"
    min_interval_seconds: ClassVar[int] = 60
    url_hosts: ClassVar[tuple[str, ...]] = ("bestbuy.com",)
    url_patterns: ClassVar[list[tuple[str, re.Pattern[str]]]] = [
        ("sku", re.compile(r"[?&]skuId=(\d+)", re.IGNORECASE)),
        ("sku", re.compile(r"/(\d{5,9})\.p\b")),
        # Current PDP format: /product/<slug>/<code>/sku/<sku>
        ("sku", re.compile(r"/sku/(\d{5,9})(?:[/?#]|$)", re.IGNORECASE)),
    ]

    @classmethod
    def identifiers_from_url(cls, url: str) -> dict[str, str]:
        out = super().identifiers_from_url(url)
        if "sku" not in out:
            if "/product/" in out["url"]:
                raise ValueError("this Best Buy link has no numeric SKU; open the product page and copy the "
                                 "link that ends in /sku/<number> (or add the SKU to config.yaml)")
            raise ValueError(f"no Best Buy SKU found in URL: {url}")
        return out

    def product_url(self, target: Target) -> str:
        sku = target.get("sku")
        if sku:
            return f"{SITE_BASE}/site/{sku}.p?skuId={sku}"
        return target.get("url") or SITE_BASE

    def cart_url(self) -> str:
        return CART_URL

    # ---- stock check ----
    @property
    def limiter(self) -> ApiRateLimiter:
        extra = self.settings.extra if self.settings else {}
        return shared_limiter(int(extra.get("daily_call_budget") or DEFAULT_DAILY_CALL_BUDGET))

    async def _get_json(self, url: str, params: dict[str, str]) -> Any:
        await self.limiter.acquire()  # ToS rate limits: 5/s and 50,000/day per key
        try:
            resp = await self.http.get(url, params=params, headers={"Accept": "application/json"})
        except httpx.TimeoutException as e:
            raise AdapterError(f"bestbuy API timeout ({type(e).__name__})", kind=ErrorKind.TIMEOUT) from None
        except httpx.HTTPError as e:
            raise AdapterError(f"bestbuy API transport error ({type(e).__name__})",
                               kind=ErrorKind.HTTP_ERROR) from None
        body = resp.text
        if resp.status_code in (403, 429):
            # Best Buy answers both "over quota" and "bad key" with 403; say which when it tells us.
            msg = _error_message(body)
            raise BlockedError(f"bestbuy API refused (HTTP {resp.status_code})"
                               + (f": {msg}" if msg else "; check the API key or rate limit"))
        if looks_like_challenge(resp.status_code, body if "json" not in resp.headers.get("content-type", "") else None):
            raise BlockedError(f"bestbuy API blocked (HTTP {resp.status_code})")
        raise_for_http(resp)
        try:
            return json.loads(body)
        except ValueError:
            raise ParseError(f"bestbuy API returned non-JSON (HTTP {resp.status_code})") from None

    async def fetch_payload(self, sku: str, zip_code: str, api_key: str) -> dict[str, Any]:
        """Fetch product then stores; skip the stores call for product types the ToS does not license."""
        product = await self._get_json(f"{API_BASE}/products/{sku}.json",
                                       {"show": PRODUCT_FIELDS, "apiKey": api_key, "format": "json"})
        if unlicensed_type(product):
            return {"product": product, "stores": None}
        stores = await self._get_json(f"{API_BASE}/products/{sku}/stores.json",
                                      {"postalCode": zip_code, "apiKey": api_key, "format": "json"})
        return {"product": product, "stores": stores}

    async def check(self, target: Target, home: Home) -> StockResult:
        sku = target.get("sku")
        if not sku or not sku.isdigit():
            return StockResult.failure(self.name, ErrorKind.CONFIG, "bestbuy target needs a numeric 'sku'")
        api_key = (self.settings.api_key if self.settings else "").strip()
        if not api_key or api_key.startswith("${"):
            return StockResult.failure(self.name, ErrorKind.CONFIG,
                                       "Best Buy API key missing (set BESTBUY_API_KEY)")
        payload = await self.fetch_payload(sku, home.zip, api_key)
        kind = unlicensed_type(payload["product"])
        if kind:
            return StockResult.failure(
                self.name, ErrorKind.CONFIG,
                f"Best Buy API terms do not license {kind} titles (games, music, movies); "
                "remove this SKU or track it at another retailer")
        return parse_bestbuy(payload, home, target)

    # ---- add to cart ----
    async def add_to_cart(self, target: Target, result: StockResult, browser: BrowserCtx,
                          channel: ChannelAvail | None = None) -> CartResult:
        """Select the matched fulfillment option (pickup or shipping), add to cart, confirm the count rose.

        Best Buy picks up at the store saved in the account; the bot cannot change it, so the
        message says so when the page does not show the matched store.
        """
        def fail(msg: str, **kw: Any) -> CartResult:
            return CartResult(self.name, success=False, message=msg, cart_url=CART_URL, **kw)

        page = await browser.cart_page(self.name)
        resp = await page.goto(self.product_url(target), wait_until="domcontentloaded", timeout=20_000)
        status = getattr(resp, "status", None) if resp is not None else None
        if looks_like_challenge(status, await page.content()):
            return fail("Best Buy showed a challenge page; not retrying")

        if await is_signed_in(page) is False:
            return fail("Sign in needed for Best Buy", signed_out=True)

        before = await _cart_count(page)
        atc = page.locator(ADD_TO_CART_SELECTOR).first
        try:
            await atc.wait_for(state="visible", timeout=10_000)
        except Exception:
            return fail("Add to Cart button not found", cart_count_before=before)

        matched = resolve_cart_channel(result, channel)
        note = ""
        if matched is not None and matched.channel in LOCAL_CHANNELS:
            option = page.locator(PICKUP_OPTION_SELECTOR)
            if await option.count() > 0:
                try:
                    await safe_click(option.first)
                except ForbiddenClickError as e:
                    log.warning("bestbuy: skipped pickup option: %s", e)
            page_store = " ".join(filter(None, [await _first_text(page, PICKUP_STORE_SELECTOR),
                                                await _first_text(page, PICKUP_OPTION_SELECTOR)]))
            note = f" ({pickup_store_note('Best Buy', matched, page_store)})"
        elif matched is not None and matched.channel == "ship":
            option = page.locator(SHIPPING_OPTION_SELECTOR)
            if await option.count() > 0:
                try:
                    await safe_click(option.first)
                except ForbiddenClickError as e:
                    log.warning("bestbuy: skipped shipping option: %s", e)
            note = " (shipping)"

        if await atc.is_disabled():
            return fail("Add to Cart is disabled (sold out?)", cart_count_before=before)
        try:
            await safe_click(atc)
        except ForbiddenClickError:
            return fail("refused to click: control looks like checkout", cart_count_before=before)

        after = before
        for _ in range(CART_POLL_ATTEMPTS):
            after = await _cart_count(page)
            if after > before:
                return CartResult(self.name, success=True, message=f"Added to cart{note}",
                                  cart_url=CART_URL, cart_count_before=before, cart_count_after=after)
            await asyncio.sleep(CART_POLL_INTERVAL_S)
        return fail("Clicked Add to Cart but the cart count did not change",
                    cart_count_before=before, cart_count_after=after)
