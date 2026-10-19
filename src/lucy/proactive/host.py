"""The seam between the proactive system and the rest of Lucy Core.

The proactive package never imports api.py (that would be circular and impossible to test).
api.py builds one ProactiveHost - a bundle of callables - and hands it to `proactive.init()`.
Every field has a harmless default, so tests (and a half-wired install) still run.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable


async def _false() -> bool:
    return False


@dataclass
class ProactiveHost:
    root: Path
    runtime: Path
    # --- chat storage -------------------------------------------------------------------------------
    append_message: Callable[[str, str, str], None]                  # (session_id, role, content)
    pick_session: Callable[[], str] = lambda: "default"              # where unprompted messages go
    recent_messages: Callable[[str, int], list] = lambda sid, n: []  # [{"role","content"}], oldest first
    # --- state of the rest of the system ------------------------------------------------------------
    is_busy: Callable[[], bool] = lambda: False                      # a chat reply is being generated right now
    paused: Callable[[], bool] = lambda: False                       # the user pressed "Pause agent" (safety layer)
    health: Callable[[], dict] = lambda: {}                          # {"llm_phase","llm_error","llm_label"}
    app_timezone: Callable[[], str] = lambda: "system"               # Settings > Timezone value
    # --- model -----------------------------------------------------------------------------------------
    persona: Callable[[], str] = lambda: "You are Lucy, a companion."
    llm_ready: Callable[[], Awaitable[bool]] = _false                # model is up and idle-able (never start it for us)
    llm_complete: Callable[[list, dict], Awaitable[str]] | None = None
    run_agent: Callable[[list, list, float, int], Awaitable[str]] | None = None   # (messages, allowed tools, timeout, max_rounds)
    memory_context: Callable[[str], str] = lambda q: ""
    # --- extras ----------------------------------------------------------------------------------------
    sticker: Callable[[str], str | None] = lambda emotion: None      # -> relative media path or None
    speak: Callable[[str], Awaitable[None]] | None = None            # speak through Lucy Audio on this PC
