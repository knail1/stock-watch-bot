"""Retailer adapter contract (spec section 6).

Design rule: every adapter splits *fetching* from *parsing*. Parsing is a pure
function over a recorded payload (JSON dict or HTML string) so it can be unit
tested against fixtures in tests/fixtures/<retailer>/.
"""
from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, ClassVar, Protocol, runtime_checkable
from urllib.parse import SplitResult, urlsplit, urlunsplit

import httpx

from ..models import CHANNELS, CartResult, ChannelAvail, ErrorKind, Home, StockResult, Target

if TYPE_CHECKING:  # pragma: no cover
    from ..config import RetailerCfg


class AdapterError(Exception):
    """Raised inside an adapter; the scheduler converts it to StockResult.error."""

    kind: str = ErrorKind.UNKNOWN

    def __init__(self, detail: str = "", kind: str | None = None):
        super().__init__(detail)
        if kind:
            self.kind = kind


class BlockedError(AdapterError):
    kind = ErrorKind.BLOCKED


class ParseError(AdapterError):
    """Payload shape changed. Must surface loudly as 'adapter broken'."""
    kind = ErrorKind.PARSE_FAILED


class SignedOutError(AdapterError):
    kind = ErrorKind.SIGNED_OUT


@runtime_checkable
class BrowserCtx(Protocol):
    """Implemented by stockwatch.browser.BrowserManager.

    One persistent Chrome profile (~/StockWatch/chrome-profile); one reusable
    page per retailer for stock checks (spec: shared context per retailer, no
    fresh sessions per poll) and a separate page for cart work.
    """

    async def page_for(self, retailer: str) -> Any:  # playwright Page, reused across checks
        ...

    async def cart_page(self, retailer: str) -> Any:  # playwright Page used only by the cart helper
        ...

    async def fetch_html(self, retailer: str, url: str, *, wait_selector: str | None = None,
                         timeout_ms: int = 30_000) -> tuple[int, str]:
        """Navigate the retailer's shared page to url; return (http_status, html). Raises BlockedError on challenge."""
        ...


@runtime_checkable
class RetailerAdapter(Protocol):
    name: str
    min_interval_seconds: int

    async def check(self, target: Target, home: Home) -> StockResult: ...
    async def add_to_cart(self, target: Target, result: StockResult, browser: BrowserCtx,
                          channel: ChannelAvail | None = None) -> CartResult:
        """Add to the signed-in cart using ``channel`` (the matched fulfillment option and store).

        ``channel=None`` means "best available in ``result``". Never checks out.
        """
        ...

    def product_url(self, target: Target) -> str: ...
    def cart_url(self) -> str: ...


