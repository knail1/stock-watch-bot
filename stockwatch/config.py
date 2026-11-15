"""Config loading/saving (spec section 5).

config.yaml is the source of truth. We keep the *raw* YAML document (with ${VAR}
references intact) so dashboard edits can be written back without leaking
secrets into the file, and build a resolved `Config` view for runtime use.

Secret lookup order for ${VAR}: process env -> ~/StockWatch/.env -> macOS
Keychain generic password (service "stockwatch", account VAR).
"""
from __future__ import annotations

import contextlib
import copy
import os
import re
import stat
import subprocess
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import dotenv_values

from . import paths
from .models import CHANNELS, RETAILERS, Home, Target

VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
KEYCHAIN_SERVICE = "stockwatch"

DEFAULT_MIN_INTERVALS: dict[str, int] = {
    "bestbuy": 60,
    "target": 90,
    "gamestop": 120,
    "walmart": 180,
    "nintendo": 120,
}


#: Built-in per-retailer severity for online-only stock (open decision 5). Nintendo
#: only sells online, so "local is preferred" never applies there; override with
#: `retailers.nintendo.online_severity: info`.
DEFAULT_ONLINE_SEVERITY: dict[str, str] = {"nintendo": "critical"}
SEVERITIES = ("critical", "warning", "info", "low")
LOCAL_FULFILLMENT = ("store_pickup", "in_store")

#: Only these top-level sections may contain ${VAR} references. `watches` holds
#: user-entered names/URLs (dashboard, Telegram) and is never expanded, so a
#: watch named "${TELEGRAM_BOT_TOKEN}" cannot read a secret back out.
EXPANDED_SECTIONS = ("home", "defaults", "notify", "dashboard", "health", "browser", "retailers")


class ConfigError(ValueError):
    pass


def default_online_only_severity(fulfillment: list[str]) -> str:
    """Spec 7: online-only stock is *info* when the watch prefers a local channel over ship."""
    for ch in fulfillment:
        if ch in LOCAL_FULFILLMENT:
            return "info"
        if ch == "ship":
            return "critical"
    return "critical"


# ---------------------------------------------------------------- secrets

