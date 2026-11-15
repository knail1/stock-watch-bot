"""Best Buy API Terms of Service compliance (docs/policies/bestbuy-compliance.md)."""
from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
import respx

from stockwatch.adapters import bestbuy
from stockwatch.adapters.base import BlockedError
from stockwatch.adapters.bestbuy import ApiRateLimiter, BestBuyAdapter, BudgetExceeded
from stockwatch.config import RetailerCfg
from stockwatch.db import Database
from stockwatch.models import ErrorKind, Home, StockResult, Target, utcnow

KEY = "sekrit-key-123"
HOME = Home(zip="75001", radius_miles=25)
PRODUCT_URL = "https://api.bestbuy.com/v1/products/6614313.json"
STORES_URL = "https://api.bestbuy.com/v1/products/6614313/stores.json"


def tgt() -> Target:
    return Target(retailer="bestbuy", identifiers={"sku": "6614313"}, watch_id="w")


def adapter(http: httpx.AsyncClient) -> BestBuyAdapter:
    return BestBuyAdapter(http=http, settings=RetailerCfg(api_key=KEY))


class FakeClock:
    def __init__(self) -> None:
        self.t = 1000.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.t

    async def sleep(self, s: float) -> None:
        self.slept.append(s)
        self.t += s


# ---------------------------------------------------------------- rate limits

async def test_limiter_spaces_calls_under_5_per_second() -> None:
    clk = FakeClock()
    lim = ApiRateLimiter(per_second=4, clock=clk, sleep=clk.sleep)
    start = clk.t
    for _ in range(9):
        await lim.acquire()
    # 9 calls at 4/s: first is immediate, the other 8 are spaced 0.25s apart.
    assert clk.t - start == pytest.approx(2.0)
    assert all(s == pytest.approx(0.25) for s in clk.slept)


async def test_limiter_never_allows_more_than_5_per_second_even_if_misconfigured() -> None:
    clk = FakeClock()
    lim = ApiRateLimiter(per_second=50, clock=clk, sleep=clk.sleep)
    assert lim.spacing == pytest.approx(0.2)


async def test_limiter_enforces_rolling_daily_budget() -> None:
    clk = FakeClock()
    lim = ApiRateLimiter(daily_budget=3, clock=clk, sleep=clk.sleep)
    for _ in range(3):
        await lim.acquire()
    with pytest.raises(BudgetExceeded) as ei:
        await lim.acquire()
    assert ei.value.kind == ErrorKind.BLOCKED
    clk.t += 86_400  # calls age out of the 24h window
    await lim.acquire()
    assert lim.calls_last_24h() == 1


def test_budget_is_capped_at_best_buy_limit() -> None:
    assert ApiRateLimiter(daily_budget=10**9).daily_budget == bestbuy.DAILY_CALL_LIMIT


@respx.mock
async def test_budget_exhaustion_sends_no_request() -> None:
    product = respx.get(PRODUCT_URL).mock(return_value=httpx.Response(200, json={}))
    bestbuy._LIMITER._calls.append(bestbuy._LIMITER._clock())  # one call already used
    async with httpx.AsyncClient() as http:
        a = BestBuyAdapter(http=http, settings=RetailerCfg(api_key=KEY, extra={"daily_call_budget": 1}))
        with pytest.raises(BlockedError):
            await a.check(tgt(), HOME)
    assert product.call_count == 0


def test_daily_budget_configurable_via_settings() -> None:
    a = BestBuyAdapter(http=None, settings=RetailerCfg(api_key=KEY, extra={"daily_call_budget": 1234}))  # type: ignore[arg-type]
    assert a.limiter.daily_budget == 1234


def test_estimated_daily_calls() -> None:
    assert bestbuy.estimated_daily_calls(60) == 2880
    assert bestbuy.estimated_daily_calls(60, targets=20) == 57_600


# ---------------------------------------------------------------- 403 detail

@respx.mock
async def test_403_reports_best_buy_reason_without_key() -> None:
    respx.get(PRODUCT_URL).mock(return_value=httpx.Response(
        403, json={"error": {"code": 403, "message": f"Over Quota for apiKey={KEY}"}}))
    async with httpx.AsyncClient() as http:
        with pytest.raises(BlockedError) as ei:
            await adapter(http).check(tgt(), HOME)
    assert "Over Quota" in str(ei.value)
    assert KEY not in str(ei.value)


# ---------------------------------------------------------------- unlicensed content

