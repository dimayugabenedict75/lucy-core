"""Proactive-system configuration.

Three layers, later wins:
    1. DEFAULTS below                       (always complete)
    2. <root>/.core/proactive.yaml          (edit by hand, or Lucy can with edit_local_file)
    3. runtime overrides in proactive.sqlite (changed live through the proactive_control tool / API)

`Config.data` always returns the merged dict; it is rebuilt when the yaml file or an override changes.
Also here: the small time helpers every other module shares (timezone resolution, quiet hours,
rest days), so there is exactly one definition of "what time is it for the user".
"""

from __future__ import annotations

import copy
import logging
import re
import threading
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger("lucy.core.proactive")

DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "user_name": "Benny",
    "startup_delay_seconds": 5,
    "tick_seconds": 10,
    "watch_interval_seconds": 60,
    "quiet_hours": {"enabled": True, "start": "23:00", "end": "07:00"},
    "rest_days": ["thursday"],
    "reminders": {
        "compose": "template",             # template = instant, no model call | llm = Lucy rephrases (costs a model call)
        "ignore_quiet_hours": True,        # a reminder you asked for fires even at night
        "min_interval_minutes": 10,        # shortest allowed "every N minutes"
        "max_active": 200,
        "catchup_recurring_minutes": 180,  # a repeating reminder missed by more than this is skipped, not replayed
    },
    "delivery": {
        "session": "active",               # active = the most recently used chat | default | <session id>
        "voice": "off",                    # off | reminders | all  (speaks through Lucy Audio on this PC)
        "sticker": True,
        "sticker_chance": 0.35,
        "stickers": {
            "greeting": ["Happy", "Cozy", "Hopeful"],
            "checkin": ["Cozy", "Confused", "Hopeful"],
            "alert": ["Annoyed", "Shocked"],
            "task": ["Smug", "Confident"],
            "reminder": [],
        },
        "max_defer_seconds": 180,          # how long a reminder waits for a running chat reply to finish
    },
    "limits": {
        "max_per_hour": 3,                 # unprompted messages (greeting / check-in / alert); reminders are exempt
        "max_per_day": 8,
        "ignored_backoff": True,           # every unanswered message doubles the quiet time before the next
        "stop_after_ignored": 4,           # this many unanswered in a row -> no more check-ins until you write
    },
    "greeting": {"enabled": True, "min_gap_hours": 6, "compose": "llm"},
    "checkin": {
        "enabled": True,
        "idle_minutes": 180,
        "idle_max_hours": 48,
        "hours": ["10:00", "20:00"],
        "max_per_day": 1,
        "cooldown_minutes": 240,
        "skip_cooldown_minutes": 120,      # after the model decides there is nothing worth saying
        "on_rest_days": False,
    },
    "alerts": {
        "enabled": True,
        "model_error": True,
        "model_error_after_seconds": 90,
        "disk_free_gb": 5,
        "log_mb": 50,
        "cooldown_hours": 6,
    },
    "tasks": {
        "enabled": True,
        "allowed_tools": ["web_search", "web_extract", "read_local_file", "list_local_files", "find_file",
                          "recall_memory", "search_conversations"],
        "max_rounds": 6,
        "timeout_seconds": 300,
        "retry_minutes": 10,
        "max_attempts": 3,
        "max_runs_per_day": 6,
    },
    "jobs": {                              # P8 task queue (lucy.autonomy): long / repeating / idle-time work through the agent loop
        "enabled": True,
        "read_tools": ["web_search", "web_extract", "read_local_file", "list_local_files", "find_file", "recall_memory",
                       "search_conversations"],      # what an `access: read` job may use
        "blocked_tools": ["task", "core_change", "user_tool", "proactive_control"],   # no job spawns jobs or edits Lucy itself
        "default_budget": {"seconds": 1200, "tool_calls": 60, "tokens": 80000},
        "max_budget": {"seconds": 14400, "tool_calls": 600, "tokens": 800000},
        "max_runs_per_day": 8,             # started goal-runs per day (the user's "Run now" / Continue ignore the caps)
        "max_tool_calls_per_day": 400,
        "max_seconds_per_day": 10800,
        "max_active": 30,
        "max_attempts": 3,                 # a run that errors is retried (from its checkpoint) this many times
        "retry_minutes": 10,
        "park_timeout_hours": 48,          # an unanswered approval counts as a denial after this long
        "auto_start_model_full": False,    # False: a task the MODEL made with full tools waits for the user's OK
        "idle_start_minutes": 10,          # "idle" tasks start after this much quiet
        "poll_seconds": 1.0,
        "report_max_chars": 1200,
        "keep_days": 60,
        "system_jobs": True,               # reflection + weekly trace review run on the queue (false = their own loops)
    },
    "llm": {"timeout_seconds": 45, "max_tokens": 200, "temperature": 0.7, "idle_before_llm_seconds": 20},
}

# Runtime-changeable settings (path -> validator). Anything else is yaml-only.
def _bool(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, str) and v.strip().lower() in ("true", "false", "yes", "no", "on", "off", "1", "0"):
        return v.strip().lower() in ("true", "yes", "on", "1")
    raise ValueError("expected true/false")


def _hhmm(v: Any) -> str:
    h, m = parse_hhmm(v)
    return f"{h:02d}:{m:02d}"


