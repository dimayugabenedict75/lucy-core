"""The system prompt's persona block = CORE (human-owned lucy.md) + SELF (agent-written lucy_self.md).

Both are re-read when their mtime changes, so an edit shows up on the next message without restarting Lucy Core.
SELF comes right after CORE and before the per-message facts (`[USER FACTS]`), so the stable part of the prompt stays
cache-friendly: the prompt only changes when CORE or SELF really changed.
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Optional

from .self_store import SelfStore

DEFAULT_CORE = "You are Lucy, a helpful and witty AI companion."


class PersonaLoader:
    def __init__(self, core_path: Path, store: SelfStore):
        self.core_path = Path(core_path)
        self.store = store
        self._lock = threading.Lock()
        self._core_stat: Optional[tuple] = None
        self._core = DEFAULT_CORE

    def core(self) -> str:
        with self._lock:
            try:
                st = self.core_path.stat()
                key = (st.st_mtime_ns, st.st_size)
            except OSError:
                self._core_stat, self._core = None, DEFAULT_CORE
                return self._core
            if key != self._core_stat:
                try:
                    self._core = self.core_path.read_text(encoding="utf-8") or DEFAULT_CORE
                except OSError:
                    self._core = DEFAULT_CORE
                self._core_stat = key
            return self._core

    def prompt(self) -> str:
        """CORE, then SELF (if it has anything in it)."""
        core = self.core()
        try:
            own = self.store.prompt()
        except Exception:                         # noqa: BLE001 - a broken SELF must never take the chat down
            own = ""
        return core.rstrip() + ("\n\n" + own if own else "")
