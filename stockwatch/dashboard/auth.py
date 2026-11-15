"""PIN / token auth, signed session cookies and CSRF tokens for the dashboard.

Sessions are stateless: the cookie is ``<sid>.<issued>.<sig>`` signed with
HMAC-SHA256 under a key derived from a per-process secret *and* the PIN, so
changing the PIN (or restarting without a fixed secret) logs everyone out.
The CSRF token for a session is ``HMAC(key, "csrf:" + sid)``, so it is tied to
the session and needs no server-side storage.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from collections import defaultdict, deque

COOKIE_NAME = "sw_session"
CSRF_FIELD = "csrf"
CSRF_HEADER = "x-csrf-token"
SESSION_MAX_AGE = 30 * 24 * 3600

LOGIN_MAX_FAILURES = 5
LOGIN_WINDOW_SECONDS = 300


def pin_matches(supplied: str | None, pin: str) -> bool:
    """Constant-time PIN comparison; an empty configured PIN never matches."""
    if not pin or supplied is None:
        return False
    return hmac.compare_digest(supplied.encode(), pin.encode())


class SessionSigner:
    def __init__(self, pin: str, secret: bytes | None = None, max_age: int = SESSION_MAX_AGE):
        base = secret or secrets.token_bytes(32)
        self._key = hmac.new(base, b"session:" + pin.encode(), hashlib.sha256).digest()
        self.max_age = max_age

    def _sig(self, msg: str) -> str:
        return hmac.new(self._key, msg.encode(), hashlib.sha256).hexdigest()

    def new_session(self, now: float | None = None) -> tuple[str, str]:
        """Return (sid, cookie_value)."""
        sid = secrets.token_urlsafe(18)
        payload = f"{sid}.{int(now if now is not None else time.time())}"
        return sid, f"{payload}.{self._sig(payload)}"

    def verify(self, cookie: str | None, now: float | None = None) -> str | None:
        """Return the session id if the cookie is authentic and unexpired."""
        if not cookie:
            return None
        parts = cookie.split(".")
        if len(parts) != 3:
            return None
        sid, issued, sig = parts
        if not hmac.compare_digest(sig, self._sig(f"{sid}.{issued}")):
            return None
        try:
            age = (now if now is not None else time.time()) - int(issued)
        except ValueError:
            return None
        if age < 0 or age > self.max_age:
            return None
        return sid

    def csrf_token(self, sid: str) -> str:
        return self._sig("csrf:" + sid)

    def csrf_valid(self, sid: str | None, token: str | None) -> bool:
        if not sid or not token:
            return False
        return hmac.compare_digest(token, self.csrf_token(sid))


class LoginThrottle:
    """Lock a client out after repeated wrong PINs (per remote address)."""

    def __init__(self, max_failures: int = LOGIN_MAX_FAILURES, window: float = LOGIN_WINDOW_SECONDS):
        self.max_failures = max_failures
        self.window = window
        self._fails: dict[str, deque[float]] = defaultdict(deque)

    def _prune(self, key: str, now: float) -> deque[float]:
        q = self._fails[key]
        while q and now - q[0] > self.window:
            q.popleft()
        return q

    def locked(self, key: str, now: float | None = None) -> bool:
        return len(self._prune(key, now if now is not None else time.monotonic())) >= self.max_failures

    def failure(self, key: str, now: float | None = None) -> None:
        now = now if now is not None else time.monotonic()
        self._prune(key, now).append(now)

    def success(self, key: str) -> None:
        self._fails.pop(key, None)
