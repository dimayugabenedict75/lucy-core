"""SQLite + FTS5 storage for Lucy's long-term memories (replaces the knowledge_store.json file).

  runtime/data/knowledge.sqlite
    memories      id, text, category, importance, created, updated, last_used, uses, source,
                  status ('active' | 'archived'), flag (e.g. 'may conflict with #12'), merged_into, decayed_at
    memories_fts  FTS5 index over memories.text (kept in step by triggers)
    meta          key/value (e.g. json_migrated)

Nothing is lost silently: consolidation ARCHIVES (status='archived', merged_into=...) instead of deleting; only an
explicit `forget` deletes. The old JSON file is never modified or deleted: it is imported once (and copied to
`knowledge_store.json.pre-sqlite.bak`), after that it just sits there as a backup.

The Conductor keeps the active memories in a Python list (a few hundred small rows, ranked with TF-IDF as before) and
calls `sync()` after each change; FTS5 adds fast keyword search on top of that ranking.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import sqlite3
import threading
from pathlib import Path
from typing import Dict, List, Optional

COLUMNS = ("id", "text", "category", "importance", "created", "updated", "last_used", "uses", "source",
           "flag", "merged_into", "decayed_at")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS memories (
    id          INTEGER PRIMARY KEY,
    text        TEXT NOT NULL,
    category    TEXT DEFAULT 'fact',
    importance  INTEGER DEFAULT 3,
    created     TEXT,
    updated     TEXT,
    last_used   TEXT,
    uses        INTEGER DEFAULT 0,
    source      TEXT,
    status      TEXT DEFAULT 'active',
    flag        TEXT,
    merged_into INTEGER,
    decayed_at  TEXT
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE INDEX IF NOT EXISTS memories_status ON memories(status);
CREATE TABLE IF NOT EXISTS memory_vectors (
    memory_id INTEGER NOT NULL,
    kind      TEXT NOT NULL DEFAULT 'text',
    model_id  TEXT NOT NULL,
    text_sha  TEXT,
    dim       INTEGER NOT NULL,
    vec       BLOB NOT NULL,
    PRIMARY KEY (memory_id, kind)
);
"""
_FTS = """
CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(text, content='memories', content_rowid='id', tokenize='unicode61');
CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
    INSERT INTO memories_fts(rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, text) VALUES ('delete', old.id, old.text);
END;
CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE OF text ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, text) VALUES ('delete', old.id, old.text);
    INSERT INTO memories_fts(rowid, text) VALUES (new.id, new.text);
END;
"""


def _row(r: sqlite3.Row) -> dict:
    d = {k: r[k] for k in COLUMNS}
    d["status"] = r["status"]
    return d


class MemoryStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._snap: Dict[int, tuple] = {}              # id -> last synced values (so sync() writes only what changed)
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self.fts = self._init_fts()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------------ setup
    def _init_fts(self) -> bool:
        try:
            self._conn.executescript(_FTS)
            if self.meta_get("fts_built") != "1":              # first time (or an older DB): index what is there
                self._conn.execute("INSERT INTO memories_fts(memories_fts) VALUES ('rebuild')")
                self.meta_set("fts_built", "1")
            self._conn.commit()
            return True
        except sqlite3.OperationalError:                       # SQLite without FTS5: TF-IDF ranking still works
            return False

    def meta_get(self, key: str, default: Optional[str] = None) -> Optional[str]:
        with self._lock:
            r = self._conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
            return r["value"] if r else default

    def meta_set(self, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute("INSERT INTO meta(key, value) VALUES (?, ?) "
                               "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, str(value)))
            self._conn.commit()

    # ------------------------------------------------------------------ migration from the old JSON file
    def migrate_json(self, json_path: Path) -> int:
        """Import knowledge_store.json once. Returns how many memories were imported. The file itself is left alone."""
        with self._lock:
            if self.meta_get("json_migrated") == "1":
                return 0
            json_path = Path(json_path)
            imported = 0
            if json_path.exists():
                try:
                    raw = json.loads(json_path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    raw = []
                bak = json_path.with_name(json_path.name + ".pre-sqlite.bak")
                try:
                    if not bak.exists():
                        shutil.copy2(json_path, bak)
                except OSError:
                    pass
                docs, next_id = [], max([d.get("id", 0) for d in raw if isinstance(d, dict)
                                         and isinstance(d.get("id"), int)] + [0]) + 1
                for d in raw if isinstance(raw, list) else []:
                    if not isinstance(d, dict) or not str(d.get("text", "")).strip():
                        continue
                    if not isinstance(d.get("id"), int):
                        d["id"], next_id = next_id, next_id + 1
                    docs.append(d)
                with self._conn:
                    for d in docs:
                        self._conn.execute(
                            "INSERT OR IGNORE INTO memories(id, text, category, importance, created, updated, last_used, "
                            "uses, source, status) VALUES (?,?,?,?,?,?,?,?,?, 'active')",
                            (d["id"], str(d["text"]), d.get("category") or "fact", int(d.get("importance") or 3),
                             d.get("created"), d.get("updated"), d.get("last_used"), int(d.get("uses") or 0),
                             d.get("source")))
                        imported += 1
            self.meta_set("json_migrated", "1")
            return imported

    # ------------------------------------------------------------------ reading
    def load_active(self) -> List[dict]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM memories WHERE status = 'active' ORDER BY id").fetchall()
            docs = [_row(r) for r in rows]
            self._snap = {d["id"]: self._values(d) for d in docs}
            return docs

    def archived(self, limit: int = 200) -> List[dict]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM memories WHERE status = 'archived' ORDER BY id DESC LIMIT ?",
                                      (limit,)).fetchall()
            return [_row(r) for r in rows]

    def max_id(self) -> int:
        with self._lock:
            return self._conn.execute("SELECT COALESCE(MAX(id), 0) FROM memories").fetchone()[0]

    def fts_search(self, query: str, limit: int = 10) -> List[int]:
        """Ids of ACTIVE memories matching the words of `query` (any word), best first. [] if FTS5 is unavailable."""
        if not self.fts:
            return []
        terms = [t for t in re.findall(r"\w+", str(query).lower()) if len(t) > 1]
        if not terms:
            return []
        match = " OR ".join(f'"{t}"*' for t in terms[:12])
        with self._lock:
            try:
                rows = self._conn.execute(
                    "SELECT m.id FROM memories_fts JOIN memories m ON m.id = memories_fts.rowid "
                    "WHERE memories_fts MATCH ? AND m.status = 'active' ORDER BY bm25(memories_fts) LIMIT ?",
                    (match, limit)).fetchall()
            except sqlite3.OperationalError:
                return []
            return [r["id"] for r in rows]

    # ------------------------------------------------------------------ writing
    @staticmethod
    def _values(d: dict) -> tuple:
        return (d.get("text"), d.get("category"), d.get("importance"), d.get("created"), d.get("updated"),
                d.get("last_used"), d.get("uses"), d.get("source"), d.get("flag"), d.get("merged_into"),
                d.get("decayed_at"))

    def sync(self, docs: List[dict]) -> None:
        """Make the ACTIVE rows equal `docs` (insert new, update changed, delete the ones that are gone).
        Archived rows are never touched here."""
        with self._lock, self._conn:
            ids = {d["id"] for d in docs}
            for d in docs:
                vals = self._values(d)
                if self._snap.get(d["id"]) == vals:
                    continue
                self._conn.execute(
                    "INSERT INTO memories(id, text, category, importance, created, updated, last_used, uses, source, "
                    "flag, merged_into, decayed_at, status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?, 'active') "
                    "ON CONFLICT(id) DO UPDATE SET text=excluded.text, category=excluded.category, "
                    "importance=excluded.importance, created=excluded.created, updated=excluded.updated, "
                    "last_used=excluded.last_used, uses=excluded.uses, source=excluded.source, flag=excluded.flag, "
                    "merged_into=excluded.merged_into, decayed_at=excluded.decayed_at, status='active'",
                    (d["id"],) + vals)
                self._snap[d["id"]] = vals
            gone = [i for i in self._snap if i not in ids]
            for i in gone:
                self._conn.execute("DELETE FROM memories WHERE id = ? AND status = 'active'", (i,))
                self._snap.pop(i, None)

    def archive(self, memory_id: int, merged_into: Optional[int] = None, note: Optional[str] = None) -> bool:
        with self._lock, self._conn:
            cur = self._conn.execute(
                "UPDATE memories SET status = 'archived', merged_into = ?, flag = COALESCE(?, flag) WHERE id = ?",
                (merged_into, note, memory_id))
            self._snap.pop(memory_id, None)
            return cur.rowcount > 0

    def unarchive(self, memory_id: int) -> bool:
        with self._lock, self._conn:
            cur = self._conn.execute(
                "UPDATE memories SET status = 'active', merged_into = NULL, flag = NULL "
                "WHERE id = ? AND status = 'archived'", (memory_id,))
            return cur.rowcount > 0

    # ------------------------------------------------------------------ neural vectors (side table, never touches `memories`)
    @staticmethod
    def text_sha(text: str) -> str:
        return hashlib.sha1(" ".join(str(text).split()).encode("utf-8")).hexdigest()

    def put_vector(self, memory_id: int, text: str, model_id: str, vec, kind: str = "text") -> None:
        import numpy as np
        v = np.asarray(vec, dtype=np.float32)
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO memory_vectors(memory_id, kind, model_id, text_sha, dim, vec) VALUES (?,?,?,?,?,?) "
                "ON CONFLICT(memory_id, kind) DO UPDATE SET model_id=excluded.model_id, text_sha=excluded.text_sha, "
                "dim=excluded.dim, vec=excluded.vec",
                (int(memory_id), kind, model_id, self.text_sha(text), int(v.shape[0]), v.tobytes()))

    def fresh_vectors(self, model_id: str, docs: List[dict], kind: str = "text") -> dict:
        """{memory_id: ndarray} for the docs whose stored vector matches model_id AND the current text (stale ones are left out)."""
        import numpy as np
        if not docs:
            return {}
        with self._lock:
            rows = self._conn.execute(
                "SELECT memory_id, model_id, text_sha, vec FROM memory_vectors WHERE kind = ?", (kind,)).fetchall()
        want = {d["id"]: self.text_sha(d["text"]) for d in docs}
        out = {}
        for r in rows:
            if r["model_id"] == model_id and want.get(r["memory_id"]) == r["text_sha"]:
                out[r["memory_id"]] = np.frombuffer(r["vec"], dtype=np.float32)
        return out

    def vacuum_vectors(self) -> int:
        """Drop vectors whose memory no longer exists (deleted by forget). Archived memories keep theirs."""
        with self._lock, self._conn:
            return self._conn.execute(
                "DELETE FROM memory_vectors WHERE memory_id NOT IN (SELECT id FROM memories)").rowcount
