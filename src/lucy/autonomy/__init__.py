"""P8 autonomy: a persistent job queue for long-running work (lucy.autonomy).

    store.py    SQLite queue (runtime/data/jobs.sqlite): jobs, runs, progress log, one pending approval per job
    budget.py   time / tool-call / token budgets (defaults modest, clamped)
    gate.py     the permission gate for unattended runs: asks become a PARKED approval, never a block
    runner.py   JobRunner: started by the proactive engine's tick; runs each job through the SAME AgentLoop as chat
    tools.py    the model-facing `task` tool (add | list | status | cancel | resume)
    web.py      /api/jobs/* routes for the Tasks panel (the USER's decisions: approve, deny, run now, extend)

The proactive engine is the scheduler (quiet hours, rest days, pause, "busy" all come from it).
Nothing here edits a constitution file: the safety layer is only CALLED (decide / describe / gate), never changed.
"""

from __future__ import annotations

from .budget import Budget
from .runner import BUILTIN_LABELS, JobHostHooks, JobRunner
from .store import JobStore, STATUS_LABELS
from .tools import TOOL_DEFS, TOOL_NAMES, run_tool
from .web import router

__all__ = ["Budget", "JobHostHooks", "JobRunner", "JobStore", "STATUS_LABELS", "BUILTIN_LABELS", "TOOL_DEFS",
           "TOOL_NAMES", "run_tool", "router", "get_runner"]


def get_runner():
    """The runner of the running proactive engine (None before startup)."""
    try:
        from lucy.proactive import get_engine
        eng = get_engine()
        return getattr(eng, "jobs", None) if eng else None
    except Exception:                                   # noqa: BLE001
        return None
