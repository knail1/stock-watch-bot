"""Acceptance criterion 7: the cart helper cannot click any checkout or purchase control.

Three layers of proof:

1. Table-driven label tests over ``is_forbidden_label`` (visible text, aria labels,
   ids, classes, hrefs, data attributes).
2. Fake Playwright locators: ``safe_click`` reads the element and its closest
   clickable ancestor and never calls ``.click`` on a forbidden element.
3. A static scan of every module under ``stockwatch/`` (except ``cart/safety.py``)
   failing if any code clicks, taps, dispatches a click, submits a form or presses
   Enter other than through ``safe_click``.
"""
from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pytest

from stockwatch.cart import safety
from stockwatch.cart.safety import ForbiddenClickError, assert_click_allowed, is_forbidden_label, safe_click

# --------------------------------------------------------------------------- 1. labels

FORBIDDEN_LABELS = [
    # visible text / aria labels
    "Checkout", "checkout", "CHECKOUT", "Check out", "Check-out", "Secure checkout",
    "Proceed to checkout", "Proceed to Checkout (2 items)", "Continue to checkout",
    "Express checkout", "Guest checkout",
    "Place order", "Place Order", "Place your order", "PLACE YOUR ORDER",
    "Submit order", "Submit your order", "Complete order", "Complete purchase",
    "Complete your purchase", "Confirm order", "Confirm purchase",
    "Buy now", "Buy Now", "BUY NOW", "Buy-Now", "Buy Now with 1-Click", "Buy it now",
    "1-Click", "One-click ordering",
    "Pay", "pay", "Pay now", "Pay Now", "Pay $449.99", "Pay in 4 interest-free payments",
    "PayPal", "Paypal", "Checkout with PayPal", "PayPal Checkout",
    "Apple Pay", "Buy with Apple Pay", "ApplePay", "Google Pay", "GPay with Google Pay",
    "Shop Pay", "Express Pay", "Continue to payment", "Payment", "Review payment",
    "Purchase", "Complete purchase now",
    "Proceed to payment", "Proceed to shipping",
    # fullwidth / compatibility characters are NFKC-normalised
    "Ｃｈｅｃｋｏｕｔ",  # fullwidth "Checkout"
    "Pay now",  # no-break space
    # ids / classes / data attributes
    "checkout-btn", "btn-checkout", "checkoutButton", "CheckoutButton", "checkout_button",
    "fast-track-checkout", "c-button checkout-buttons__checkout", "proceedToCheckout",
    "ProceedToCheckout", "place-order-button", "placeOrderBtn", "PlaceOrder", "place_order",
    "buyNowButton", "buy-now", "buy_now", "BuyNow", "PAYNOW", "submit-order", "submitOrderButton",
    "paypal-button-container", "apple-pay-button", "applePayButton",
    "pay-now-btn", "payment-continue",
    # hrefs / form actions
    "/checkout", "/checkout/r/fast-track", "https://www.bestbuy.com/checkout/r/fast-track",
    "https://www.target.com/checkout", "https://www.walmart.com/checkout/review-order",
    "https://www.gamestop.com/checkout/", "https://www.nintendo.com/us/checkout/",
    "/cart/checkout?step=payment", "https://www.paypal.com/checkoutnow?token=x",
]

ALLOWED_LABELS = [
    "Add to cart", "Add to Cart", "ADD TO CART", "Add to bag", "add-to-cart-button",
    "addToCartButton", "Pick up here", "Pickup here", "Ship it", "Ship It", "shipItButton",
    "Select store", "Choose store", "Change store", "Choose options", "Select options",
    "Shipping", "Pickup", "Pick up", "Pickup today", "Same Day Delivery", "Deliver it",
    "Check stores", "View cart", "Go to cart", "/cart", "https://www.bestbuy.com/cart",
    "Continue shopping", "Close", "No, thanks", "Save for later", "Quantity", "Display options",
    "Add protection plan", "Add to registry", "Store details",
]


@pytest.mark.parametrize("label", FORBIDDEN_LABELS)
def test_forbidden_labels_are_refused(label: str) -> None:
    assert is_forbidden_label(label), f"denylist missed {label!r}"


@pytest.mark.parametrize("label", ALLOWED_LABELS)
def test_benign_labels_are_allowed(label: str) -> None:
    assert not is_forbidden_label(label), f"denylist false positive on {label!r}"


def test_any_forbidden_label_among_many_refuses() -> None:
    assert is_forbidden_label("Add to cart", None, "", "btn-primary", "/checkout")
    assert not is_forbidden_label(None, "", "Add to cart")


