"""Lucy's proactive system: reminders, scheduled follow-up tasks, greetings, check-ins and alerts.

    from lucy import proactive
    proactive.init(ProactiveHost(...))      # once, from api.py
    await proactive.start() / stop()        # on server startup / shutdown
    proactive.touch(session_id)             # whenever the user is active
    proactive.TOOL_DEFS / run_tool(...)     # the model-facing tools

See .core/harness.md ("Proactive system") for the overview.
"""

from __future__ import annotations

import logging

from .chat_format import normalize_history
from .engine import ProactiveEngine, UserError
from .host import ProactiveHost
from .runtime import get_engine, set_engine
from .tools import TOOL_DEFS, TOOL_NAMES, run_tool
from .web import inject_client, public_router, router

logger = logging.getLogger("lucy.core.proactive")

__all__ = ["ProactiveEngine", "ProactiveHost", "UserError", "TOOL_DEFS", "TOOL_NAMES", "run_tool", "router",
           "public_router", "inject_client", "normalize_history", "init", "start", "stop", "touch", "now_note", "wants_clock",
           "get_engine"]


def init(host: ProactiveHost) -> ProactiveEngine:
    eng = ProactiveEngine(host)
    set_engine(eng)
    return eng


async def start() -> None:
    eng = get_engine()
    if eng:
        await eng.start()


async def stop() -> None:
    eng = get_engine()
    if eng:
        await eng.stop()


def touch(session_id: str | None = None) -> None:
    """Never raises: called from the chat hot path."""
    try:
        eng = get_engine()
        if eng:
            eng.touch(session_id)
    except Exception:                                  # noqa: BLE001
        pass


def now_note() -> str:
    try:
        eng = get_engine()
        return eng.now_note() if eng else ""
    except Exception:                                  # noqa: BLE001
        return ""


_TIME_HINT = None


def wants_clock(text: str) -> bool:
    """Only attach the clock note when the message is plausibly about time: elsewhere it is noise the model
    may comment on ("the internal note you provided..."). Reminders get their times parsed by the harness."""
    global _TIME_HINT
    if _TIME_HINT is None:
        import re
        _TIME_HINT = re.compile(
            r"\b(?:time|clock|date|today|tonight|tomorrow|yesterday|now|later|soon|morning|afternoon|evening|night|"
            r"week|weekend|month|year|day|hour|minute|second|deadline|schedule|remind|reminder|alarm|timer|"
            r"mon(?:day)?|tue(?:s|sday)?|wed(?:nesday)?|thu(?:r|rs|rsday)?|fri(?:day)?|sat(?:urday)?|sun(?:day)?|"
            r"\d{1,2}\s*(?:am|pm)|\d{1,2}:\d{2}|o'?clock|ago|when|how long|until|before|after)\b", re.IGNORECASE)
    return bool(_TIME_HINT.search(text or ""))
