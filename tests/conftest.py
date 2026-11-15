"""Shared test fixtures."""
from __future__ import annotations

import pytest

from stockwatch.adapters import bestbuy


@pytest.fixture(autouse=True)
def _fresh_bestbuy_limiter(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each test gets its own Best Buy rate limiter whose spacing does not really sleep."""

    async def no_sleep(_: float) -> None:
        return None

    monkeypatch.setattr(bestbuy, "_LIMITER", bestbuy.ApiRateLimiter(sleep=no_sleep))
