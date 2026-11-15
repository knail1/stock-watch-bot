"""Dedupe state machine tests (spec section 11)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from stockwatch.config import WatchCfg
from stockwatch.db import Database
from stockwatch.engine.state import Decision, StateEngine, qualifying_channels
from stockwatch.models import ChannelAvail, ErrorKind, Home, Severity, StockResult, Target

HOME = Home(zip="75034", radius_miles=25)
T0 = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)


class Clock:
    def __init__(self) -> None:
        self.n = 0

    def next(self) -> datetime:
        self.n += 1
        return T0 + timedelta(minutes=self.n)


@pytest.fixture
def db():
    d = Database(":memory:")
    d.sync_watch("sw2", "Switch 2", True, {})
    yield d
    d.close()


@pytest.fixture
def target(db):
    t = Target(retailer="bestbuy", identifiers={"sku": "123"}, watch_id="sw2")
    db.ensure_target(t)
    return t


@pytest.fixture
def watch(target):
    return WatchCfg(id="sw2", name="Switch 2", targets=[target])


clock = Clock()


def store(sid="1", name="Best Buy Frisco", dist=6.2, channel="store_pickup", available=True):
    return ChannelAvail(channel=channel, available=available, store_id=sid, store_name=name,
                        distance_miles=dist)


def ship(available=True):
    return ChannelAvail(channel="ship", available=available)


def instock(*channels, price=449.99):
    return StockResult(retailer="bestbuy", in_stock=True, channels=list(channels), price=price,
                       raw_status="IN_STOCK", checked_at=clock.next())


def oos(price=449.99):
    return StockResult(retailer="bestbuy", in_stock=False, channels=[store(available=False)],
                       price=price, raw_status="OUT_OF_STOCK", checked_at=clock.next())


def fail(kind=ErrorKind.TIMEOUT):
    r = StockResult.failure("bestbuy", kind, "detail")
    r.checked_at = clock.next()
    return r


class Runner:
    def __init__(self, engine: StateEngine, target: Target, watch: WatchCfg, max_price=None):
        self.engine, self.target, self.watch, self.max_price = engine, target, watch, max_price

    def __call__(self, result: StockResult) -> list[Decision]:
        return self.engine.process(self.target, result, self.watch, self.max_price, HOME)


@pytest.fixture
def run(db, target, watch):
    return Runner(StateEngine(db), target, watch, max_price=500.0)


def kinds(ds):
    return [(d.kind, d.severity) for d in ds]


# ------------------------------------------------------------------ restock lifecycle

def test_first_ever_check_in_stock_opens_event_and_alerts_critical(run, db, target):
    ds = run(instock(store()))
    assert kinds(ds) == [("restock_alert", Severity.CRITICAL)]
    assert ds[0].is_critical and not ds[0].realert
    assert ds[0].channel.store_name == "Best Buy Frisco"
    ev = db.get_open_event(target.db_id, "restock")
    assert ev and ev["id"] == ds[0].event_id


def test_out_then_in_opens_event(run):
    assert run(oos()) == []
    assert run(oos()) == []
    assert kinds(run(instock(store()))) == [("restock_alert", Severity.CRITICAL)]


def test_exactly_one_critical_per_event_while_in_stock(run):
    alerts = [d for _ in range(10) for d in run(instock(store()))]
    assert len(alerts) == 1 and alerts[0].is_critical


def test_single_oos_does_not_close_event(run, db, target):
    eid = run(instock(store()))[0].event_id
    run(oos())
    assert run(instock(store())) == []  # same event still open, no new alert
    assert db.get_open_event(target.db_id, "restock")["id"] == eid


def test_two_consecutive_oos_close_event_and_next_instock_realerts(run, db, target):
    eid1 = run(instock(store()))[0].event_id
    run(oos())
    run(oos())
    assert db.get_open_event(target.db_id, "restock") is None
    ds = run(instock(store()))
    assert kinds(ds) == [("restock_alert", Severity.CRITICAL)]
    assert ds[0].event_id != eid1


def test_oos_streak_resets_on_instock(run, db, target):
    run(instock(store()))
    run(oos())
    run(instock(store()))
    run(oos())
    assert db.get_open_event(target.db_id, "restock") is not None
    run(oos())
    assert db.get_open_event(target.db_id, "restock") is None


def test_errors_do_not_count_as_out_of_stock(run, db, target):
    run(instock(store()))
    run(oos())
    run(fail())
    run(fail(ErrorKind.BLOCKED))
    assert db.get_open_event(target.db_id, "restock") is not None
    run(oos())
    assert db.get_open_event(target.db_id, "restock") is None


def test_realert_on_new_store_is_not_critical_again(run):
    run(instock(store("1")))
    ds = run(instock(store("1"), store("2", "Best Buy Plano", 10.0)))
    assert kinds(ds) == [("restock_alert", Severity.INFO)]
    assert ds[0].realert and ds[0].channel.store_id == "2"
    assert run(instock(store("1"), store("2", "Best Buy Plano", 10.0))) == []
    # a store that disappears and returns within the event is not "new"
    run(instock(store("1")))
    assert run(instock(store("2", "Best Buy Plano", 10.0))) == []


def test_realert_on_lower_price_only(run):
    run(instock(store(), price=449.99))
    assert run(instock(store(), price=459.99)) == []      # higher: no alert
    ds = run(instock(store(), price=429.99))
    assert len(ds) == 1 and ds[0].realert and "lower price" in ds[0].reason
    assert run(instock(store(), price=439.99)) == []      # above best seen
    assert run(instock(store(), price=429.99)) == []      # equal to best


def test_over_max_price_is_info_then_drop_below_max_is_critical(run):
    ds = run(instock(store(), price=599.99))
    assert kinds(ds) == [("price_info", Severity.INFO)]
    assert run(instock(store(), price=599.99)) == []
    ds = run(instock(store(), price=479.99))
    assert kinds(ds) == [("restock_alert", Severity.CRITICAL)]
    assert run(instock(store(), price=469.99))[0].severity is Severity.INFO  # critical already sent


def test_no_max_price_or_unknown_price_alerts(db, target, watch):
    r = Runner(StateEngine(db), target, watch, max_price=None)
    assert r(instock(store(), price=9999))[0].is_critical
    r2 = Runner(StateEngine(db), Target("target", {"tcin": "9"}, watch_id="sw2"), watch, max_price=100)
    assert r2(instock(store(), price=None))[0].is_critical


# ------------------------------------------------------------------ channel filtering

def test_online_only_uses_watch_severity(db, target, watch):
    watch.online_only_severity = "info"
    r = Runner(StateEngine(db), target, watch, max_price=500)
    ds = r(instock(ship()))
    assert kinds(ds) == [("restock_alert", Severity.INFO)]
    # local store shows up later in the same event -> the one critical alert
    ds = r(instock(ship(), store()))
    assert kinds(ds) == [("restock_alert", Severity.CRITICAL)]
    assert ds[0].channel.channel == "store_pickup"


def test_online_only_default_is_info_when_local_preferred(run):
    # spec 7: default fulfillment lists store_pickup/in_store ahead of ship -> info
    assert kinds(run(instock(ship()))) == [("restock_alert", Severity.INFO)]


def test_online_only_critical_when_ship_preferred(db, target, watch):
    watch.fulfillment = ["ship", "store_pickup"]
    r = Runner(StateEngine(db), target, watch, max_price=500)
    assert r(instock(ship()))[0].is_critical


def test_online_only_explicit_severity_argument_wins(db, target, watch):
    engine = StateEngine(db)
    ds = engine.process(target, instock(ship()), watch, 500, HOME, online_severity="critical")
    assert ds[0].is_critical


def test_stores_outside_radius_ignored(run, db, target):
    assert run(instock(store(dist=40.0))) == []
    assert db.get_open_event(target.db_id, "restock") is None
    # in stock only far away counts as out of stock for an open event
    run(instock(store()))
    run(instock(store(dist=40.0)))
    run(instock(store(dist=41.0)))
    assert db.get_open_event(target.db_id, "restock") is None


def test_channels_not_in_fulfillment_ignored(db, target, watch):
    watch.fulfillment = ["store_pickup"]
    r = Runner(StateEngine(db), target, watch, max_price=500)
    assert r(instock(ship(), store(channel="in_store"))) == []
    assert r(instock(store(channel="store_pickup")))[0].is_critical


def test_store_ids_restrict_stores(db, watch):
    t = Target("bestbuy", {"sku": "5"}, store_ids=["1"], watch_id="sw2")
    r = Runner(StateEngine(db), t, watch, max_price=500)
    assert r(instock(store("2"))) == []
    assert r(instock(store("1")))[0].is_critical


def test_qualifying_channels_order_prefers_nearest_local_then_ship():
    w = WatchCfg(id="w", name="w")
    res = instock(ship(), store("far", dist=20), store("near", dist=3), store("x", available=False, dist=1))
    order = [c.store_id or c.channel for c in qualifying_channels(res, w, HOME)]
    assert order == ["near", "far", "ship"]


def test_in_stock_without_channel_detail_alerts(run):
    r = StockResult(retailer="bestbuy", in_stock=True, channels=[], price=449.99, checked_at=clock.next())
    ds = run(r)
    assert ds[0].is_critical and ds[0].channel is None


# ------------------------------------------------------------------ blocked / failures

def test_blocked_one_warning_per_episode(run, db, target):
    ds = [d for _ in range(8) for d in run(fail(ErrorKind.BLOCKED))]
    assert kinds(ds) == [("blocked_notice", Severity.WARNING)]
    assert db.get_open_event(target.db_id, "blocked")["id"] == ds[0].event_id
    # no failure_warning on top of the blocked notice even past 5 failures
    rec = run(oos())
    assert kinds(rec) == [("recovered", Severity.INFO)]
    assert db.get_open_event(target.db_id, "blocked") is None
    # new episode -> new single warning
    assert kinds(run(fail(ErrorKind.BLOCKED)) + run(fail(ErrorKind.BLOCKED))) == [
        ("blocked_notice", Severity.WARNING)]


def test_five_consecutive_failures_one_warning(run, db, target):
    out = []
    for i in range(1, 12):
        ds = run(fail())
        out.append(ds)
    assert [i for i, ds in enumerate(out, 1) if ds] == [5]
    assert kinds(out[4]) == [("failure_warning", Severity.WARNING)]
    assert db.get_open_event(target.db_id, "error") is not None
    # restock alert first: it must never wait behind the INFO "recovered" send
    assert kinds(run(instock(store()))) == [("restock_alert", Severity.CRITICAL), ("recovered", Severity.INFO)]


def test_failure_count_resets_on_success(run):
    for _ in range(4):
        assert run(fail()) == []
    run(oos())
    for _ in range(4):
        assert run(fail()) == []
    assert kinds(run(fail())) == [("failure_warning", Severity.WARNING)]


def test_parse_failed_is_immediate_broken_notice_once(run):
    ds = run(fail(ErrorKind.PARSE_FAILED))
    assert kinds(ds) == [("broken_notice", Severity.WARNING)]
    assert all(run(fail(ErrorKind.PARSE_FAILED)) == [] for _ in range(6))
    assert kinds(run(oos())) == [("recovered", Severity.INFO)]


def test_mixed_failures_count_together(run):
    seq = [ErrorKind.TIMEOUT, ErrorKind.HTTP_ERROR, ErrorKind.UNKNOWN, ErrorKind.QUEUE]
    assert all(run(fail(k)) == [] for k in seq)
    assert kinds(run(fail(ErrorKind.TIMEOUT))) == [("failure_warning", Severity.WARNING)]


def test_custom_threshold(db, target, watch):
    r = Runner(StateEngine(db, failure_threshold=2), target, watch)
    assert r(fail()) == []
    assert r(fail())[0].kind == "failure_warning"


def test_checks_are_recorded(run, db, target):
    run(instock(store()))
    run(fail())
    rows = db.recent_checks(target.db_id)
    assert len(rows) == 2 and rows[0]["error"] == ErrorKind.TIMEOUT


def test_record_checks_can_be_disabled(db, target, watch):
    Runner(StateEngine(db, record_checks=False), target, watch)(instock(store()))
    assert db.recent_checks(target.db_id) == []


def test_unsaved_target_gets_db_id(db, watch):
    t = Target("walmart", {"item_id": "77"}, watch_id="sw2")
    Runner(StateEngine(db), t, watch)(oos())
    assert t.db_id is not None


# ------------------------------------------------------------------ restart survival

def test_open_restock_survives_restart(db, target, watch):
    Runner(StateEngine(db), target, watch, 500)(instock(store("1")))
    r = Runner(StateEngine(db), target, watch, 500)  # fresh engine == service restart
    assert r(instock(store("1"))) == []
    ds = r(instock(store("1"), store("2", "Plano", 9)))
    assert ds and ds[0].severity is Severity.INFO  # critical already sent pre-restart
    r(oos())
    r3 = Runner(StateEngine(db), target, watch, 500)
    r3(oos())  # oos streak persisted across restart -> closes
    assert db.get_open_event(target.db_id, "restock") is None


def test_blocked_episode_survives_restart(db, target, watch):
    Runner(StateEngine(db), target, watch)(fail(ErrorKind.BLOCKED))
    r = Runner(StateEngine(db), target, watch)
    assert r(fail(ErrorKind.BLOCKED)) == []
    assert kinds(r(oos())) == [("recovered", Severity.INFO)]


def test_failure_streak_survives_restart(db, target, watch):
    r = Runner(StateEngine(db), target, watch)
    for _ in range(3):
        r(fail())
    r2 = Runner(StateEngine(db), target, watch)
    assert r2(fail()) == []
    assert kinds(r2(fail())) == [("failure_warning", Severity.WARNING)]
    r3 = Runner(StateEngine(db), target, watch)
    assert r3(fail()) == []  # already warned (error event open)