def _choice(*opts: str) -> Callable[[Any], str]:
    def check(v: Any) -> str:
        if str(v).lower() not in opts:
            raise ValueError(f"expected one of {', '.join(opts)}")
        return str(v).lower()
    return check


SETTABLE: dict[str, Callable[[Any], Any]] = {
    "quiet_hours.enabled": _bool, "quiet_hours.start": _hhmm, "quiet_hours.end": _hhmm,
    "greeting.enabled": _bool, "checkin.enabled": _bool, "alerts.enabled": _bool, "tasks.enabled": _bool, "jobs.enabled": _bool,
    "delivery.voice": _choice("off", "reminders", "all"), "delivery.sticker": _bool,
}


def deep_merge(base: dict, extra: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (extra or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def _set_path(d: dict, path: str, value: Any) -> None:
    cur = d
    parts = path.split(".")
    for p in parts[:-1]:
        cur = cur.setdefault(p, {})
    cur[parts[-1]] = value


class Config:
    """Merged view of defaults + yaml + runtime overrides."""

    def __init__(self, yaml_path: Path, get_overrides: Callable[[], dict] | None = None):
        self.yaml_path = Path(yaml_path)
        self._get_overrides = get_overrides or (lambda: {})
        self._lock = threading.RLock()
        self._cache: dict | None = None
        self._stamp: tuple | None = None

    def _yaml(self) -> dict:
        try:
            import yaml
            raw = yaml.safe_load(self.yaml_path.read_text(encoding="utf-8")) or {}
            return raw if isinstance(raw, dict) else {}
        except FileNotFoundError:
            return {}
        except Exception as e:                       # a broken yaml must never stop the engine
            logger.warning(f"proactive.yaml ignored ({e}); using defaults")
            return {}

    @property
    def data(self) -> dict:
        with self._lock:
            try:
                mtime = self.yaml_path.stat().st_mtime
            except OSError:
                mtime = 0.0
            overrides = self._get_overrides() or {}
            stamp = (mtime, repr(sorted(overrides.items())))
            if self._cache is None or stamp != self._stamp:
                merged = deep_merge(DEFAULTS, self._yaml())
                for path, val in overrides.items():
                    if path in SETTABLE:
                        _set_path(merged, path, val)
                self._cache, self._stamp = merged, stamp
            return self._cache

    def get(self, path: str, default: Any = None) -> Any:
        cur: Any = self.data
        for p in path.split("."):
            if not isinstance(cur, dict) or p not in cur:
                return default
            cur = cur[p]
        return cur


# ------------------------------------------------------------------------------------ time helpers
_HHMM = re.compile(r"^\s*(\d{1,2})(?::(\d{2}))?\s*$")


def parse_hhmm(value: Any) -> tuple[int, int]:
    m = _HHMM.match(str(value))
    if not m:
        raise ValueError(f"'{value}' is not a time like 23:00")
    h, mi = int(m.group(1)), int(m.group(2) or 0)
    if not (0 <= h <= 23 and 0 <= mi <= 59):
        raise ValueError(f"'{value}' is not a valid time of day")
    return h, mi


def local_tz() -> tzinfo:
    """This PC's timezone (Windows date / time / timezone settings). There is no timezone setting in Lucy."""
    return datetime.now().astimezone().tzinfo or timezone.utc


def tz_label(dt: datetime) -> str:
    off = dt.utcoffset() or timedelta(0)
    mins = int(off.total_seconds() // 60)
    sign = "+" if mins >= 0 else "-"
    mins = abs(mins)
    return f"UTC{sign}{mins // 60:02d}:{mins % 60:02d}"


def in_quiet_hours(dt: datetime, start: str, end: str) -> bool:
    """True if dt's clock time is inside [start, end). Handles windows that wrap midnight (23:00 -> 07:00)."""
    sh, sm = parse_hhmm(start)
    eh, em = parse_hhmm(end)
    now_m, s_m, e_m = dt.hour * 60 + dt.minute, sh * 60 + sm, eh * 60 + em
    if s_m == e_m:
        return False
    if s_m < e_m:
        return s_m <= now_m < e_m
    return now_m >= s_m or now_m < e_m


def quiet_ends_at(dt: datetime, end: str) -> datetime:
    """The next moment the clock reads `end` (used to say when a deferred message can go out)."""
    eh, em = parse_hhmm(end)
    cand = dt.replace(hour=eh, minute=em, second=0, microsecond=0)
    if cand <= dt:
        cand += timedelta(days=1)
    return cand


_DAYS = {"mon": 0, "monday": 0, "tue": 1, "tues": 1, "tuesday": 1, "wed": 2, "wednesday": 2, "thu": 3, "thur": 3,
         "thurs": 3, "thursday": 3, "fri": 4, "friday": 4, "sat": 5, "saturday": 5, "sun": 6, "sunday": 6}


def weekday_index(name: str) -> int | None:
    return _DAYS.get(str(name).strip().lower())


def is_rest_day(dt: datetime, rest_days: list[str] | None) -> bool:
    idx = {weekday_index(d) for d in (rest_days or [])}
    return dt.weekday() in idx


def period_of_day(dt: datetime) -> str:
    h = dt.hour
    return "morning" if 5 <= h < 12 else "afternoon" if 12 <= h < 18 else "evening" if 18 <= h < 23 else "night"
