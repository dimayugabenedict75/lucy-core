"""Undo: snapshot a file before Lucy changes it, restore it later.

Every write/edit made through the tools is snapshotted first (any allowed folder, not only Lucy Core), with size limits.
Index: runtime/backups/undo/index.jsonl (append-only). Files: runtime/backups/undo/<id>.bak
A restore snapshots the current state first, so a restore can itself be undone.
Files Lucy CREATED are "restored" by moving them to the Recycle Bin (never deleted outright).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import List, Optional


def move_to_trash(path: Path, fallback_dir: Path) -> str:
    """Recycle Bin on Windows (send2trash if installed), else a trash folder inside runtime/. Returns where it went."""
    path = Path(path)
    try:
        import send2trash                                              # optional dependency
        send2trash.send2trash(str(path))
        return "the Recycle Bin"
    except ImportError:
        pass
    except Exception:                                                  # noqa: BLE001 - fall through to the other ways
        pass
    if sys.platform == "win32":
        ps = ("Add-Type -AssemblyName Microsoft.VisualBasic; "
              "[Microsoft.VisualBasic.FileIO.FileSystem]::{0}($env:LUCY_TRASH_PATH, 'OnlyErrorDialogs', 'SendToRecycleBin')"
              .format("DeleteDirectory" if path.is_dir() else "DeleteFile"))
        try:
            r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps], timeout=30,
                               capture_output=True, env={**os.environ, "LUCY_TRASH_PATH": str(path)})
            if r.returncode == 0 and not path.exists():
                return "the Recycle Bin"
        except (OSError, subprocess.SubprocessError):
            pass
    fallback_dir.mkdir(parents=True, exist_ok=True)
    dest = fallback_dir / f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}-{path.name}"
    shutil.move(str(path), str(dest))
    return dest.as_posix()


class Backups:
    def __init__(self, root: Path, *, max_file_bytes: int = 50 * 2**20, max_total_bytes: int = 2 * 2**30,
                 trash_dir: Optional[Path] = None):
        self.dir = Path(root)
        self.index = self.dir / "index.jsonl"
        self.trash_dir = Path(trash_dir) if trash_dir else self.dir.parent.parent / "trash"
        self.max_file, self.max_total = max_file_bytes, max_total_bytes
        self._lock = threading.Lock()

    # ------------------------------------------------------------------------------------------ index
    def _append(self, entry: dict) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        with open(self.index, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def entries(self) -> List[dict]:
        try:
            lines = self.index.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        out = []
        for ln in lines:
            try:
                out.append(json.loads(ln))
            except ValueError:
                continue
        return out

    def get(self, bid: str) -> Optional[dict]:
        for e in reversed(self.entries()):
            if e.get("id") == bid:
                return e
        return None

    def list(self, session: Optional[str] = None, limit: int = 30) -> List[dict]:
        es = [e for e in self.entries() if e.get("kind") in ("modified", "created")]
        if session is not None:
            es = [e for e in es if e.get("session") == session]
        return list(reversed(es))[:limit]

    # ------------------------------------------------------------------------------------------ snapshot
    def snapshot(self, path: Path, *, session: Optional[str], tool: str) -> Optional[dict]:
        """Call BEFORE a change. Returns the index entry (None if nothing could be recorded)."""
        path = Path(path)
        bid = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
        entry = {"id": bid, "ts": time.strftime("%Y-%m-%d %H:%M:%S"), "session": session, "tool": tool,
                 "path": path.as_posix()}
        with self._lock:
            try:
                if path.is_dir():
                    return None                                         # folders are not snapshotted (yet)
                if not path.exists():
                    entry["kind"] = "created"                           # undo = move the new file to the Recycle Bin
                else:
                    size = path.stat().st_size
                    if size > self.max_file:
                        entry.update(kind="too_big", size=size)
                    else:
                        self.dir.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(path, self.dir / f"{bid}.bak")
                        entry.update(kind="modified", size=size, backup=f"{bid}.bak")
                self._append(entry)
            except OSError as e:
                entry.update(kind="failed", error=str(e))
                try:
                    self._append(entry)
                except OSError:
                    pass
                return entry
        self._prune()
        return entry

    def _prune(self) -> None:
        try:
            files = sorted(self.dir.glob("*.bak"), key=lambda p: p.stat().st_mtime)
            total = sum(p.stat().st_size for p in files)
            while files and total > self.max_total:
                p = files.pop(0)
                total -= p.stat().st_size
                p.unlink(missing_ok=True)
        except OSError:
            pass

    # ------------------------------------------------------------------------------------------ restore
    def restore(self, bid: str, *, session: Optional[str] = None, by: str = "user") -> str:
        """Put a file back the way it was before change `bid`. session=None means the user (any change)."""
        e = self.get(bid)
        if not e or e.get("kind") not in ("modified", "created"):
            return f"Error: no restorable backup with id {bid!r}. Use action 'list' to see them."
        if session is not None and e.get("session") != session:
            return "Refused: Lucy can only undo changes made in this chat. The user can restore it from Settings > Safety."
        path = Path(e["path"])
        before = self.snapshot(path, session=session, tool=f"restore_from_backup({bid})")
        undo = f" (undo this with id {before['id']})" if before and before.get("kind") in ("modified", "created") else ""
        if e["kind"] == "created":
            if not path.exists():
                return f"Nothing to do: {path.as_posix()} no longer exists."
            where = move_to_trash(path, self.trash_dir)
            return f"Undone: {path.as_posix()} was created by Lucy; it was moved to {where}.{undo}"
        src = self.dir / e.get("backup", "")
        if not src.is_file():
            return f"Error: the backup file for {bid} is gone (pruned by the size limit)."
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".restore-lucy")
        shutil.copy2(src, tmp)
        os.replace(tmp, path)
        return f"Restored {path.as_posix()} to the version from {e['ts']}.{undo}"
