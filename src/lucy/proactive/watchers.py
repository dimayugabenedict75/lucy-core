"""Things Lucy notices on her own. Each watcher looks at the situation and may propose a Candidate;
the engine then asks the Policy whether she is allowed to say it and has the model (or a template) word it.

  greeting   - a chat window opened after a long absence
  checkin    - quiet for a long while in the daytime; the model decides if there is anything worth saying
  alerts     - the model failed to load, the disk is nearly full, the log has grown huge

Watchers never talk to the model or the user directly and never raise: they are cheap, synchronous looks.
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

from .config import parse_hhmm, period_of_day
from .policy import Policy
from .store import Store
from .compose import recent_tail

logger = logging.getLogger("lucy.core.proactive")


@dataclass
class Candidate:
    kind: str                       # greeting | checkin | alert
    key: str = ""                   # dedupe/cooldown key
    facts: dict = field(default_factory=dict)
    text: str | None = None         # already written (alerts); None = compose it
    cooldown_s: float = 0.0         # silence for this key after it is sent
    llm_allowed: bool = True


@dataclass
class Ctx:
    now: float
    dt: datetime
    cfg: dict
    store: Store
    policy: Policy
    ui_clients: int
    ui_connected_for: float
    last_user_ts: float
    session: str | None
    recent: Callable[[str, int], list]
    health: dict
    root: Path
    runtime: Path
    memory: Callable[[str], str]
    state: dict                     # watcher-private memory that lives as long as the engine


def _cooling(ctx: Ctx, kind: str, key: str) -> bool:
    return ctx.now < float(ctx.store.kv_get(f"cool:{kind}:{key}", 0) or 0)


# ------------------------------------------------------------------------------------ greeting
def greeting(ctx: Ctx) -> Candidate | None:
    g = ctx.cfg["greeting"]
    if not g.get("enabled") or ctx.ui_clients <= 0 or ctx.ui_connected_for < 2:
        return None
    gap = float(g["min_gap_hours"]) * 3600
    if ctx.now - ctx.last_user_ts < gap or _cooling(ctx, "greeting", ""):
        return None
    end_of_day = ctx.dt.replace(hour=23, minute=59, second=59).timestamp()
    ahead = [f"{datetime.fromtimestamp(r['due_ts'], ctx.dt.tzinfo).strftime('%H:%M')} {r['text'][:60]}"
             for r in ctx.store.active_between(ctx.now, end_of_day, 5) if r["kind"] == "reminder"]
    hours_away = round((ctx.now - ctx.last_user_ts) / 3600) if ctx.last_user_ts else 0
    facts = {"name": ctx.cfg["user_name"], "period": period_of_day(ctx.dt), "weekday": ctx.dt.strftime("%A"),
             "date": f"{ctx.dt.day} {ctx.dt.strftime('%b')}", "rest_day": ctx.policy.is_rest_day(ctx.dt),
             "reminders_today": ahead, "hours_away": hours_away if hours_away < 24 * 14 else 0}
    return Candidate("greeting", "", facts, cooldown_s=gap, llm_allowed=g.get("compose", "llm") == "llm")


# ------------------------------------------------------------------------------------ check-in
def checkin(ctx: Ctx) -> Candidate | None:
    c = ctx.cfg["checkin"]
    if not c.get("enabled") or ctx.ui_clients <= 0 or not ctx.session:
        return None
    idle = ctx.now - ctx.last_user_ts
    if idle < float(c["idle_minutes"]) * 60 or idle > float(c["idle_max_hours"]) * 3600:
        return None
    lo, hi = c["hours"]
    h_lo, h_hi = parse_hhmm(lo), parse_hhmm(hi)
    now_m = ctx.dt.hour * 60 + ctx.dt.minute
    if not (h_lo[0] * 60 + h_lo[1] <= now_m < h_hi[0] * 60 + h_hi[1]):
        return None
    if _cooling(ctx, "checkin", ""):
        return None
    msgs = ctx.recent(ctx.session, 8)
    if not msgs:
        return None                                  # nothing to follow up on
    last_user = next((m["content"] for m in reversed(msgs) if m.get("role") == "user"), "")
    mins = int(idle // 60)
    facts = {"name": ctx.cfg["user_name"], "period": period_of_day(ctx.dt), "weekday": ctx.dt.strftime("%A"),
             "idle": f"{mins // 60}h {mins % 60}m" if mins >= 60 else f"{mins} minutes",
             "rest_day": ctx.policy.is_rest_day(ctx.dt), "tail": recent_tail(msgs),
             "memory": (ctx.memory(str(last_user)[:200]) or "")[:500]}
    return Candidate("checkin", "", facts, cooldown_s=float(c["skip_cooldown_minutes"]) * 60)


# ------------------------------------------------------------------------------------ alerts
def alerts(ctx: Ctx) -> list[Candidate]:
    a = ctx.cfg["alerts"]
    if not a.get("enabled"):
        return []
    out: list[Candidate] = []
    cool = float(a["cooldown_hours"]) * 3600

    # the model failed to load (and stayed failed)
    h = ctx.health or {}
    if a.get("model_error") and h.get("llm_phase") == "error":
        first = ctx.state.setdefault("model_error_since", ctx.now)
        if ctx.now - first >= float(a["model_error_after_seconds"]) and not _cooling(ctx, "alert", "model_error"):
            err = str(h.get("llm_error") or "no details").strip().splitlines()[0][:140]
            out.append(Candidate("alert", "model_error", {"text": f"{h.get('llm_label', 'The model')} failed to load: {err}. "
                                                                  "Try again from Settings > Model."},
                                 text=None, cooldown_s=cool, llm_allowed=False))
    else:
        ctx.state.pop("model_error_since", None)

    # low disk
    try:
        free_gb = shutil.disk_usage(ctx.root).free / 1e9
        if free_gb < float(a["disk_free_gb"]) and not _cooling(ctx, "alert", "disk"):
            out.append(Candidate("alert", "disk", {"text": f"Only **{free_gb:.1f} GB** free on this drive. Models and logs need room."},
                                 cooldown_s=cool, llm_allowed=False))
    except OSError:
        pass

    # runaway log
    try:
        log = ctx.runtime / "lucy_core.log"
        mb = log.stat().st_size / 1e6 if log.exists() else 0
        if mb >= float(a["log_mb"]) and not _cooling(ctx, "alert", "log"):
            out.append(Candidate("alert", "log", {"text": f"`runtime/lucy_core.log` is **{mb:.0f} MB**. Worth trimming."},
                                 cooldown_s=cool * 4, llm_allowed=False))
    except OSError:
        pass
    return out
