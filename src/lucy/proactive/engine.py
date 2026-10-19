"""The proactive engine: one background loop that makes Lucy able to speak first.

Every `tick_seconds` it
  1. fires reminders / runs scheduled tasks that are due          (explicit - the user asked for them)
  2. every `watch_interval_seconds`, lets the watchers propose greetings, check-ins and alerts
     (initiative - gated by Policy: quiet hours, pause, caps, back-off, ...)
and delivers whatever passes: appended to the chat session as an assistant message, pushed live to open
chat windows (SSE), optionally spoken through Lucy Audio.

Design rules
  * never start or wait on the model for a nicety; a missing model means a template or silence
  * never speak into a chat reply that is being generated (history order, single llama slot)
  * every refusal has a reason; everything sent is logged in proactive.sqlite with how the user reacted
  * one failing reminder/watcher must never stop the loop
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from datetime import datetime
from pathlib import Path
from typing import Callable

from . import compose as _compose
from . import watchers as _watchers
from .bus import EventBus
from .config import Config, SETTABLE, resolve_tz, tz_label
from .host import ProactiveHost
from .policy import MUTABLE_KINDS, Policy
from .schedule import describe_rule, make_rule, next_after
from .store import Store
from .timeparse import describe, humanize_delta, parse_when

logger = logging.getLogger("lucy.core.proactive")

KIND_TITLES = {"reminder": "Reminder", "task": "Follow-up", "greeting": "Lucy", "checkin": "Lucy",
               "alert": "Heads up", "test": "Test"}


class UserError(ValueError):
    """A problem the model/user can fix (bad time, unknown id...). The message is shown to them as-is."""


class ProactiveEngine:
    def __init__(self, host: ProactiveHost, *, store: Store | None = None, config_path: Path | None = None,
                 clock: Callable[[], float] = time.time, rng: random.Random | None = None):
        self.host = host
        self.clock = clock
        self.rng = rng or random.Random()
        self.store = store or Store(Path(host.runtime) / "data" / "proactive.sqlite")
        self.config = Config(config_path or (Path(host.root) / ".core" / "proactive.yaml"), self._overrides)
        self.bus = EventBus()
        self.policy = Policy(lambda: self.config.data, self.store, self.clock, self.now_local)
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._wake = asyncio.Event()
        self._deliver_lock = asyncio.Lock()
        self._last_watch = 0.0
        self._watch_state: dict = {}
        self._connected_at = 0.0
        self.ui_visible: bool | None = None
        self.last_user_ts: float = float(self.store.kv_get("last_user_ts", 0) or 0)
        self._last_persist = 0.0
        self._why: dict = {}                          # (kind, key) -> last refusal reason (log only on change)
        self.counters = {"sent": 0, "suppressed": 0, "errors": 0}
        self.started_at = 0.0

    # ================================================================== small helpers
    @property
    def cfg(self) -> dict:
        return self.config.data

    def _overrides(self) -> dict:
        try:
            return self.store.kv_get("overrides", {}) or {}
        except Exception:                              # noqa: BLE001
            return {}

    def tz(self):
        return resolve_tz(self.cfg.get("timezone"), self.host.app_timezone())

    def now_local(self, ts: float | None = None) -> datetime:
        return datetime.fromtimestamp(self.clock() if ts is None else ts, self.tz())

    def busy(self) -> bool:
        try:
            return bool(self.host.is_busy())
        except Exception:                              # noqa: BLE001
            return False

    def ui_clients(self) -> int:
        return self.bus.count()

    # ================================================================== lifecycle
    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stop.clear()
        self.bus.closed = False
        self.started_at = self.clock()
        self._task = asyncio.get_running_loop().create_task(self._loop(), name="lucy-proactive")
        logger.info(f"Proactive engine started (tz {tz_label(self.now_local())}, "
                    f"{self.store.count_active()} active reminder(s))")

    async def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        self.bus.close()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):    # noqa: BLE001
                pass
            self._task = None
        logger.info("Proactive engine stopped")

    def wake(self, watch: bool = False) -> None:
        if watch:
            self._last_watch = 0.0
        self._wake.set()

    async def _loop(self) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=float(self.cfg["startup_delay_seconds"]))
            return
        except asyncio.TimeoutError:
            pass
        while not self._stop.is_set():
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:                           # noqa: BLE001 - the loop must outlive any bug
                self.counters["errors"] += 1
                logger.exception("proactive tick failed")
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=float(self.cfg["tick_seconds"]))
            except asyncio.TimeoutError:
                pass
            self._wake.clear()

    async def tick(self) -> None:
        cfg = self.cfg
        if not cfg.get("enabled", True):
            return
        try:
            if self.host.paused():                  # "Pause agent": nothing proactive runs until the user resumes
                return
        except Exception:                           # noqa: BLE001
            pass
        now = self.clock()
        await self._process_due(now)
        if now - self._last_watch >= float(cfg["watch_interval_seconds"]):
            self._last_watch = now
            await self._process_watchers(now)
        if now - float(self._watch_state.get("pruned", 0)) > 6 * 3600:
            self._watch_state["pruned"] = now
            self.store.prune(now - 90 * 86400)

    # ================================================================== user activity / presence
    def touch(self, session_id: str | None = None) -> None:
        """The user is using the chat (called on every message and when a reply ends)."""
        now = self.clock()
        self.last_user_ts = now
        if now - self._last_persist > 20:
            self._last_persist = now
            try:
                self.store.kv_set("last_user_ts", now)
            except Exception:                           # noqa: BLE001
                pass
        try:
            self.store.mark_engaged(0)                 # any message from the user ends an ignored streak
        except Exception:                               # noqa: BLE001
            pass

    def client_connected(self) -> None:
        self._connected_at = self.clock()
        self.ui_visible = True
        self.wake(watch=False)
        loop = asyncio.get_running_loop()
        loop.call_later(3.0, lambda: self.wake(watch=True))      # let the page settle, then consider a greeting

    def client_disconnected(self) -> None:
        if self.bus.count() == 0:
            self.ui_visible = None

    def set_presence(self, visible: bool) -> None:
        self.ui_visible = bool(visible)

    # ================================================================== reminders API (tools + HTTP)
    def add_reminder(self, *, text: str, when: str, kind: str = "reminder", repeat: str | None = None,
                     every_minutes: int | None = None, session: str | None = None,
                     replaces_id: int | None = None) -> dict:
        text = " ".join(str(text or "").split())
        if not text:
            raise UserError("The reminder text is empty.")
        if len(text) > 800:
            raise UserError("That is too long for a reminder (800 characters max).")
        now_dt = self.now_local()
        due = parse_when(str(when or ""), now_dt)
        if due is None:
            raise UserError(f"I could not understand the time '{when}'. Use something like 'in 20 minutes', "
                            "'tomorrow 9am', 'friday 5pm', 'oct 20 8:30' or '2026-10-14 09:00'. "
                            "Ask the user for a clearer time instead of guessing.")
        if due.timestamp() < self.clock() - 30:
            raise UserError(f"That time ({describe(due, now_dt)}) is already in the past. It is now "
                            f"{now_dt.strftime('%a %H:%M')} ({tz_label(now_dt)}).")
        try:
            rule = make_rule(repeat, every_minutes, due, int(self.cfg["reminders"]["min_interval_minutes"]))
        except ValueError as e:
            raise UserError(str(e))
        if replaces_id not in (None, "", 0):
            old = self.store.get_reminder(int(replaces_id))
            if not old or old["status"] != "active":
                raise UserError(f"There is no active reminder #{replaces_id} to replace.")
            self.store.update_reminder(old["id"], text=text, due_ts=due.timestamp(), rule=rule, kind=kind,
                                       attempts=0, last_error=None)
            rid, replaced = old["id"], True
        else:
            if self.store.count_active() >= int(self.cfg["reminders"]["max_active"]):
                raise UserError("There are too many active reminders. Cancel some first.")
            rid, replaced = self.store.add_reminder(kind, text, due.timestamp(), rule, session, self.clock()), False
        self.wake()
        return {"id": rid, "text": text, "due": due, "due_text": describe(due, now_dt), "rule": rule,
                "repeat_text": describe_rule(rule), "replaced": replaced, "kind": kind}

    def list_reminders(self, include_inactive: bool = False) -> list[dict]:
        return self.store.list_reminders(include_inactive)

    def cancel_reminder(self, rid: int) -> dict:
        r = self.store.get_reminder(int(rid))
        if not r:
            raise UserError(f"There is no reminder #{rid}.")
        if r["status"] != "active":
            raise UserError(f"Reminder #{rid} is already {r['status']}.")
        self.store.set_status(r["id"], "cancelled")
        return r

    def cancel_all(self) -> int:
        return self.store.cancel_all()

    # ================================================================== controls (tools + HTTP)
    def pause(self, minutes: float = 60, scope: str = "initiative") -> float:
        scope = "all" if str(scope).lower() == "all" else "initiative"
        minutes = max(1.0, min(float(minutes), 60 * 24 * 14))
        until = self.clock() + minutes * 60
        self.store.kv_set("paused_until", until)
        self.store.kv_set("pause_scope", scope)
        return until

    def resume(self) -> None:
        self.store.kv_set("paused_until", 0)

    def mute(self, kind: str) -> list[str]:
        if kind not in MUTABLE_KINDS:
            raise UserError(f"kind must be one of {', '.join(MUTABLE_KINDS)}")
        muted = set(self.policy.muted_kinds()) | {kind}
        self.store.kv_set("muted_kinds", sorted(muted))
        return sorted(muted)

    def unmute(self, kind: str) -> list[str]:
        muted = set(self.policy.muted_kinds()) - {kind}
        self.store.kv_set("muted_kinds", sorted(muted))
        return sorted(muted)

    def set_setting(self, path: str, value) -> None:
        if path not in SETTABLE:
            raise UserError(f"'{path}' cannot be changed at runtime. Settable: {', '.join(sorted(SETTABLE))}")
        try:
            clean = SETTABLE[path](value)
        except ValueError as e:
            raise UserError(f"{path}: {e}")
        ov = dict(self.store.kv_get("overrides", {}) or {})
        ov[path] = clean
        self.store.kv_set("overrides", ov)

    def set_quiet_hours(self, start: str | None = None, end: str | None = None, enabled: bool | None = None) -> dict:
        if start is not None:
            self.set_setting("quiet_hours.start", start)
        if end is not None:
            self.set_setting("quiet_hours.end", end)
        if enabled is not None:
            self.set_setting("quiet_hours.enabled", enabled)
        return dict(self.cfg["quiet_hours"])

    def feedback(self, event_id: int, action: str) -> dict:
        """What the user did with a message in the chat window: open | dismiss | snooze | mute."""
        ev = self.store.get_event(event_id)
        if not ev:
            raise UserError("unknown event")
        action = str(action).lower()
        self.store.ack(event_id, action)
        if action == "open":
            self.store.set_engaged(event_id)
        elif action == "snooze":
            self.pause(60, "initiative")
        elif action == "mute" and ev["kind"] in MUTABLE_KINDS:
            self.mute(ev["kind"])
        return {"ok": True, "action": action}

    # ================================================================== status
    def status(self) -> dict:
        now = self.clock()
        dt = self.now_local()
        paused, scope, until = self.policy.pause_state()
        nxt = self.store.next_due()
        return {
            "enabled": bool(self.cfg.get("enabled", True)),
            "running": bool(self._task and not self._task.done()),
            "time": dt.strftime("%Y-%m-%d %H:%M"), "timezone": tz_label(dt),
            "quiet_now": self.policy.is_quiet(dt), "rest_day_today": self.policy.is_rest_day(dt),
            "quiet_hours": dict(self.cfg["quiet_hours"]), "rest_days": list(self.cfg.get("rest_days") or []),
            "paused": paused, "pause_scope": scope if paused else None,
            "paused_for_min": round((until - now) / 60) if paused else 0,
            "muted": self.policy.muted_kinds(),
            "ui_clients": self.ui_clients(), "ui_visible": self.ui_visible,
            "idle_minutes": round((now - self.last_user_ts) / 60) if self.last_user_ts else None,
            "active_reminders": self.store.count_active(),
            "next": ({"id": nxt["id"], "kind": nxt["kind"], "text": nxt["text"][:80],
                      "in": humanize_delta(nxt["due_ts"] - now), "due": describe(self.now_local(nxt["due_ts"]), dt)}
                     if nxt else None),
            "sent_today": self.store.count_sent(self.policy.local_midnight_ts(), ("greeting", "checkin", "alert", "reminder", "task")),
            "ignored_streak": self.policy.ignored_streak(),
            "voice": self.cfg["delivery"]["voice"], "counters": dict(self.counters),
        }

    def status_text(self) -> str:
        s = self.status()
        lines = [f"Proactive system: {'ON' if s['enabled'] else 'OFF'}, now {s['time']} ({s['timezone']})."]
        if s["paused"]:
            lines.append(f"Paused for another {s['paused_for_min']} min (scope: {s['pause_scope']}).")
        qh = s["quiet_hours"]
        lines.append(f"Quiet hours {qh['start']}-{qh['end']} ({'on' if qh['enabled'] else 'off'}); quiet right now: "
                     f"{'yes' if s['quiet_now'] else 'no'}. Rest days: {', '.join(s['rest_days']) or 'none'}"
                     f"{' (today is one)' if s['rest_day_today'] else ''}.")
        if s["muted"]:
            lines.append("Muted: " + ", ".join(s["muted"]) + ".")
        lines.append(f"{s['active_reminders']} active reminder(s)/task(s)."
                     + (f" Next: #{s['next']['id']} {s['next']['due']} (in {s['next']['in']}) - {s['next']['text']}" if s["next"] else ""))
        return "\n".join(lines)

    # ================================================================== processing: due reminders & tasks
    async def _process_due(self, now: float) -> None:
        for r in self.store.due(now, limit=10):
            try:
                if r["kind"] == "task":
                    await self._run_task(r, now)
                else:
                    await self._fire_reminder(r, now)
            except asyncio.CancelledError:
                raise
            except Exception as e:                       # noqa: BLE001
                self.counters["errors"] += 1
                logger.exception(f"proactive: reminder #{r['id']} failed")
                attempts = int(r.get("attempts") or 0) + 1
                if attempts >= 5:
                    self.store.set_status(r["id"], "failed", f"{type(e).__name__}: {e}")
                else:
                    self.store.update_reminder(r["id"], attempts=attempts, last_error=f"{type(e).__name__}: {e}")

    def _overdue_text(self, r: dict, now: float) -> str:
        late = now - r["due_ts"]
        return describe(self.now_local(r["due_ts"]), self.now_local(now)) if late > 120 else ""

    def _should_wait_for_reply(self, r: dict, now: float) -> bool:
        """A reminder waits for a running chat reply to finish, but not forever."""
        return (now - r["due_ts"]) < float(self.cfg["delivery"]["max_defer_seconds"])

    def _advance(self, r: dict, now: float, *, count: bool = True) -> None:
        fields: dict = {"last_fired_ts": now, "fire_count": int(r.get("fire_count") or 0) + (1 if count else 0),
                        "attempts": 0, "last_error": None}
        if r["rule"]:
            nxt = next_after(r["rule"], self.now_local(max(now, r["due_ts"])))
            fields["due_ts"] = nxt.timestamp()
            self.store.update_reminder(r["id"], **fields)
        else:
            self.store.update_reminder(r["id"], status="done", **fields)

    async def _fire_reminder(self, r: dict, now: float) -> None:
        cfg = self.cfg
        late = now - r["due_ts"]
        if r["rule"] and late > float(cfg["reminders"]["catchup_recurring_minutes"]) * 60:
            # a repeating reminder we missed by hours (server was off): skip it, don't replay a stale alarm
            self.store.add_event("reminder", r["text"], key=f"rem:{r['id']}", source_id=r["id"], status="suppressed",
                                 reason="missed_recurring", ts=now)
            self._advance(r, now, count=False)
            return
        verdict = self.policy.check("reminder", ui_connected=self.ui_clients() > 0, busy=self.busy())
        if not verdict and not (verdict.reason == "busy" and not self._should_wait_for_reply(r, now)):
            self._note(("reminder", r["id"]), verdict.reason)      # held back; due stays due, retried next tick
            return
        facts = {"text": r["text"], "overdue": self._overdue_text(r, now), "name": cfg["user_name"]}
        text, source = None, "template"
        if cfg["reminders"]["compose"] == "llm" and not facts["overdue"] and self._llm_idle(now):
            text, source = await _compose.compose("reminder", facts, self.host, cfg)
            if text == _compose.SKIP or source == "template":
                text = None
        if not text:
            text = _compose.template("reminder", facts)
        await self._deliver("reminder", text, key=f"rem:{r['id']}", source_id=r["id"])
        self._advance(r, now)

    def _llm_idle(self, now: float) -> bool:
        return (now - self.last_user_ts) >= float(self.cfg["llm"]["idle_before_llm_seconds"]) and not self.busy()

    async def _run_task(self, r: dict, now: float) -> None:
        cfg = self.cfg
        tk = cfg["tasks"]
        if not tk.get("enabled", True):
            return
        verdict = self.policy.check("task", ui_connected=self.ui_clients() > 0, busy=self.busy())
        if not verdict:
            self._note(("task", r["id"]), verdict.reason)
            return
        if self.store.task_runs_since(self.policy.local_midnight_ts()) >= int(tk["max_runs_per_day"]):
            self._note(("task", r["id"]), "task_cap")
            return
        retry = float(tk["retry_minutes"]) * 60
        if self.host.run_agent is None or not await self.host.llm_ready():
            if now - r["due_ts"] > 24 * 3600:
                await self._fail_task(r, now, "the model was not running for a whole day")
            else:
                self.store.update_reminder(r["id"], due_ts=now + retry, last_error="model not running")
            return
        if not self._llm_idle(now):
            self.store.update_reminder(r["id"], due_ts=now + 60)          # the user is chatting: try again in a minute
            return
        title = r["text"][:70]
        dt = self.now_local(now)
        persona = self.host.persona()
        system = (persona.strip() + "\n\nBACKGROUND TASK - you are running a task you scheduled earlier; the user is not "
                  "waiting and cannot answer questions. Use your tools to do it now. You have read-only tools only. "
                  "Then write a SHORT report (max 6 short lines) for " + cfg["user_name"] + ": what you found, "
                  "with the specifics (numbers, names, links). If you could not find or confirm something, say so "
                  "plainly - never guess or invent. Do not ask questions. Do not describe your process.")
        user = (f"It is {dt.strftime('%A %Y-%m-%d %H:%M')} ({tz_label(dt)}). Scheduled task (set "
                f"{describe(self.now_local(r['created_ts']), dt)}):\n{r['text']}")
        try:
            result = await asyncio.wait_for(
                self.host.run_agent([{"role": "system", "content": system}, {"role": "user", "content": user}],
                                    list(tk["allowed_tools"]), float(tk["timeout_seconds"]), int(tk["max_rounds"])),
                timeout=float(tk["timeout_seconds"]) + 30)
        except asyncio.CancelledError:
            raise
        except Exception as e:                           # noqa: BLE001
            attempts = int(r.get("attempts") or 0) + 1
            err = f"{type(e).__name__}: {e}"[:200]
            logger.warning(f"proactive: task #{r['id']} attempt {attempts} failed: {err}")
            if attempts >= int(tk["max_attempts"]):
                await self._fail_task(r, now, err)
            else:
                self.store.update_reminder(r["id"], attempts=attempts, due_ts=now + retry, last_error=err)
            return
        result = _compose.clean_report(result)
        if not result:
            attempts = int(r.get("attempts") or 0) + 1
            if attempts >= int(tk["max_attempts"]):
                await self._fail_task(r, now, "no usable result")
            else:
                self.store.update_reminder(r["id"], attempts=attempts, due_ts=now + retry, last_error="empty result")
            return
        text = _compose.template("task", {"title": title, "result": result})
        await self._deliver("task", text, key=f"task:{r['id']}", source_id=r["id"])
        self._advance(r, now)

    async def _fail_task(self, r: dict, now: float, why: str) -> None:
        text = _compose.template("alert", {"text": f"I could not finish the follow-up \"{r['text'][:70]}\": {why}."})
        await self._deliver("alert", text, key=f"task-fail:{r['id']}", source_id=r["id"])
        if r["rule"]:
            self._advance(r, now, count=False)
        else:
            self.store.set_status(r["id"], "failed", why)

    # ================================================================== processing: watchers
    def _ctx(self, now: float) -> _watchers.Ctx:
        sid = None
        try:
            sid = self.host.pick_session()
        except Exception:                                # noqa: BLE001
            pass
        try:
            health = self.host.health() or {}
        except Exception:                                # noqa: BLE001
            health = {}
        n = self.ui_clients()
        return _watchers.Ctx(now=now, dt=self.now_local(now), cfg=self.cfg, store=self.store, policy=self.policy,
                             ui_clients=n, ui_connected_for=(now - self._connected_at) if n else 0.0,
                             last_user_ts=self.last_user_ts, session=sid, recent=self.host.recent_messages,
                             health=health, root=Path(self.host.root), runtime=Path(self.host.runtime),
                             memory=self.host.memory_context, state=self._watch_state)

    async def _process_watchers(self, now: float) -> None:
        ctx = self._ctx(now)
        cands: list[_watchers.Candidate] = []
        for fn in (_watchers.greeting, _watchers.checkin):
            try:
                c = fn(ctx)
                if c:
                    cands.append(c)
            except Exception:                            # noqa: BLE001
                logger.exception(f"proactive watcher {fn.__name__} failed")
        try:
            cands += _watchers.alerts(ctx)
        except Exception:                                # noqa: BLE001
            logger.exception("proactive alert watcher failed")
        for c in cands:
            try:
                await self._handle_candidate(c, now)
            except asyncio.CancelledError:
                raise
            except Exception:                            # noqa: BLE001
                self.counters["errors"] += 1
                logger.exception(f"proactive: candidate {c.kind}/{c.key} failed")

    async def _handle_candidate(self, c: _watchers.Candidate, now: float) -> None:
        verdict = self.policy.check(c.kind, ui_connected=self.ui_clients() > 0, busy=self.busy())
        if not verdict:
            self._note((c.kind, c.key), verdict.reason)
            return
        cooldown_key = f"cool:{c.kind}:{c.key}"
        text = c.text
        if text is None:
            if c.kind == "alert":
                text = _compose.template("alert", c.facts)
            elif not c.llm_allowed:
                text = _compose.template(c.kind, c.facts)
            else:
                if not self._llm_idle(now):
                    return
                text, source = await _compose.compose(c.kind, c.facts, self.host, self.cfg)
                if text == _compose.SKIP:
                    self.store.add_event(c.kind, "", key=c.key, status="suppressed", reason="llm_skip", ts=now)
                    self.counters["suppressed"] += 1
                    self.store.kv_set(cooldown_key, now + c.cooldown_s)
                    return
                if c.kind == "checkin" and source != "llm":
                    return                                 # a generic canned "just checking in" is worse than silence
        if not text:
            return
        # the situation may have changed while the model was thinking
        verdict = self.policy.check(c.kind, ui_connected=self.ui_clients() > 0, busy=self.busy())
        if not verdict:
            self._note((c.kind, c.key), verdict.reason)
            return
        await self._deliver(c.kind, text, key=c.key, sticker=True)
        if c.cooldown_s:
            self.store.kv_set(cooldown_key, now + c.cooldown_s)

    def _note(self, key: tuple, reason: str) -> None:
        if self._why.get(key) != reason:
            self._why[key] = reason
            logger.debug(f"proactive: {key[0]} {key[1]!r} held back: {reason}")

    # ================================================================== delivery
    async def _deliver(self, kind: str, text: str, *, key: str = "", source_id: int | None = None,
                       sticker: bool | None = None, session: str | None = None) -> int:
        cfg = self.cfg
        async with self._deliver_lock:
            sid = session or self._target_session()
            media = None
            emotions = (cfg["delivery"].get("stickers") or {}).get(kind) or []
            want_sticker = True if sticker is None else sticker
            if (want_sticker and cfg["delivery"].get("sticker") and emotions
                    and self.rng.random() < float(cfg["delivery"].get("sticker_chance", 0.35))):
                try:
                    media = self.host.sticker(self.rng.choice(emotions))
                except Exception:                        # noqa: BLE001
                    media = None
            content = text + (f"\nMEDIA:{media}" if media else "")
            self.host.append_message(sid, "assistant", content)
            now = self.clock()
            eid = self.store.add_event(kind, text, key=key, session_id=sid, media=media, source_id=source_id, ts=now)
            event = {"id": eid, "ts": now, "kind": kind, "title": KIND_TITLES.get(kind, "Lucy"), "text": text,
                     "media": media, "session_id": sid, "mutable": kind in MUTABLE_KINDS}
            if self.bus.publish(event):
                self.store.mark_live(eid)
            self.counters["sent"] += 1
            logger.info(f"proactive: sent {kind} #{eid} to session {sid[:8] if sid else '-'}: {text[:80]!r}")
            voice = cfg["delivery"].get("voice", "off")
            if self.host.speak and (voice == "all" or (voice == "reminders" and kind in ("reminder", "task"))):
                asyncio.get_running_loop().create_task(self._speak(text))
            return eid

    async def _speak(self, text: str) -> None:
        try:
            await self.host.speak(text)
        except Exception as e:                           # noqa: BLE001
            logger.warning(f"proactive: speaking failed: {e}")

    def _target_session(self) -> str:
        mode = str(self.cfg["delivery"].get("session", "active"))
        if mode == "active":
            try:
                return self.host.pick_session() or "default"
            except Exception:                            # noqa: BLE001
                return "default"
        return mode or "default"

    async def send_test(self, text: str | None = None) -> int:
        """Deliver a visible test message right now (HTTP POST /api/proactive/test)."""
        return await self._deliver("test", text or "👋 Proactive system test. If you can read this, I can reach you first.",
                                   key="test", sticker=False)

    # ================================================================== for api.py's prompt
    def now_note(self) -> str:
        dt = self.now_local()
        note = f"Current local time: {dt.strftime('%A %Y-%m-%d %H:%M')} ({tz_label(dt)})."
        if self.policy.is_rest_day(dt):
            note += " Today is the user's rest day."
        return note
