"""Search by MEANING over past chat messages, files from the Library and (optionally) pictures, using Stella vectors.

Vectors live in their own file (runtime/data/semantic_index.sqlite) and can be deleted any time: they are rebuilt in
the background. Nothing here touches sessions.sqlite or the Library tables except to READ them.

  index_pending()   embeds new messages / files / images, a few batches per call. Only talks to an embed server that is
                    ALREADY up (never starts it, never resets its idle timer), so it can't wake the GPU while you game.
  search()          query -> best matches. Returns [] when Stella is off or nothing is indexed yet; the caller then just
                    uses the keyword search it already had. Results are checked against the live tables, so deleted
                    sessions / files never show up.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
from pathlib import Path

import numpy as np

from lucy.rag.embedding_engine import DOC, QUERY

log = logging.getLogger("lucy.core.semantic")

MIN_MSG_CHARS = 25
MAX_MSG_CHARS = 1200
CHUNK_CHARS = 900
CHUNK_OVERLAP = 120
MAX_CHUNKS_PER_FILE = 40
MAX_FILE_BYTES = 400_000
MAX_IMAGE_BYTES = 6_000_000
BATCH = 24
MIN_COSINE = 0.60                  # guess from the embed guide: calibrate with tools/embed_calibrate.py on your own chats
TEXT_SUFFIXES = {".txt", ".md", ".markdown", ".csv", ".json", ".log", ".py", ".js", ".ts", ".html", ".css", ".yaml",
                 ".yml", ".ini", ".toml", ".xml", ".sql", ".bat", ".ps1", ".sh", ".tsv", ".rst"}
IMAGE_SUFFIXES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp",
                  ".gif": "image/gif"}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sem_vectors (
    kind TEXT NOT NULL, ref TEXT NOT NULL, model_id TEXT NOT NULL, dim INTEGER NOT NULL, vec BLOB NOT NULL,
    meta TEXT, PRIMARY KEY (kind, ref));
CREATE TABLE IF NOT EXISTS sem_state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def chunk_text(text: str) -> list[str]:
    text = " ".join(text.split())
    if not text:
        return []
    out, i = [], 0
    while i < len(text) and len(out) < MAX_CHUNKS_PER_FILE:
        out.append(text[i:i + CHUNK_CHARS])
        i += CHUNK_CHARS - CHUNK_OVERLAP
    return out


class SemanticIndex:
    def __init__(self, path: Path, neural, sessions_conn: sqlite3.Connection, sessions_lock, library=None) -> None:
        self.neural, self.sc, self.sl, self.lib = neural, sessions_conn, sessions_lock, library
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(_SCHEMA)
        self.conn.commit()
        self._lock = threading.RLock()
        self._busy = threading.Lock()
        self._cache: dict = {}                       # kind -> (version, refs, metas, matrix)
        self._version = 0

    # -- state --------------------------------------------------------------------------
    def _get(self, key: str, default: str = "") -> str:
        r = self.conn.execute("SELECT value FROM sem_state WHERE key = ?", (key,)).fetchone()
        return r["value"] if r else default

    def _set(self, key: str, value: str) -> None:
        self.conn.execute("INSERT INTO sem_state(key, value) VALUES (?, ?) "
                          "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, str(value)))

    def _put(self, kind: str, ref: str, mid: str, vec, meta: dict) -> None:
        v = np.asarray(vec, dtype=np.float32)
        self.conn.execute("INSERT INTO sem_vectors(kind, ref, model_id, dim, vec, meta) VALUES (?,?,?,?,?,?) "
                          "ON CONFLICT(kind, ref) DO UPDATE SET model_id=excluded.model_id, dim=excluded.dim, "
                          "vec=excluded.vec, meta=excluded.meta",
                          (kind, ref, mid, int(v.shape[0]), v.tobytes(), json.dumps(meta, ensure_ascii=False)))
        self._version += 1

    def counts(self) -> dict:
        with self._lock:
            rows = self.conn.execute("SELECT kind, COUNT(*) AS n FROM sem_vectors GROUP BY kind").fetchall()
        return {r["kind"]: r["n"] for r in rows}

    def _reset_if_model_changed(self, mid: str) -> None:
        if self._get("model_id") != mid:
            log.info("embedding model changed: rebuilding the semantic index")
            self.conn.execute("DELETE FROM sem_vectors")
            self.conn.execute("DELETE FROM sem_state")
            self._set("model_id", mid)
            self.conn.commit()
            self._version += 1

    # -- indexing -----------------------------------------------------------------------
    def index_pending(self, max_batches: int = 6) -> dict:
        """Embed what is new. Returns {'msg': n, 'file': n, 'image': n} written this call (all 0 if Stella is not up)."""
        done = {"msg": 0, "file": 0, "image": 0}
        if self.neural is None or not self._busy.acquire(blocking=False):
            return done
        try:
            mid = self.neural.model_id()
            with self._lock:
                self._reset_if_model_changed(mid)
                for _ in range(max_batches):
                    rows_seen, embedded = self._index_messages(mid)
                    done["msg"] += embedded
                    if rows_seen == 0:
                        break
                if self.lib is not None:
                    done["file"] += self._index_files(mid, max_batches)
                    done["image"] += self._index_images(mid, max_batches)
                self.conn.commit()
        except Exception as e:                       # indexing is a convenience: never let it take anything down
            log.warning(f"semantic indexing stopped: {e}")
        finally:
            self._busy.release()
        return done

    def _embed(self, texts: list[str]):
        return self.neural.embed_texts(texts, DOC, timeout=60.0, background=True)

    def _index_messages(self, mid: str) -> tuple[int, int]:
        """(rows looked at, rows embedded). Short messages are skipped but still advance the cursor."""
        cursor = int(self._get("msg_cursor", "0"))
        with self.sl:
            rows = self.sc.execute(
                "SELECT rowid AS id, session_id, role, content, created_at FROM messages "
                "WHERE rowid > ? AND role IN ('user','assistant') ORDER BY rowid LIMIT ?", (cursor, BATCH)).fetchall()
        if not rows:
            return 0, 0
        keep = [r for r in rows if len((r["content"] or "").strip()) >= MIN_MSG_CHARS]
        if keep:
            vecs = self._embed([(r["content"] or "")[:MAX_MSG_CHARS] for r in keep])
            if vecs is None:
                return 0, 0                           # server not up: try again later, don't advance
            for r, v in zip(keep, vecs):
                self._put("msg", str(r["id"]), mid, v, {"session_id": r["session_id"], "role": r["role"],
                                                        "date": r["created_at"]})
        self._set("msg_cursor", str(rows[-1]["id"]))
        self.conn.commit()
        return len(rows), len(keep)

    def _library_rows(self, kinds: set[str]) -> list[dict]:
        with self.lib.lock:
            rows = self.lib.conn.execute(
                "SELECT i.sha, i.category, i.rel_path, "
                "(SELECT name FROM library_refs WHERE sha = i.sha ORDER BY ref_id DESC LIMIT 1) AS name "
                "FROM library_items i ORDER BY i.rowid DESC LIMIT 500").fetchall()
        out = []
        for r in rows:
            try:
                path = self.lib.abs_path(r["rel_path"])
            except ValueError:
                continue
            name = r["name"] or path.name
            suf = path.suffix.lower()
            kind = "image" if suf in IMAGE_SUFFIXES else ("file" if suf in TEXT_SUFFIXES else None)
            if kind in kinds and path.is_file():
                out.append({"sha": r["sha"], "name": name, "path": path, "kind": kind, "suffix": suf})
        return out

    def _index_files(self, mid: str, max_batches: int) -> int:
        n = 0
        for it in self._library_rows({"file"}):
            if self._get(f"file:{it['sha']}"):
                continue
            if max_batches <= 0:
                break
            try:
                if it["path"].stat().st_size > MAX_FILE_BYTES:
                    self._set(f"file:{it['sha']}", "skipped")
                    continue
                text = it["path"].read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            chunks = chunk_text(text)
            if not chunks:
                self._set(f"file:{it['sha']}", "empty")
                continue
            vecs = self._embed([f"{it['name']}\n{c}" for c in chunks])
            if vecs is None:
                break
            for i, (c, v) in enumerate(zip(chunks, vecs)):
                self._put("file", f"{it['sha']}:{i}", mid, v, {"sha": it["sha"], "name": it["name"], "chunk": i,
                                                                "text": c[:400]})
            self._set(f"file:{it['sha']}", str(len(chunks)))
            self.conn.commit()
            n += len(chunks)
            max_batches -= 1
        return n

    def _index_images(self, mid: str, max_batches: int) -> int:
        if not getattr(self.neural, "srv", None) or not self.neural.srv.cfg.get("use_mmproj"):
            return 0
        n = 0
        for it in self._library_rows({"image"}):
            if self._get(f"image:{it['sha']}"):
                continue
            if max_batches <= 0:
                break
            try:
                data = it["path"].read_bytes()
            except OSError:
                continue
            if len(data) > MAX_IMAGE_BYTES:
                self._set(f"image:{it['sha']}", "skipped")
                continue
            v = self.neural.embed_image(data, IMAGE_SUFFIXES[it["suffix"]], background=True)
            if v is None:
                break
            self._put("image", it["sha"], mid, v, {"sha": it["sha"], "name": it["name"]})
            self._set(f"image:{it['sha']}", "1")
            self.conn.commit()
            n += 1
            max_batches -= 1
        return n

    # -- searching ----------------------------------------------------------------------
    def _matrix(self, kind: str, mid: str):
        c = self._cache.get(kind)
        if c and c[0] == self._version:
            return c[1], c[2], c[3]
        rows = self.conn.execute("SELECT ref, meta, vec FROM sem_vectors WHERE kind = ? AND model_id = ?",
                                 (kind, mid)).fetchall()
        refs = [r["ref"] for r in rows]
        metas = [json.loads(r["meta"] or "{}") for r in rows]
        mat = np.stack([np.frombuffer(r["vec"], dtype=np.float32) for r in rows]) if rows else np.zeros((0, 1), np.float32)
        self._cache[kind] = (self._version, refs, metas, mat)
        return refs, metas, mat

    def search(self, query: str, *, kinds=("msg", "file", "image"), limit: int = 5, session_id: str | None = None,
               exclude_session: str | None = None, days: int | None = None, min_cosine: float = MIN_COSINE) -> list[dict]:
        """Best matches by meaning, best first. Each result has `kind`, `score`, and for chats the same fields as
        history.search_history (id, session_id, title, role, date, snippet); for files `name`, `sha`, `snippet`."""
        if self.neural is None or not str(query).strip():
            return []
        qv = self.neural.embed_texts([query], QUERY, timeout=3.0)
        if qv is None:
            return []
        mid = self.neural.model_id()
        cands = []
        with self._lock:
            for kind in kinds:
                if kind == "image" and not getattr(self.neural, "srv", None):
                    continue
                refs, metas, mat = self._matrix(kind, mid)
                if not len(refs):
                    continue
                sims = mat @ qv[0]                      # for "image" this is text-vs-picture (same vector space)
                for i in np.argsort(-sims)[: limit * 4]:
                    if sims[i] >= min_cosine:
                        cands.append((float(sims[i]), kind, refs[i], metas[i]))
        cands.sort(key=lambda x: -x[0])
        out: list[dict] = []
        for score, kind, ref, meta in cands:
            item = self._resolve(kind, ref, meta, session_id, exclude_session, days)
            if item:
                item["score"] = round(score, 3)
                item["kind"] = kind
                out.append(item)
            if len(out) >= limit:
                break
        return out

    def _resolve(self, kind, ref, meta, session_id, exclude_session, days) -> dict | None:
        """Check the hit still exists (and passes the filters); add title/snippet."""
        if kind == "msg":
            if session_id and meta.get("session_id") != session_id:
                return None
            if exclude_session and meta.get("session_id") == exclude_session:
                return None
            sql = ("SELECT m.rowid AS id, m.session_id, m.role, m.content, m.created_at, s.title FROM messages m "
                   "LEFT JOIN sessions s ON s.session_id = m.session_id WHERE m.rowid = ?")
            args: list = [int(ref)]
            if days:
                sql += " AND m.created_at >= datetime('now', ?, 'localtime')"
                args.append(f"-{int(days)} days")
            with self.sl:
                r = self.sc.execute(sql, args).fetchone()
            if not r:
                return None
            text = " ".join((r["content"] or "").split())
            return {"id": r["id"], "session_id": r["session_id"], "title": r["title"], "role": r["role"],
                    "date": r["created_at"], "snippet": text[:420] + ("..." if len(text) > 420 else ""),
                    "semantic": True}
        if self.lib is None:
            return None
        sha = meta.get("sha")
        with self.lib.lock:
            row = self.lib.conn.execute(
                "SELECT r.session_id, r.created_at, s.title FROM library_refs r LEFT JOIN sessions s "
                "ON s.session_id = r.session_id WHERE r.sha = ? ORDER BY r.ref_id DESC LIMIT 1", (sha,)).fetchone()
        if not row:
            return None
        if session_id and row["session_id"] != session_id:
            return None
        if exclude_session and row["session_id"] == exclude_session:
            return None
        snippet = meta.get("text", "") if kind == "file" else "(picture)"
        return {"id": None, "session_id": row["session_id"], "title": row["title"], "role": "file",
                "date": row["created_at"], "name": meta.get("name"), "sha": sha, "snippet": snippet, "semantic": True}
