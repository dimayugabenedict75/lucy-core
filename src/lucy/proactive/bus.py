"""In-process fan-out of proactive events to open chat windows (the /api/proactive/stream SSE endpoint)."""

from __future__ import annotations

import asyncio
import threading
from collections import deque
from typing import Any


class EventBus:
    def __init__(self, history: int = 50):
        self._subs: set[asyncio.Queue] = set()
        self._lock = threading.Lock()
        self._recent: deque = deque(maxlen=history)
        self.closed = False

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=100)
        with self._lock:
            self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        with self._lock:
            self._subs.discard(q)

    def count(self) -> int:
        with self._lock:
            return len(self._subs)

    def publish(self, event: dict[str, Any]) -> int:
        """Send to every open window. Returns how many received it (0 = nobody is looking)."""
        self._recent.append(event)
        with self._lock:
            subs = list(self._subs)
        delivered = 0
        for q in subs:
            try:
                q.put_nowait(event)
                delivered += 1
            except asyncio.QueueFull:
                pass                                   # a stuck window must not block the engine
        return delivered

    def recent_after(self, last_id: int) -> list[dict]:
        return [e for e in self._recent if e.get("id", 0) > last_id]

    def close(self) -> None:
        """Wake every stream so it can end (server shutdown)."""
        self.closed = True
        with self._lock:
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait(None)
            except asyncio.QueueFull:
                pass
