"""Versioned SQLite upgrades with an automatic backup first, plus a rolling daily backup.

Why: `CREATE TABLE IF NOT EXISTS` never changes a table that already exists, so old databases silently keep old
column defaults (that is how messages ended up with UTC times). Real schema changes go in MIGRATIONS below instead:
numbered, run once, in order. The number lives in the database itself (PRAGMA user_version).

  run(conn, lock, path)          before applying anything: copy the database to runtime/backups/db/, then apply the
                                 pending migrations one by one (each in its own transaction). A failing migration
                                 is rolled back and logged, and the app keeps running on the old schema.
  daily_backup(path)             one copy per day of a database (keeps the newest `keep`), safe while it is in use.

To add a change: append (next_number, "what it does", function(conn)) to MIGRATIONS. Never edit or reorder old ones.
"""
from __future__ import annotations

import logging
import re
import sqlite3
import time
from pathlib import Path
from typing import Callable

from lucy.clock import now_str
from lucy.paths import RUNTIME

log = logging.getLogger("lucy.core.db")

BACKUP_DIR = RUNTIME / "backups" / "db"


def _m1_baseline(conn: sqlite3.Connection) -> None:
    """Marks the schema as it was before versioning existed. Nothing to change."""


def _m2_messages_created_index(conn: sqlite3.Connection) -> None:
    """History search by date ('last 7 days') used to scan every message."""
    conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_created ON messages(created_at)")


MIGRATIONS: list[tuple[int, str, Callable[[sqlite3.Connection], None]]] = [
    (1, "baseline", _m1_baseline),
    (2, "index messages(created_at)", _m2_messages_created_index),
]


def version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def _stamp() -> str:
    return re.sub(r"[^0-9]", "", now_str())[:14]


def backup(conn_or_path, label: str, keep: int = 5, dest_dir: Path | None = None) -> Path | None:
    """Copy a database with SQLite's online backup API (consistent even while it is being written)."""
    dest_dir = dest_dir or BACKUP_DIR
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
        if isinstance(conn_or_path, sqlite3.Connection):
            src, name, close = conn_or_path, "db", False
            row = conn_or_path.execute("PRAGMA database_list").fetchone()
            if row and row[2]:
                name = Path(row[2]).stem
        else:
            p = Path(conn_or_path)
            if not p.is_file():
                return None
            src, name, close = sqlite3.connect(str(p)), p.stem, True
        out = dest_dir / f"{name}.{label}.{_stamp()}.sqlite"
        dst = sqlite3.connect(str(out))
        try:
            src.backup(dst)
        finally:
            dst.close()
            if close:
                src.close()
        _prune(dest_dir, name, label.split("-")[0], keep)
        return out
    except Exception as e:                                   # a failed backup must never stop the app
        log.warning(f"database backup failed ({label}): {e}")
        return None


def _prune(dest_dir: Path, name: str, kind: str, keep: int) -> None:
    files = sorted(dest_dir.glob(f"{name}.{kind}*.sqlite"))
    for f in files[:-keep] if keep > 0 else []:
        try:
            f.unlink()
        except OSError:
            pass


def _has_rows(conn: sqlite3.Connection) -> bool:
    for (t,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall():
        try:
            if conn.execute(f'SELECT 1 FROM "{t}" LIMIT 1').fetchone():
                return True
        except sqlite3.DatabaseError:
            continue                                          # virtual tables etc.: not worth failing over
    return False


def run(conn: sqlite3.Connection, lock, path: str | Path | None = None,
        migrations=None, dest_dir: Path | None = None) -> dict:
    """Apply pending migrations (backup first). Returns {'from': n, 'to': m, 'applied': [...], 'backup': path|None, 'error': str|None}."""
    migrations = MIGRATIONS if migrations is None else migrations
    with lock:
        cur = version(conn)
        pending = [m for m in migrations if m[0] > cur]
        report = {"from": cur, "to": cur, "applied": [], "backup": None, "error": None}
        if not pending:
            return report
        if _has_rows(conn):                                   # a brand-new empty database has nothing worth saving
            b = backup(conn, f"premigrate-v{cur}", keep=5, dest_dir=dest_dir)
            report["backup"] = str(b) if b else None
        for num, title, fn in pending:
            try:
                conn.execute("BEGIN")
                fn(conn)
                conn.execute(f"PRAGMA user_version = {int(num)}")
                conn.commit()
                report["applied"].append(f"{num}: {title}")
                report["to"] = num
                log.info(f"database migration {num} applied: {title}")
            except Exception as e:
                conn.rollback()
                report["error"] = f"migration {num} ({title}) failed: {e}"
                log.error(report["error"] + " - staying on the old schema")
                break
    return report


def daily_backup(path: str | Path, keep: int = 7, dest_dir: Path | None = None) -> Path | None:
    """One backup per calendar day (the PC's date), newest `keep` kept. Returns the file, or None if today's exists."""
    dest_dir = dest_dir or BACKUP_DIR
    p = Path(path)
    today = time.strftime("%Y%m%d")
    if any(dest_dir.glob(f"{p.stem}.daily.{today}*.sqlite")):
        return None
    return backup(p, "daily", keep=keep, dest_dir=dest_dir)
