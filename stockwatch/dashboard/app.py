"""FastAPI dashboard (spec section 10).

The dashboard talks to the running service only through the `DashboardControl`
protocol below, so it can be tested with a fake and the service stays free to
change its internals.

Screens: watches (status cards), watch detail (targets + recent checks, check
now), add/edit watch (pasted URLs), events, settings (notify channels + test,
home ZIP, quiet hours, retailer sign-in status + open sign-in browser).
"""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Protocol, runtime_checkable
from urllib.parse import urlencode

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.datastructures import FormData

from ..adapters.registry import identifiers_from_url
from ..models import CHANNELS, RETAILERS, AlertOutcome, Target
from .auth import (
    COOKIE_NAME,
    CSRF_FIELD,
    CSRF_HEADER,
    LoginThrottle,
    SessionSigner,
    pin_matches,
)

log = logging.getLogger(__name__)

HERE = Path(__file__).resolve().parent
StatusColor = Literal["green", "yellow", "red", "grey"]

RETAILER_LABELS = {"bestbuy": "Best Buy", "target": "Target", "walmart": "Walmart",
                   "gamestop": "GameStop", "nintendo": "Nintendo"}
CHANNEL_LABELS = {"store_pickup": "Store pickup", "in_store": "In store", "ship": "Ship"}
NOTIFY_LABELS = {"pushover": "Pushover", "telegram": "Telegram", "mac_sound": "Mac sound"}
OK_MESSAGES = {
    "added": "Watch added.",
    "saved": "Saved.",
    "paused": "Watch paused.",
    "resumed": "Watch resumed.",
    "deleted": "Watch deleted.",
    "checking": "Check started. Refresh in a few seconds.",
    "signin": "Sign-in browser opened on the Mac. Sign in to each retailer there.",
}
SIGNIN_TIMEOUT_SECONDS = 15.0
LOCAL_HOSTNAMES = {"127.0.0.1", "localhost", "[::1]", "::1"}
PUBLIC_PATHS = ("/login", "/static/", "/manifest.webmanifest", "/favicon.ico")


# ------------------------------------------------------------------ view models

@dataclass
class TargetStatus:
    """One target as the dashboard shows it (built by the control from scheduler.status() + db)."""

    retailer: str
    identifiers: dict[str, str] = field(default_factory=dict)
    store_ids: list[str] | None = None
    product_url: str | None = None
    last_check_at: datetime | None = None
    last_result: str | None = None          # raw_status, e.g. "IN_STOCK"
    last_in_stock: bool | None = None
    last_price: float | None = None
    last_in_stock_at: datetime | None = None
    interval_seconds: float | None = None
    blocked: bool = False
    error: str | None = None                # current ErrorKind, None if last check succeeded
    consecutive_failures: int = 0
    paused: bool = False

    @property
    def status(self) -> StatusColor:
        if self.paused:
            return "grey"
        if self.blocked:
            return "yellow"
        if self.error:
            return "red"
        return "green"

    @property
    def label(self) -> str:
        ident = next((v for k, v in self.identifiers.items() if k != "url"), None)
        name = RETAILER_LABELS.get(self.retailer, self.retailer)
        return f"{name} {ident}" if ident else name

    def to_target(self, watch_id: str) -> Target:
        return Target(retailer=self.retailer, identifiers=dict(self.identifiers),
                      store_ids=list(self.store_ids) if self.store_ids is not None else None,
                      watch_id=watch_id)


