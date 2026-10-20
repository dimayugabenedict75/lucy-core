"""Library: the persistent file/image cache for Lucy Core chats.

What it does
------------
* Every file or image that goes through a chat (sent by you, or delivered by Lucy with
  send_file / write tools) is copied ONCE into  <Lucy_Core>/cache/<Category>/  and recorded in
  sessions.sqlite. The chat history then only stores a tiny reference (the SHA-256), so
  attachments survive a page refresh, a server restart, and the original file being edited.
* Content-addressed = automatic de-duplication: the same screenshot sent 5 times is stored once.
* Files are served with immutable HTTP caching (the URL *is* the content hash), so the browser
  never downloads the same image twice. Images get small WebP thumbnails for the Library grid.
* Stickers (assets/stickers/**, sent by you or by Lucy's send_sticker tool) are NEVER cached:
  they already live in assets/ forever. Their chat messages keep a plain path reference instead.
* The cache folder and its sub-folders are created lazily, only when there is something to
  cache, together with a  cache/.gitignore  so git ignores the whole folder.
* Self-healing: on startup, records whose file vanished are dropped and stray files that are
  not in the database are removed. Deleting a session removes the files only it was using.

Layout
------
    cache/
      .gitignore          (auto, keeps the folder out of git)
      Images/ Documents/ Code/ Audio/ Video/ Archives/ Other/
      Thumbnails/         (auto, small WebP previews of images)

Environment overrides
---------------------
    LUCY_CACHE_DIR          cache location                       (default: <Lucy_Core>/cache)
    LUCY_CACHE_MAX_FILE_MB  larger files are not copied into it  (default: 200)
"""

from __future__ import annotations

import hashlib
import logging
import mimetypes
import os
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Callable, Iterable, Optional

log = logging.getLogger("lucy.core.library")

CATEGORIES = ("Images", "Documents", "Code", "Audio", "Video", "Archives", "Other")
THUMB_DIR = "Thumbnails"
THUMB_WIDTH = 360
MAX_FILE_BYTES = int(float(os.environ.get("LUCY_CACHE_MAX_FILE_MB", "200")) * 1024 * 1024)

_SHA_RE = re.compile(r"^[0-9a-f]{64}$")

_IMAGE = {"png", "jpg", "jpeg", "gif", "webp", "bmp", "svg", "ico", "avif", "tif", "tiff", "heic"}
_AUDIO = {"wav", "mp3", "ogg", "flac", "m4a", "aac", "opus", "wma"}
_VIDEO = {"mp4", "mkv", "webm", "mov", "avi", "wmv", "m4v"}
_ARCHIVE = {"zip", "7z", "rar", "tar", "gz", "bz2", "xz", "tgz"}
_DOCUMENT = {"pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "odt", "ods", "odp", "rtf",
             "txt", "md", "markdown", "csv", "tsv", "log", "epub"}
_CODE = {"py", "pyw", "js", "mjs", "cjs", "jsx", "ts", "tsx", "json", "jsonl", "html", "htm", "xml",
         "css", "scss", "less", "yaml", "yml", "toml", "ini", "cfg", "conf", "env", "sh", "bash",
         "zsh", "bat", "cmd", "ps1", "psm1", "sql", "c", "h", "cpp", "cc", "cxx", "hpp", "cs",
         "java", "kt", "go", "rs", "php", "rb", "lua", "swift", "r", "pl", "diff", "patch",
         "vue", "svelte", "ipynb"}
_CODE_NAMES = {"dockerfile", "makefile", ".gitignore", ".env"}
# served as plain text so the browser shows (not runs) them
_TEXT_SERVED = _CODE | {"txt", "md", "markdown", "csv", "tsv", "log"}


def categorize(name: str) -> str:
    base = os.path.basename(name or "").lower()
    ext = base.rsplit(".", 1)[-1] if "." in base else ""
    if base in _CODE_NAMES:
        return "Code"
    if ext in _IMAGE:
        return "Images"
    if ext in _CODE:
        return "Code"
    if ext in _DOCUMENT:
        return "Documents"
    if ext in _AUDIO:
        return "Audio"
    if ext in _VIDEO:
        return "Video"
    if ext in _ARCHIVE:
        return "Archives"
    return "Other"


