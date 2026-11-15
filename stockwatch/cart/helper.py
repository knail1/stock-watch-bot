"""Cart helper (spec section 8).

Wraps each retailer adapter's ``add_to_cart`` with the guarantees the service
relies on:

* a hard timeout (25 s by default) enforced with ``asyncio.wait_for`` that also
  covers waiting for the per-retailer lock;
* a catch-all, so ``try_add`` never raises (except when its own task is
  cancelled) and always returns a ``CartResult``;
* signed-out detection mapped to ``CartResult(signed_out=True)`` with a
  "Sign in needed for <Retailer>" message;
* one ``asyncio.Lock`` per retailer, so two restocks never drive the same cart
  page concurrently;
* logging that never includes secrets (query-string keys/tokens are redacted).

The helper itself never touches the page: all clicks happen inside adapters,
which must use ``stockwatch.cart.safety.safe_click``. The service schedules
``try_add`` as a task *after* the alert has been sent; nothing here may delay
an alert.
"""
from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from ..adapters.base import AdapterError, BlockedError, BrowserCtx, SignedOutError, resolve_cart_channel
from ..models import CartResult, ChannelAvail, ErrorKind, StockResult, Target
from .safety import ForbiddenClickError

if TYPE_CHECKING:  # pragma: no cover
    from ..config import WatchCfg

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 25.0
_MAX_MESSAGE_LEN = 300

RETAILER_NAMES: dict[str, str] = {
    "bestbuy": "Best Buy",
    "target": "Target",
    "walmart": "Walmart",
    "gamestop": "GameStop",
    "nintendo": "Nintendo",
}

# key=value pairs in URLs / messages whose value must never reach logs or alerts.
_SECRET_KV_RE = re.compile(
    r"(?i)\b((?:api[_-]?key|apikey|key|token|access[_-]?token|secret|password|passwd|pin|"
    r"session(?:id)?|sig(?:nature)?)\s*[=:]\s*)(\"?)[^\s&\"',;]+"
)
_AUTH_HEADER_RE = re.compile(r"(?i)\b(authorization\s*[:=]\s*)(?:(bearer|basic|token)\s+)?[^\s&\"',;]+")
_BEARER_RE = re.compile(r"(?i)\b(bearer|basic)\s+(?!\*\*\*)[A-Za-z0-9._~+/=-]{8,}")


def redact(text: str) -> str:
    """Strip secret-looking values (api keys, tokens, auth headers) from free text."""
    text = _AUTH_HEADER_RE.sub(lambda m: f"{m.group(1)}{m.group(2) + ' ' if m.group(2) else ''}***", text)
    text = _SECRET_KV_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}***", text)
    return _BEARER_RE.sub(lambda m: f"{m.group(1)} ***", text)


def _clean(text: str) -> str:
    text = redact(" ".join(str(text).split()))
    return text if len(text) <= _MAX_MESSAGE_LEN else text[: _MAX_MESSAGE_LEN - 1] + "…"


def retailer_display(retailer: str) -> str:
    return RETAILER_NAMES.get(retailer, retailer)


def signed_out_message(retailer: str) -> str:
    return f"Sign in needed for {retailer_display(retailer)}"


