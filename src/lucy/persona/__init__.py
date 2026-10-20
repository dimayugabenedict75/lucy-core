"""Lucy's persona that grows: CORE (human-owned) + SELF (agent-written), plus the reflection job.

`get_store()` / `get_loader()` give the process-wide instances (paths come from lucy.paths); tests swap them with
`set_store()`. Not a tool module and not a constitution file: the protections live in guard.py / self_store.py and
in the hash-locked CORE file.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from lucy import paths as _paths

from .loader import PersonaLoader
from .self_store import SelfError, SelfStore

_store: Optional[SelfStore] = None
_loader: Optional[PersonaLoader] = None


def self_path() -> Path:
    return _paths.ROOT / ".core" / "personas" / "lucy_self.md"


def get_store() -> SelfStore:
    global _store
    if _store is None:
        _store = SelfStore(self_path())
    return _store


def set_store(store: Optional[SelfStore]) -> None:
    global _store, _loader
    _store, _loader = store, None


def get_loader() -> PersonaLoader:
    global _loader
    if _loader is None:
        _loader = PersonaLoader(_paths.PERSONA_PATH, get_store())
    return _loader


__all__ = ["SelfError", "SelfStore", "PersonaLoader", "get_store", "set_store", "get_loader", "self_path"]
