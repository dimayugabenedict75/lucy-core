"""The one engine instance of this process (set by proactive.init, read by tools / routes / api.py)."""

from __future__ import annotations

_engine = None


def set_engine(e) -> None:
    global _engine
    _engine = e


def get_engine():
    return _engine