class BaseAdapter:
    """Convenience base class. Subclasses set name/min_interval_seconds and implement the rest."""

    name: ClassVar[str] = ""
    min_interval_seconds: ClassVar[int] = 60
    # Regexes over a product URL that yield identifiers, e.g. {"sku": "6614313"}.
    url_patterns: ClassVar[list[tuple[str, re.Pattern[str]]]] = []
    url_hosts: ClassVar[tuple[str, ...]] = ()
    # Identifier keys a pasted URL must yield; identifiers_from_url raises ValueError otherwise.
    required_identifiers: ClassVar[tuple[str, ...]] = ()

    def __init__(self, http: httpx.AsyncClient, browser: BrowserCtx | None = None,
                 settings: "RetailerCfg | None" = None):
        self.http = http
        self.browser = browser
        self.settings = settings

    # ---- URL helpers (used by dashboard "paste a URL" and Telegram /add) ----
    @classmethod
    def matches_url(cls, url: str) -> bool:
        split = safe_split_url(url)
        if split is None:
            return False
        host = split.hostname or ""
        return any(host == h or host.endswith("." + h) for h in cls.url_hosts)

    @classmethod
    def canonical_url(cls, url: str) -> str:
        """Validated, normalised product URL on one of ``url_hosts`` (no fragment, no userinfo).

        Raises ValueError for anything that is not a plain http(s) URL on the
        retailer's own host, or that points at a cart/checkout/account page.
        """
        split = safe_split_url(url)
        if split is None or "${" in url or not cls.matches_url(url):
            raise ValueError(f"not a {cls.name or 'retailer'} product URL: {url.strip()[:200]}")
        path = split.path or "/"
        if FORBIDDEN_PATH_RE.search(path):
            raise ValueError(f"that is a cart/checkout/account page, not a product page: {url.strip()[:200]}")
        host = split.hostname or ""
        if split.port:
            host = f"{host}:{split.port}"
        return urlunsplit((split.scheme.lower(), host, path, split.query, ""))

    @classmethod
    def identifiers_from_url(cls, url: str) -> dict[str, str]:
        """Extract identifiers; always includes 'url'. Raises ValueError if the URL lacks the required id."""
        canonical = cls.canonical_url(url)
        out: dict[str, str] = {}
        for key, pat in cls.url_patterns:
            m = pat.search(canonical)
            if m and key not in out:
                out[key] = m.group(1)
        missing = [k for k in cls.required_identifiers if k not in out]
        if missing:
            raise ValueError(f"no {', '.join(missing)} found in {cls.name or 'retailer'} URL")
        out["url"] = canonical
        return out

    async def check(self, target: Target, home: Home) -> StockResult:  # pragma: no cover
        raise NotImplementedError

    async def add_to_cart(self, target: Target, result: StockResult, browser: BrowserCtx,
                          channel: ChannelAvail | None = None) -> CartResult:  # pragma: no cover
        raise NotImplementedError

    def product_url(self, target: Target) -> str:  # pragma: no cover
        raise NotImplementedError

    def cart_url(self) -> str:  # pragma: no cover
        raise NotImplementedError


# ------------------------------------------------------------------ shared helpers

#: Paths a "product" URL may never point at (the bot must never land on checkout).
FORBIDDEN_PATH_RE = re.compile(r"/(checkout|cart|account|login|signin|identity)(?:[/.?#]|$)", re.IGNORECASE)
_UNSAFE_URL_CHARS_RE = re.compile(r"[\\\s\x00-\x1f\x7f]")


def safe_split_url(url: str) -> SplitResult | None:
    """Parse ``url`` strictly; None unless it is a plain http(s) URL with a real host.

    Rejects backslashes, whitespace/control characters, userinfo (``user@host``)
    and missing hosts, so tricks like ``https://evil.example?.retailer.com/`` or
    ``https://evil.example\\.retailer.com/`` can never pass a host check.
    """
    url = (url or "").strip()
    if not url or _UNSAFE_URL_CHARS_RE.search(url):
        return None
    if "://" not in url:
        url = "https://" + url  # pasted without a scheme, e.g. "www.target.com/p/-/A-123"
    try:
        split = urlsplit(url)
        _ = split.port  # raises ValueError on a malformed port
    except ValueError:
        return None
    if split.scheme.lower() not in ("http", "https") or not split.hostname:
        return None
    if "@" in split.netloc:
        return None
    return split


CHALLENGE_MARKERS = (
    "captcha", "are you a human", "verify you are human", "press & hold", "press and hold",
    "px-captcha", "perimeterx", "access denied", "request unsuccessful", "unusual traffic",
    "robot check", "bot detection", "akamai", "_incapsula_resource", "cf-chl", "challenge-platform",
    "please verify you are a human", "blocked",
)


LOCAL_CHANNELS = ("store_pickup", "in_store")


def resolve_cart_channel(result: StockResult, channel: ChannelAvail | None,
                         fulfillment: list[str] | tuple[str, ...] | None = None) -> ChannelAvail | None:
    """The channel the cart should use: ``channel`` if given, else the best available one
    in ``fulfillment`` priority order (nearest store first)."""
    if channel is not None:
        return channel
    avail = result.available_channels()
    for ch in fulfillment or CHANNELS:
        hits = [c for c in avail if c.channel == ch]
        if hits:
            return min(hits, key=lambda c: (c.distance_miles is None, c.distance_miles or 0.0))
    return None