class CartHelper:
    """Run adapter add-to-cart attempts safely. See module docstring."""

    def __init__(self, browser: BrowserCtx | None, adapters: Mapping[str, Any],
                 timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS) -> None:
        self.browser = browser
        self.adapters = adapters
        self.timeout_seconds = float(timeout_seconds)
        self._locks: dict[str, asyncio.Lock] = {}

    def lock_for(self, retailer: str) -> asyncio.Lock:
        """The lock serialising cart work on one retailer's cart page."""
        lock = self._locks.get(retailer)
        if lock is None:
            lock = self._locks[retailer] = asyncio.Lock()
        return lock

    def _cart_url(self, retailer: str) -> str | None:
        adapter = self.adapters.get(retailer)
        if adapter is None:
            return None
        try:
            return adapter.cart_url()
        except Exception:
            return None

    async def try_add(self, target: Target, watch: "WatchCfg | None" = None,
                      result: StockResult | None = None, *,
                      channel: ChannelAvail | None = None) -> CartResult:
        """Attempt to add ``target`` to the signed-in cart. Never raises, never checks out.

        ``channel`` is the matched fulfillment option and store (``Decision.channel``,
        spec 8). When omitted, the best available channel honouring
        ``watch.fulfillment`` and ``target.store_ids`` is used.
        """
        retailer = target.retailer
        watch_id = getattr(watch, "id", None) or target.watch_id

        skip = self._precheck(target, watch, result)
        if skip is not None:
            log.info("cart skipped retailer=%s watch=%s: %s", retailer, watch_id, skip.message)
            return skip

        assert result is not None  # guaranteed by _precheck
        adapter = self.adapters[retailer]
        channel = channel or self._choose_channel(target, watch, result)
        loop = asyncio.get_running_loop()
        started = loop.time()
        try:
            cart = await asyncio.wait_for(self._locked_add(adapter, target, result, channel),
                                          timeout=self.timeout_seconds)
            cart = self._normalize(retailer, cart)
        except asyncio.CancelledError:
            # Our own task was cancelled (service shutdown): propagate, do not swallow.
            raise
        except TimeoutError:
            cart = CartResult(retailer=retailer, success=False,
                              message=f"Add to cart timed out after {self.timeout_seconds:g}s",
                              cart_url=self._cart_url(retailer))
        except SignedOutError:
            cart = CartResult(retailer=retailer, success=False, signed_out=True,
                              message=signed_out_message(retailer), cart_url=self._cart_url(retailer))
        except ForbiddenClickError:
            # Safety net tripped: the adapter tried to click a checkout/purchase control.
            log.error("cart refused checkout/purchase click retailer=%s watch=%s", retailer, watch_id)
            cart = CartResult(retailer=retailer, success=False,
                              message="Add to cart aborted: refused to click a checkout/purchase control",
                              cart_url=self._cart_url(retailer))
        except BlockedError as exc:
            cart = CartResult(retailer=retailer, success=False,
                              message=_clean(f"Blocked by {retailer_display(retailer)} ({exc}); not retrying"),
                              cart_url=self._cart_url(retailer))
        except AdapterError as exc:
            if exc.kind == ErrorKind.SIGNED_OUT:
                cart = CartResult(retailer=retailer, success=False, signed_out=True,
                                  message=signed_out_message(retailer), cart_url=self._cart_url(retailer))
            else:
                cart = CartResult(retailer=retailer, success=False,
                                  message=_clean(f"Add to cart failed ({exc.kind}): {exc}"),
                                  cart_url=self._cart_url(retailer))
        except Exception as exc:  # catch-all: the cart attempt must never crash the service
            cart = CartResult(retailer=retailer, success=False,
                              message=_clean(f"Add to cart failed: {type(exc).__name__}: {exc}"),
                              cart_url=self._cart_url(retailer))

        elapsed = loop.time() - started
        level = logging.INFO if cart.success else logging.WARNING
        log.log(level, "cart result retailer=%s watch=%s success=%s signed_out=%s elapsed=%.1fs msg=%s",
                retailer, watch_id, cart.success, cart.signed_out, elapsed, _clean(cart.message))
        return cart

    def _precheck(self, target: Target, watch: "WatchCfg | None",
                  result: StockResult | None) -> CartResult | None:
        retailer = target.retailer
        if watch is not None and not watch.add_to_cart:
            return CartResult(retailer=retailer, success=False, message="Add to cart disabled for this watch")
        if result is None or not result.in_stock or result.error:
            return CartResult(retailer=retailer, success=False, message="Not in stock; add to cart skipped")
        if retailer not in self.adapters:
            return CartResult(retailer=retailer, success=False, message=f"No adapter for {retailer}")
        if self.browser is None:
            return CartResult(retailer=retailer, success=False, message="Browser unavailable; add to cart skipped",
                              cart_url=self._cart_url(retailer))
        return None

    @staticmethod
    def _choose_channel(target: Target, watch: "WatchCfg | None", result: StockResult) -> ChannelAvail | None:
        fulfillment = list(watch.fulfillment) if watch is not None else None
        if target.store_ids:
            allowed = set(target.store_ids)
            result = StockResult(retailer=result.retailer, in_stock=result.in_stock, channels=[
                c for c in result.channels if c.channel == "ship" or c.store_id is None or c.store_id in allowed])
        return resolve_cart_channel(result, None, fulfillment)

    async def _locked_add(self, adapter: Any, target: Target, result: StockResult,
                          channel: ChannelAvail | None) -> CartResult:
        async with self.lock_for(target.retailer):
            return await adapter.add_to_cart(target, result, self.browser, channel=channel)

    def _normalize(self, retailer: str, cart: Any) -> CartResult:
        """Validate the adapter's return value and apply signed-out / cart-URL conventions."""
        if not isinstance(cart, CartResult):
            return CartResult(retailer=retailer, success=False,
                              message=f"Adapter returned {type(cart).__name__}, not CartResult",
                              cart_url=self._cart_url(retailer))
        if cart.signed_out:
            cart.success = False
            cart.message = signed_out_message(retailer)
        if not cart.cart_url:
            cart.cart_url = self._cart_url(retailer)
        cart.message = _clean(cart.message or ("Added to cart" if cart.success else "Add to cart failed"))
        return cart
