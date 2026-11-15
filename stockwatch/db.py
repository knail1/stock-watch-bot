"""SQLite storage (spec section 11).

Synchronous sqlite3 with a single connection guarded by a lock; calls are tiny
and infrequent (one row per check), so running them on the event loop thread is
fine. Use `Database(":memory:")` in tests.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from .models import StockResult, Target, utcnow

SCHEMA = """
CREATE TABLE IF NOT EXISTS watches (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    config_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS targets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    watch_id TEXT NOT NULL REFERENCES watches(id) ON DELETE CASCADE,
    retailer TEXT NOT NULL,
    identifiers_json TEXT NOT NULL,
    store_ids_json TEXT,
    UNIQUE(watch_id, retailer, identifiers_json)
);
CREATE TABLE IF NOT EXISTS checks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    target_id INTEGER NOT NULL REFERENCES targets(id) ON DELETE CASCADE,
    checked_at TEXT NOT NULL,
    in_stock INTEGER NOT NULL,
    price REAL,
    channels_json TEXT,
    error TEXT,
    duration_ms INTEGER,
    raw_status TEXT
);
CREATE INDEX IF NOT EXISTS ix_checks_target_time ON checks(target_id, checked_at);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    target_id INTEGER NOT NULL REFERENCES targets(id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK (kind IN ('restock','blocked','error')),
    started_at TEXT NOT NULL,
    ended_at TEXT,
    details_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS ix_events_target_open ON events(target_id, kind, ended_at);
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id INTEGER REFERENCES events(id) ON DELETE SET NULL,
    channel TEXT NOT NULL,
    sent_at TEXT NOT NULL,
    status TEXT NOT NULL,
    ack_at TEXT,
    receipt TEXT
);
"""

CHECK_RETENTION_DAYS = 14

#: Retailers whose terms cap how long their content may be stored. Best Buy API ToS:
#: "store or cache any Content except on a temporary basis not to exceed seventy-two (72) hours".
#: We purge at 71h and run the purge hourly, so nothing outlives 72h.
RESTRICTED_CONTENT_HOURS: dict[str, int] = {"bestbuy": 71}
#: Event detail keys that hold retailer content (prices, store ids/names).
CONTENT_DETAIL_KEYS = ("best_price", "first_price", "seen_locations", "primary")


def _iso(dt: datetime | None) -> str | None:
    return dt.astimezone(timezone.utc).isoformat() if dt else None


class Database:
    def __init__(self, path: str | Path):
        self.path = str(path)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        if self.path != ":memory:":
            self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        with self._lock:
            self.conn.close()

    def _exec(self, sql: str, params: tuple | dict = ()) -> sqlite3.Cursor:
        with self._lock:
            return self.conn.execute(sql, params)

    def _all(self, sql: str, params: tuple | dict = ()) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def _one(self, sql: str, params: tuple | dict = ()) -> dict[str, Any] | None:
        with self._lock:
            r = self.conn.execute(sql, params).fetchone()
            return dict(r) if r else None

    # ------------------------------------------------------------ watches/targets

    def sync_watch(self, watch_id: str, name: str, enabled: bool, config: dict[str, Any]) -> None:
        now = _iso(utcnow())
        self._exec(
            """INSERT INTO watches(id,name,enabled,config_json,created_at,updated_at)
               VALUES(?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET name=excluded.name, enabled=excluded.enabled,
                 config_json=excluded.config_json, updated_at=excluded.updated_at""",
            (watch_id, name, int(enabled), json.dumps(config, default=str), now, now),
        )

    def ensure_target(self, target: Target) -> int:
        """Insert-or-get the targets row; sets and returns target.db_id."""
        idents = json.dumps(target.identifiers, sort_keys=True)
        stores = json.dumps(target.store_ids) if target.store_ids is not None else None
        with self._lock:
            row = self._one(
                "SELECT id FROM targets WHERE watch_id=? AND retailer=? AND identifiers_json=?",
                (target.watch_id, target.retailer, idents),
            )
            if row:
                self._exec("UPDATE targets SET store_ids_json=? WHERE id=?", (stores, row["id"]))
                target.db_id = row["id"]
            else:
                cur = self._exec(
                    "INSERT INTO targets(watch_id,retailer,identifiers_json,store_ids_json) VALUES(?,?,?,?)",
                    (target.watch_id, target.retailer, idents, stores),
                )
                target.db_id = cur.lastrowid
        return target.db_id  # type: ignore[return-value]

    def delete_watches_not_in(self, watch_ids: list[str]) -> None:
        if not watch_ids:
            self._exec("DELETE FROM watches")
            return
        q = ",".join("?" * len(watch_ids))
        self._exec(f"DELETE FROM watches WHERE id NOT IN ({q})", tuple(watch_ids))

    def target_row(self, target_id: int) -> dict[str, Any] | None:
        return self._one("SELECT * FROM targets WHERE id=?", (target_id,))

    # ------------------------------------------------------------ checks

    def record_check(self, target_id: int, result: StockResult) -> int:
        cur = self._exec(
            """INSERT INTO checks(target_id,checked_at,in_stock,price,channels_json,error,duration_ms,raw_status)
               VALUES(?,?,?,?,?,?,?,?)""",
            (
                target_id, _iso(result.checked_at), int(result.in_stock), result.price,
                json.dumps([c.__dict__ for c in result.channels]), result.error,
                result.duration_ms, result.raw_status,
            ),
        )
        return cur.lastrowid  # type: ignore[return-value]

    def recent_checks(self, target_id: int, limit: int = 20) -> list[dict[str, Any]]:
        return self._all(
            "SELECT * FROM checks WHERE target_id=? ORDER BY checked_at DESC, id DESC LIMIT ?",
            (target_id, limit),
        )

    def last_check(self, target_id: int) -> dict[str, Any] | None:
        rows = self.recent_checks(target_id, 1)
        return rows[0] if rows else None

    def last_in_stock(self, target_id: int) -> dict[str, Any] | None:
        return self._one(
            "SELECT * FROM checks WHERE target_id=? AND in_stock=1 ORDER BY checked_at DESC LIMIT 1",
            (target_id,),
        )

    def prune_checks(self, days: int = CHECK_RETENTION_DAYS) -> int:
        cutoff = _iso(utcnow() - timedelta(days=days))
        return self._exec("DELETE FROM checks WHERE checked_at < ?", (cutoff,)).rowcount

    def prune_restricted_content(self, limits: dict[str, int] | None = None,
                                 now: datetime | None = None) -> dict[str, int]:
        """Delete/scrub content of retailers with storage limits (see RESTRICTED_CONTENT_HOURS).

        Check rows older than the limit are deleted. Events keep their kind and timestamps
        (needed for dedupe and the events log) but lose prices, store ids and cart messages.
        Returns {"checks": n_deleted, "events": n_scrubbed}.
        """
        limits = RESTRICTED_CONTENT_HOURS if limits is None else limits
        now = now or utcnow()
        deleted = scrubbed = 0
        with self._lock:
            for retailer, hours in limits.items():
                cutoff = _iso(now - timedelta(hours=hours))
                deleted += self._exec(
                    "DELETE FROM checks WHERE checked_at < ? AND target_id IN "
                    "(SELECT id FROM targets WHERE retailer=?)", (cutoff, retailer)).rowcount
                rows = self._all(
                    "SELECT e.id, e.details_json FROM events e JOIN targets t ON t.id=e.target_id "
                    "WHERE t.retailer=? AND e.started_at < ?", (retailer, cutoff))
                for r in rows:
                    details = json.loads(r["details_json"] or "{}")
                    changed = False
                    for k in CONTENT_DETAIL_KEYS:
                        if k in details:
                            del details[k]
                            changed = True
                    cart = details.get("cart")
                    if isinstance(cart, dict) and cart.get("message"):
                        cart["message"] = ""
                        changed = True
                    if changed:
                        details["content_scrubbed"] = True
                        self.update_event_details(r["id"], details)
                        scrubbed += 1
        return {"checks": deleted, "events": scrubbed}

    def check_stats_since(self, since: datetime) -> dict[str, int]:
        row = self._one(
            """SELECT COUNT(*) AS checks,
                      SUM(CASE WHEN error IS NOT NULL THEN 1 ELSE 0 END) AS errors,
                      SUM(in_stock) AS in_stock
               FROM checks WHERE checked_at >= ?""",
            (_iso(since),),
        ) or {}
        return {k: int(v or 0) for k, v in row.items()}

    # ------------------------------------------------------------ events

    def open_event(self, target_id: int, kind: str, details: dict[str, Any],
                   started_at: datetime | None = None) -> int:
        cur = self._exec(
            "INSERT INTO events(target_id,kind,started_at,details_json) VALUES(?,?,?,?)",
            (target_id, kind, _iso(started_at or utcnow()), json.dumps(details, default=str)),
        )
        return cur.lastrowid  # type: ignore[return-value]

    def update_event_details(self, event_id: int, details: dict[str, Any]) -> None:
        self._exec("UPDATE events SET details_json=? WHERE id=?", (json.dumps(details, default=str), event_id))

    def close_event(self, event_id: int, ended_at: datetime | None = None) -> None:
        self._exec("UPDATE events SET ended_at=? WHERE id=? AND ended_at IS NULL",
                   (_iso(ended_at or utcnow()), event_id))

    def get_open_event(self, target_id: int, kind: str) -> dict[str, Any] | None:
        row = self._one(
            "SELECT * FROM events WHERE target_id=? AND kind=? AND ended_at IS NULL ORDER BY id DESC LIMIT 1",
            (target_id, kind),
        )
        if row:
            row["details"] = json.loads(row.pop("details_json") or "{}")
        return row

    def recent_events(self, limit: int = 100, watch_id: str | None = None) -> list[dict[str, Any]]:
        sql = """SELECT e.*, t.retailer, t.watch_id FROM events e JOIN targets t ON t.id=e.target_id"""
        params: tuple = ()
        if watch_id:
            sql += " WHERE t.watch_id=?"
            params = (watch_id,)
        sql += " ORDER BY e.started_at DESC, e.id DESC LIMIT ?"
        rows = self._all(sql, params + (limit,))
        for r in rows:
            r["details"] = json.loads(r.pop("details_json") or "{}")
        return rows

    def events_since(self, since: datetime) -> list[dict[str, Any]]:
        rows = self._all("SELECT * FROM events WHERE started_at >= ? ORDER BY started_at", (_iso(since),))
        for r in rows:
            r["details"] = json.loads(r.pop("details_json") or "{}")
        return rows

    # ------------------------------------------------------------ alerts

    def record_alert(self, event_id: int | None, channel: str, status: str,
                     receipt: str | None = None) -> int:
        cur = self._exec(
            "INSERT INTO alerts(event_id,channel,sent_at,status,receipt) VALUES(?,?,?,?,?)",
            (event_id, channel, _iso(utcnow()), status, receipt),
        )
        return cur.lastrowid  # type: ignore[return-value]

    def ack_alert(self, alert_id: int, ack_at: datetime | None = None) -> None:
        self._exec("UPDATE alerts SET ack_at=? WHERE id=?", (_iso(ack_at or utcnow()), alert_id))

    def recent_alerts(self, limit: int = 100) -> list[dict[str, Any]]:
        return self._all("SELECT * FROM alerts ORDER BY sent_at DESC, id DESC LIMIT ?", (limit,))

    def unacked_pushover_receipts(self) -> Iterator[dict[str, Any]]:
        yield from self._all(
            "SELECT * FROM alerts WHERE channel='pushover' AND receipt IS NOT NULL AND ack_at IS NULL"
        )