def _safe_name(name: str) -> str:
    base = os.path.basename((name or "").replace("\\", "/")).strip() or "file"
    stem, dot, ext = base.rpartition(".")
    if not dot:
        stem, ext = base, ""
    stem = re.sub(r"[^\w\-. ]+", "_", stem, flags=re.UNICODE).strip(" ._") or "file"
    ext = re.sub(r"[^\w]+", "", ext)[:10]
    return (stem[:80] + ("." + ext if ext else ""))


def _hash_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class Library:
    """Content-addressed cache + the database tables behind the Library view."""

    def __init__(self, cache_dir: Path, conn: sqlite3.Connection, lock: threading.RLock,
                 sticker_dir: Path, now_fn: Optional[Callable[[], str]] = None):
        self.dir = Path(cache_dir)
        self.conn = conn
        self.lock = lock
        self.sticker_dir = Path(sticker_dir)
        self._now = now_fn or (lambda: time.strftime("%Y-%m-%d %H:%M:%S"))
        self.rev = int(time.time())                 # bumps whenever library contents change (used in ETags)
        self._sticker_cache: dict[str, tuple[int, int, str]] = {}   # path -> (mtime_ns, size, sha)
        self._sticker_sig = 0.0
        self._sticker_shas: set[str] = set()
        self._init_schema()

    # ------------------------------------------------------------------ schema / folders
    def _init_schema(self) -> None:
        with self.lock:
            self.conn.executescript("""
                CREATE TABLE IF NOT EXISTS library_items (
                    sha        TEXT PRIMARY KEY,
                    category   TEXT NOT NULL,
                    mime       TEXT,
                    size       INTEGER NOT NULL,
                    rel_path   TEXT NOT NULL,
                    created_at TEXT
                );
                CREATE TABLE IF NOT EXISTS library_refs (
                    ref_id        INTEGER PRIMARY KEY AUTOINCREMENT,
                    sha           TEXT NOT NULL,
                    session_id    TEXT NOT NULL,
                    message_rowid INTEGER,
                    origin        TEXT NOT NULL,
                    name          TEXT NOT NULL,
                    created_at    TEXT,
                    UNIQUE (sha, session_id, message_rowid)
                );
                CREATE INDEX IF NOT EXISTS idx_lib_refs_sha     ON library_refs(sha);
                CREATE INDEX IF NOT EXISTS idx_lib_refs_session ON library_refs(session_id);
                -- P9: files that must survive even when no chat message references them (pins, project files)
                CREATE TABLE IF NOT EXISTS library_keep (
                    sha    TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    PRIMARY KEY (sha, reason)
                );
                CREATE TABLE IF NOT EXISTS library_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            """)
            self.conn.commit()

    def _ensure_dir(self, category: str) -> Path:
        """Create cache/ (+ .gitignore) and the category folder - only when something is stored."""
        d = self.dir / category
        d.mkdir(parents=True, exist_ok=True)
        gi = self.dir / ".gitignore"
        if not gi.exists():
            try:
                gi.write_text("# Lucy Core cache - generated automatically, never commit it.\n*\n!.gitignore\n",
                              encoding="utf-8")
            except OSError:
                pass
        return d

    def abs_path(self, rel_path: str) -> Path:
        p = (self.dir / rel_path).resolve()
        p.relative_to(self.dir.resolve())          # raises ValueError if it ever points outside the cache
        return p

    # ------------------------------------------------------------------ stickers (never cached)
    def _refresh_stickers(self) -> None:
        now = time.monotonic()
        if now - self._sticker_sig < 5.0 and self._sticker_shas:
            return
        self._sticker_sig = now
        seen: dict[str, tuple[int, int, str]] = {}
        try:
            for p in self.sticker_dir.rglob("*"):
                if not p.is_file():
                    continue
                st = p.stat()
                key = str(p)
                old = self._sticker_cache.get(key)
                if old and old[0] == st.st_mtime_ns and old[1] == st.st_size:
                    seen[key] = old
                else:
                    seen[key] = (st.st_mtime_ns, st.st_size, _hash_file(p))
        except OSError:
            pass
        self._sticker_cache = seen
        self._sticker_shas = {v[2] for v in seen.values()}

    def is_sticker_sha(self, sha: str) -> bool:
        self._refresh_stickers()
        return sha in self._sticker_shas

    def is_sticker_path(self, path: Path) -> bool:
        try:
            Path(path).resolve().relative_to(self.sticker_dir.resolve())
            return True
        except (ValueError, OSError):
            return False

    # ------------------------------------------------------------------ ingest
    def _store(self, sha: str, size: int, name: str, write: Callable[[Path], None]) -> dict:
        """Make sure the content is in the cache (copy once), return the attachment record."""
        category = categorize(name)
        with self.lock:
            row = self.conn.execute("SELECT category, rel_path FROM library_items WHERE sha = ?", (sha,)).fetchone()
        if row:
            category = row["category"]
            if self.abs_path(row["rel_path"]).is_file():
                return self._attachment(sha, name, category, size)
        rel = f"{category}/{sha[:16]}_{_safe_name(name)}"
        dest = self._ensure_dir(category) / Path(rel).name
        tmp = dest.with_suffix(dest.suffix + ".part")
        write(tmp)
        os.replace(tmp, dest)
        mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
        with self.lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO library_items (sha, category, mime, size, rel_path, created_at) VALUES (?,?,?,?,?,?)",
                (sha, category, mime, size, rel, self._now()))
            self.conn.commit()
        self.rev += 1
        return self._attachment(sha, name, category, size)

    @staticmethod
    def _attachment(sha: str, name: str, category: str, size: int) -> dict:
        mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
        clean = os.path.basename((name or "").replace("\\", "/")).strip() or "file"
        return {"sha": sha, "name": clean, "type": mime, "size": size, "kind": category.lower()}

    def ingest_bytes(self, data: bytes, name: str, origin: str = "user") -> Optional[dict]:
        """Cache bytes (an upload). Returns the attachment record, or None (sticker / too big / error)."""
        try:
            if not data or len(data) > MAX_FILE_BYTES:
                return None
            sha = hashlib.sha256(data).hexdigest()
            if self.is_sticker_sha(sha):
                return None

            def write(tmp: Path) -> None:
                tmp.write_bytes(data)
            return self._store(sha, len(data), name, write)
        except Exception as e:                                   # the chat must never fail because of the cache
            log.warning("Library: could not cache upload %r: %s", name, e)
            return None

    def ingest_path(self, path: Path, origin: str = "assistant") -> Optional[dict]:
        """Cache a file on disk as it is right now (a snapshot of what Lucy delivered)."""
        try:
            p = Path(path)
            if not p.is_file():
                return None
            size = p.stat().st_size
            if size == 0 or size > MAX_FILE_BYTES or self.is_sticker_path(p):
                return None
            try:
                p.resolve().relative_to(self.dir.resolve())
                return None                                      # already inside the cache
            except ValueError:
                pass
            sha = _hash_file(p)
            if self.is_sticker_sha(sha):
                return None

            def write(tmp: Path) -> None:
                import shutil
                shutil.copyfile(p, tmp)
            return self._store(sha, size, p.name, write)
        except Exception as e:
            log.warning("Library: could not cache %s: %s", path, e)
            return None

    # ------------------------------------------------------------------ references (which message used which file)
    def add_refs(self, session_id: str, message_rowid: int, origin: str, attachments: Iterable[dict]) -> None:
        rows = [(a["sha"], session_id, message_rowid, origin, a.get("name") or "file", self._now())
                for a in attachments if a.get("sha")]
        if not rows:
            return
        with self.lock:
            self.conn.executemany(
                "INSERT OR IGNORE INTO library_refs (sha, session_id, message_rowid, origin, name, created_at) "
                "VALUES (?,?,?,?,?,?)", rows)
            self.conn.commit()
        self.rev += 1

    def existing_shas(self, shas: Iterable[str]) -> set[str]:
        shas = list({s for s in shas if s})
        if not shas:
            return set()
        with self.lock:
            q = ",".join("?" * len(shas))
            rows = self.conn.execute(f"SELECT sha FROM library_items WHERE sha IN ({q})", shas).fetchall()
        return {r["sha"] for r in rows}

    # ------------------------------------------------------------------ queries for the Library view
    def _filters(self, session_id, origin) -> tuple[str, list]:
        where, args = [], []
        if session_id:
            where.append("session_id = ?"); args.append(session_id)
        if origin in ("user", "assistant"):
            where.append("origin = ?"); args.append(origin)
        return (" WHERE " + " AND ".join(where)) if where else "", args

    def list_items(self, session_id: str | None = None, categories: list[str] | None = None,
                   origin: str | None = None, q: str | None = None, limit: int = 60, offset: int = 0,
                   pinned_only: bool = False) -> dict:
        sub_where, sub_args = self._filters(session_id, origin)
        base = (f"FROM library_items i "
                f"JOIN (SELECT sha, MAX(ref_id) AS rid, COUNT(*) AS n FROM library_refs{sub_where} GROUP BY sha) c ON c.sha = i.sha "
                f"JOIN library_refs r ON r.ref_id = c.rid "
                f"LEFT JOIN sessions s ON s.session_id = r.session_id ")
        cond, args = [], list(sub_args)
        if q:
            cond.append("r.name LIKE ?"); args.append(f"%{q.strip()}%")
        count_where = (" WHERE " + " AND ".join(cond)) if cond else ""
        cats = [c for c in (categories or []) if c in CATEGORIES]
        item_cond, item_args = list(cond), list(args)
        if cats:
            item_cond.append(f"i.category IN ({','.join('?' * len(cats))})"); item_args += cats
        if pinned_only:
            item_cond.append("i.sha IN (SELECT sha FROM library_keep WHERE reason = 'pin')")
        item_where = (" WHERE " + " AND ".join(item_cond)) if item_cond else ""
        with self.lock:
            counts = {r["category"]: r["n"] for r in self.conn.execute(
                f"SELECT i.category AS category, COUNT(*) AS n {base}{count_where} GROUP BY i.category", args).fetchall()}
            total = self.conn.execute(f"SELECT COUNT(*) AS n {base}{item_where}", item_args).fetchone()["n"]
            rows = self.conn.execute(
                f"SELECT i.sha, i.category, i.mime, i.size, r.name, r.origin, r.session_id, r.created_at, "
                f"s.title AS session_title, c.n AS refs, "
                f"EXISTS (SELECT 1 FROM library_keep k WHERE k.sha = i.sha AND k.reason = 'pin') AS pinned "
                f"{base}{item_where} ORDER BY pinned DESC, r.ref_id DESC LIMIT ? OFFSET ?",
                item_args + [max(1, min(int(limit), 200)), max(0, int(offset))]).fetchall()
        items = [{
            "sha": r["sha"], "name": r["name"], "type": r["mime"], "size": r["size"], "category": r["category"],
            "kind": r["category"].lower(), "origin": r["origin"], "session_id": r["session_id"],
            "session_title": r["session_title"] or "Deleted session", "created_at": r["created_at"], "refs": r["refs"],
            "pinned": bool(r["pinned"]),
            "url": f"/api/library/file/{r['sha']}",
            "thumb_url": f"/api/library/thumb/{r['sha']}" if r["category"] == "Images" else None,
        } for r in rows]
        counts["_all"] = sum(counts.values())
        return {"items": items, "total": total, "counts": counts}

    def get(self, sha: str) -> Optional[dict]:
        if not _SHA_RE.match(sha or ""):
            return None
        with self.lock:
            row = self.conn.execute(
                "SELECT i.sha, i.category, i.mime, i.size, i.rel_path, "
                "(SELECT name FROM library_refs WHERE sha = i.sha ORDER BY ref_id DESC LIMIT 1) AS name "
                "FROM library_items i WHERE i.sha = ?", (sha,)).fetchone()
        if not row:
            return None
        try:
            path = self.abs_path(row["rel_path"])
        except ValueError:
            return None
        return {"sha": row["sha"], "category": row["category"], "mime": row["mime"], "size": row["size"],
                "name": row["name"] or Path(row["rel_path"]).name, "path": path}

    def served_media_type(self, item: dict) -> str:
        ext = item["name"].rsplit(".", 1)[-1].lower() if "." in item["name"] else ""
        if ext in _TEXT_SERVED:
            return "text/plain; charset=utf-8"
        return item["mime"] or "application/octet-stream"

    # ------------------------------------------------------------------ thumbnails
    def thumbnail(self, sha: str) -> Optional[Path]:
        item = self.get(sha)
        if not item or item["category"] != "Images" or not item["path"].is_file():
            return None
        out = self.dir / THUMB_DIR / f"{sha[:16]}_{THUMB_WIDTH}.webp"
        if out.is_file():
            return out
        try:
            from PIL import Image                                  # optional dependency
            with Image.open(item["path"]) as im:
                im.thumbnail((THUMB_WIDTH, THUMB_WIDTH * 2))
                im = im.convert("RGBA" if im.mode in ("RGBA", "LA", "P") else "RGB")
                out.parent.mkdir(parents=True, exist_ok=True)
                tmp = out.with_suffix(".part")
                im.save(tmp, "WEBP", quality=78)
            os.replace(tmp, out)
            return out
        except Exception:
            return None                                            # caller falls back to the original

    # ------------------------------------------------------------------ deleting / housekeeping
    def _drop_item(self, sha: str, rel_path: str) -> None:
        try:
            self.abs_path(rel_path).unlink(missing_ok=True)
        except (OSError, ValueError):
            pass
        try:
            (self.dir / THUMB_DIR / f"{sha[:16]}_{THUMB_WIDTH}.webp").unlink(missing_ok=True)
        except OSError:
            pass

    def gc_orphans(self) -> int:
        """Remove cached files that no message references any more."""
        with self.lock:
            rows = self.conn.execute(
                "SELECT sha, rel_path FROM library_items WHERE sha NOT IN (SELECT sha FROM library_refs) "
                "AND sha NOT IN (SELECT sha FROM library_keep)").fetchall()
            for r in rows:
                self.conn.execute("DELETE FROM library_items WHERE sha = ?", (r["sha"],))
            self.conn.commit()
        for r in rows:
            self._drop_item(r["sha"], r["rel_path"])
        if rows:
            self.rev += 1
        return len(rows)

    def delete_session(self, session_id: str) -> int:
        with self.lock:
            self.conn.execute("DELETE FROM library_refs WHERE session_id = ?", (session_id,))
            self.conn.commit()
        return self.gc_orphans()

    def delete_item(self, sha: str) -> bool:
        if not _SHA_RE.match(sha or ""):
            return False
        with self.lock:
            row = self.conn.execute("SELECT rel_path FROM library_items WHERE sha = ?", (sha,)).fetchone()
            if not row:
                return False
            self.conn.execute("DELETE FROM library_refs WHERE sha = ?", (sha,))
            self.conn.execute("DELETE FROM library_keep WHERE sha = ?", (sha,))
            self.conn.execute("DELETE FROM library_items WHERE sha = ?", (sha,))
            self.conn.commit()
        self._drop_item(sha, row["rel_path"])
        self.rev += 1
        return True

    def reconcile(self) -> dict:
        """Self-heal: forget records whose file is gone, delete files nobody knows about."""
        missing = orphan_files = 0
        with self.lock:
            rows = self.conn.execute("SELECT sha, rel_path FROM library_items").fetchall()
        known = set()
        for r in rows:
            try:
                p = self.abs_path(r["rel_path"])
            except ValueError:
                p = None
            if p is not None and p.is_file():
                known.add(p)
                continue
            missing += 1
            with self.lock:
                self.conn.execute("DELETE FROM library_keep WHERE sha = ?", (r["sha"],))
                self.conn.execute("DELETE FROM library_refs WHERE sha = ?", (r["sha"],))
                self.conn.execute("DELETE FROM library_items WHERE sha = ?", (r["sha"],))
                self.conn.commit()
        with self.lock:                                            # references to sessions that no longer exist
            self.conn.execute("DELETE FROM library_refs WHERE session_id NOT IN (SELECT session_id FROM sessions)")
            self.conn.commit()
        removed = self.gc_orphans()
        known_resolved = {k.resolve() for k in known}
        if self.dir.is_dir():
            for cat in CATEGORIES:
                d = self.dir / cat
                if not d.is_dir():
                    continue
                for f in d.iterdir():
                    if f.is_file() and f.resolve() not in known_resolved:
                        try:
                            f.unlink(); orphan_files += 1
                        except OSError:
                            pass
        if missing or removed or orphan_files:
            self.rev += 1
            log.info("Library reconcile: %d missing records, %d orphan records, %d stray files removed",
                     missing, removed, orphan_files)
        return {"missing_records": missing, "orphan_records": removed, "stray_files": orphan_files}

    # ------------------------------------------------------------------ P9: pins, size cap, cleanup
    def keep(self, sha: str, reason: str) -> bool:
        """Protect a cached file from cleanup / orphan removal (reason: 'pin', 'project:<id>')."""
        if not _SHA_RE.match(sha or ""):
            return False
        with self.lock:
            if not self.conn.execute("SELECT 1 FROM library_items WHERE sha = ?", (sha,)).fetchone():
                return False
            self.conn.execute("INSERT OR IGNORE INTO library_keep (sha, reason) VALUES (?, ?)", (sha, reason))
            self.conn.commit()
        return True

    def unkeep(self, sha: str, reason: str) -> None:
        with self.lock:
            self.conn.execute("DELETE FROM library_keep WHERE sha = ? AND reason = ?", (sha, reason))
            self.conn.commit()
        self.gc_orphans()

    def set_pin(self, sha: str, pinned: bool) -> bool:
        ok = self.keep(sha, "pin") if pinned else (self.unkeep(sha, "pin") or self.get(sha) is not None)
        if ok:
            self.rev += 1
        return bool(ok)

    def meta_get(self, key: str, default: str | None = None) -> str | None:
        with self.lock:
            row = self.conn.execute("SELECT value FROM library_meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def meta_set(self, key: str, value: str) -> None:
        with self.lock:
            self.conn.execute("INSERT INTO library_meta (key, value) VALUES (?, ?) "
                              "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, str(value)))
            self.conn.commit()

    def cap_bytes(self) -> int:
        """Size cap of the cache in bytes (0 = no cap). Setting: Library > cap; default LUCY_CACHE_CAP_MB or 2 GB."""
        env = os.environ.get("LUCY_CACHE_CAP_MB", "2048")
        try:
            default = int(float(env) * 1024 * 1024)
        except ValueError:
            default = 2048 * 1024 * 1024
        try:
            return max(0, int(self.meta_get("cap_bytes", str(default))))
        except ValueError:
            return default

    def set_cap_bytes(self, n: int) -> int:
        n = max(0, int(n))
        self.meta_set("cap_bytes", str(n))
        return n

    def _removable(self, older_than_days: float | None = None, categories: Iterable[str] | None = None) -> list:
        """Unpinned, unprotected files, oldest first. `older_than_days` compares with the newest message using the file."""
        cats = [c for c in (categories or []) if c in CATEGORIES]
        cond = ["i.sha NOT IN (SELECT sha FROM library_keep)"]
        args: list = []
        if cats:
            cond.append(f"i.category IN ({','.join('?' * len(cats))})"); args += cats
        with self.lock:
            rows = self.conn.execute(
                "SELECT i.sha, i.rel_path, i.size, i.category, "
                "COALESCE((SELECT MAX(created_at) FROM library_refs r WHERE r.sha = i.sha), i.created_at) AS last_used "
                f"FROM library_items i WHERE {' AND '.join(cond)} ORDER BY last_used ASC, i.rowid ASC", args).fetchall()
        if older_than_days is not None and older_than_days > 0:
            cutoff = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - older_than_days * 86400))
            rows = [r for r in rows if (r["last_used"] or "") < cutoff]
        return rows

    def cleanup_preview(self, older_than_days: float | None = None, categories: Iterable[str] | None = None) -> dict:
        rows = self._removable(older_than_days, categories)
        return {"files": len(rows), "bytes": sum(r["size"] for r in rows)}

    def cleanup(self, older_than_days: float | None = None, categories: Iterable[str] | None = None) -> dict:
        """Delete unpinned files (the chat keeps showing them as 'removed'). Pinned and project files are never touched."""
        rows = self._removable(older_than_days, categories)
        freed = 0
        for r in rows:
            if self._remove_one(r["sha"], r["rel_path"]):
                freed += r["size"]
        if rows:
            self.rev += 1
        return {"removed": len(rows), "bytes": freed}

    def _remove_one(self, sha: str, rel_path: str) -> bool:
        with self.lock:
            self.conn.execute("DELETE FROM library_refs WHERE sha = ?", (sha,))
            self.conn.execute("DELETE FROM library_items WHERE sha = ?", (sha,))
            self.conn.commit()
        self._drop_item(sha, rel_path)
        return True

    def enforce_cap(self, cap: int | None = None) -> dict:
        """Over the cap -> delete the least recently used unpinned files until it fits. Returns what was removed."""
        cap = self.cap_bytes() if cap is None else int(cap)
        if cap <= 0:
            return {"removed": 0, "bytes": 0}
        total = self.stats()["bytes"]
        if total <= cap:
            return {"removed": 0, "bytes": 0}
        removed = freed = 0
        for r in self._removable():
            if total <= cap:
                break
            self._remove_one(r["sha"], r["rel_path"])
            total -= r["size"]; freed += r["size"]; removed += 1
        if removed:
            self.rev += 1
            log.info("Library cap: removed %d file(s), freed %d bytes", removed, freed)
        return {"removed": removed, "bytes": freed}

    def stats(self) -> dict:
        with self.lock:
            rows = self.conn.execute(
                "SELECT category, COUNT(*) AS files, COALESCE(SUM(size),0) AS bytes FROM library_items GROUP BY category").fetchall()
        by = {r["category"]: {"files": r["files"], "bytes": r["bytes"]} for r in rows}
        with self.lock:
            pins = self.conn.execute("SELECT COUNT(DISTINCT sha) AS n FROM library_keep WHERE reason = 'pin'").fetchone()["n"]
        return {"dir": str(self.dir), "files": sum(v["files"] for v in by.values()),
                "bytes": sum(v["bytes"] for v in by.values()), "by_category": by,
                "pinned": pins, "cap_bytes": self.cap_bytes()}


