"""Etiquette: when Lucy may speak first, and when she must keep quiet.

Two classes of message:

  EXPLICIT   (reminder, task)  - the user asked for these. They fire on time, even in quiet hours
                                 (tasks wait for morning - they use the model), unless the user chose
                                 pause scope "all".
  INITIATIVE (greeting, checkin, alert) - Lucy's own idea. Held back by: pause, per-kind mute, quiet
                                 hours, rest days (check-ins), no open chat window, a running chat reply,
                                 hourly/daily caps, per-kind cooldown, and a back-off that doubles the
                                 silence after every message the user ignored.

`check()` returns a Verdict with a short machine-readable reason; every refusal is logged by the engine.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from .config import in_quiet_hours, is_rest_day
from .store import INITIATIVE_KINDS, Store

EXPLICIT_KINDS = ("reminder", "task")
ALL_KINDS = EXPLICIT_KINDS + INITIATIVE_KINDS + ("test",)
MUTABLE_KINDS = INITIATIVE_KINDS            # kinds the user can mute


@dataclass
class Verdict:
    ok: bool
    reason: str = ""

    def __bool__(self) -> bool:
        return self.ok


class Policy:
    def __init__(self, cfg: Callable[[], dict], store: Store, clock: Callable[[], float],
                 now_local: Callable[[], datetime]):
        self._cfg, self.store, self.clock, self.now_local = cfg, store, clock, now_local

    # ------------------------------------------------------------------ state
    def pause_state(self) -> tuple[bool, str, float]:
        until = float(self.store.kv_get("paused_until", 0) or 0)
        if until > self.clock():
            return True, str(self.store.kv_get("pause_scope", "initiative")), until
        return False, "initiative", 0.0

    def muted_kinds(self) -> list[str]:
        return list(self.store.kv_get("muted_kinds", []) or [])

    def is_quiet(self, dt: datetime | None = None) -> bool:
        q = self._cfg()["quiet_hours"]
        return bool(q.get("enabled")) and in_quiet_hours(dt or self.now_local(), q["start"], q["end"])

    def is_rest_day(self, dt: datetime | None = None) -> bool:
        return is_rest_day(dt or self.now_local(), self._cfg().get("rest_days"))

    def ignored_streak(self) -> int:
        return self.store.trailing_ignored(self.clock() - 30 * 60)

    def backoff_factor(self) -> int:
        lim = self._cfg()["limits"]
        if not lim.get("ignored_backoff"):
            return 1
        return 2 ** min(self.ignored_streak(), 3)

    def local_midnight_ts(self) -> float:
        dt = self.now_local()
        return dt.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()

    # ------------------------------------------------------------------ the decision
    def check(self, kind: str, *, ui_connected: bool, busy: bool) -> Verdict:
        cfg = self._cfg()
        if not cfg.get("enabled", True):
            return Verdict(False, "disabled")
        paused, scope, _ = self.pause_state()

        if kind in EXPLICIT_KINDS or kind == "test":
            if kind == "test":
                return Verdict(True)
            if paused and scope == "all":
                return Verdict(False, "paused")
            if self.is_quiet() and (kind == "task" or not cfg["reminders"].get("ignore_quiet_hours", True)):
                return Verdict(False, "quiet_hours")
            if busy:
                return Verdict(False, "busy")
            return Verdict(True)

        # ---- initiative
        if kind not in INITIATIVE_KINDS:
            return Verdict(False, "unknown_kind")
        if paused:
            return Verdict(False, "paused")
        if kind in self.muted_kinds():
            return Verdict(False, "muted")
        if self.is_quiet():
            return Verdict(False, "quiet_hours")
        if kind in ("greeting", "checkin") and not ui_connected:
            return Verdict(False, "no_ui")
        if busy:
            return Verdict(False, "busy")
        if kind == "checkin":
            if self.is_rest_day() and not cfg["checkin"].get("on_rest_days", False):
                return Verdict(False, "rest_day")
            streak = self.ignored_streak()
            stop = int(cfg["limits"].get("stop_after_ignored", 4))
            if cfg["limits"].get("ignored_backoff") and stop and streak >= stop:
                return Verdict(False, "ignored")
        now = self.clock()
        if self.store.count_sent(now - 3600) >= int(cfg["limits"]["max_per_hour"]):
            return Verdict(False, "rate_hour")
        if self.store.count_sent(self.local_midnight_ts()) >= int(cfg["limits"]["max_per_day"]):
            return Verdict(False, "rate_day")
        if kind == "checkin":
            ck = cfg["checkin"]
            if self.store.count_sent(self.local_midnight_ts(), ("checkin",)) >= int(ck["max_per_day"]):
                return Verdict(False, "rate_day")
            last = self.store.last_event("checkin")
            cool = float(ck["cooldown_minutes"]) * 60 * self.backoff_factor()
            if last and now - last["ts"] < cool:
                return Verdict(False, "cooldown")
        return Verdict(True)