def test_safe_click_has_no_bypass_parameter() -> None:
    import inspect

    params = set(inspect.signature(safe_click).parameters)
    assert params == {"locator", "timeout_ms"}


# Former denylist gaps, fixed in safety.py; kept as regression tests.
KNOWN_GAPS = [
    "Check​out",          # zero-width space (Unicode Cf) defeats \s
    "Check‑out",          # non-breaking hyphen is not in the separator class
    "placeorder", "submitorder", "completepurchase", "buyitnow",  # concatenated lowercase ids
    "Order now", "Buy with Shop", "Afterpay", "Klarna", "Affirm", "Venmo", "gpay-button",
]


@pytest.mark.parametrize("label", KNOWN_GAPS)
def test_known_denylist_gaps(label: str) -> None:
    assert is_forbidden_label(label)


# --------------------------------------------------------------------------- 2. fake locators


class FakeLocator:
    """Minimal stand-in for a Playwright Locator used by safety.describe_element."""

    def __init__(self, text: str = "", attrs: dict[str, str] | None = None,
                 ancestor: str = "", fail_reads: bool = False) -> None:
        self.text = text
        self.attrs = attrs or {}
        self.ancestor = ancestor
        self.fail_reads = fail_reads
        self.clicks: list[dict[str, Any]] = []
        self.taps = 0
        self.presses: list[str] = []

    async def inner_text(self, timeout: int | None = None) -> str:
        if self.fail_reads:
            raise RuntimeError("detached")
        return self.text

    async def get_attribute(self, name: str, timeout: int | None = None) -> str | None:
        if self.fail_reads:
            raise RuntimeError("detached")
        return self.attrs.get(name)

    async def evaluate(self, script: str) -> str:
        if self.fail_reads:
            raise RuntimeError("detached")
        return self.ancestor

    async def click(self, **kwargs: Any) -> None:
        self.clicks.append(kwargs)

    async def tap(self, **kwargs: Any) -> None:  # pragma: no cover - must never be used
        self.taps += 1

    async def press(self, key: str, **kwargs: Any) -> None:  # pragma: no cover - must never be used
        self.presses.append(key)

    def touched(self) -> bool:
        return bool(self.clicks or self.taps or self.presses)


FORBIDDEN_ELEMENTS = [
    pytest.param(FakeLocator(text="Checkout"), id="text-checkout"),
    pytest.param(FakeLocator(text="Proceed to checkout"), id="text-proceed"),
    pytest.param(FakeLocator(text="Place your order"), id="text-place-your-order"),
    pytest.param(FakeLocator(text="Buy Now with 1-Click"), id="text-1click"),
    pytest.param(FakeLocator(text="Pay"), id="text-pay"),
    pytest.param(FakeLocator(text="", attrs={"aria-label": "Pay with PayPal"}), id="aria-paypal"),
    pytest.param(FakeLocator(text="", attrs={"aria-label": "Buy with Apple Pay"}), id="aria-applepay"),
    pytest.param(FakeLocator(text="Continue", attrs={"id": "checkout-btn"}), id="id-checkout"),
    pytest.param(FakeLocator(text="Continue", attrs={"class": "btn btn-lg checkoutButton"}), id="class-checkout"),
    pytest.param(FakeLocator(text="Go", attrs={"data-testid": "place-order-button"}), id="testid-place-order"),
    pytest.param(FakeLocator(text="Go", attrs={"data-test": "buyNowButton"}), id="datatest-buynow"),
    pytest.param(FakeLocator(text="Go", attrs={"data-automation-id": "pay-now-btn"}), id="automation-paynow"),
    pytest.param(FakeLocator(text="Next", attrs={"href": "/checkout/r/fast-track"}), id="href-checkout"),
    pytest.param(FakeLocator(text="", attrs={"value": "Complete purchase"}), id="input-value"),
    pytest.param(FakeLocator(text="", attrs={"title": "Checkout"}), id="title"),
    pytest.param(FakeLocator(text="", attrs={"alt": "PayPal"}), id="img-alt"),
    pytest.param(FakeLocator(text="", attrs={"name": "placeOrder"}), id="name-attr"),
    # A <span> nested inside a checkout button: its own text is innocuous, the
    # closest clickable ancestor's is not.
    pytest.param(FakeLocator(text="2 items", ancestor="Checkout 2 items"), id="span-in-checkout-button"),
    pytest.param(FakeLocator(text="", ancestor="  /checkout "), id="svg-in-checkout-link"),
    pytest.param(FakeLocator(text="Continue", ancestor="Continue  /checkout/payment"), id="span-in-form-action"),
    pytest.param(FakeLocator(text="Now", ancestor="Buy Now"), id="span-in-buynow"),
]


