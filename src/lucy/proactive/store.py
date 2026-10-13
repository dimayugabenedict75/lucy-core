"""SQLite persistence for the proactive system: runtime/data/proactive.sqlite.

Separate from sessions.sqlite on purpose: no shared locks with the chat, and deleting this file resets
the proactive system without touching a single conversation.

  reminders  what is scheduled (kind = reminder | task). Times are UTC epoch seconds.
  events     everything Lucy said on her own (and what was held back), with how the user reacted.
  kv         small state: pause, muted kinds, runtime setting overrides, last-greeting stamp ...
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS reminders (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    kind          TEXT NOT NULL DEFAULT 'reminder',      -- reminder | task
    text          TEXT NOT NULL,                         -- the reminder, or the task instruction
    due_ts        REAL NOT NULL,
    rule          TEXT,                                  -- JSON repeat rule or NULL
    status        TEXT NOT NULL DEFAULT 'active',        -- active | done | cancelled | failed
    created_ts    REAL NOT NULL,
    created_session TEXT,
    last_fired_ts REAL,
    fire_count    INTEGER NOT NULL DEFAULT 0,
    attempts      INTEGER NOT NULL DEFAULT 0,
    last_error    TEXT
);
CREATE INDEX IF NOT EXISTS idx_rem_due ON reminders(status, due_ts);
CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         REAL NOT NULL,
    kind       TEXT NOT NULL,               -- reminder | task | greeting | checkin | alert | test
    key        TEXT,
    session_id TEXT,
    text       TEXT,
    media      TEXT,
    source_id  INTEGER,                     -- reminders.id when it came from one
    status     TEXT NOT NULL DEFAULT 'sent',-- sent | suppressed
    reason     TEXT,
    live       INTEGER NOT NULL DEFAULT 0,  -- reached an open chat window as it happened
    acked      INTEGER NOT NULL DEFAULT 0,  -- the window confirmed it (toast shown / dismissed)
    engaged    INTEGER NOT NULL DEFAULT 0,  -- the user wrote something afterwards
    feedback   TEXT
);
CREATE INDEX IF NOT EXISTS idx_ev_ts ON events(ts);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""

INITIATIVE_KINDS = ("greeting", "checkin", "alert")


class Store:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            try:
                self._conn.execute("PRAGMA journal_mode=WAL")
            except sqlite3.DatabaseError:
                pass
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------------ helpers
    def _q(self, sql: str, args: tuple = ()) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, args).fetchall()]

    def _x(self, sql: str, args: tuple = ()) -> int:
        with self._lock:
            cur = self._conn.execute(sql, args)
            self._conn.commit()
            return cur.lastrowid if sql.lstrip().upper().startswith("INSERT") else cur.rowcount

    # ------------------------------------------------------------------ kv
    def kv_get(self, key: str, default: Any = None) -> Any:
        rows = self._q("SELECT value FROM kv WHERE key=?", (key,))
        if not rows:
            return default
        try:
            return json.loads(rows[0]["value"])
        except ValueError:
            return default

    def kv_set(self, key: str, value: Any) -> None:
        self._x("INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, json.dumps(value)))

    def kv_del(self, key: str) -> None:
        self._x("DELETE FROM kv WHERE key=?", (key,))

    # ------------------------------------------------------------------ reminders
    def add_reminder(self, kind: str, text: str, due_ts: float, rule: dict | None, session: str | None,
                     now: float | None = None) -> int:
        return self._x(
            "INSERT INTO reminders(kind,text,due_ts,rule,created_ts,created_session) VALUES(?,?,?,?,?,?)",
            (kind, text, float(due_ts), json.dumps(rule) if rule else None, now or time.time(), session))

    def get_reminder(self, rid: int) -> dict | None:
        rows = self._q("SELECT * FROM reminders WHERE id=?", (int(rid),))
        return self._decode(rows[0]) if rows else None

    @staticmethod
    def _decode(row: dict) -> dict:
        row["rule"] = json.loads(row["rule"]) if row.get("rule") else None
        return row

    def list_reminders(self, include_inactive: bool = False, limit: int = 100) -> list[dict]:
        where = "" if include_inactive else "WHERE status='active'"
        order = "due_ts ASC" if not include_inactive else "id DESC"
        return [self._decode(r) for r in self._q(f"SELECT * FROM reminders {where} ORDER BY {order} LIMIT ?", (limit,))]

    def due(self, now: float, limit: int = 20) -> list[dict]:
        return [self._decode(r) for r in self._q(
            "SELECT * FROM reminders WHERE status='active' AND due_ts<=? ORDER BY due_ts ASC LIMIT ?", (now, limit))]

    def next_due(self) -> dict | None:
        rows = self._q("SELECT * FROM reminders WHERE status='active' ORDER BY due_ts ASC LIMIT 1")
        return self._decode(rows[0]) if rows else None

    def count_active(self) -> int:
        return self._q("SELECT COUNT(*) AS n FROM reminders WHERE status='active'")[0]["n"]

    def active_between(self, start_ts: float, end_ts: float, limit: int = 10) -> list[dict]:
        return [self._decode(r) for r in self._q(
            "SELECT * FROM reminders WHERE status='active' AND due_ts>? AND due_ts<=? ORDER BY due_ts ASC LIMIT ?",
            (start_ts, end_ts, limit))]

    def update_reminder(self, rid: int, **fields: Any) -> int:
        if not fields:
            return 0
        if "rule" in fields:
            fields["rule"] = json.dumps(fields["rule"]) if fields["rule"] else None
        cols = ", ".join(f"{k}=?" for k in fields)
        return self._x(f"UPDATE reminders SET {cols} WHERE id=?", (*fields.values(), int(rid)))

    def set_status(self, rid: int, status: str, error: str | None = None) -> int:
        return self._x("UPDATE reminders SET status=?, last_error=COALESCE(?, last_error) WHERE id=?",
                       (status, error, int(rid)))

    def cancel_all(self) -> int:
        return self._x("UPDATE reminders SET status='cancelled' WHERE status='active'")

    def task_runs_since(self, ts: float) -> int:
        return self._q("SELECT COUNT(*) AS n FROM events WHERE kind='task' AND status='sent' AND ts>=?", (ts,))[0]["n"]

    # ------------------------------------------------------------------ events
    def add_event(self, kind: str, text: str, *, key: str = "", session_id: str | None = None,
                  media: str | None = None, source_id: int | None = None, status: str = "sent",
                  reason: str = "", live: bool = False, ts: float | None = None) -> int:
        return self._x(
            "INSERT INTO events(ts,kind,key,session_id,text,media,source_id,status,reason,live) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (ts or time.time(), kind, key, session_id, text, media, source_id, status, reason, 1 if live else 0))

    def get_event(self, eid: int) -> dict | None:
        rows = self._q("SELECT * FROM events WHERE id=?", (int(eid),))
        return rows[0] if rows else None

    def mark_live(self, eid: int) -> None:
        self._x("UPDATE events SET live=1 WHERE id=?", (eid,))

    def ack(self, eid: int, feedback: str | None = None) -> int:
        return self._x("UPDATE events SET acked=1, feedback=COALESCE(?, feedback) WHERE id=?", (feedback, int(eid)))

    def set_engaged(self, eid: int) -> None:
        self._x("UPDATE events SET engaged=1 WHERE id=?", (int(eid),))

    def mark_engaged(self, since_ts: float) -> None:
        """The user just wrote something: every recent unanswered unprompted message counts as answered."""
        self._x("UPDATE events SET engaged=1 WHERE engaged=0 AND status='sent' AND ts>=?", (since_ts,))

    def recent_events(self, limit: int = 20, kinds: tuple | None = None, status: str | None = None) -> list[dict]:
        sql, args = "SELECT * FROM events WHERE 1=1", []
        if kinds:
            sql += f" AND kind IN ({','.join('?' * len(kinds))})"
            args += list(kinds)
        if status:
            sql += " AND status=?"
            args.append(status)
        return self._q(sql + " ORDER BY id DESC LIMIT ?", (*args, limit))

    def events_after(self, last_id: int, limit: int = 50) -> list[dict]:
        return self._q("SELECT * FROM events WHERE id>? AND status='sent' ORDER BY id ASC LIMIT ?", (int(last_id), limit))

    def unseen_events(self, since_ts: float, limit: int = 20) -> list[dict]:
        """Sent while no window was open and never confirmed: shown when a window connects."""
        return self._q("SELECT * FROM events WHERE status='sent' AND live=0 AND acked=0 AND ts>=? "
                       "AND kind IN ('reminder','task','alert') ORDER BY id ASC LIMIT ?", (since_ts, limit))

    def count_sent(self, since_ts: float, kinds: tuple = INITIATIVE_KINDS) -> int:
        return self._q(f"SELECT COUNT(*) AS n FROM events WHERE status='sent' AND ts>=? AND kind IN ({','.join('?' * len(kinds))})",
                       (since_ts, *kinds))[0]["n"]

    def last_event(self, kind: str, status: str = "sent") -> dict | None:
        rows = self._q("SELECT * FROM events WHERE kind=? AND status=? ORDER BY id DESC LIMIT 1", (kind, status))
        return rows[0] if rows else None

    def trailing_ignored(self, grace_before_ts: float, kinds: tuple = ("greeting", "checkin"), look: int = 8) -> int:
        """How many of the latest unprompted messages in a row went unanswered (only counting ones older than the grace period)."""
        rows = self._q(
            f"SELECT engaged, feedback FROM events WHERE status='sent' AND ts<=? AND kind IN ({','.join('?' * len(kinds))}) "
            "ORDER BY id DESC LIMIT ?", (grace_before_ts, *kinds, look))
        n = 0
        for r in rows:
            if r["engaged"]:
                break
            n += 1
        return n

    def prune(self, older_than_ts: float) -> None:
        self._x("DELETE FROM events WHERE ts<?", (older_than_ts,))
        self._x("DELETE FROM reminders WHERE status IN ('done','cancelled','failed') AND created_ts<?", (older_than_ts,))
