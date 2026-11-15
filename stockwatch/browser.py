"""Playwright browser manager (spec sections 3, 7 and 9).

One persistent context on the dedicated Chrome profile (``~/StockWatch/chrome-profile``),
driven through real Google Chrome (``channel="chrome"``). There is deliberately NO
stealth: no user-agent override, no fingerprint patches, no proxies, no extra
launch flags. The bot identifies as a normal Chrome simply by being one.

Pages:
* one reusable *check* page per retailer (``page_for``), navigation serialized by an
  ``asyncio.Lock`` per retailer, used by ``fetch_html``;
* one separate *cart* page per retailer (``cart_page``) for the cart helper.

The context is started lazily and transparently restarted if Chrome crashes or the
user closes the window. A launch runs as one shared task: callers await it through
``asyncio.shield``, so a caller's timeout never orphans a half-launched Chrome on the
profile, and concurrent callers reuse the same launch. After ``close()`` the manager
refuses to start again (no Chrome relaunch from stragglers during shutdown).

Sign-in status is cached (``SIGNIN_CACHE_TTL``) and each retailer's home-page probe
goes through ``request_gate`` (the scheduler's per-retailer gate) when one is set.
"""
from __future__ import annotations

import asyncio
import contextlib
import importlib
import logging
import time
from collections.abc import Awaitable, Callable, Iterable
from contextlib import AbstractAsyncContextManager
from pathlib import Path
from typing import Any

from . import paths
from .adapters.base import AdapterError, BlockedError, looks_like_challenge
from .config import BrowserCfg
from .models import ErrorKind

log = logging.getLogger(__name__)

#: Where the user signs in during setup (the bot never stores passwords).
SIGNIN_URLS: dict[str, str] = {
    "bestbuy": "https://www.bestbuy.com/identity/global/signin",
    "target": "https://www.target.com/login",
    "walmart": "https://www.walmart.com/account/login",
    "gamestop": "https://www.gamestop.com/login/",
    "nintendo": "https://accounts.nintendo.com/login",
}

#: Neutral page loaded before running an adapter's ``is_signed_in(page)`` hook.
HOME_URLS: dict[str, str] = {
    "bestbuy": "https://www.bestbuy.com/",
    "target": "https://www.target.com/",
    "walmart": "https://www.walmart.com/",
    "gamestop": "https://www.gamestop.com/",
    "nintendo": "https://www.nintendo.com/us/",
}

SigninCheck = Callable[[Any], Awaitable[bool | None]]
PlaywrightStarter = Callable[[], Awaitable[Any]]
RequestGate = Callable[[str], AbstractAsyncContextManager[Any]]

#: How long a sign-in probe result is reused before the retailer's home page is loaded again.
SIGNIN_CACHE_TTL = 600.0


async def _default_starter() -> Any:
    from playwright.async_api import async_playwright

    return await async_playwright().start()


def _pw_errors() -> tuple[type[BaseException], type[BaseException]]:
    """(TimeoutError, Error) from Playwright, imported lazily."""
    from playwright.async_api import Error, TimeoutError

    return TimeoutError, Error


def _hook_from_module(retailer: str) -> SigninCheck | None:
    """Find ``is_signed_in(page)`` on the retailer's adapter module or adapter class."""
    try:
        mod = importlib.import_module(f"stockwatch.adapters.{retailer}")
    except ImportError:
        return None
    hook = getattr(mod, "is_signed_in", None)
    if callable(hook):
        return hook
    for obj in vars(mod).values():
        if isinstance(obj, type) and getattr(obj, "name", None) == retailer:
            hook = getattr(obj, "is_signed_in", None)
            if callable(hook):
                return hook
    return None


