"""Tables for the P9 desktop-style features. They live in the same sessions.sqlite as chats (one file to back up).

  projects / project_files / project_notes   item 1: a project = folder + instructions + files + its own notes
  sessions.project_id                        which project a chat belongs to (NULL = no project)
  checkpoints                                item 6: one row per user turn (rewind point)
  desk_settings                              small key/value store (plan mode default, etc.)
"""

from __future__ import annotations

import sqlite3
import threading


def ensure_schema(conn: sqlite3.Connection, lock: threading.RLock) -> None:
    with lock:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS projects (
                id           TEXT PRIMARY KEY,
                name         TEXT NOT NULL,
                folder       TEXT NOT NULL DEFAULT '',
                instructions TEXT NOT NULL DEFAULT '',
                created_at   TEXT,
                updated_at   TEXT
            );
            CREATE TABLE IF NOT EXISTS project_files (
                project_id TEXT NOT NULL,
                sha        TEXT NOT NULL,
                name       TEXT NOT NULL,
                size       INTEGER NOT NULL DEFAULT 0,
                created_at TEXT,
                PRIMARY KEY (project_id, sha)
            );
            CREATE TABLE IF NOT EXISTS project_notes (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id TEXT NOT NULL,
                text       TEXT NOT NULL,
                created_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_project_notes ON project_notes(project_id);
            CREATE TABLE IF NOT EXISTS checkpoints (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id  TEXT NOT NULL,
                user_rowid  INTEGER NOT NULL,
                backup_mark INTEGER NOT NULL DEFAULT 0,
                created_at  TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_checkpoints_session ON checkpoints(session_id);
            CREATE TABLE IF NOT EXISTS desk_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        """)
        try:
            conn.execute("ALTER TABLE sessions ADD COLUMN project_id TEXT")
        except sqlite3.OperationalError:
            pass                                          # column already exists
        conn.commit()


def setting_get(conn, lock, key: str, default: str | None = None) -> str | None:
    with lock:
        row = conn.execute("SELECT value FROM desk_settings WHERE key = ?", (key,)).fetchone()
    return row[0] if row else default


def setting_set(conn, lock, key: str, value: str) -> None:
    with lock:
        conn.execute("INSERT INTO desk_settings (key, value) VALUES (?, ?) "
                     "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, str(value)))
        conn.commit()
