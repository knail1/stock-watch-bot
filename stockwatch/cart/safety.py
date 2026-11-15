"""Checkout denylist (spec section 8, acceptance criterion 7).

EVERY click the cart helper or an adapter performs in a retailer page MUST go
through `safe_click`. It reads the element's accessible label/text/value/aria
attributes and refuses to click if any of them matches the denylist. There is
no bypass flag by design.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Any

# Matches checkout / place order / buy now / pay (and close variants).
_DENY_PATTERNS = [
    r"check\s*-?\s*out",
    r"place\s*(your\s*)?order",
    r"submit\s*(your\s*)?order",
    r"complete\s*(your\s*)?(order|purchase)",
    r"confirm\s*(your\s*)?(order|purchase)",
    r"\border\s*now\b",
    r"buy\s*-?\s*now",
    r"buy\s*it\s*now",
    r"\bbuy\s+with\b",
    r"1\s*-?\s*click",
    r"one\s*-?\s*click",
    r"express\s+(checkout|pay)",
    r"\bpay\b",
    r"\bpay\s*(now|pal|ment)",
    r"\bpaypal\b",
    r"apple\s*pay",
    r"google\s*pay",
    r"\bg\s*pay\b",
    r"shop\s*pay",
    r"after\s*pay|klarna|affirm|venmo|sezzle|zip\s*pay",
    r"purchase\b",
    r"\bproceed\s+to\b",
]
DENY_RE = re.compile("|".join(f"(?:{p})" for p in _DENY_PATTERNS), re.IGNORECASE)

# Attributes inspected on the element before clicking.
LABEL_ATTRS = ("aria-label", "title", "value", "name", "id", "data-test", "data-testid",
               "data-automation-id", "data-track", "href", "alt", "class", "formaction")


class ForbiddenClickError(RuntimeError):
    """Raised when a click target looks like a checkout/purchase control."""


def _normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "")
    # Drop invisible format characters (zero-width space, soft hyphen) that would split words.
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Cf")
    # treat separators used in ids/classes as spaces: "checkout-btn", "buyNow", "place_order"
    text = re.sub(r"([a-z])([A-Z])", r"\1 \2", text)
    text = re.sub(r"[_\-./\u2010-\u2015\u2212]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def is_forbidden_label(*labels: str | None) -> bool:
    for label in labels:
        if label and DENY_RE.search(_normalize(label)):
            return True
    return False


async def describe_element(locator: Any) -> list[str]:
    """Collect every human/machine label for a Playwright locator's element (and ancestors' role buttons)."""
    labels: list[str] = []
    try:
        labels.append(await locator.inner_text(timeout=2000))
    except Exception:
        pass
    for attr in LABEL_ATTRS:
        try:
            v = await locator.get_attribute(attr, timeout=2000)
        except Exception:
            v = None
        if v:
            labels.append(v)
    try:
        # Text of the closest clickable ancestor (a span inside a "Checkout" button, etc.).
        anc = await locator.evaluate(
            "el => { const a = el.closest('button,a,[role=button],input[type=submit],form');"
            " if (!a) return ''; const f = a.tagName === 'FORM' ? (a.getAttribute('action')||'') : '';"
            " return (a.innerText||'') + ' ' + (a.getAttribute('aria-label')||'') + ' ' +"
            " (a.getAttribute('href')||'') + ' ' + (a.value||'') + ' ' + f; }"
        )
        if anc:
            labels.append(anc)
    except Exception:
        pass
    return labels


async def assert_click_allowed(locator: Any) -> None:
    """Fail closed: refuse when the label matches the denylist OR no label could be read at all."""
    labels = await describe_element(locator)
    if not any((label or "").strip() for label in labels):
        raise ForbiddenClickError("refusing to click: element label could not be read")
    if is_forbidden_label(*labels):
        raise ForbiddenClickError(f"refusing to click checkout/purchase control: {labels!r}")


async def safe_click(locator: Any, *, timeout_ms: int = 10_000) -> None:
    """The only sanctioned way to click inside a retailer page."""
    await assert_click_allowed(locator)
    await locator.click(timeout=timeout_ms)