@pytest.mark.parametrize("loc", FORBIDDEN_ELEMENTS)
async def test_safe_click_never_clicks_forbidden_element(loc: FakeLocator) -> None:
    with pytest.raises(ForbiddenClickError):
        await safe_click(loc)
    assert not loc.touched()
    assert loc.clicks == []


@pytest.mark.parametrize("loc", FORBIDDEN_ELEMENTS)
async def test_assert_click_allowed_refuses(loc: FakeLocator) -> None:
    with pytest.raises(ForbiddenClickError):
        await assert_click_allowed(loc)


ALLOWED_ELEMENTS = [
    pytest.param(FakeLocator(text="Add to Cart", attrs={"class": "c-button add-to-cart-button"},
                             ancestor="Add to Cart"), id="bestbuy-atc"),
    pytest.param(FakeLocator(text="Ship it", attrs={"data-test": "shipItButton"}, ancestor="Ship it"), id="target-ship"),
    pytest.param(FakeLocator(text="Pick up here", attrs={"data-test": "orderPickupButton"}), id="target-pickup"),
    pytest.param(FakeLocator(text="Select store", attrs={"href": "/store-locator"}), id="select-store"),
    pytest.param(FakeLocator(text="Choose options"), id="choose-options"),
    pytest.param(FakeLocator(text="Pickup", ancestor="Pickup Ready today"), id="pickup-tile"),
    pytest.param(FakeLocator(text="Shipping", ancestor="Shipping Arrives Fri"), id="shipping-tile"),
]


@pytest.mark.parametrize("loc", ALLOWED_ELEMENTS)
async def test_safe_click_clicks_benign_element(loc: FakeLocator) -> None:
    await safe_click(loc, timeout_ms=1234)
    assert loc.clicks == [{"timeout": 1234}]


async def test_forbidden_attribute_wins_over_benign_text() -> None:
    loc = FakeLocator(text="Add to cart", attrs={"href": "https://www.walmart.com/checkout"})
    with pytest.raises(ForbiddenClickError):
        await safe_click(loc)
    assert loc.clicks == []


async def test_label_read_errors_on_some_attributes_still_refuse() -> None:
    class Flaky(FakeLocator):
        async def inner_text(self, timeout: int | None = None) -> str:
            raise RuntimeError("timeout")

    loc = Flaky(attrs={"id": "checkout"})
    with pytest.raises(ForbiddenClickError):
        await safe_click(loc)
    assert loc.clicks == []


async def test_unreadable_element_is_not_clicked() -> None:
    loc = FakeLocator(fail_reads=True)
    with pytest.raises(ForbiddenClickError):
        await safe_click(loc)
    assert loc.clicks == []


async def test_adapter_style_flow_stops_before_checkout() -> None:
    """Simulate an add-to-cart flow whose 'next' button is Checkout: only the benign click lands."""
    atc = FakeLocator(text="Add to cart")
    checkout = FakeLocator(text="Checkout", attrs={"href": "/checkout"})
    await safe_click(atc)
    with pytest.raises(ForbiddenClickError):
        await safe_click(checkout)
    assert len(atc.clicks) == 1 and checkout.clicks == []


# --------------------------------------------------------------------------- 3. static scan

PKG_ROOT = Path(__file__).resolve().parents[1] / "stockwatch"
SAFETY_FILE = PKG_ROOT / "cart" / "safety.py"

# Methods that perform a pointer/touch activation on a Playwright Page/Locator/Mouse/Touchscreen.
_ACTIVATION_METHODS = {"click", "dblclick", "tap"}
_ENTER_KEYS = ("enter", "numpadenter", "\n", "\r")
# JavaScript that would activate an element when run via page.evaluate & friends.
_JS_ACTIVATION = (".click(", ".submit(", "requestsubmit(", "dispatchevent(new mouseevent",
                  "dispatchevent(new pointerevent", "new mouseevent('click'", 'new mouseevent("click"')


