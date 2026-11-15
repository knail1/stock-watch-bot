"""Small helpers shared by the notification clients."""
from __future__ import annotations

from typing import Iterable


def redact(text: str, secrets: Iterable[str]) -> str:
    """Replace every non-empty secret in `text` with a placeholder.

    httpx exception messages can include request URLs, and the Telegram bot
    token is part of its API URL, so every error detail passes through here
    before it is logged, stored or returned (spec section 12).
    """
    for s in secrets:
        if s and len(s) >= 4:
            text = text.replace(s, "***")
    return text


def truncate(text: str, limit: int) -> str:
    """Trim to `limit` characters, marking the cut with an ellipsis."""
    return text if len(text) <= limit else text[: max(0, limit - 1)] + "…"
