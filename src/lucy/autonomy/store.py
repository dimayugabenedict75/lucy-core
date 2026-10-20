"""SQLite persistence for the job queue: runtime/data/jobs.sqlite (separate file: deleting it resets the queue only).

  jobs      one row per job. Times are epoch seconds (UTC). `checkpoint` = the job's conversation so far (JSON), so a job
            survives a restart and resumes where it was. `park` = the ONE approval the job is waiting for, if any.
  runs      one row per run of a job (a recurring job has many): outcome, summary, usage
  log       short progress lines per job (what the Tasks panel shows)

Statuses
  waiting_ok  made by the model with full tools: does not start until the USER presses OK in the Tasks panel
  scheduled   will start when due (and the model is free / quiet hours are over / caps allow)
  running     a run is in progress
  parked      needs the user's approval for one step; nothing runs until they answer (or the approval expires)
  stopped     ran out of budget before finishing (one-off): the user can continue it with more budget
  done / failed / cancelled   final
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

STATUSES = ("waiting_ok", "scheduled", "running", "parked", "stopped", "done", "failed", "cancelled")
ACTIVE = ("waiting_ok", "scheduled", "running", "parked", "stopped")
FINAL = ("done", "failed", "cancelled")
STATUS_LABELS = {"waiting_ok": "needs your OK to start", "scheduled": "scheduled", "running": "running",
                 "parked": "waiting for your approval", "stopped": "stopped: budget used up", "done": "done",
                 "failed": "failed", "cancelled": "cancelled"}

JSON_COLS = ("rule", "budget", "usage", "park", "allow_tools")

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    kind          TEXT NOT NULL DEFAULT 'goal',          -- goal (agent loop) | builtin (python handler)
    title         TEXT NOT NULL DEFAULT '',
    goal          TEXT NOT NULL DEFAULT '',
    handler       TEXT,                                  -- builtin only
    access        TEXT NOT NULL DEFAULT 'read',          -- read (read-only tools) | full (normal tools through the gate)
    source        TEXT NOT NULL DEFAULT 'user',          -- user | model | system
    session       TEXT,
    status        TEXT NOT NULL DEFAULT 'scheduled',
    due_ts        REAL NOT NULL DEFAULT 0,
    rule          TEXT,                                  -- JSON repeat rule (proactive.schedule) or NULL
    idle_after    REAL NOT NULL DEFAULT 0,               -- seconds the user must have been quiet before it starts
    notify        TEXT NOT NULL DEFAULT 'always',        -- always | changes (silent when the run found nothing new)
    budget        TEXT,
    usage         TEXT,                                  -- usage of the CURRENT run (continues after a park / restart)
    resume        INTEGER NOT NULL DEFAULT 0,            -- 1 = next start continues the checkpoint
    park          TEXT,
    allow_tools   TEXT,                                  -- tools the user said "always allow" for this job
    deadline_ts   REAL,
    deadline_alerted INTEGER NOT NULL DEFAULT 0,
    quiet_ok      INTEGER NOT NULL DEFAULT 0,
    rest_ok       INTEGER NOT NULL DEFAULT 1,
    runs          INTEGER NOT NULL DEFAULT 0,
    last_run_ts   REAL,
    last_result   TEXT,
    last_error    TEXT,
    attempts      INTEGER NOT NULL DEFAULT 0,
    created_ts    REAL NOT NULL,
    updated_ts    REAL NOT NULL,
    checkpoint    TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_due ON jobs(status, due_ts);
CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id      INTEGER NOT NULL,
    started_ts  REAL NOT NULL,
    ended_ts    REAL,
    outcome     TEXT,                                    -- done | budget | parked | cancelled | paused | error | stopped
    summary     TEXT,
    tool_calls  INTEGER NOT NULL DEFAULT 0,
    tokens      INTEGER NOT NULL DEFAULT 0,
    seconds     REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_runs_job ON runs(job_id, id);
CREATE INDEX IF NOT EXISTS idx_runs_ts ON runs(started_ts);
CREATE TABLE IF NOT EXISTS log (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id  INTEGER NOT NULL,
    ts      REAL NOT NULL,
    kind    TEXT NOT NULL,                               -- step | note | approval | error | report
    text    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_log_job ON log(job_id, id);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""

LOG_KEEP = 150          # progress lines kept per job


class JobStore:
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

    @staticmethod
    def _dec(row: dict) -> dict:
        for c in JSON_COLS:
            v = row.get(c)
            if isinstance(v, str):
                try:
                    row[c] = json.loads(v)
                except ValueError:
                    row[c] = None
        return row

    @staticmethod
    def _enc(fields: dict) -> dict:
        out = dict(fields)
        for c in JSON_COLS:
            if c in out:
                out[c] = json.dumps(out[c]) if out[c] not in (None, "") else None
        return out

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

    # ------------------------------------------------------------------ jobs
    def add(self, *, title: str, goal: str = "", kind: str = "goal", handler: str | None = None, access: str = "read",
            source: str = "user", session: str | None = None, status: str = "scheduled", due_ts: float = 0.0,
            rule: dict | None = None, idle_after: float = 0.0, notify: str = "always", budget: dict | None = None,
            deadline_ts: float | None = None, quiet_ok: bool = False, rest_ok: bool = True,
            now: float | None = None) -> int:
        now = now if now is not None else time.time()
        f = self._enc({"rule": rule, "budget": budget})
        return self._x(
            "INSERT INTO jobs(kind,title,goal,handler,access,source,session,status,due_ts,rule,idle_after,notify,budget,"
            "deadline_ts,quiet_ok,rest_ok,created_ts,updated_ts) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (kind, title, goal, handler, access, source, session, status, float(due_ts), f["rule"], float(idle_after),
             notify, f["budget"], deadline_ts, 1 if quiet_ok else 0, 1 if rest_ok else 0, now, now))

    def get(self, jid: int, checkpoint: bool = False) -> dict | None:
        cols = "*" if checkpoint else _LIGHT
        rows = self._q(f"SELECT {cols} FROM jobs WHERE id=?", (int(jid),))
        return self._dec(rows[0]) if rows else None

    def find_handler(self, handler: str) -> dict | None:
        rows = self._q(f"SELECT {_LIGHT} FROM jobs WHERE kind='builtin' AND handler=? ORDER BY id LIMIT 1", (handler,))
        return self._dec(rows[0]) if rows else None

    def list(self, statuses: tuple | None = None, limit: int = 100, sources: tuple | None = None) -> list[dict]:
        sql, args = f"SELECT {_LIGHT} FROM jobs WHERE 1=1", []
        if statuses:
            sql += f" AND status IN ({','.join('?' * len(statuses))})"
            args += list(statuses)
        if sources:
            sql += f" AND source IN ({','.join('?' * len(sources))})"
            args += list(sources)
        rows = self._q(sql + " ORDER BY CASE WHEN status IN ('done','failed','cancelled') THEN 1 ELSE 0 END, "
                       "COALESCE(deadline_ts, 1e18), due_ts, id LIMIT ?", (*args, limit))
        return [self._dec(r) for r in rows]

    def due(self, now: float, limit: int = 20) -> list[dict]:
        """Scheduled jobs whose time has come. Nearest deadline first, then the oldest due time."""
        rows = self._q(f"SELECT {_LIGHT} FROM jobs WHERE status='scheduled' AND due_ts<=? "
                       "ORDER BY COALESCE(deadline_ts, 1e18), due_ts, id LIMIT ?", (now, limit))
        return [self._dec(r) for r in rows]

    def update(self, jid: int, **fields: Any) -> int:
        if not fields:
            return 0
        fields.setdefault("updated_ts", time.time())
        f = self._enc(fields)
        cols = ", ".join(f"{k}=?" for k in f)
        return self._x(f"UPDATE jobs SET {cols} WHERE id=?", (*f.values(), int(jid)))

    def set_checkpoint(self, jid: int, messages: list | None) -> None:
        self._x("UPDATE jobs SET checkpoint=?, updated_ts=? WHERE id=?",
                (json.dumps(messages, ensure_ascii=False) if messages is not None else None, time.time(), int(jid)))

    def get_checkpoint(self, jid: int) -> list | None:
        rows = self._q("SELECT checkpoint FROM jobs WHERE id=?", (int(jid),))
        if not rows or not rows[0]["checkpoint"]:
            return None
        try:
            v = json.loads(rows[0]["checkpoint"])
            return v if isinstance(v, list) else None
        except ValueError:
            return None

    def count(self, statuses: tuple) -> int:
        return self._q(f"SELECT COUNT(*) AS n FROM jobs WHERE status IN ({','.join('?' * len(statuses))})",
                       statuses)[0]["n"]

    def recover(self) -> list[int]:
        """After a restart: jobs that were `running` continue from their checkpoint."""
        ids = [r["id"] for r in self._q("SELECT id FROM jobs WHERE status='running'")]
        for jid in ids:
            self.update(jid, status="scheduled", resume=1, due_ts=0)
        return ids

    def prune(self, older_than_ts: float) -> None:
        self._x("DELETE FROM runs WHERE started_ts<?", (older_than_ts,))
        old = [r["id"] for r in self._q("SELECT id FROM jobs WHERE status IN ('done','failed','cancelled') AND updated_ts<?",
                                        (older_than_ts,))]
        for jid in old:
            self._x("DELETE FROM log WHERE job_id=?", (jid,))
            self._x("DELETE FROM jobs WHERE id=?", (jid,))

    # ------------------------------------------------------------------ runs
    def start_run(self, jid: int, now: float) -> int:
        return self._x("INSERT INTO runs(job_id,started_ts) VALUES(?,?)", (int(jid), now))

    def finish_run(self, rid: int, now: float, outcome: str, summary: str, usage: dict) -> None:
        self._x("UPDATE runs SET ended_ts=?, outcome=?, summary=?, tool_calls=?, tokens=?, seconds=? WHERE id=?",
                (now, outcome, (summary or "")[:600], int(usage.get("tool_calls", 0)), int(usage.get("tokens", 0)),
                 float(usage.get("seconds", 0)), int(rid)))

    def recent_runs(self, jid: int, n: int = 5) -> list[dict]:
        return self._q("SELECT * FROM runs WHERE job_id=? ORDER BY id DESC LIMIT ?", (int(jid), n))

    def usage_since(self, ts: float) -> dict:
        """What the queue used since `ts` (daily caps). Counts every run that STARTED after ts."""
        r = self._q("SELECT COUNT(*) AS runs, COALESCE(SUM(r.tool_calls),0) AS tool_calls, COALESCE(SUM(r.seconds),0) AS seconds "
                    "FROM runs r JOIN jobs j ON j.id=r.job_id WHERE j.kind='goal' AND r.started_ts>=?", (ts,))[0]
        return {"runs": int(r["runs"]), "tool_calls": int(r["tool_calls"]), "seconds": float(r["seconds"])}

    # ------------------------------------------------------------------ log
    def log(self, jid: int, kind: str, text: str, now: float | None = None) -> None:
        self._x("INSERT INTO log(job_id,ts,kind,text) VALUES(?,?,?,?)",
                (int(jid), now if now is not None else time.time(), kind, str(text)[:400]))
        self._x("DELETE FROM log WHERE job_id=? AND id NOT IN (SELECT id FROM log WHERE job_id=? ORDER BY id DESC LIMIT ?)",
                (int(jid), int(jid), LOG_KEEP))

    def log_tail(self, jid: int, n: int = 12) -> list[dict]:
        rows = self._q("SELECT ts,kind,text FROM log WHERE job_id=? ORDER BY id DESC LIMIT ?", (int(jid), n))
        return list(reversed(rows))


_LIGHT = ("id,kind,title,goal,handler,access,source,session,status,due_ts,rule,idle_after,notify,budget,usage,resume,park,"
          "allow_tools,deadline_ts,deadline_alerted,quiet_ok,rest_ok,runs,last_run_ts,last_result,last_error,attempts,"
          "created_ts,updated_ts")
