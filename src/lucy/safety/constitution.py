"""Constitution files: the safety layer itself, hash-locked so Lucy can never quietly weaken it.

THE LIST (this file is on it, so the list protects itself):
  * src/lucy/safety/*.py                    permissions, approvals, audit, undo, this list
  * src/lucy/tools/registry.py              the one place every tool call passes the safety checks
  * src/lucy/tools/_common.py               path resolution used by every file tool
  * src/lucy/server/file_guard.py           path rules, edit validation
  * .core/permissions.yaml                  the user's rules
  * .core/mcp.json                          the MCP servers Lucy may launch (a server entry is a program that runs on this PC)
  * .core/constitution.lock                 the hashes (written only by `python -m lucy.safety seal` or the Settings UI)

At startup the hashes are compared with the lock. A mismatch means something changed these files without the user
sealing the change -> the agent refuses to run tools and the chat says why. First start (no lock yet) seals the
current files ("trust on first use"). While running, the files are re-checked (cheap stat, hash on change).
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .config import is_inside, norm_path

_PKG = Path(__file__).resolve().parent                    # src/lucy/safety
_LUCY = _PKG.parent                                       # src/lucy

CODE_FILES = [_LUCY / "tools" / "registry.py", _LUCY / "tools" / "_common.py", _LUCY / "server" / "file_guard.py"]
LOCK_NAME = "constitution.lock"


def code_files() -> List[Path]:
    return sorted(_PKG.glob("*.py")) + CODE_FILES


def config_files(root: Path) -> List[Path]:
    return [Path(root) / ".core" / "permissions.yaml", Path(root) / ".core" / "mcp.json"]


def lock_path(root: Path) -> Path:
    return Path(root) / ".core" / LOCK_NAME


def _hash(p: Path) -> Optional[str]:
    try:
        # line endings are normalised: an editor switching CRLF<->LF is not a change of meaning
        return hashlib.sha256(p.read_bytes().replace(b"\r\n", b"\n")).hexdigest()
    except OSError:
        return None


def _key(p: Path, root: Path) -> str:
    try:
        return Path(p).resolve().relative_to(Path(root).resolve()).as_posix()
    except ValueError:
        return Path(p).resolve().as_posix()


class Constitution:
    def __init__(self, root: Path, extra_protected: List[Path] = ()):
        self.root = Path(root)
        self.files = code_files() + config_files(self.root)
        self.lock = lock_path(self.root)
        # Folders/files the agent may never write (besides the files above): the safety package folder (no new
        # modules sneaking in) and Lucy's safety state (audit log, undo backups, pause flag).
        self._protected = [norm_path(p) for p in self.files + [self.lock, _PKG] + list(extra_protected)]
        self._stat: Dict[str, Tuple[int, int]] = {}
        self.problems: List[str] = []
        self.note = ""

    # ------------------------------------------------------------------------------------------ protection
    def is_protected(self, normed: str) -> bool:
        return any(is_inside(normed, p) for p in self._protected)

    # ------------------------------------------------------------------------------------------ hashing
    def current(self) -> Dict[str, Optional[str]]:
        return {_key(p, self.root): _hash(p) for p in self.files}

    def _remember_stats(self) -> None:
        self._stat = {}
        for p in self.files + [self.lock]:
            try:
                st = p.stat()
                self._stat[str(p)] = (st.st_mtime_ns, st.st_size)
            except OSError:
                self._stat[str(p)] = (0, -1)

    def read_lock(self) -> Optional[Dict[str, Optional[str]]]:
        try:
            data = json.loads(self.lock.read_text(encoding="utf-8"))
            return dict(data.get("files") or {})
        except (OSError, ValueError, AttributeError):
            return None

    def seal(self, only: Optional[List[Path]] = None, by: str = "user") -> None:
        """Write the lock. only=[...] re-hashes just those files (used by the Settings UI mode switch)."""
        files = self.read_lock() or {}
        cur = self.current()
        if only is None:
            files = cur
        else:
            for p in only:
                k = _key(p, self.root)
                files[k] = cur.get(k, _hash(p))
        self.lock.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.lock.with_name(self.lock.name + ".tmp")
        tmp.write_text(json.dumps({"sealed": time.strftime("%Y-%m-%d %H:%M:%S"), "by": by, "files": files},
                                  indent=1, sort_keys=True), encoding="utf-8")
        os.replace(tmp, self.lock)
        self.problems = []
        self._remember_stats()

    def diff(self) -> List[str]:
        locked = self.read_lock()
        if locked is None:
            return ["constitution.lock is missing or unreadable"]
        cur = self.current()
        out = []
        for k in sorted(set(locked) | set(cur)):
            a, b = locked.get(k), cur.get(k)
            if a == b:
                continue
            if a is None:
                out.append(f"{k}: new file")
            elif b is None:
                out.append(f"{k}: missing")
            else:
                out.append(f"{k}: changed")
        return out

    def verify(self, trust_on_first_use: bool = True) -> bool:
        if not self.lock.exists() and trust_on_first_use:
            self.seal(by="first start")
            self.note = "constitution sealed on first start"
            return True
        self.problems = self.diff()
        self._remember_stats()
        return not self.problems

    def recheck(self) -> bool:
        """Cheap runtime check: only hashes again when a file's mtime/size changed."""
        changed = False
        for p in self.files + [self.lock]:
            try:
                st = p.stat()
                cur = (st.st_mtime_ns, st.st_size)
            except OSError:
                cur = (0, -1)
            if self._stat.get(str(p)) != cur:
                changed = True
                break
        if changed:
            self.problems = self.diff()
            self._remember_stats()
        return not self.problems