def pickup_store_note(retailer_display: str, matched: ChannelAvail, page_text: str | None) -> str:
    """Describe a pickup add: confirms the matched store, or says honestly that the retailer
    used the store saved in the account (which the bot does not change) instead."""
    name = (matched.store_name or "").strip()
    if not name:
        return "store pickup"
    if page_text and name.lower() in page_text.lower():
        return f"pickup at {name}"
    return (f"pickup at the store set in your {retailer_display} account, which may not be {name}; "
            f"check the pickup store in the cart")


_CHALLENGE_PHRASES = ("verify you are human", "press & hold", "press &amp; hold", "press and hold",
                      "are you a human", "robot or human", "robot check", "please verify you are a human")
#: Markup of an *active* challenge (not just a script that could show one later).
_CHALLENGE_ELEMENTS = ('id="px-captcha"', "id='px-captcha'", 'class="g-recaptcha"', "cf-chl-",
                       "challenge-platform", "_incapsula_resource")
_PX_MODAL_RE = re.compile(r'<iframe[^>]*id="px-captcha-modal"[^>]*style="[^"]*display:\s*block', re.I)
#: Real product pages are hundreds of KB; challenge interstitials are small.
_SMALL_PAGE = 50_000


def looks_like_challenge(status: int | None, html: str | None) -> bool:
    """Heuristic for 403/429 or a challenge/CAPTCHA interstitial or overlay (spec: back off, never retry).

    Bare words like "captcha" are common inside normal pages' scripts and config
    (e.g. Target's ``GLOBAL_CAPTCHA_ENABLED``), so on full-size pages only visible challenge
    wording or an active challenge element counts.
    """
    if status in (403, 429, 435):
        return True
    if not html:
        return False
    low = html.lower()
    if _PX_MODAL_RE.search(html):           # PerimeterX overlay shown on top of the page
        return True
    if any(m in low for m in _CHALLENGE_ELEMENTS):
        return True
    small = len(html) < _SMALL_PAGE
    if any(p in (low if small else _visible_text(low)) for p in _CHALLENGE_PHRASES):
        return True
    if small and ("captcha" in low or "access denied" in low or "request unsuccessful" in low
                  or "unusual traffic" in low):
        return True
    return False


def _visible_text(low_html: str) -> str:
    """Lower-cased page text without <script>/<style> contents (cheap, regex-based)."""
    return re.sub(r"<(script|style)\b[^>]*>.*?</\1>", " ", low_html, flags=re.S)


#: Non-standard status PerimeterX/HUMAN bot protection returns with a JSON block payload.
BOT_BLOCK_STATUSES = (403, 429, 435)
_BOT_BLOCK_JSON_MARKERS = ("blockscript", "perimeterx", "px-cdn.net", "px-captcha", "captcha.js")


def looks_like_bot_block(status: int | None, body: str | None) -> bool:
    """True for bot-protection blocks that arrive as JSON (e.g. PerimeterX HTTP 435 + blockScript)."""
    if status in BOT_BLOCK_STATUSES:
        return True
    head = (body or "")[:4000].lower()
    return any(m in head for m in _BOT_BLOCK_JSON_MARKERS)


def raise_for_http(resp: httpx.Response) -> None:
    if resp.status_code in BOT_BLOCK_STATUSES:
        raise BlockedError(f"HTTP {resp.status_code}")
    if resp.status_code == 404:
        raise AdapterError("HTTP 404", kind=ErrorKind.NOT_FOUND)
    if resp.status_code >= 400:
        raise AdapterError(f"HTTP {resp.status_code}", kind=ErrorKind.HTTP_ERROR)


def parse_price(text: str | None) -> float | None:
    if text is None:
        return None
    if isinstance(text, (int, float)):
        return float(text)
    # Comma-grouped form needs at least one ",ddd" group, so "1299.99" is never cut to "129".
    m = _PRICE_NUM_RE.search(str(text).replace("$", ""))
    return float(m.group(1).replace(",", "")) if m else None


_PRICE_NUM_RE = re.compile(r"(?<![\d,])(\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?)")
