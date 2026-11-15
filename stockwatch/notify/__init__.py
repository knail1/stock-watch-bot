"""Notification channels: Pushover (primary), Telegram (backup), Mac sound (local)."""
from .macsound import MacSound, resolve_sound_file
from .notifier import (
    RETAILER_DISPLAY,
    Notifier,
    format_cart_followup,
    format_restock_alert,
    in_quiet_hours,
    pick_channel,
    retailer_display,
)
from .pushover import PushoverClient
from .telegram import TelegramClient, TelegramError, format_alert_html

__all__ = [
    "MacSound", "Notifier", "PushoverClient", "RETAILER_DISPLAY", "TelegramClient",
    "TelegramError", "format_alert_html", "format_cart_followup", "format_restock_alert",
    "in_quiet_hours", "pick_channel", "resolve_sound_file", "retailer_display",
]