def _keychain_lookup(name: str) -> str | None:
    try:
        out = subprocess.run(
            ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-a", name, "-w"],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode == 0:
        return out.stdout.strip() or None
    return None


class SecretResolver:
    def __init__(self, env_file: Path | None = None, use_keychain: bool = True):
        self.env_file = env_file if env_file is not None else paths.env_path()
        self.use_keychain = use_keychain
        self._dotenv: dict[str, str] | None = None

    def _dot(self) -> dict[str, str]:
        if self._dotenv is None:
            self._dotenv = {}
            if self.env_file and self.env_file.exists():
                self._dotenv = {k: v for k, v in dotenv_values(self.env_file).items() if v is not None}
        return self._dotenv

    def get(self, name: str) -> str | None:
        if os.environ.get(name):
            return os.environ[name]
        if name in self._dot():
            return self._dot()[name]
        if self.use_keychain:
            return _keychain_lookup(name)
        return None

    def expand(self, value: Any, missing: list[str]) -> Any:
        if isinstance(value, str):
            def sub(m: re.Match[str]) -> str:
                v = self.get(m.group(1))
                if v is None:
                    missing.append(m.group(1))
                    return ""
                return v
            return VAR_RE.sub(sub, value)
        if isinstance(value, dict):
            return {k: self.expand(v, missing) for k, v in value.items()}
        if isinstance(value, list):
            return [self.expand(v, missing) for v in value]
        return value


# ---------------------------------------------------------------- typed view

@dataclass
class PushoverCfg:
    enabled: bool = False
    user_key: str = ""
    app_token: str = ""
    priority: int = 2
    retry_seconds: int = 30
    expire_seconds: int = 900
    sound: str = "siren"


@dataclass
class TelegramCfg:
    enabled: bool = False
    bot_token: str = ""
    chat_id: str = ""


@dataclass
class MacSoundCfg:
    enabled: bool = True
    file: str = "assets/alarm.aiff"
    repeat: int = 3
    speak: bool = True


@dataclass
class NotifyCfg:
    pushover: PushoverCfg = field(default_factory=PushoverCfg)
    telegram: TelegramCfg = field(default_factory=TelegramCfg)
    mac_sound: MacSoundCfg = field(default_factory=MacSoundCfg)
    # "<Retailer> paused" / "checks recovered" notices. Off keeps them on the dashboard and in the
    # log only; blocked-retailer state still shows there. In-stock, price and broken alerts are unaffected.
    status_notices: bool = True


@dataclass
class Defaults:
    interval_seconds: int = 90
    quiet_hours: str | None = None
    max_price: float | None = None


@dataclass
class DashboardCfg:
    port: int = 8787
    bind: list[str] = field(default_factory=lambda: ["127.0.0.1", "tailscale"])
    pin: str = ""                 # usually ${DASHBOARD_PIN}


@dataclass
class HealthCfg:
    heartbeat_url: str = ""       # e.g. https://hc-ping.com/<uuid>, usually ${HEARTBEAT_URL}
    heartbeat_interval_seconds: int = 300
    daily_summary_time: str = "09:00"
    failure_warn_threshold: int = 5
    prevent_sleep: bool = True


@dataclass
class BrowserCfg:
    headless: bool = False
    channel: str = "chrome"       # real Google Chrome, not bundled Chromium (no spoofing)
    cart_timeout_seconds: int = 25


@dataclass
class RetailerCfg:
    """Per-retailer settings: `retailers.bestbuy.api_key: ${BESTBUY_API_KEY}` etc."""
    min_interval_seconds: int | None = None
    api_key: str = ""
    online_severity: str | None = None   # severity for online-only stock at this retailer
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class WatchCfg:
    id: str
    name: str
    enabled: bool = True
    interval_seconds: int | None = None
    max_price: float | None = None
    fulfillment: list[str] = field(default_factory=lambda: ["store_pickup", "in_store", "ship"])
    add_to_cart: bool = True
    radius_miles: float | None = None
    targets: list[Target] = field(default_factory=list)
    # Explicit override for online-only (ship-only) stock. None -> Config.online_only_severity
    # decides: retailers.<name>.online_severity, then the built-in retailer default, then
    # info if a local channel is listed ahead of ship (spec 7), else critical.
    online_only_severity: str | None = None


@dataclass
class Config:
    home: Home
    defaults: Defaults
    notify: NotifyCfg
    dashboard: DashboardCfg
    health: HealthCfg
    browser: BrowserCfg
    retailers: dict[str, RetailerCfg]
    watches: list[WatchCfg]
    missing_secrets: list[str] = field(default_factory=list)

    def watch(self, watch_id: str) -> WatchCfg | None:
        return next((w for w in self.watches if w.id == watch_id), None)

    def min_interval(self, retailer: str) -> int:
        rc = self.retailers.get(retailer)
        if rc and rc.min_interval_seconds:
            return max(rc.min_interval_seconds, DEFAULT_MIN_INTERVALS.get(retailer, 60))
        return DEFAULT_MIN_INTERVALS.get(retailer, 60)

    def effective_interval(self, watch: WatchCfg, retailer: str) -> int:
        base = watch.interval_seconds or self.defaults.interval_seconds
        return max(base, self.min_interval(retailer))

    def effective_max_price(self, watch: WatchCfg) -> float | None:
        return watch.max_price if watch.max_price is not None else self.defaults.max_price

    def online_only_severity(self, watch: WatchCfg, retailer: str) -> str:
        """Severity for stock that is available to ship only (see WatchCfg.online_only_severity)."""
        if watch.online_only_severity:
            return watch.online_only_severity
        rc = self.retailers.get(retailer)
        if rc and rc.online_severity:
            return rc.online_severity
        if retailer in DEFAULT_ONLINE_SEVERITY:
            return DEFAULT_ONLINE_SEVERITY[retailer]
        return default_online_only_severity(watch.fulfillment)

    def effective_home(self, watch: WatchCfg) -> Home:
        return Home(zip=self.home.zip, radius_miles=watch.radius_miles or self.home.radius_miles)


_TARGET_ID_KEYS = ("sku", "tcin", "item_id", "url", "product_id")


def _parse_target(raw: dict[str, Any], watch_id: str) -> Target:
    if not isinstance(raw, dict) or "retailer" not in raw:
        raise ConfigError(f"watch {watch_id}: each target needs a 'retailer'")
    retailer = str(raw["retailer"]).lower()
    if retailer not in RETAILERS:
        raise ConfigError(f"watch {watch_id}: unknown retailer {retailer!r} (known: {', '.join(RETAILERS)})")
    idents = {k: str(v) for k, v in raw.items() if k in _TARGET_ID_KEYS and v not in (None, "")}
    store_ids = raw.get("store_ids")
    if store_ids is not None:
        store_ids = [str(s) for s in store_ids]
    return Target(retailer=retailer, identifiers=idents, store_ids=store_ids, watch_id=watch_id)


def _severity_or_none(value: Any, where: str) -> str | None:
    if value in (None, ""):
        return None
    v = str(value).strip().lower()
    if v not in SEVERITIES:
        raise ConfigError(f"{where} must be one of {', '.join(SEVERITIES)}")
    return v


def _reject_var_refs(value: Any, where: str) -> None:
    """Watches are user-entered and never expanded; refuse ${VAR} there outright."""
    if isinstance(value, str):
        if "${" in value:
            raise ConfigError(f"{where}: '${{...}}' references are not allowed in watch names, URLs or ids")
    elif isinstance(value, dict):
        for k, v in value.items():
            _reject_var_refs(v, f"{where}.{k}")
    elif isinstance(value, list):
        for i, v in enumerate(value):
            _reject_var_refs(v, f"{where}[{i}]")


def _sub(cls, raw: dict[str, Any] | None):
    raw = raw or {}
    known = {k: v for k, v in raw.items() if k in cls.__dataclass_fields__}
    return cls(**known)


def build_config(raw: dict[str, Any], resolver: SecretResolver | None = None) -> Config:
    resolver = resolver or SecretResolver()
    missing: list[str] = []
    data = copy.deepcopy(raw or {})
    for section in EXPANDED_SECTIONS:
        if section in data:
            data[section] = resolver.expand(data[section], missing)
    _reject_var_refs(data.get("watches"), "watches")

    home_raw = data.get("home") or {}
    if not home_raw.get("zip"):
        raise ConfigError("home.zip is required")
    home = Home(zip=str(home_raw["zip"]), radius_miles=float(home_raw.get("radius_miles", 25)))

    n = data.get("notify") or {}
    notify = NotifyCfg(
        pushover=_sub(PushoverCfg, n.get("pushover")),
        telegram=_sub(TelegramCfg, n.get("telegram")),
        mac_sound=_sub(MacSoundCfg, n.get("mac_sound")),
        status_notices=bool(n.get("status_notices", True)),
    )
    notify.telegram.chat_id = str(notify.telegram.chat_id or "")

    retailers: dict[str, RetailerCfg] = {}
    for name, rc in (data.get("retailers") or {}).items():
        rc = rc or {}
        online = _severity_or_none(rc.get("online_severity"), f"retailers.{name}.online_severity")
        retailers[name] = RetailerCfg(
            min_interval_seconds=rc.get("min_interval_seconds"),
            api_key=str(rc.get("api_key") or ""),
            online_severity=online,
            extra={k: v for k, v in rc.items() if k not in ("min_interval_seconds", "api_key", "online_severity")},
        )

    watches: list[WatchCfg] = []
    seen: set[str] = set()
    for w in data.get("watches") or []:
        wid = str(w.get("id") or "").strip()
        if not wid:
            raise ConfigError("every watch needs an id")
        if wid in seen:
            raise ConfigError(f"duplicate watch id {wid}")
        seen.add(wid)
        ful = [str(f) for f in (w.get("fulfillment") or list(CHANNELS))]
        bad = [f for f in ful if f not in CHANNELS]
        if bad:
            raise ConfigError(f"watch {wid}: unknown fulfillment {bad}")
        watches.append(WatchCfg(
            id=wid,
            name=str(w.get("name") or wid),
            enabled=bool(w.get("enabled", True)),
            interval_seconds=w.get("interval_seconds"),
            max_price=w.get("max_price"),
            fulfillment=ful,
            add_to_cart=bool(w.get("add_to_cart", True)),
            radius_miles=w.get("radius_miles"),
            targets=[_parse_target(t, wid) for t in (w.get("targets") or [])],
            online_only_severity=_severity_or_none(w.get("online_only_severity"),
                                                   f"watch {wid}: online_only_severity"),
        ))

    return Config(
        home=home,
        defaults=_sub(Defaults, data.get("defaults")),
        notify=notify,
        dashboard=_sub(DashboardCfg, data.get("dashboard")),
        health=_sub(HealthCfg, data.get("health")),
        browser=_sub(BrowserCfg, data.get("browser")),
        retailers=retailers,
        watches=watches,
        missing_secrets=sorted(set(missing)),
    )


class ConfigStore:
    """Holds the raw YAML document and writes edits back to disk.

    Only the `watches` (and a few safe scalar settings) are edited from the
    dashboard/Telegram; ${VAR} references elsewhere are preserved verbatim.
    """

    def __init__(self, path: Path | None = None, resolver: SecretResolver | None = None):
        self.path = path or paths.config_path()
        self.resolver = resolver or SecretResolver()
        self._lock = threading.RLock()
        self.raw: dict[str, Any] = {}
        self.config: Config | None = None

    def load(self) -> Config:
        with self._lock:
            if not self.path.exists():
                raise ConfigError(f"config not found at {self.path} (run `stockwatch init`)")
            self.raw = yaml.safe_load(self.path.read_text()) or {}
            self.config = build_config(self.raw, self.resolver)
            return self.config

    def save(self) -> Config:
        """Validate then atomically write the raw doc; returns the rebuilt Config."""
        with self._lock:
            cfg = build_config(self.raw, self.resolver)  # validate before writing
            tmp = self.path.with_suffix(".yaml.tmp")
            # Keep the user's permissions (e.g. chmod 600); a new file starts private.
            try:
                mode = stat.S_IMODE(self.path.stat().st_mode)
            except FileNotFoundError:
                mode = 0o600
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write(yaml.safe_dump(self.raw, sort_keys=False, allow_unicode=True))
                os.chmod(tmp, mode)
                os.replace(tmp, self.path)
            except BaseException:
                with contextlib.suppress(OSError):
                    tmp.unlink()
                raise
            self.config = cfg
            return cfg

    # ---- watch edits (raw dict level) ----
    def raw_watches(self) -> list[dict[str, Any]]:
        return self.raw.setdefault("watches", [])

    def raw_watch(self, watch_id: str) -> dict[str, Any] | None:
        return next((w for w in self.raw_watches() if str(w.get("id")) == watch_id), None)

    @contextlib.contextmanager
    def _transaction(self) -> Iterator[None]:
        """Apply raw edits; restore the previous raw doc if validation/saving fails."""
        with self._lock:
            backup = copy.deepcopy(self.raw)
            try:
                yield
            except BaseException:
                self.raw = backup
                raise

    def upsert_watch(self, watch: dict[str, Any]) -> Config:
        with self._transaction():
            existing = self.raw_watch(str(watch["id"]))
            if existing is not None:
                existing.clear()
                existing.update(watch)
            else:
                self.raw_watches().append(watch)
            return self.save()

    def set_enabled(self, watch_id: str, enabled: bool) -> Config:
        with self._transaction():
            w = self.raw_watch(watch_id)
            if w is None:
                raise ConfigError(f"no watch {watch_id}")
            w["enabled"] = enabled
            return self.save()

    def delete_watch(self, watch_id: str) -> Config:
        with self._transaction():
            self.raw["watches"] = [w for w in self.raw_watches() if str(w.get("id")) != watch_id]
            return self.save()

    def set_value(self, dotted: str, value: Any) -> Config:
        """Set a non-secret scalar like 'home.zip' or 'defaults.quiet_hours'."""
        return self.set_values({dotted: value})

    def set_values(self, values: dict[str, Any]) -> Config:
        """Set several dotted scalars and save once. Nothing is written if validation fails."""
        with self._transaction():
            for dotted, value in values.items():
                node = self.raw
                parts = dotted.split(".")
                for p in parts[:-1]:
                    node = node.setdefault(p, {})
                node[parts[-1]] = value
            return self.save()