@dataclass
class WatchStatus:
    id: str
    name: str
    enabled: bool = True
    targets: list[TargetStatus] = field(default_factory=list)
    max_price: float | None = None
    fulfillment: list[str] = field(default_factory=lambda: list(CHANNELS))
    radius_miles: float | None = None
    interval_seconds: int | None = None
    add_to_cart: bool = True

    @property
    def status(self) -> StatusColor:
        """grey paused, red any error, yellow any blocked, else green (checking)."""
        if not self.enabled or not self.targets or all(t.paused for t in self.targets):
            return "grey"
        active = [t.status for t in self.targets if not t.paused]
        if "red" in active:
            return "red"
        if "yellow" in active:
            return "yellow"
        return "green"

    @property
    def last_check_at(self) -> datetime | None:
        return max((t.last_check_at for t in self.targets if t.last_check_at), default=None)

    @property
    def last_in_stock_at(self) -> datetime | None:
        return max((t.last_in_stock_at for t in self.targets if t.last_in_stock_at), default=None)


@dataclass
class CheckView:
    retailer: str
    checked_at: datetime
    in_stock: bool
    price: float | None = None
    raw_status: str | None = None
    error: str | None = None
    duration_ms: int | None = None
    summary: str = ""                       # e.g. "Pickup: Best Buy Frisco (6.2 mi)"


@dataclass
class EventView:
    at: datetime
    kind: str                               # restock | blocked | error | alert | cart
    title: str
    detail: str = ""
    watch_id: str | None = None
    retailer: str | None = None
    ended_at: datetime | None = None
    url: str | None = None


@dataclass
class NotifyChannelView:
    name: str                               # pushover | telegram | mac_sound
    enabled: bool
    configured: bool                        # secrets present


@dataclass
class SettingsView:
    home_zip: str
    radius_miles: float
    quiet_hours: str | None
    channels: list[NotifyChannelView] = field(default_factory=list)


@dataclass
class WatchSpec:
    """A complete watch definition submitted from the add/edit forms."""

    id: str
    name: str
    targets: list[Target]
    fulfillment: list[str]
    max_price: float | None = None
    radius_miles: float | None = None
    interval_seconds: int | None = None
    add_to_cart: bool = True
    enabled: bool = True

    def to_raw(self) -> dict[str, Any]:
        """config.yaml shape (spec section 5) for ConfigStore.upsert_watch."""
        raw: dict[str, Any] = {"id": self.id, "name": self.name, "enabled": self.enabled}
        if self.max_price is not None:
            raw["max_price"] = self.max_price
        if self.radius_miles is not None:
            raw["radius_miles"] = self.radius_miles
        if self.interval_seconds is not None:
            raw["interval_seconds"] = self.interval_seconds
        raw["fulfillment"] = list(self.fulfillment)
        raw["add_to_cart"] = self.add_to_cart
        targets = []
        for t in self.targets:
            row: dict[str, Any] = {"retailer": t.retailer, **t.identifiers}
            if t.store_ids is not None:
                row["store_ids"] = list(t.store_ids)
            targets.append(row)
        raw["targets"] = targets
        return raw


@runtime_checkable
class DashboardControl(Protocol):
    """What the dashboard needs from the running service.

    Mutating methods may raise ValueError (incl. ConfigError) with a
    user-facing message, or KeyError for an unknown watch id.

    Optional: ``spawn_background(coro, name)``; when present the dashboard runs its
    fire-and-forget work (check now) through it so the service owns those tasks.
    ``signin_status`` should answer quickly from a cache (it is awaited on page load).
    """

    def watches(self) -> list[WatchStatus]: ...
    def recent_checks(self, watch_id: str, limit: int = 30) -> list[CheckView]: ...
    def recent_events(self, limit: int = 100) -> list[EventView]: ...
    def settings(self) -> SettingsView: ...

    async def signin_status(self) -> dict[str, bool | None]: ...
    async def open_signin_browser(self) -> None: ...
    async def send_test(self, channel: str | None = None) -> list[AlertOutcome]: ...
    async def update_settings(self, home_zip: str, radius_miles: float, quiet_hours: str | None) -> None: ...

    async def add_watch(self, spec: WatchSpec) -> None: ...
    async def update_watch(self, spec: WatchSpec) -> None: ...
    async def set_paused(self, watch_id: str, paused: bool) -> None: ...
    async def delete_watch(self, watch_id: str) -> None: ...
    async def check_now(self, watch_id: str) -> None: ...


