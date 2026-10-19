"""Pending approval requests: the agent loop waits on a future, the UI resolves it (Approve / Deny / Always allow)."""
from __future__ import annotations

import asyncio
import time
import uuid
from typing import Dict, List, Optional

DECISIONS = ("approve", "deny", "always")


class ApprovalBroker:
    def __init__(self) -> None:
        self._pending: Dict[str, dict] = {}

    def create(self, session: Optional[str], payload: dict) -> str:
        aid = "ap_" + uuid.uuid4().hex[:12]
        fut = asyncio.get_running_loop().create_future()
        self._pending[aid] = {"id": aid, "session": session, "created": time.time(), "payload": payload, "future": fut}
        return aid

    async def wait(self, aid: str, timeout: float) -> str:
        """-> 'approve' | 'always' | 'deny' | 'timeout'. Cancelling the waiting task (Stop button) removes it."""
        item = self._pending.get(aid)
        if item is None:
            return "deny"
        try:
            return await asyncio.wait_for(asyncio.shield(item["future"]), timeout=timeout)
        except asyncio.TimeoutError:
            return "timeout"
        finally:
            self._pending.pop(aid, None)

    def resolve(self, aid: str, decision: str, session: Optional[str] = None) -> bool:
        if decision not in DECISIONS:
            raise ValueError(f"decision must be one of {DECISIONS}")
        item = self._pending.get(aid)
        if item is None or item["future"].done():
            return False
        if session is not None and item["session"] not in (None, session):
            return False
        item["future"].get_loop().call_soon_threadsafe(
            lambda: item["future"].done() or item["future"].set_result(decision))
        return True

    def pending(self, session: Optional[str] = None) -> List[dict]:
        return [{"id": i["id"], "session": i["session"], **i["payload"]} for i in self._pending.values()
                if session is None or i["session"] == session]

    def cancel_all(self) -> None:
        for i in list(self._pending.values()):
            if not i["future"].done():
                i["future"].get_loop().call_soon_threadsafe(lambda f=i["future"]: f.done() or f.set_result("deny"))