# ---------------------------------------------------------------------- HTTP routes
def build_router(get_lib: Callable[[], Library], on_change: Callable[[], None] | None = None):
    """Routes under /api/library. api.py includes it with the API-key dependency."""
    from fastapi import APIRouter, HTTPException
    from fastapi.responses import FileResponse

    router = APIRouter(prefix="/api/library", tags=["library"])
    IMMUTABLE = {
        "Cache-Control": "private, max-age=31536000, immutable",   # the URL contains the content hash
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "sandbox",                       # an HTML file can never run scripts in the app's origin
    }

    @router.get("")
    def list_library(session_id: str | None = None, category: str | None = None, origin: str | None = None,
                     q: str | None = None, limit: int = 60, offset: int = 0, pinned: int = 0):
        cats = [c.strip().capitalize() for c in (category or "").split(",") if c.strip()]
        return get_lib().list_items(session_id, cats, origin, q, limit, offset, pinned_only=bool(pinned))

    @router.post("/{sha}/pin")
    def library_pin(sha: str, body: dict | None = None):
        pinned = bool((body or {}).get("pinned", True))
        if not get_lib().set_pin(sha, pinned):
            raise HTTPException(status_code=404, detail="Not in the library")
        return {"sha": sha, "pinned": pinned}

    @router.get("/cleanup/preview")
    def library_cleanup_preview(older_than_days: float = 0, category: str | None = None):
        cats = [c.strip().capitalize() for c in (category or "").split(",") if c.strip()]
        return get_lib().cleanup_preview(older_than_days or None, cats)

    @router.post("/cleanup")
    def library_cleanup(body: dict | None = None):
        b = body or {}
        cats = [c.strip().capitalize() for c in str(b.get("category") or "").split(",") if c.strip()]
        res = get_lib().cleanup(float(b.get("older_than_days") or 0) or None, cats)
        if on_change:
            on_change()
        return res

    @router.post("/cap")
    def library_set_cap(body: dict):
        try:
            mb = float(body.get("cap_mb"))
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="cap_mb must be a number (0 = no cap)")
        lib = get_lib()
        lib.set_cap_bytes(int(mb * 1024 * 1024))
        return {"cap_bytes": lib.cap_bytes(), **lib.enforce_cap()}

    @router.get("/stats")
    def library_stats():
        return get_lib().stats()

    @router.post("/prune")
    def library_prune():
        return get_lib().reconcile()

    @router.get("/file/{sha}")
    def library_file(sha: str, download: int = 0):
        item = get_lib().get(sha)
        if not item or not item["path"].is_file():
            raise HTTPException(status_code=404, detail="Not in the library")
        if download:
            return FileResponse(item["path"], media_type=item["mime"] or "application/octet-stream",
                                filename=item["name"], headers=IMMUTABLE)
        return FileResponse(item["path"], media_type=get_lib().served_media_type(item), headers=IMMUTABLE)

    @router.get("/thumb/{sha}")
    def library_thumb(sha: str):
        lib = get_lib()
        item = lib.get(sha)
        if not item or not item["path"].is_file():
            raise HTTPException(status_code=404, detail="Not in the library")
        thumb = lib.thumbnail(sha)
        if thumb:
            return FileResponse(thumb, media_type="image/webp", headers=IMMUTABLE)
        return FileResponse(item["path"], media_type=item["mime"] or "image/png", headers=IMMUTABLE)

    @router.delete("/{sha}")
    def library_delete(sha: str):
        if not get_lib().delete_item(sha):
            raise HTTPException(status_code=404, detail="Not in the library")
        return {"status": "deleted", "sha": sha}

    return router