# ------------------------------------------------------------------ form parsing

QUIET_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d-([01]\d|2[0-3]):[0-5]\d$")
ZIP_RE = re.compile(r"^\d{5}$")


def slugify(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:40].strip("-") or "watch"


def unique_id(base: str, taken: set[str]) -> str:
    if base not in taken:
        return base
    n = 2
    while f"{base}-{n}" in taken:
        n += 1
    return f"{base}-{n}"


def parse_urls(text: str) -> tuple[list[Target], list[str]]:
    """One URL per line -> (targets, errors). Duplicate products are dropped."""
    targets: list[Target] = []
    errors: list[str] = []
    seen: set[tuple[str, tuple[tuple[str, str], ...]]] = set()
    for line in (text or "").splitlines():
        url = line.strip()
        if not url:
            continue
        try:
            retailer, idents = identifiers_from_url(url)
        except ValueError as e:
            errors.append(str(e))
            continue
        key = (retailer, tuple(sorted(idents.items())))
        if key in seen:
            continue
        seen.add(key)
        targets.append(Target(retailer=retailer, identifiers=idents))
    return targets, errors


def _opt_float(form: FormData, name: str, label: str, errors: list[str], positive: bool = True) -> float | None:
    raw = str(form.get(name) or "").strip().lstrip("$")
    if not raw:
        return None
    try:
        v = float(raw)
    except ValueError:
        errors.append(f"{label} must be a number")
        return None
    if positive and v <= 0:
        errors.append(f"{label} must be greater than 0")
        return None
    return v


def _form_values(form: FormData) -> dict[str, Any]:
    """Echo submitted values back into a re-rendered form."""
    return {
        "name": str(form.get("name") or ""),
        "urls": str(form.get("urls") or ""),
        "fulfillment": [str(f) for f in form.getlist("fulfillment")],
        "max_price": str(form.get("max_price") or ""),
        "radius_miles": str(form.get("radius_miles") or ""),
        "interval_seconds": str(form.get("interval_seconds") or ""),
        "add_to_cart": bool(form.get("add_to_cart")),
        "keep": [str(k) for k in form.getlist("keep")],
    }


def build_spec(form: FormData, *, watch_id: str | None, existing: WatchStatus | None,
               taken_ids: set[str]) -> tuple[WatchSpec | None, list[str]]:
    errors: list[str] = []
    new_targets, url_errors = parse_urls(str(form.get("urls") or ""))
    errors += url_errors
    targets: list[Target] = []
    if existing is not None:
        keep = {str(k) for k in form.getlist("keep")}
        targets = [t.to_target(existing.id) for i, t in enumerate(existing.targets) if str(i) in keep]
    have = {(t.retailer, tuple(sorted(t.identifiers.items()))) for t in targets}
    targets += [t for t in new_targets if (t.retailer, tuple(sorted(t.identifiers.items()))) not in have]
    if not targets and not url_errors:
        errors.append("Paste at least one product URL (one per line)")

    fulfillment = [c for c in CHANNELS if c in {str(f) for f in form.getlist("fulfillment")}]
    if not fulfillment:
        errors.append("Choose at least one fulfillment option")
    max_price = _opt_float(form, "max_price", "Max price", errors)
    radius = _opt_float(form, "radius_miles", "Radius", errors)
    interval_f = _opt_float(form, "interval_seconds", "Interval", errors)
    interval = int(interval_f) if interval_f is not None else None

    name = str(form.get("name") or "").strip()
    if not name and targets:
        first = targets[0]
        name = f"{RETAILER_LABELS.get(first.retailer, first.retailer)} " + next(
            (v for k, v in first.identifiers.items() if k != "url"), "item")
    if len(name) > 80:
        errors.append("Name is too long")
    if "${" in name:
        errors.append("Name may not contain '${'")
    if errors:
        return None, errors
    wid = watch_id or unique_id(slugify(name), taken_ids)
    for t in targets:
        t.watch_id = wid
    return WatchSpec(id=wid, name=name, targets=targets, fulfillment=fulfillment, max_price=max_price,
                     radius_miles=radius, interval_seconds=interval,
                     add_to_cart=bool(form.get("add_to_cart")),
                     enabled=existing.enabled if existing is not None else True), []