class BrowserManager:
    """Implements :class:`stockwatch.adapters.base.BrowserCtx`."""

    def __init__(self, cfg: BrowserCfg | None = None, *, profile_dir: Path | None = None,
                 playwright_starter: PlaywrightStarter | None = None,
                 signin_checks: dict[str, SigninCheck] | None = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.cfg = cfg or BrowserCfg()
        self.profile_dir = profile_dir
        self._starter = playwright_starter or _default_starter
        self._signin_checks = signin_checks
        self._clock = clock
        self._pw: Any = None
        self._context: Any = None
        self._headless_now: bool | None = None
        self._force_headed = False
        self._closed = False
        self._launch_task: asyncio.Task[Any] | None = None
        self._start_lock = asyncio.Lock()   # serialises restart/close (not plain start)
        self._pages: dict[str, Any] = {}
        self._cart_pages: dict[str, Any] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._signin_cache: dict[str, tuple[float, bool | None]] = {}
        #: Optional per-retailer request gate (the scheduler's RetailerGate) for sign-in probes.
        self.request_gate: RequestGate | None = None

    # ------------------------------------------------------------------ lifecycle
    @property
    def started(self) -> bool:
        return self._context is not None

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def headless(self) -> bool:
        return bool(self.cfg.headless) and not self._force_headed

    async def start(self) -> Any:
        """Launch (or return) the persistent context. Safe to call repeatedly and concurrently.

        The launch itself runs in one shared task awaited through ``asyncio.shield``:
        cancelling a caller (e.g. a ``wait_for`` timeout) does not abandon it, and the
        context is recorded when it finishes even if every caller has gone.
        """
        if self._closed:
            raise AdapterError("browser is shut down", kind=ErrorKind.CONFIG)
        if self._context is not None:
            return self._context
        task = self._launch_task
        if task is None or task.done():
            task = self._launch_task = asyncio.create_task(self._launch(), name="browser-launch")
            task.add_done_callback(self._launch_done)
        return await asyncio.shield(task)

    def _launch_done(self, task: asyncio.Task[Any]) -> None:
        if self._launch_task is task:
            self._launch_task = None
        if not task.cancelled() and task.exception() is not None:
            log.warning("browser launch failed: %s", _short(task.exception()))  # type: ignore[arg-type]

    async def _launch(self) -> Any:
        profile = self.profile_dir or paths.chrome_profile_dir()
        profile.mkdir(parents=True, exist_ok=True)
        profile.chmod(0o700)
        if self._pw is None:
            self._pw = await self._starter()
        headless = self.headless
        ctx = await self._pw.chromium.launch_persistent_context(
            user_data_dir=str(profile), channel=self.cfg.channel, headless=headless,
        )
        if self._closed:  # shut down while Chrome was starting: don't leave it running
            with contextlib.suppress(Exception):
                await ctx.close()
            raise AdapterError("browser is shut down", kind=ErrorKind.CONFIG)
        ctx.on("close", lambda *_: self._on_context_closed(ctx))
        self._context = ctx
        self._headless_now = headless
        log.info("browser started (channel=%s, headless=%s)", self.cfg.channel, headless)
        return ctx

    async def _settle_launch(self) -> None:
        """Wait for an in-flight launch so restart/close act on its result, not around it."""
        task = self._launch_task
        if task is not None and not task.done():
            with contextlib.suppress(Exception):
                await asyncio.shield(task)

    async def close(self) -> None:
        """Close the context and stop Playwright. The manager cannot be started again."""
        self._closed = True
        async with self._start_lock:
            await self._settle_launch()
            ctx, self._context = self._context, None
            self._forget_pages()
            if ctx is not None:
                try:
                    await ctx.close()
                except Exception:  # already dead
                    log.debug("context close failed", exc_info=True)
            pw, self._pw = self._pw, None
            if pw is not None:
                try:
                    await pw.stop()
                except Exception:
                    log.debug("playwright stop failed", exc_info=True)

    async def restart(self, stale: Any = None) -> Any:
        """Replace a dead (or wrong-headless-mode) context with a fresh one.

        ``stale``: the context the caller found dead. If another task has already
        replaced it, the current context is returned instead of closing a healthy,
        just-launched one. ``None`` means "restart whatever is running".
        """
        async with self._start_lock:
            await self._settle_launch()
            current = self._context
            if stale is not None and current is not None and current is not stale:
                return current
            self._context = None
            self._forget_pages()
            if current is not None:
                try:
                    await current.close()
                except Exception:
                    log.debug("context close during restart failed", exc_info=True)
            return await self.start()

    def _on_context_closed(self, ctx: Any) -> None:
        if ctx is self._context:
            log.warning("browser context closed unexpectedly; it will be restarted on next use")
            self._context = None
            self._forget_pages()

    def _forget_pages(self) -> None:
        self._pages.clear()
        self._cart_pages.clear()

    async def _ensure_context(self) -> Any:
        ctx = self._context
        if ctx is None:
            return await self.start()
        if not self._context_alive(ctx):
            return await self.restart(stale=ctx)
        return ctx

    @staticmethod
    def _context_alive(ctx: Any) -> bool:
        try:
            browser = getattr(ctx, "browser", None)
            if browser is not None and hasattr(browser, "is_connected") and not browser.is_connected():
                return False
            _ = ctx.pages  # raises on some dead contexts
            return True
        except Exception:
            return False

    # ------------------------------------------------------------------ pages
    def lock_for(self, retailer: str) -> asyncio.Lock:
        """Lock serializing navigation of the retailer's shared check page."""
        lock = self._locks.get(retailer)
        if lock is None:
            lock = self._locks[retailer] = asyncio.Lock()
        return lock

    async def _page_in(self, store: dict[str, Any], retailer: str) -> Any:
        page = store.get(retailer)
        if page is not None and not page.is_closed() and self._context is not None:
            return page
        ctx = await self._ensure_context()
        try:
            page = await ctx.new_page()
        except Exception:
            # Context died between the liveness check and new_page(): one restart (of *that* context).
            ctx = await self.restart(stale=ctx)
            try:
                page = await ctx.new_page()
            except Exception as exc:
                raise AdapterError(f"browser error: {_short(exc)}") from None
        store[retailer] = page
        return page

    async def page_for(self, retailer: str) -> Any:
        """Reusable stock-check page for a retailer (spec: no fresh session per poll)."""
        return await self._page_in(self._pages, retailer)

    async def cart_page(self, retailer: str) -> Any:
        """Separate page used only by the cart helper, so checks and carting don't collide."""
        return await self._page_in(self._cart_pages, retailer)

    # ------------------------------------------------------------------ fetching
    async def fetch_html(self, retailer: str, url: str, *, wait_selector: str | None = None,
                         timeout_ms: int = 30_000) -> tuple[int, str]:
        """Navigate the retailer's shared page to ``url`` and return ``(status, html)``.

        Raises ``BlockedError`` on 403/429 or a challenge/CAPTCHA interstitial (the
        scheduler backs off; we never retry here), ``AdapterError(kind=TIMEOUT)`` on
        navigation timeouts.
        """
        pw_timeout, pw_error = _pw_errors()
        async with self.lock_for(retailer):
            page = await self.page_for(retailer)
            try:
                resp = await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                status = int(resp.status) if resp is not None else 0
                if status in (403, 429):
                    raise BlockedError(f"HTTP {status}")
                if wait_selector:
                    try:
                        await page.wait_for_selector(wait_selector, timeout=timeout_ms)
                    except pw_timeout:
                        html = await page.content()
                        if looks_like_challenge(status, html):
                            raise BlockedError("challenge page") from None
                        raise AdapterError(f"timed out waiting for {wait_selector!r}",
                                           kind=ErrorKind.TIMEOUT) from None
                html = await page.content()
            except AdapterError:
                raise
            except pw_timeout as exc:
                raise AdapterError(f"navigation timeout: {_short(exc)}", kind=ErrorKind.TIMEOUT) from None
            except pw_error as exc:
                if self._page_dead(page):
                    self._pages.pop(retailer, None)
                    if self._context is not None and not self._context_alive(self._context):
                        self._on_context_closed(self._context)
                raise AdapterError(f"browser error: {_short(exc)}") from None
        if looks_like_challenge(status, html):
            raise BlockedError("challenge page")
        return status, html

    @staticmethod
    def _page_dead(page: Any) -> bool:
        try:
            return bool(page.is_closed())
        except Exception:
            return True

    # ------------------------------------------------------------------ setup flow
    async def open_signin_pages(self, retailers: Iterable[str]) -> list[str]:
        """Open each retailer's sign-in page in its own tab, headed, for manual sign-in.

        Accepts retailer names (looked up in ``SIGNIN_URLS``) or explicit URLs. If the
        browser is running headless it is relaunched headed (setup needs a window).
        Returns the URLs opened.
        """
        urls: list[str] = []
        for item in retailers:
            if item in SIGNIN_URLS:
                urls.append(SIGNIN_URLS[item])
            elif item.startswith(("http://", "https://")):
                urls.append(item)
            else:
                log.warning("no sign-in URL for %r", item)
        self._force_headed = True
        self._signin_cache.clear()  # the user is about to sign in: re-probe next time
        if self._context is not None and self._headless_now:
            await self.restart(stale=self._context)
        ctx = await self._ensure_context()
        for url in urls:
            page = await ctx.new_page()
            try:
                await page.goto(url, wait_until="domcontentloaded")
            except Exception as exc:  # the tab stays open; user can retry by hand
                log.warning("could not load sign-in page %s: %s", url, _short(exc))
        return urls

    async def end_signin(self) -> None:
        """Return to the configured headless mode after the setup flow."""
        self._force_headed = False
        if self._context is not None and self._headless_now != self.headless:
            await self.restart(stale=self._context)

    def _signin_hook(self, retailer: str) -> SigninCheck | None:
        if self._signin_checks is not None:
            return self._signin_checks.get(retailer)
        return _hook_from_module(retailer)

    def cached_signin_status(self, retailers: Iterable[str]) -> dict[str, bool | None]:
        """Last known sign-in state per retailer (None = unknown / never probed); no I/O."""
        return {name: (self._signin_cache.get(name) or (0.0, None))[1] for name in retailers}

    def signin_stale(self, retailers: Iterable[str], max_age: float = SIGNIN_CACHE_TTL) -> list[str]:
        """Retailers whose cached sign-in state is missing or older than ``max_age`` seconds."""
        now = self._clock()
        return [n for n in retailers
                if n not in self._signin_cache or now - self._signin_cache[n][0] > max_age]

    async def signin_status(self, retailers: Iterable[str] | None = None, *,
                            max_age: float = SIGNIN_CACHE_TTL, launch: bool = True) -> dict[str, bool | None]:
        """Per-retailer sign-in state: True/False from the adapter hook, None if unknown.

        Results younger than ``max_age`` come from the cache. Otherwise the retailer's
        home page is loaded in a throwaway tab (so shared check/cart pages are not
        disturbed), through ``request_gate`` when set so the probe respects the same
        per-retailer spacing as stock checks. ``launch=False`` never starts Chrome:
        retailers that would need it keep their cached value.
        """
        names = list(retailers) if retailers is not None else list(SIGNIN_URLS)
        out: dict[str, bool | None] = {}
        stale = set(self.signin_stale(names, max_age))
        for name in names:
            if name not in stale:
                out[name] = self._signin_cache[name][1]
                continue
            hook = self._signin_hook(name)
            if hook is None:
                out[name] = None
                continue
            if self._closed or (not launch and not self.started):
                out[name] = self.cached_signin_status([name])[name]
                continue
            gate = self.request_gate(name) if self.request_gate is not None else contextlib.nullcontext()
            async with gate:
                out[name] = await self._probe_signin(name, hook)
            self._signin_cache[name] = (self._clock(), out[name])
        return out

    async def _probe_signin(self, name: str, hook: SigninCheck) -> bool | None:
        page = None
        try:
            ctx = await self._ensure_context()
            page = await ctx.new_page()
            home = HOME_URLS.get(name)
            if home:
                resp = await page.goto(home, wait_until="domcontentloaded", timeout=30_000)
                status = int(resp.status) if resp is not None else 0
                if looks_like_challenge(status, await page.content()):
                    return None  # blocked: don't guess, don't retry
            result = await hook(page)
            return None if result is None else bool(result)
        except Exception as exc:
            log.warning("sign-in check for %s failed: %s", name, _short(exc))
            return None
        finally:
            if page is not None:
                with contextlib.suppress(Exception):
                    await page.close()


def _short(exc: BaseException, limit: int = 200) -> str:
    text = str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__
    return text[:limit]
