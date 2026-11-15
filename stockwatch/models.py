"""Core data types shared by every component (spec sections 6, 7, 11).

These are the cross-module contracts. Change them carefully.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal

Channel = Literal["store_pickup", "in_store", "ship"]
CHANNELS: tuple[Channel, ...] = ("store_pickup", "in_store", "ship")

RETAILERS: tuple[str, ...] = ("bestbuy", "target", "walmart", "gamestop", "nintendo")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ErrorKind:
    """Canonical values for StockResult.error."""

    BLOCKED = "blocked"          # 403 / 429 / challenge / CAPTCHA page
    TIMEOUT = "timeout"
    PARSE_FAILED = "parse_failed"  # page/JSON shape changed -> adapter broken, must fail loud
    HTTP_ERROR = "http_error"
    NOT_FOUND = "not_found"
    CONFIG = "config"            # missing identifier / api key
    QUEUE = "queue"              # Nintendo queue / invite-only detected
    SIGNED_OUT = "signed_out"
    UNKNOWN = "unknown"


@dataclass
class Home:
    zip: str
    radius_miles: float = 25.0


@dataclass
class Target:
    """One retailer target inside a watch.

    identifiers holds retailer-specific keys: sku (bestbuy), tcin (target),
    item_id (walmart), url (gamestop/nintendo; optional on others).
    """

    retailer: str
    identifiers: dict[str, str] = field(default_factory=dict)
    store_ids: list[str] | None = None
    # Filled in by the service from the owning Watch so adapters/cart have context.
    watch_id: str = ""
    db_id: int | None = None  # targets.id row

    @property
    def key(self) -> str:
        """Stable identity for a target within the running service."""
        ident = ",".join(f"{k}={v}" for k, v in sorted(self.identifiers.items()))
        return f"{self.watch_id}:{self.retailer}:{ident}"

    def get(self, name: str) -> str | None:
        v = self.identifiers.get(name)
        return str(v) if v not in (None, "") else None


@dataclass
class ChannelAvail:
    channel: Channel
    available: bool
    store_id: str | None = None
    store_name: str | None = None
    distance_miles: float | None = None
    low_stock: bool | None = None
    quantity: int | None = None


@dataclass
class StockResult:
    retailer: str
    in_stock: bool
    channels: list[ChannelAvail] = field(default_factory=list)
    price: float | None = None
    checked_at: datetime = field(default_factory=utcnow)
    raw_status: str = "UNKNOWN"   # e.g. "IN_STOCK", "LIMITED", "OUT_OF_STOCK", "QUEUE"
    error: str | None = None      # ErrorKind value or None
    error_detail: str | None = None
    duration_ms: int | None = None
    product_name: str | None = None

    @classmethod
    def failure(cls, retailer: str, error: str, detail: str | None = None,
                raw_status: str = "ERROR") -> "StockResult":
        return cls(retailer=retailer, in_stock=False, raw_status=raw_status,
                   error=error, error_detail=detail)

    def available_channels(self) -> list[ChannelAvail]:
        return [c for c in self.channels if c.available]

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["checked_at"] = self.checked_at.isoformat()
        return d


@dataclass
class CartResult:
    retailer: str
    success: bool
    message: str = ""
    cart_url: str | None = None
    signed_out: bool = False
    cart_count_before: int | None = None
    cart_count_after: int | None = None


class Severity(str, Enum):
    CRITICAL = "critical"  # Pushover priority 2
    WARNING = "warning"    # priority 1
    INFO = "info"          # priority 0
    LOW = "low"            # priority -1 (used for "paused, check manually")


PUSHOVER_PRIORITY: dict[Severity, int] = {
    Severity.CRITICAL: 2,
    Severity.WARNING: 1,
    Severity.INFO: 0,
    Severity.LOW: -1,
}


@dataclass
class Alert:
    """A message to fan out to notification channels."""

    severity: Severity
    title: str
    body: str
    url: str | None = None          # primary link (cart or product)
    url_title: str | None = None
    extra_url: str | None = None    # second link, Telegram only (store map)
    extra_url_title: str | None = None
    speak: str | None = None        # text for macOS `say`
    event_id: int | None = None
    play_sound: bool | None = None  # None -> notifier decides (critical only)


@dataclass
class AlertOutcome:
    channel: str
    ok: bool
    detail: str = ""