# ------------------------------------------------------------------ template filters

def _local(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone()


def fmt_dt(dt: datetime | None) -> str:
    return _local(dt).strftime("%b %d %H:%M:%S") if dt else "never"


def fmt_ago(dt: datetime | None, now: datetime | None = None) -> str:
    if not dt:
        return "never"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    secs = int(((now or datetime.now(timezone.utc)) - dt).total_seconds())
    if secs < 0:
        secs = 0
    if secs < 60:
        return f"{secs}s ago"
    if secs < 3600:
        return f"{secs // 60}m ago"
    if secs < 86400:
        return f"{secs // 3600}h ago"
    return f"{secs // 86400}d ago"


def fmt_price(v: float | None) -> str:
    return f"${v:,.2f}" if v is not None else ""


# ------------------------------------------------------------------ app factory

def _safe_next(target: str | None) -> str:
    if target and target.startswith("/") and not target.startswith("//") and "\\" not in target:
        return target
    return "/"


def create_app(control: DashboardControl, *, pin: str | None = None,
               session_secret: bytes | None = None) -> FastAPI:
    """Build the dashboard app.

    pin: dashboard PIN/token (``dashboard.pin``). When empty the app accepts
    only requests addressed to a loopback Host, and `serve()` refuses to bind
    anything but 127.0.0.1.
    session_secret: optional fixed secret so sessions survive restarts.
    """
    pin = pin or ""
    app = FastAPI(title="StockWatch", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.pin_configured = bool(pin)
    app.state.control = control
    signer = SessionSigner(pin, session_secret)
    throttle = LoginThrottle()
    templates = Jinja2Templates(directory=str(HERE / "templates"))
    templates.env.filters.update(dt=fmt_dt, ago=fmt_ago, price=fmt_price)
    templates.env.globals.update(retailer_labels=RETAILER_LABELS, channel_labels=CHANNEL_LABELS,
                                 notify_labels=NOTIFY_LABELS, channels=CHANNELS, pin_configured=bool(pin))
    background: set[asyncio.Task[Any]] = set()
    app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")

    def _set_cookie(resp: Response, value: str) -> None:
        resp.set_cookie(COOKIE_NAME, value, max_age=signer.max_age, httponly=True, samesite="strict",
                        secure=False, path="/")  # plain http over Tailscale; WireGuard encrypts the link

    @app.middleware("http")
    async def auth_middleware(request: Request, call_next: Any) -> Response:
        path = request.url.path
        request.state.sid = signer.verify(request.cookies.get(COOKIE_NAME))
        request.state.token_auth = False
        new_cookie: str | None = None

        if not pin:
            # Localhost-only mode: block DNS-rebinding by requiring a loopback Host header.
            if (request.url.hostname or "") not in LOCAL_HOSTNAMES:
                return Response("Forbidden host", status_code=403)
            if request.state.sid is None:
                request.state.sid, new_cookie = signer.new_session()
        elif path.startswith(PUBLIC_PATHS):
            pass
        elif "token" in request.query_params:
            # Same lockout as the login form: ?token= must not allow unlimited PIN guessing.
            client = request.client.host if request.client else "?"
            if throttle.locked(client):
                return _harden(Response("Too many attempts. Wait a few minutes.", status_code=429))
            if not pin_matches(request.query_params.get("token"), pin):
                throttle.failure(client)
                return _harden(Response("Invalid token", status_code=401))
            throttle.success(client)
            if request.state.sid is None:
                request.state.sid, new_cookie = signer.new_session()
            if request.method == "GET":
                # Drop the token from the address bar / history once a cookie is set.
                rest = [(k, v) for k, v in request.query_params.multi_items() if k != "token"]
                resp: Response = RedirectResponse(path + ("?" + urlencode(rest) if rest else ""), 303)
                if new_cookie:
                    _set_cookie(resp, new_cookie)
                return _harden(resp)
            request.state.token_auth = True
        elif request.state.sid is None:
            if request.method == "GET":
                nxt = path + (f"?{request.url.query}" if request.url.query else "")
                return _harden(RedirectResponse("/login?" + urlencode({"next": nxt}), 303))
            return _harden(Response("Not signed in", status_code=401))

        resp = await call_next(request)
        if new_cookie:
            _set_cookie(resp, new_cookie)
        return _harden(resp)

    def _harden(resp: Response) -> Response:
        h = resp.headers
        h.setdefault("Cache-Control", "no-store")
        h["X-Frame-Options"] = "DENY"
        h["X-Content-Type-Options"] = "nosniff"
        h["Referrer-Policy"] = "no-referrer"
        h["Content-Security-Policy"] = ("default-src 'self'; img-src 'self' data:; style-src 'self'; "
                                        "script-src 'self'; form-action 'self'; frame-ancestors 'none'; "
                                        "base-uri 'none'")
        return resp

    async def _form(request: Request) -> FormData:
        """Read the POST body and enforce CSRF (token-authenticated requests are exempt)."""
        form = await request.form()
        if request.state.token_auth:
            return form
        token = str(form.get(CSRF_FIELD) or request.headers.get(CSRF_HEADER) or "")
        if not signer.csrf_valid(request.state.sid, token):
            raise HTTPException(403, "CSRF check failed. Reload the page and try again.")
        return form

    def render(request: Request, name: str, ctx: dict[str, Any] | None = None, status: int = 200) -> HTMLResponse:
        sid = request.state.sid
        base = {"csrf": signer.csrf_token(sid) if sid else "", "path": request.url.path,
                "ok": OK_MESSAGES.get(request.query_params.get("ok", ""))}
        return templates.TemplateResponse(request, name, {**base, **(ctx or {})}, status_code=status)

    def back(url: str, ok: str) -> RedirectResponse:
        return RedirectResponse(f"{url}?ok={ok}", 303)

    def find_watch(watch_id: str) -> WatchStatus:
        w = next((w for w in control.watches() if w.id == watch_id), None)
        if w is None:
            raise HTTPException(404, f"No watch {watch_id}")
        return w

    def spawn(coro: Any) -> None:
        # Prefer the service's tracked spawner so shutdown cancels and awaits these tasks.
        owner = getattr(control, "spawn_background", None)
        if callable(owner):
            owner(coro, "dashboard-task")
            return
        task = asyncio.create_task(coro)
        background.add(task)
        task.add_done_callback(background.discard)
        task.add_done_callback(lambda t: t.cancelled() or not t.exception() or
                               log.warning("dashboard background task failed: %r", t.exception()))

    # ---------------------------------------------------------------- auth

    @app.get("/login", response_class=HTMLResponse)
    async def login_form(request: Request, next: str = "/") -> Response:
        if not pin or request.state.sid:
            return RedirectResponse(_safe_next(next), 303)
        return render(request, "login.html", {"next": _safe_next(next), "error": None})

    @app.post("/login")
    async def login(request: Request) -> Response:
        if not pin:
            return RedirectResponse("/", 303)
        form = await request.form()
        nxt = _safe_next(str(form.get("next") or "/"))
        client = request.client.host if request.client else "?"
        if throttle.locked(client):
            return render(request, "login.html", {"next": nxt, "error": "Too many attempts. Wait a few minutes."},
                          status=429)
        if not pin_matches(str(form.get("pin") or ""), pin):
            throttle.failure(client)
            return render(request, "login.html", {"next": nxt, "error": "Wrong PIN."}, status=401)
        throttle.success(client)
        _, cookie = signer.new_session()
        resp = RedirectResponse(nxt, 303)
        _set_cookie(resp, cookie)
        return resp

    @app.post("/logout")
    async def logout(request: Request) -> Response:
        await _form(request)
        resp = RedirectResponse("/login", 303)
        resp.delete_cookie(COOKIE_NAME, path="/")
        return resp

    # ---------------------------------------------------------------- PWA bits

    @app.get("/manifest.webmanifest")
    async def manifest() -> Response:
        return FileResponse(HERE / "static" / "manifest.webmanifest", media_type="application/manifest+json")

    @app.get("/favicon.ico")
    async def favicon() -> Response:
        return FileResponse(HERE / "static" / "icon-192.png", media_type="image/png")

    # ---------------------------------------------------------------- watches

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request) -> Response:
        watches = control.watches()
        counts = {c: sum(1 for w in watches if w.status == c) for c in ("green", "yellow", "red", "grey")}
        return render(request, "watches.html", {"watches": watches, "counts": counts})

    @app.get("/watches/new", response_class=HTMLResponse)
    async def new_watch(request: Request) -> Response:
        s = control.settings()
        values = {"fulfillment": list(CHANNELS), "add_to_cart": True, "radius_miles": "", "urls": "",
                  "name": "", "max_price": "", "interval_seconds": "", "keep": []}
        return render(request, "watch_form.html", {"watch": None, "values": values, "errors": [],
                                                   "default_radius": s.radius_miles})

    @app.post("/watches/new", response_class=HTMLResponse)
    async def create_watch(request: Request) -> Response:
        form = await _form(request)
        taken = {w.id for w in control.watches()}
        spec, errors = build_spec(form, watch_id=None, existing=None, taken_ids=taken)
        if spec is not None:
            try:
                await control.add_watch(spec)
            except ValueError as e:
                errors = [str(e)]
            else:
                spawn(control.check_now(spec.id))
                return back(f"/watches/{spec.id}", "added")
        return render(request, "watch_form.html", {"watch": None, "values": _form_values(form), "errors": errors,
                                                   "default_radius": control.settings().radius_miles}, status=400)

    @app.get("/watches/{watch_id}", response_class=HTMLResponse)
    async def watch_detail(request: Request, watch_id: str) -> Response:
        w = find_watch(watch_id)
        return render(request, "watch_detail.html", {"w": w, "checks": control.recent_checks(watch_id, 30)})

    @app.get("/watches/{watch_id}/edit", response_class=HTMLResponse)
    async def edit_watch_form(request: Request, watch_id: str) -> Response:
        w = find_watch(watch_id)
        values = {"name": w.name, "urls": "", "fulfillment": list(w.fulfillment), "add_to_cart": w.add_to_cart,
                  "max_price": "" if w.max_price is None else f"{w.max_price:g}",
                  "radius_miles": "" if w.radius_miles is None else f"{w.radius_miles:g}",
                  "interval_seconds": "" if w.interval_seconds is None else str(w.interval_seconds),
                  "keep": [str(i) for i in range(len(w.targets))]}
        return render(request, "watch_form.html", {"watch": w, "values": values, "errors": [],
                                                   "default_radius": control.settings().radius_miles})

    @app.post("/watches/{watch_id}/edit", response_class=HTMLResponse)
    async def edit_watch(request: Request, watch_id: str) -> Response:
        form = await _form(request)
        w = find_watch(watch_id)
        spec, errors = build_spec(form, watch_id=watch_id, existing=w, taken_ids=set())
        if spec is not None:
            try:
                await control.update_watch(spec)
            except ValueError as e:
                errors = [str(e)]
            else:
                return back(f"/watches/{watch_id}", "saved")
        return render(request, "watch_form.html", {"watch": w, "values": _form_values(form), "errors": errors,
                                                   "default_radius": control.settings().radius_miles}, status=400)

    async def _op(request: Request, watch_id: str, op: str) -> Response:
        form = await _form(request)
        find_watch(watch_id)
        try:
            if op == "pause":
                await control.set_paused(watch_id, True)
                ok = "paused"
            elif op == "resume":
                await control.set_paused(watch_id, False)
                ok = "resumed"
            elif op == "delete":
                await control.delete_watch(watch_id)
                return back("/", "deleted")
            else:
                spawn(control.check_now(watch_id))
                ok = "checking"
        except KeyError:
            raise HTTPException(404, f"No watch {watch_id}")
        except ValueError as e:
            raise HTTPException(400, str(e))
        return back(_safe_next(str(form.get("next") or f"/watches/{watch_id}")).split("?")[0], ok)

    @app.post("/watches/{watch_id}/pause")
    async def pause(request: Request, watch_id: str) -> Response:
        return await _op(request, watch_id, "pause")

    @app.post("/watches/{watch_id}/resume")
    async def resume(request: Request, watch_id: str) -> Response:
        return await _op(request, watch_id, "resume")

    @app.post("/watches/{watch_id}/delete")
    async def delete(request: Request, watch_id: str) -> Response:
        return await _op(request, watch_id, "delete")

    @app.post("/watches/{watch_id}/check")
    async def check(request: Request, watch_id: str) -> Response:
        return await _op(request, watch_id, "check")

    # ---------------------------------------------------------------- events

    @app.get("/events", response_class=HTMLResponse)
    async def events(request: Request) -> Response:
        return render(request, "events.html", {"events": control.recent_events(100)})

    # ---------------------------------------------------------------- settings

    async def _settings_page(request: Request, *, outcomes: list[AlertOutcome] | None = None,
                             errors: list[str] | None = None, status: int = 200) -> Response:
        try:
            signin = await asyncio.wait_for(control.signin_status(), SIGNIN_TIMEOUT_SECONDS)
        except Exception as e:  # browser not running, timeout, ...
            log.info("dashboard: sign-in status unavailable: %r", e)
            signin = {}
        return render(request, "settings.html", {
            "s": control.settings(), "signin": {r: signin.get(r) for r in RETAILERS},
            "outcomes": outcomes, "errors": errors or []}, status=status)

    @app.get("/settings", response_class=HTMLResponse)
    async def settings(request: Request) -> Response:
        return await _settings_page(request)

    @app.post("/settings", response_class=HTMLResponse)
    async def save_settings(request: Request) -> Response:
        form = await _form(request)
        errors: list[str] = []
        zip_code = str(form.get("home_zip") or "").strip()
        if not ZIP_RE.match(zip_code):
            errors.append("ZIP must be 5 digits")
        radius = _opt_float(form, "radius_miles", "Radius", errors)
        if radius is None and not errors:
            errors.append("Radius is required")
        quiet = str(form.get("quiet_hours") or "").strip() or None
        if quiet and not QUIET_RE.match(quiet):
            errors.append("Quiet hours must look like 23:00-06:00 (or be empty)")
        if not errors:
            try:
                await control.update_settings(zip_code, float(radius or 0), quiet)
            except ValueError as e:
                errors.append(str(e))
            else:
                return back("/settings", "saved")
        return await _settings_page(request, errors=errors, status=400)

    @app.post("/settings/test", response_class=HTMLResponse)
    async def test_send(request: Request) -> Response:
        form = await _form(request)
        channel = str(form.get("channel") or "") or None
        if channel is not None and channel not in NOTIFY_LABELS:
            raise HTTPException(400, "Unknown channel")
        try:
            outcomes = await control.send_test(channel)
        except Exception as e:
            outcomes = [AlertOutcome(channel=channel or "all", ok=False, detail=type(e).__name__)]
        return await _settings_page(request, outcomes=outcomes)

    @app.post("/settings/signin")
    async def open_signin(request: Request) -> Response:
        await _form(request)
        try:
            await control.open_signin_browser()
        except Exception as e:
            return await _settings_page(request, errors=[f"Could not open sign-in browser: {e}"], status=500)
        return back("/settings", "signin")

    return app