@pytest.mark.parametrize("ptype", ["Game", "Music", "Movie"])
@respx.mock
async def test_games_music_movies_are_refused(ptype: str) -> None:
    respx.get(PRODUCT_URL).mock(return_value=httpx.Response(200, json={
        "sku": 6614313, "name": "Some Title", "type": ptype, "salePrice": 69.99,
        "onlineAvailability": True, "orderable": "Available"}))
    stores = respx.get(STORES_URL).mock(return_value=httpx.Response(200, json={"stores": []}))
    async with httpx.AsyncClient() as http:
        r = await adapter(http).check(tgt(), HOME)
    assert r.error == ErrorKind.CONFIG and not r.in_stock
    assert "do not license" in (r.error_detail or "")
    assert stores.call_count == 0  # no further content fetched


@respx.mock
async def test_hardgood_console_is_allowed() -> None:
    respx.get(PRODUCT_URL).mock(return_value=httpx.Response(200, json={
        "sku": 6614313, "name": "Nintendo Switch 2", "type": "HardGood", "salePrice": 449.99,
        "onlineAvailability": False, "orderable": "SoldOut"}))
    respx.get(STORES_URL).mock(return_value=httpx.Response(200, json={"ispuEligible": True, "stores": []}))
    async with httpx.AsyncClient() as http:
        r = await adapter(http).check(tgt(), HOME)
    assert r.error is None


def test_product_fields_request_type() -> None:
    assert "type" in bestbuy.PRODUCT_FIELDS.split(",")


# ---------------------------------------------------------------- 72h storage limit

def _seed(db: Database, retailer: str, ident: dict[str, str]) -> int:
    t = Target(retailer=retailer, identifiers=ident, watch_id="w")
    db.sync_watch("w", "W", True, {})
    return db.ensure_target(t)


def test_bestbuy_content_purged_after_72h_others_kept() -> None:
    db = Database(":memory:")
    bb = _seed(db, "bestbuy", {"sku": "1"})
    tg = _seed(db, "target", {"tcin": "2"})
    old = utcnow() - timedelta(hours=73)
    recent = utcnow() - timedelta(hours=10)
    for tid in (bb, tg):
        db.record_check(tid, StockResult(retailer="x", in_stock=True, price=449.99, checked_at=old))
        db.record_check(tid, StockResult(retailer="x", in_stock=True, price=449.99, checked_at=recent))
    ev_old = db.open_event(bb, "restock", {"best_price": 449.99, "first_price": 449.99,
                                           "seen_locations": ["store:123"], "primary": "store:123",
                                           "critical_sent": True,
                                           "cart": {"success": True, "message": "pickup at Best Buy Frisco"}},
                           started_at=old)
    ev_new = db.open_event(bb, "restock", {"best_price": 1.0}, started_at=recent)
    ev_tg = db.open_event(tg, "restock", {"best_price": 449.99}, started_at=old)

    res = db.prune_restricted_content()
    assert res == {"checks": 1, "events": 1}
    assert len(db.recent_checks(bb)) == 1 and len(db.recent_checks(tg)) == 2
    details = {e["id"]: e["details"] for e in db.recent_events()}
    assert details[ev_old] == {"critical_sent": True, "cart": {"success": True, "message": ""},
                               "content_scrubbed": True}
    assert details[ev_new] == {"best_price": 1.0}
    assert details[ev_tg] == {"best_price": 449.99}
    assert db.prune_restricted_content() == {"checks": 0, "events": 0}  # idempotent


def test_retention_limit_is_under_72_hours() -> None:
    from stockwatch import db as dbmod
    from stockwatch.health import CONTENT_PRUNE_INTERVAL_SECONDS

    # purge age + purge interval must stay within the ToS 72h cap
    assert dbmod.RESTRICTED_CONTENT_HOURS["bestbuy"] * 3600 + CONTENT_PRUNE_INTERVAL_SECONDS <= 72 * 3600


def test_scrubbed_open_event_rebaselines_without_realert() -> None:
    from stockwatch.config import WatchCfg
    from stockwatch.engine.state import StateEngine
    from stockwatch.models import ChannelAvail

    db = Database(":memory:")
    eng = StateEngine(db)
    w = WatchCfg(id="w", name="Switch", fulfillment=["store_pickup"])
    t = tgt()
    db.sync_watch("w", "W", True, {})
    db.ensure_target(t)

    def instock() -> StockResult:
        return StockResult(retailer="bestbuy", in_stock=True, price=449.99, channels=[
            ChannelAvail("store_pickup", True, store_id="123", store_name="Frisco", distance_miles=5)])

    first = eng.process(t, instock(), w, None, HOME)
    assert [d.kind for d in first] == ["restock_alert"]
    # 73h later the content is scrubbed while the event is still open
    ev = db.get_open_event(t.db_id, "restock")
    db.update_event_details(ev["id"], {**{k: v for k, v in ev["details"].items()
                                          if k not in ("best_price", "first_price", "seen_locations", "primary")},
                                       "content_scrubbed": True})
    again = eng.process(t, instock(), w, None, HOME)
    assert not [d for d in again if d.kind == "restock_alert"]
