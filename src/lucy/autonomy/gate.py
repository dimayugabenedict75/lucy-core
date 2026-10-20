"""The permission gate for UNATTENDED runs.

In chat, a call that needs the user's OK shows a card and the turn waits. A background job has nobody watching, and a
job parked for hours must not hold a model slot or a worker, so instead of waiting it is PARKED:

    gate -> the layer says "ask" -> the run records the pending call (a card for the Tasks panel) and stops;
            the job is saved (checkpoint) and the user answers whenever they like;
            on resume the harness performs the approved call itself (or tells the model it was denied).

Everything that is NOT an "ask" goes straight to the real safety layer (`layer.gate` with no approver): auto calls run,
hard denials / pause / a broken constitution are refused, and every decision is audited exactly as in chat.
This file only CALLS the safety layer (decide / describe / gate); it changes nothing in it.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Callable, Optional

from lucy.safety.layer import CURRENT, _sig
from lucy.safety.policy import ASK, ASK_HARD

Card = dict


def call_sig(name: str, args: dict) -> str:
    """Identity of one tool call (same args -> same signature)."""
    return name + ":" + json.dumps(args or {}, sort_keys=True, default=str, ensure_ascii=False)


class JobGate:
    """One per run. Instances are callable with the AgentLoop gate signature: `await gate(name, args, emit)`."""

    def __init__(self, *, layer_fn: Callable[[], Any], tier_fn: Callable[[str], str], session: str,
                 on_park: Callable[[Card], None], blocked: frozenset = frozenset(),
                 only: Optional[frozenset] = None, approved: Optional[set] = None, denied: Optional[dict] = None):
        self.layer_fn, self.tier_fn, self.session, self.on_park = layer_fn, tier_fn, session, on_park
        self.blocked, self.only = blocked, only
        self.approved: set = approved if approved is not None else set()
        self.denied: dict = denied if denied is not None else {}

    def _not_available(self, name: str) -> Optional[str]:
        if name in self.blocked:
            return (f"Not available in a background job: {name}. Do the rest of the job without it and say in your "
                    "report what you could not do.")
        if self.only is not None and name not in self.only:
            return f"Error: {name} is not available in this background job (read-only tools only)."
        return None

    async def __call__(self, name: str, args: dict, emit=None) -> Optional[str]:
        na = self._not_available(name)
        if na:
            return na
        layer = self.layer_fn()
        if layer.paused or layer.halt_reason():
            return await layer.gate(name, args, None, self.session)         # a refusal, audited by the layer
        d = layer.decide(name, self.tier_fn(name), args, self.session)
        if d.level in (ASK, ASK_HARD):
            sig = call_sig(name, args)
            if sig in self.denied:
                return self.denied[sig]
            if sig in self.approved:                                         # the user said yes to exactly this call
                CURRENT.set({"sig": _sig(name, args), "decision": "approved", "by": "user (tasks panel)",
                             "reason": d.reason})
                return None
            card = dict(layer.describe(name, args, d))
            card.update({"sig": sig, "args": args or {}, "level": "hard" if d.level == ASK_HARD else "ask"})
            self.on_park(card)
            await asyncio.Event().wait()                                    # never returns: the runner stops this run
        return await layer.gate(name, args, None, self.session)

    def mark_approved(self, name: str, args: dict, reason: str = "") -> None:
        """Used when the HARNESS runs an approved call on resume: tells the audit line who decided."""
        CURRENT.set({"sig": _sig(name, args), "decision": "approved", "by": "user (tasks panel)", "reason": reason})
