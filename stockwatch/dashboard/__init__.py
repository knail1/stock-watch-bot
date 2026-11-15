"""Local dashboard (spec section 10): FastAPI app, PIN auth, localhost + Tailscale binding."""
from .app import (
    CheckView,
    DashboardControl,
    EventView,
    NotifyChannelView,
    SettingsView,
    TargetStatus,
    WatchSpec,
    WatchStatus,
    create_app,
)
from .server import LOOPBACK, BindError, check_bind_safety, resolve_bind_hosts, serve, supervise, tailscale_ipv4

__all__ = [
    "BindError", "CheckView", "DashboardControl", "EventView", "NotifyChannelView", "SettingsView",
    "TargetStatus", "WatchSpec", "WatchStatus", "check_bind_safety", "create_app", "resolve_bind_hosts",
    "LOOPBACK", "serve", "supervise", "tailscale_ipv4",
]