def _const_str(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def find_unsafe_clicks(source: str, filename: str = "<src>") -> list[str]:
    """Return 'file:line: reason' for every activation that bypasses safe_click."""
    tree = ast.parse(source, filename=filename)
    problems: list[str] = []

    def flag(node: ast.AST, why: str) -> None:
        problems.append(f"{filename}:{getattr(node, 'lineno', '?')}: {why}")

    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            attr = node.func.attr
            args = [s for s in (_const_str(a) for a in node.args) if s is not None]
            args += [s for s in (_const_str(k.value) for k in node.keywords) if s is not None]
            if attr in _ACTIVATION_METHODS:
                flag(node, f".{attr}() called directly; use safe_click")
            elif attr == "dispatch_event" and any("click" in a.lower() or "pointer" in a.lower()
                                                  or "mouse" in a.lower() or "touch" in a.lower()
                                                  or "submit" in a.lower() for a in args):
                flag(node, "dispatch_event of an activation event; use safe_click")
            elif attr in {"press", "down", "type", "press_sequentially", "insert_text"} and any(
                any(k in a.lower() for k in _ENTER_KEYS) for a in args
            ):
                flag(node, f".{attr}() sends Enter; keyboard activation bypasses safe_click")
            elif attr == "submit":
                flag(node, ".submit() called; form submission bypasses safe_click")
            elif attr in {"evaluate", "evaluate_handle", "eval_on_selector", "eval_on_selector_all",
                          "add_init_script", "add_script_tag"} and any(
                any(js in a.lower().replace(" ", "") for js in _JS_ACTIVATION) for a in args
            ):
                flag(node, f"JavaScript activation inside .{attr}(); use safe_click")
    return problems


def _package_files() -> list[Path]:
    return sorted(p for p in PKG_ROOT.rglob("*.py") if p.resolve() != SAFETY_FILE.resolve())


def test_static_scan_covers_package() -> None:
    files = _package_files()
    assert files, "no source files found"
    assert SAFETY_FILE.exists()
    assert all(p != SAFETY_FILE for p in files)


def test_no_module_clicks_outside_safe_click() -> None:
    problems: list[str] = []
    for path in _package_files():
        problems += find_unsafe_clicks(path.read_text(encoding="utf-8"), str(path.relative_to(PKG_ROOT.parent)))
    assert not problems, "direct clicks bypass the checkout denylist:\n" + "\n".join(problems)


def test_safe_click_is_the_only_click_in_safety_module() -> None:
    """safety.py is excluded above; make sure its only click is the guarded one inside safe_click."""
    tree = ast.parse(SAFETY_FILE.read_text(encoding="utf-8"))
    clicks = [(fn.name, n) for fn in ast.walk(tree) if isinstance(fn, ast.AsyncFunctionDef)
              for n in ast.walk(fn) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
              and n.func.attr in _ACTIVATION_METHODS]
    all_clicks = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                  and n.func.attr in _ACTIVATION_METHODS]
    assert len(all_clicks) == 1 and [name for name, _ in clicks] == ["safe_click"]
    body = next(fn for fn in ast.walk(tree) if isinstance(fn, ast.AsyncFunctionDef) and fn.name == "safe_click").body
    first_await = next(n for stmt in body for n in ast.walk(stmt) if isinstance(n, ast.Await))
    call = first_await.value
    assert isinstance(call, ast.Call) and isinstance(call.func, ast.Name) and call.func.id == "assert_click_allowed"


@pytest.mark.parametrize("snippet", [
    "await page.click('#atc')",
    "await locator.click(timeout=1000)",
    "await page.locator('button').first.click()",
    "await btn.dblclick()",
    "await page.tap('#buy')",
    "await page.touchscreen.tap(10, 10)",
    "await page.mouse.click(100, 200)",
    "await el.dispatch_event('click')",
    'await el.dispatch_event("click")',
    "await el.dispatch_event(type='pointerdown')",
    "await page.keyboard.press('Enter')",
    'await locator.press("Enter")',
    "await page.press('#q', 'NumpadEnter')",
    "await page.keyboard.down('Enter')",
    "await page.keyboard.type('hello\\n')",
    "await page.evaluate(\"document.querySelector('#checkout').click()\")",
    "await page.evaluate('el => el.form.requestSubmit()')",
    "await form.submit()",
])
def test_scanner_flags_unsafe_patterns(snippet: str) -> None:
    src = f"async def f(page, locator, btn, el, form):\n    {snippet}\n"
    assert find_unsafe_clicks(src), f"scanner missed: {snippet}"


@pytest.mark.parametrize("snippet", [
    "await safe_click(locator)",
    "await safety.safe_click(button, timeout_ms=5000)",
    "await page.keyboard.press('Escape')",
    "await locator.fill('75034')",
    "await adapter.check(target, home)",
    "await page.evaluate('() => document.title')",
    "await el.dispatch_event('focus')",
])
def test_scanner_allows_safe_patterns(snippet: str) -> None:
    src = f"async def f(page, locator, button, el, adapter, target, home, safety, safe_click):\n    {snippet}\n"
    assert find_unsafe_clicks(src) == []


def test_denylist_regex_is_module_level_and_case_insensitive() -> None:
    import re

    assert safety.DENY_RE.flags & re.IGNORECASE
