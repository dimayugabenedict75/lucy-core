"""JobRunner: long-running work for Lucy, driven by the proactive engine's tick.

A job is a goal (an instruction to Lucy herself) or a builtin upkeep handler, with a schedule (once, repeating, cron,
"when idle"), a budget (time / tool calls / tokens) and a conversation checkpoint. Goal jobs run through the SAME
AgentLoop as chat (api.py builds it: same tools, routing, retries, no-give-up behaviour) with two differences:

  * the permission gate is `JobGate`: an "ask" PARKS the job (the user answers in the Tasks panel) instead of blocking;
  * every run is supervised: budgets, Stop / pause / shutdown all end it cleanly and the work so far is checkpointed.

When the engine says it is OK (not quiet hours, not paused, nobody chatting, model up, daily caps) the next due job
starts - one at a time (there is one model slot). A job that was running when the server stopped continues from its
checkpoint. Results are posted into the chat like other proactive messages.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Awaitable, Callable

from lucy.proactive import compose as _compose
from lucy.proactive.engine import UserError
from lucy.proactive.schedule import describe_rule, make_rule, next_after
from lucy.proactive.timeparse import describe, humanize_delta, parse_when

from .budget import Budget
from .gate import JobGate, call_sig
from .store import ACTIVE, FINAL, STATUS_LABELS, JobStore

logger = logging.getLogger("lucy.core.jobs")

_NOTE = "[Harness note - internal, do not mention or quote it] "      # same marker the agent loop uses
NOTHING_NEW = re.compile(r"^\W*nothing\s+new\b", re.IGNORECASE)
NOW_WORDS = ("", "now", "asap", "right now", "immediately", "today", "as soon as possible")
BUILTIN_LABELS: dict[str, str] = {}

RULES = ("BACKGROUND JOB - you are working on a job you were given; the user is not watching and cannot answer questions. "
         "Work through it step by step with your tools. If something needs the user's approval the system pauses the job "
         "and asks them for you: do not ask in your reply and do not stop early. Never invent results: if you could not "
         "find or finish something, say so plainly. Finish with a SHORT report (max 8 short lines): what you did or found, "
         "with the specifics (numbers, names, links, file paths). Do not describe your process.")


class RunError(RuntimeError):
    pass


async def _true() -> bool:
    return True


@dataclass
class JobHostHooks:
    """What the runner needs from the rest of Lucy. api.py fills these in; every field has a harmless default."""
    make_agent: Callable[..., Any] | None = None                    # (job, execute, gate, session_id, access, read_tools) -> agent
    execute: Callable[[str, dict], Awaitable[str]] | None = None    # runs one tool (the registry: hard rules + audit)
    persona: Callable[[], str] = lambda: "You are Lucy, a helpful assistant."
    llm_ready: Callable[[], Awaitable[bool]] = _true
    llm_complete: Callable[[list, dict], Awaitable[str]] | None = None
    count_tokens: Callable[[str], int] = lambda text: max(1, len(text) // 4)
    paused: Callable[[], bool] = lambda: False                      # the safety layer's "Pause agent"
    layer: Callable[[], Any] | None = None                          # -> the SafetyLayer (for the gate)
    tier_of: Callable[[str], str] = lambda name: "exec"
    begin_session: Callable[[str], None] | None = None              # sets api's current-session for this run's task


@dataclass
class RunCtl:
    job_id: int
    session: str
    started: float
    base_seconds: float = 0.0
    waited: float = 0.0
    tool_calls: int = 0
    tokens: int = 0
    run_id: int = 0
    stop: str | None = None                       # cancel | shutdown (set from outside)
    parked: dict | None = None
    park_evt: asyncio.Event = field(default_factory=asyncio.Event)
    gate: JobGate | None = None
    content: list = field(default_factory=list)
    media: list = field(default_factory=list)
    messages: list = field(default_factory=list)
    counted: int = 0
    budget: Budget = field(default_factory=Budget)
    steps: int = 0
    force: bool = False


class JobRunner:
    def __init__(self, store: JobStore, hooks: JobHostHooks, *, cfg: Callable[[], dict], clock: Callable[[], float] = time.time,
                 now_local: Callable[[float | None], datetime], policy: Any, deliver: Callable[..., Awaitable[Any]],
                 llm_idle: Callable[[float], bool], busy: Callable[[], bool], last_user_ts: Callable[[], float],
                 wake: Callable[[], None] = lambda: None):
        self.store, self.hooks, self.cfg_fn, self.clock = store, hooks, cfg, clock
        self.now_local, self.policy, self.deliver = now_local, policy, deliver
        self.llm_idle, self.busy, self.last_user_ts, self.wake = llm_idle, busy, last_user_ts, wake
        self._task: asyncio.Task | None = None
        self.ctl: RunCtl | None = None
        self._builtins: dict[str, dict] = {}
        self._why: dict = {}
        self._force: set[int] = set()
        self.counters = {"runs": 0, "errors": 0}

    # ================================================================== config / helpers
    @property
    def cfg(self) -> dict:
        return self.cfg_fn().get("jobs") or {}

    def enabled(self) -> bool:
        return bool(self.cfg.get("enabled", True))

    def _budget_defaults(self) -> tuple[dict, dict]:
        c = self.cfg
        return (c.get("default_budget") or {}), (c.get("max_budget") or {})

    def _poll(self) -> float:
        return max(0.01, float(self.cfg.get("poll_seconds", 1.0)))

    def _note(self, key, reason: str) -> None:
        if self._why.get(key) != reason:
            self._why[key] = reason
            logger.debug(f"jobs: {key} held back: {reason}")

    def _midnight(self) -> float:
        return self.now_local().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()

    # ================================================================== builtins (upkeep jobs run by the queue)
    def register_builtin(self, name: str, fn: Callable[[], Any], *, label: str, needs_llm: bool = False,
                         every_minutes: int | None = None, cron: str | None = None) -> None:
        """A python handler the queue can run (reflection, trace review, ...). `fn()` may be sync or async and returns a
        short summary text, or "" when there was nothing to do (such a run is not recorded)."""
        self._builtins[name] = {"fn": fn, "label": label, "needs_llm": needs_llm, "every_minutes": every_minutes, "cron": cron}
        BUILTIN_LABELS[name] = label

    def ensure_system_jobs(self) -> int:
        """Create the schedule row of each registered builtin once (a job the user cancelled is NOT recreated)."""
        n = 0
        for name, b in self._builtins.items():
            if self.store.find_handler(name):
                continue
            now = self.clock()
            first = self.now_local(now)
            rule = make_rule(None, b.get("every_minutes"), first, 1, cron=b.get("cron")) if (b.get("every_minutes") or b.get("cron")) else None
            due = next_after(rule, first).timestamp() if (rule and rule["type"] == "cron") else now
            self.store.add(title=b["label"], kind="builtin", handler=name, source="system", status="scheduled", due_ts=due,
                           rule=rule, notify="never", quiet_ok=True, rest_ok=True, now=now)
            n += 1
        return n

    # ================================================================== creating / changing jobs (tool + HTTP)
    def add_job(self, *, goal: str, title: str | None = None, when: str | None = None, repeat: str | None = None,
                every_minutes: int | None = None, cron: str | None = None, deadline: str | None = None,
                access: str = "read", minutes: float | None = None, idle: bool = False, notify: str = "always",
                source: str = "user", session: str | None = None) -> dict:
        c = self.cfg
        goal = " ".join(str(goal or "").split())
        if len(goal) < 8:
            raise UserError("The task needs a complete instruction (what to do, with every detail: names, links, what counts as news).")
        if len(goal) > 1500:
            raise UserError("That instruction is too long (1500 characters max). Put the details in a file and point to it.")
        title = " ".join(str(title or goal).split())[:70]
        access = "full" if str(access or "").lower() in ("full", "normal", "all") else "read"
        notify = str(notify or "always").lower()
        notify = notify if notify in ("always", "changes") else "always"
        active = [j for j in self.store.list(ACTIVE) if j["source"] != "system"]
        if len(active) >= int(c.get("max_active", 30)):
            raise UserError("There are too many active tasks. Cancel some first.")
        now = self.clock()
        now_dt = self.now_local(now)
        w = str(when or "").strip().lower()
        due_dt = None
        if w not in NOW_WORDS:
            due_dt = parse_when(str(when), now_dt)
            if due_dt is None:
                raise UserError(f"I could not understand the time '{when}'. Use something like 'in 20 minutes', 'tomorrow 9am', "
                                "'friday 5pm' or leave it out to start as soon as possible.")
            if due_dt.timestamp() < now - 30:
                raise UserError(f"That time ({describe(due_dt, now_dt)}) is already in the past.")
        try:                                                           # the rule's time of day follows the FIRST run
            rule = make_rule(repeat, every_minutes, due_dt or now_dt, int(self.cfg_fn()["reminders"]["min_interval_minutes"]), cron=cron)
        except ValueError as e:
            raise UserError(str(e))
        if due_dt is not None:
            due = due_dt.timestamp()
        else:
            due = next_after(rule, now_dt).timestamp() if (rule and rule["type"] == "cron") else now
        dl = None
        if deadline not in (None, ""):
            dd = parse_when(str(deadline), now_dt)
            if dd is None:
                raise UserError(f"I could not understand the deadline '{deadline}'.")
            dl = dd.timestamp()
        defaults, ceiling = self._budget_defaults()
        asked = {"seconds": float(minutes) * 60} if minutes not in (None, "", 0) else {}
        budget = Budget.make(asked, defaults, ceiling)
        status = "scheduled"
        if source == "model" and access == "full" and not c.get("auto_start_model_full", False):
            status = "waiting_ok"
        idle_after = float(c.get("idle_start_minutes", 10)) * 60 if idle else 0.0
        jid = self.store.add(title=title, goal=goal, access=access, source=source, session=session, status=status, due_ts=due,
                             rule=rule, idle_after=idle_after, notify=notify, budget=budget.to_dict(), deadline_ts=dl, now=now)
        self.store.log(jid, "note", f"created ({source}), {access} tools, budget {budget.describe()}", now)
        self.wake()
        return self.card(self.store.get(jid))

    def _need(self, jid) -> dict:
        try:
            job = self.store.get(int(jid))
        except (TypeError, ValueError):
            job = None
        if not job:
            raise UserError(f"There is no task #{jid}.")
        return job

    def cancel(self, jid) -> dict:
        job = self._need(jid)
        if job["status"] in FINAL:
            raise UserError(f"Task #{job['id']} is already {job['status']}.")
        if self.ctl and self.ctl.job_id == job["id"]:
            self.ctl.stop = "cancel"                                    # the supervisor ends the run and records it
        else:
            self.store.update(job["id"], status="cancelled", park=None, resume=0)
            self.store.set_checkpoint(job["id"], None)
            self.store.log(job["id"], "note", "cancelled", self.clock())
        return job

    def ok_to_start(self, jid) -> dict:
        """The user's OK for a job the model made with full tools."""
        job = self._need(jid)
        if job["status"] != "waiting_ok":
            raise UserError(f"Task #{job['id']} is not waiting for an OK (it is {job['status']}).")
        self.store.update(job["id"], status="scheduled")
        self.store.log(job["id"], "note", "OK'd by the user", self.clock())
        self.wake()
        return self.store.get(job["id"])

    def run_now(self, jid) -> dict:
        """Start as soon as the model is free, ignoring quiet hours, idle rules and the daily caps (the user asked)."""
        job = self._need(jid)
        if job["status"] not in ("scheduled", "waiting_ok"):
            raise UserError(f"Task #{job['id']} cannot be started now (it is {job['status']}).")
        self._force.add(job["id"])
        self.store.update(job["id"], status="scheduled", due_ts=0)
        self.wake()
        return self.store.get(job["id"])

    def extend(self, jid, minutes: float | None = None) -> dict:
        """Give a budget-stopped job another round of budget and continue it."""
        job = self._need(jid)
        if job["status"] != "stopped":
            raise UserError(f"Task #{job['id']} is not stopped (it is {job['status']}).")
        defaults, ceiling = self._budget_defaults()
        add = Budget.make({"seconds": float(minutes) * 60} if minutes else {}, defaults, ceiling)
        cur = Budget.make(job.get("budget"), defaults, ceiling)
        new = Budget.make({"seconds": cur.seconds + add.seconds, "tool_calls": cur.tool_calls + add.tool_calls,
                           "tokens": cur.tokens + add.tokens}, defaults, ceiling)
        self.store.update(job["id"], status="scheduled", resume=1 if self.store.get_checkpoint(job["id"]) else 0,
                          budget=new.to_dict(), due_ts=0, attempts=0)
        self.store.log(job["id"], "note", f"continued with more budget ({new.describe()})", self.clock())
        self._force.add(job["id"])
        self.wake()
        return self.store.get(job["id"])

    def decide(self, jid, decision: str, approval_id: str | None = None) -> dict:
        """The user's answer to a parked approval (Tasks panel). approve | deny | always."""
        job = self._need(jid)
        park = job.get("park")
        if job["status"] != "parked" or not park or park.get("decision"):
            raise UserError(f"Task #{job['id']} is not waiting for an approval.")
        if approval_id and approval_id != park.get("id"):
            raise UserError("That approval is out of date.")
        decision = str(decision or "").lower()
        if decision not in ("approve", "deny", "always"):
            raise UserError("decision must be approve, deny or always")
        if decision == "always" and not park.get("can_always"):
            decision = "approve"                                         # this kind of call can never be pre-approved
        park = {**park, "decision": decision, "decided": self.clock()}
        allow = list(job.get("allow_tools") or [])
        if decision == "always" and park["tool"] not in allow:
            allow.append(park["tool"])
        self.store.update(job["id"], park=park, allow_tools=allow, status="scheduled", resume=1, due_ts=0)
        self.store.log(job["id"], "approval", f"{decision}: {park.get('summary', park['tool'])}", self.clock())
        self._force.add(job["id"])
        self.wake()
        return self.store.get(job["id"])

    def delete(self, jid) -> None:
        job = self._need(jid)
        if job["status"] not in FINAL:
            raise UserError("Cancel the task first.")
        self.store._x("DELETE FROM log WHERE job_id=?", (job["id"],))
        self.store._x("DELETE FROM jobs WHERE id=?", (job["id"],))

    # ================================================================== views
    def _usage_of(self, job: dict) -> dict:
        if self.ctl and self.ctl.job_id == job["id"]:
            return self._usage(self.ctl)
        return {"seconds": 0, "tool_calls": 0, "tokens": 0, **(job.get("usage") or {})}

    def card(self, job: dict, detail: bool = False) -> dict:
        now = self.clock()
        defaults, ceiling = self._budget_defaults()
        c = {"id": job["id"], "kind": job["kind"], "title": job["title"], "goal": (job.get("goal") or "")[:400],
             "status": job["status"], "status_label": STATUS_LABELS.get(job["status"], job["status"]),
             "access": job["access"], "source": job["source"], "notify": job["notify"], "system": job["source"] == "system",
             "repeat": describe_rule(job.get("rule")), "runs": job["runs"], "idle": bool(job.get("idle_after")),
             "budget": Budget.make(job.get("budget"), defaults, ceiling).to_dict(), "usage": self._usage_of(job),
             "due": job["due_ts"] if job["status"] == "scheduled" else None,
             "due_in": humanize_delta(job["due_ts"] - now) if job["status"] == "scheduled" and job["due_ts"] > now else "",
             "deadline": job.get("deadline_ts"), "last_run": job.get("last_run_ts"), "last_result": job.get("last_result"),
             "last_error": job.get("last_error"), "running": bool(self.ctl and self.ctl.job_id == job["id"]),
             "parked": None, "created": job["created_ts"]}
        if job["kind"] == "builtin":
            c["goal"] = BUILTIN_LABELS.get(job.get("handler") or "", job["title"])
        park = job.get("park")
        if job["status"] == "parked" and park:
            c["parked"] = {k: park.get(k) for k in ("id", "tool", "summary", "reason", "details", "details_kind", "can_always",
                                                      "created", "decision")}
        c["progress"] = [{"ts": r["ts"], "kind": r["kind"], "text": r["text"]} for r in self.store.log_tail(job["id"], 12 if detail else 3)]
        if detail:
            c["history"] = [{"started": r["started_ts"], "ended": r["ended_ts"], "outcome": r["outcome"], "summary": r["summary"],
                             "tool_calls": r["tool_calls"], "seconds": round(r["seconds"])} for r in self.store.recent_runs(job["id"], 6)]
        return c

    def list_cards(self, limit: int = 60) -> list[dict]:
        return [self.card(j) for j in self.store.list(limit=limit)]

    def status(self) -> dict:
        c = self.cfg
        used = self.store.usage_since(self._midnight())
        return {"enabled": self.enabled(), "running": self.ctl.job_id if self.ctl else None,
                "active": self.store.count(ACTIVE), "parked": self.store.count(("parked",)),
                "waiting_ok": self.store.count(("waiting_ok",)), "today": used,
                "caps": {"runs": int(c.get("max_runs_per_day", 8)), "tool_calls": int(c.get("max_tool_calls_per_day", 400)),
                         "seconds": int(c.get("max_seconds_per_day", 10800))},
                "default_budget": Budget.make(None, *self._budget_defaults()).to_dict(), "counters": dict(self.counters)}

    # ================================================================== scheduling
    def recover(self) -> None:
        ids = self.store.recover()
        for jid in ids:
            self.store.log(jid, "note", "the server restarted: continuing from the last checkpoint", self.clock())
        if ids:
            logger.info(f"jobs: {len(ids)} interrupted job(s) will continue: {ids}")
        self.ensure_system_jobs()

    async def tick(self, now: float) -> None:
        if not self.enabled():
            return
        self._expire_parks(now)
        await self._notify_parked(now)
        await self._deadline_alerts(now)
        if now - float(self.store.kv_get("pruned", 0) or 0) > 6 * 3600:
            self.store.kv_set("pruned", now)
            self.store.prune(now - int(self.cfg.get("keep_days", 60)) * 86400)
        if self._task and not self._task.done():
            return                                                     # one job at a time (one model slot)
        for job in self.store.due(now):
            why = await self._blocked(job, now)
            if why:
                self._note(("job", job["id"]), why)
                continue
            self._start(job)
            return

    async def _blocked(self, job: dict, now: float) -> str | None:
        forced = job["id"] in self._force
        try:
            if self.hooks.paused():
                return "paused"
        except Exception:                                              # noqa: BLE001
            pass
        paused, scope, _ = self.policy.pause_state()
        if paused and (scope == "all" or job["source"] == "system"):
            return "paused"
        if not forced:
            if self.policy.is_quiet() and not job.get("quiet_ok"):
                return "quiet_hours"
            if self.policy.is_rest_day() and not job.get("rest_ok", 1):
                return "rest_day"
        if self.busy():
            return "busy"
        b = self._builtins.get(job.get("handler") or "") if job["kind"] == "builtin" else None
        needs_llm = job["kind"] == "goal" or bool(b and b["needs_llm"])
        if job["kind"] == "builtin" and b is None:
            return "unknown_handler"
        if needs_llm and not self.llm_idle(now):
            return "chatting"
        if not forced and job.get("idle_after"):
            last = float(self.last_user_ts() or 0)
            if last and now - last < float(job["idle_after"]):
                return "not_idle"
        if needs_llm and not await self.hooks.llm_ready():
            return "model_down"
        if job["kind"] == "goal" and not forced:
            c, used = self.cfg, self.store.usage_since(self._midnight())
            if (used["runs"] >= int(c.get("max_runs_per_day", 8)) or used["tool_calls"] >= int(c.get("max_tool_calls_per_day", 400))
                    or used["seconds"] >= float(c.get("max_seconds_per_day", 10800))):
                return "daily_cap"
        return None

    def _start(self, job: dict) -> None:
        defaults, ceiling = self._budget_defaults()
        usage = {"seconds": 0, "tool_calls": 0, "tokens": 0, **(job.get("usage") or {})} if job.get("resume") else \
            {"seconds": 0, "tool_calls": 0, "tokens": 0}
        now = self.clock()
        ctl = RunCtl(job_id=job["id"], session=f"job-{job['id']}", started=now, base_seconds=float(usage["seconds"]),
                     tool_calls=int(usage["tool_calls"]), tokens=int(usage["tokens"]),
                     budget=Budget.make(job.get("budget"), defaults, ceiling), force=job["id"] in self._force)
        self._force.discard(job["id"])
        ctl.run_id = self.store.start_run(job["id"], now)
        self.store.update(job["id"], status="running", usage=usage)
        if job["kind"] == "goal":
            self.store.log(job["id"], "note", "resumed" if job.get("resume") else "started", now)
        self.ctl = ctl
        self.counters["runs"] += 1
        self._task = asyncio.get_running_loop().create_task(self._run(job["id"], ctl), name=f"lucy-job-{job['id']}")

    async def stop(self) -> None:
        """Server shutting down: end the current run cleanly; it continues after the next start."""
        if self.ctl:
            self.ctl.stop = "shutdown"
        if self._task:
            try:
                await asyncio.wait_for(asyncio.shield(self._task), timeout=15)
            except (asyncio.TimeoutError, asyncio.CancelledError, Exception):      # noqa: BLE001
                self._task.cancel()
                try:
                    await self._task
                except (asyncio.CancelledError, Exception):            # noqa: BLE001
                    pass
            self._task = None

    # ================================================================== parked approvals: expiry + notices
    def _expire_parks(self, now: float) -> None:
        hours = float(self.cfg.get("park_timeout_hours", 48))
        for job in self.store.list(("parked",)):
            park = job.get("park") or {}
            if park.get("decision") or now - float(park.get("created", now)) < hours * 3600:
                continue
            self.store.update(job["id"], park={**park, "decision": "expired", "decided": now}, status="scheduled",
                              resume=1, due_ts=0)
            self.store.log(job["id"], "approval", "nobody answered in time: counts as a denial", now)

    async def _notify_parked(self, now: float) -> None:
        for job in self.store.list(("parked",)):
            park = job.get("park") or {}
            if park.get("notified") or park.get("decision"):
                continue
            if self.policy.is_quiet() or self.busy():
                continue                                                # tell them when they can be reached
            text = (f"✋ **Task #{job['id']}: {job['title']}** needs your OK before it can continue:\n"
                    f"{park.get('summary', park.get('tool'))}\nOpen Settings > Tasks to approve or deny.")
            await self.deliver("task", text, key=f"job-park:{job['id']}", source_id=job["id"])
            self.store.update(job["id"], park={**park, "notified": True})

    async def _deadline_alerts(self, now: float) -> None:
        for job in self.store.list(ACTIVE):
            dl = job.get("deadline_ts")
            if not dl or dl > now or job.get("deadline_alerted") or job["source"] == "system":
                continue
            if self.policy.is_quiet():
                continue
            when = describe(self.now_local(dl), self.now_local(now))
            await self.deliver("alert", _compose.template("alert", {"text": f"Task #{job['id']} \"{job['title']}\" passed its deadline ({when}) "
                                                                    f"and is {STATUS_LABELS.get(job['status'], job['status'])}."}),
                               key=f"job-deadline:{job['id']}", source_id=job["id"])
            self.store.update(job["id"], deadline_alerted=1)

    # ================================================================== one run
    def _usage(self, ctl: RunCtl) -> dict:
        secs = ctl.base_seconds + max(0.0, self.clock() - ctl.started - ctl.waited)
        return {"seconds": round(secs, 1), "tool_calls": ctl.tool_calls, "tokens": ctl.tokens}

    async def _run(self, jid: int, ctl: RunCtl) -> None:
        job = self.store.get(jid)
        try:
            if job["kind"] == "builtin":
                await self._run_builtin(job, ctl)
            else:
                await self._run_goal(job, ctl)
        except asyncio.CancelledError:
            self._end_paused(job, ctl, "the server stopped")
            raise
        except Exception as e:                                         # noqa: BLE001
            await self._on_error(job, ctl, e)
        finally:
            self.ctl = None

    # ------------------------------------------------------------------ builtin handlers
    async def _run_builtin(self, job: dict, ctl: RunCtl) -> None:
        b = self._builtins.get(job.get("handler") or "")
        if b is None:
            raise RunError(f"unknown handler '{job.get('handler')}'")

        async def call():
            r = b["fn"]()
            if inspect.isawaitable(r):
                r = await r
            return r

        task = asyncio.ensure_future(call())
        reason = await self._supervise(ctl, task, budgeted=False)
        if reason:
            if reason == "cancel":
                return self._end_cancelled(job, ctl)
            return self._end_paused(job, ctl, reason)
        summary = str(task.result() or "").strip()
        if not summary:                                                # nothing was due: not a run
            self.store._x("DELETE FROM runs WHERE id=?", (ctl.run_id,))
            self.counters["runs"] -= 1
            return self._reschedule(job, ctl, keep_result=True, count_run=False)
        self.store.finish_run(ctl.run_id, self.clock(), "done", summary, self._usage(ctl))
        self._reschedule(job, ctl, result=summary)
        if job["notify"] != "never":
            await self.deliver("task", f"📋 **{job['title']}**\n{summary[:self.cfg.get('report_max_chars', 1200)]}",
                               key=f"job:{job['id']}", source_id=job["id"])

    # ------------------------------------------------------------------ agent jobs
    def _brief(self, job: dict) -> str:
        now = self.now_local()
        lines = [f"It is {now.strftime('%A %Y-%m-%d %H:%M')}. Background job #{job['id']}"
                 + (f" (run {job['runs'] + 1})" if job["runs"] else "") + f" \"{job['title']}\".", "Job:", job["goal"]]
        if job.get("deadline_ts"):
            lines.append("Deadline: " + describe(self.now_local(job["deadline_ts"]), now))
        if job.get("last_result") and job.get("rule"):
            lines.append("Your previous report (compare with it):\n" + str(job["last_result"])[:700])
        if job["notify"] == "changes":
            lines.append("If nothing is new or different since the previous report, reply with exactly: NOTHING NEW")
        return "\n".join(lines)

    async def _run_goal(self, job: dict, ctl: RunCtl) -> None:
        h, c = self.hooks, self.cfg
        if h.make_agent is None or h.execute is None:
            raise RunError("the job runner is not connected to the agent loop")
        if h.layer is None:
            raise RunError("no safety layer: a job never runs unattended without one")
        if h.begin_session:
            h.begin_session(ctl.session)
        read_tools = list(c.get("read_tools") or [])
        blocked = frozenset(c.get("blocked_tools") or [])
        layer = h.layer() if h.layer else None
        if layer is not None and job.get("allow_tools"):
            try:
                layer.session_allow.setdefault(ctl.session, set()).update(job["allow_tools"])
            except Exception:                                          # noqa: BLE001
                pass
        ctl.gate = JobGate(layer_fn=h.layer, tier_fn=h.tier_of, session=ctl.session, on_park=lambda card: self._on_park(ctl, card),
                           blocked=blocked, only=frozenset(read_tools) if job["access"] == "read" else None)
        system = {"role": "system", "content": h.persona().strip() + "\n\n" + RULES}
        ckpt = self.store.get_checkpoint(job["id"]) if job.get("resume") else None
        history = await self._repair(job, ctl, ckpt) if ckpt else [{"role": "user", "content": self._brief(job)}]
        ctl.messages = [system] + history
        ctl.counted = len(ctl.messages)
        agent = h.make_agent(job, lambda name, args: self._execute(ctl, name, args), ctl.gate, ctl.session, job["access"], read_tools)
        task = asyncio.ensure_future(self._consume(ctl, agent))
        reason = await self._supervise(ctl, task)
        self._sync(ctl)
        if reason is None:
            text = _compose.clean_report("".join(ctl.content))
            if not text:
                raise RunError("no usable result")
            return await self._complete(job, ctl, text)
        if reason == "parked":
            return self._end_parked(job, ctl)
        if reason.startswith("budget"):
            return await self._end_budget(job, ctl, reason.split(":", 1)[1])
        if reason == "cancel":
            return self._end_cancelled(job, ctl)
        return self._end_paused(job, ctl, reason)

    async def _consume(self, ctl: RunCtl, agent) -> None:
        async for ev in agent.run(ctl.messages):
            if ev.kind == "content":
                ctl.content.append(str(ev.data))
            elif ev.kind == "media":
                ctl.media.append(str(ev.data))
            elif ev.kind == "error":
                d = ev.data if isinstance(ev.data, dict) else {"detail": ev.data}
                raise RunError(str(d.get("detail") or d.get("error") or "the model failed"))
            self._sync(ctl)

    def _msg_tokens(self, m: dict) -> int:
        text = str(m.get("content") or "")
        for tc in m.get("tool_calls") or []:
            text += json.dumps(tc.get("function") or {}, default=str)
        try:
            return int(self.hooks.count_tokens(text))
        except Exception:                                              # noqa: BLE001
            return len(text) // 4

    def _sync(self, ctl: RunCtl) -> None:
        """Account for what the conversation grew by and save a checkpoint (cheap when nothing changed)."""
        n = len(ctl.messages)
        if n == ctl.counted:
            return
        for m in ctl.messages[ctl.counted:]:
            ctl.tokens += self._msg_tokens(m)
        ctl.counted = n
        self.store.set_checkpoint(ctl.job_id, ctl.messages[1:])
        self.store.update(ctl.job_id, usage=self._usage(ctl))

    async def _supervise(self, ctl: RunCtl, task: asyncio.Future, budgeted: bool = True) -> str | None:
        """Wait for the run; end it early on park / cancel / pause / shutdown / budget. -> None when it finished by itself."""
        park_wait = asyncio.ensure_future(ctl.park_evt.wait())
        reason = None
        try:
            while not task.done():
                await asyncio.wait({task, park_wait}, timeout=self._poll(), return_when=asyncio.FIRST_COMPLETED)
                if task.done() and not ctl.parked:
                    break
                if ctl.parked:
                    reason = "parked"
                elif ctl.stop:
                    reason = ctl.stop
                elif self._paused_now(ctl):
                    reason = "paused"
                elif budgeted:
                    over = ctl.budget.exceeded(self._usage(ctl))
                    if over:
                        reason = f"budget:{over}"
                if reason:
                    break
        finally:
            park_wait.cancel()
            if reason and not task.done():
                task.cancel()
            if reason:
                await asyncio.gather(task, return_exceptions=True)
        if reason is None and ctl.stop:
            reason = ctl.stop                                          # flagged (budget / cancel) but the run just ended: honour it
        if reason is None:
            task.result()                                              # re-raise an error from the run
        return reason

    def _paused_now(self, ctl: RunCtl) -> bool:
        try:
            if self.hooks.paused():
                return True
        except Exception:                                              # noqa: BLE001
            pass
        paused, scope, _ = self.policy.pause_state()
        return paused and scope == "all"

    async def _wait_free(self, ctl: RunCtl) -> None:
        """Between tools a job gives way to the user's chat (one model slot): it waits, and the wait is not billed."""
        while self.busy() and not ctl.stop:
            t0 = self.clock()
            await asyncio.sleep(self._poll())
            ctl.waited += max(0.0, self.clock() - t0)

    async def _execute(self, ctl: RunCtl, name: str, args: dict, approved: bool = False) -> str:
        """Run one tool for the job (the registry applies the safety hard rules + audit). Counts the call, logs a step."""
        await self._wait_free(ctl)
        over = ctl.budget.exceeded(self._usage(ctl))
        if over:
            ctl.stop = ctl.stop or f"budget:{over}"
            return f"Error: this job's {over} budget is used up. Stop now and write your report."
        ctl.tool_calls += 1
        if approved and ctl.gate is not None:
            ctl.gate.mark_approved(name, args)
        t0 = time.monotonic()
        try:
            out = await self.hooks.execute(name, args)
        except asyncio.CancelledError:
            raise
        except Exception as e:                                         # noqa: BLE001
            out = f"Tool failed: {name} raised {type(e).__name__}: {e}"
        out = "(no output)" if out is None else out if isinstance(out, str) else json.dumps(out, default=str)
        ok = not out.lower().startswith(("error", "tool failed", "refused", "not run", "command failed"))
        short = json.dumps(args or {}, ensure_ascii=False, default=str)[:90]
        self.store.log(ctl.job_id, "step", f"{name} {short} -> {'ok' if ok else 'failed'} ({int((time.monotonic() - t0) * 1000)} ms)",
                       self.clock())
        return out

    def _on_park(self, ctl: RunCtl, card: dict) -> None:
        if ctl.parked is None:
            ctl.parked = card
            ctl.park_evt.set()

    # ------------------------------------------------------------------ resuming a conversation
    async def _repair(self, job: dict, ctl: RunCtl, ckpt: list) -> list:
        """Make a saved conversation valid again: every tool call needs a result. The call the user just approved is run
        by the HARNESS now (so the model need not re-issue it exactly); a denied one gets the denial as its result."""
        msgs = [m for m in ckpt if isinstance(m, dict)]
        idx = next((i for i in range(len(msgs) - 1, -1, -1) if msgs[i].get("role") == "assistant" and msgs[i].get("tool_calls")), None)
        park = job.get("park") or {}
        if idx is not None:
            answered = {m.get("tool_call_id") for m in msgs[idx + 1:] if m.get("role") == "tool"}
            used = False
            for tc in msgs[idx]["tool_calls"]:
                if tc.get("id") in answered:
                    continue
                name = (tc.get("function") or {}).get("name", "")
                if park.get("decision") and not used and name == park.get("tool"):
                    used = True
                    sig = park.get("sig") or call_sig(name, park.get("args") or {})
                    if park["decision"] in ("approve", "always"):
                        ctl.gate.approved.add(sig)
                        res = await self._execute(ctl, name, park.get("args") or {}, approved=True)
                        if res.startswith("MEDIA:"):
                            ctl.media.append(res.split(" ", 1)[0].replace("MEDIA:", ""))
                            res = f"Delivered to the user's chat via {name}. Do not write the file path in your reply."
                        content = res[:12000]
                    else:
                        why = ("nobody answered the approval request in time, so it counts as a denial"
                               if park["decision"] == "expired" else "the user DENIED this call")
                        content = (f"Not run: {why} ({park.get('summary', name)}). Do not ask for the same call again and do not "
                                   "get the same effect through another tool. Carry on with the rest of the job.")
                        ctl.gate.denied[sig] = content
                else:
                    content = "Not run: the job was paused before this call ran. Call it again if you still need it."
                msgs.append({"role": "tool", "tool_call_id": tc.get("id"), "content": content})
        msgs.append({"role": "user", "content": _NOTE + "The job was paused and is now continuing. Carry on with it from where you "
                                                         "were; do not start over and do not repeat steps that already succeeded."})
        return msgs

    # ------------------------------------------------------------------ how a run ends
    def _next_due(self, job: dict) -> float | None:
        if not job.get("rule"):
            return None
        base = self.now_local(max(self.clock(), job["due_ts"] or 0))
        return next_after(job["rule"], base).timestamp()

    def _reschedule(self, job: dict, ctl: RunCtl, *, result: str | None = None, keep_result: bool = False,
                    count_run: bool = True, outcome_status: str = "done") -> None:
        nxt = self._next_due(job)
        f: dict = {"park": None, "resume": 0, "usage": None, "attempts": 0, "last_error": None}
        if count_run:
            f.update(runs=job["runs"] + 1, last_run_ts=self.clock())
        if result is not None:
            f["last_result"] = result[:1000]
        if nxt is not None:
            f.update(status="scheduled", due_ts=nxt)
        else:
            f["status"] = outcome_status
        self.store.update(job["id"], **f)
        self.store.set_checkpoint(job["id"], None)

    async def _complete(self, job: dict, ctl: RunCtl, text: str) -> None:
        cap = int(self.cfg.get("report_max_chars", 1200))
        usage = self._usage(ctl)
        self.store.finish_run(ctl.run_id, self.clock(), "done", text, usage)
        self.store.log(job["id"], "report", text[:300], self.clock())
        silent = job["notify"] == "never" or (job["notify"] == "changes" and NOTHING_NEW.match(text))
        self._reschedule(job, ctl, result=None if NOTHING_NEW.match(text) else text)
        if silent:
            return
        msg = f"📋 **Task #{job['id']}: {job['title']}**\n{text[:cap]}" + "".join(f"\nMEDIA:{m}" for m in ctl.media)
        await self.deliver("task", msg, key=f"job:{job['id']}", source_id=job["id"])

    def _end_parked(self, job: dict, ctl: RunCtl) -> None:
        card = ctl.parked or {}
        park = {"id": "pk_" + uuid.uuid4().hex[:10], "tool": card.get("tool"), "args": card.get("args") or {}, "sig": card.get("sig"),
                "summary": card.get("summary"), "reason": card.get("reason"), "details": card.get("details", "")[:4000],
                "details_kind": card.get("details_kind", "text"), "can_always": bool(card.get("can_always")),
                "created": self.clock(), "decision": None, "notified": False}
        usage = self._usage(ctl)
        self.store.finish_run(ctl.run_id, self.clock(), "parked", park["summary"] or "", usage)
        self.store.update(job["id"], status="parked", park=park, resume=1, usage=usage)
        self.store.log(job["id"], "approval", f"waiting for your OK: {park['summary']}", self.clock())

    def _end_cancelled(self, job: dict, ctl: RunCtl) -> None:
        self.store.finish_run(ctl.run_id, self.clock(), "cancelled", "", self._usage(ctl))
        self.store.update(job["id"], status="cancelled", park=None, resume=0, usage=None)
        self.store.set_checkpoint(job["id"], None)
        self.store.log(job["id"], "note", "cancelled", self.clock())

    def _end_paused(self, job: dict, ctl: RunCtl, why: str) -> None:
        """Paused / server stopping: nothing is lost; it continues from the checkpoint when it can run again."""
        try:
            self._sync(ctl)
        except Exception:                                              # noqa: BLE001
            pass
        self.store.finish_run(ctl.run_id, self.clock(), "paused", why, self._usage(ctl))
        self.store.update(job["id"], status="scheduled", resume=1 if job["kind"] == "goal" else 0, due_ts=0, usage=self._usage(ctl))
        self.store.log(job["id"], "note", f"paused ({why}); will continue", self.clock())

    async def _wrap_up(self, job: dict, ctl: RunCtl, why: str) -> str:
        """The budget ended the run: one tool-less model call turns the work so far into an honest report."""
        steps = [r["text"] for r in self.store.log_tail(job["id"], 6) if r["kind"] == "step"]
        digest = []
        for m in ctl.messages[1:][-14:]:
            body = str(m.get("content") or "").strip()
            if m.get("role") == "tool":
                digest.append("tool result: " + body[:300])
            elif m.get("role") == "assistant" and body:
                digest.append("you wrote: " + body[:300])
            elif m.get("role") == "assistant":
                digest.append("you called: " + ", ".join((t.get("function") or {}).get("name", "?") for t in m.get("tool_calls") or []))
        if self.hooks.llm_complete is not None:
            try:
                ask = (f"A background job ran out of its {why} budget and was stopped. Job: {job['goal']}\n\nWhat happened:\n"
                       + "\n".join(digest) + "\n\nWrite a SHORT report (max 6 short lines): what is DONE (only what the results above "
                       "confirm), what is NOT done, and what you would do next. Do not guess or invent.")
                out = await self.hooks.llm_complete([{"role": "system", "content": self.hooks.persona().strip()},
                                                     {"role": "user", "content": ask}], {"temperature": 0.3, "max_tokens": 320, "timeout": 60.0})
                out = _compose.clean_report(out or "")
                if out:
                    return out
            except asyncio.CancelledError:
                raise
            except Exception as e:                                     # noqa: BLE001
                logger.warning(f"jobs: wrap-up for #{job['id']} failed: {e}")
        return (f"I used up the {why} budget after {ctl.tool_calls} tool call(s) without finishing."
                + (" Last steps: " + "; ".join(steps[-3:]) if steps else ""))

    async def _end_budget(self, job: dict, ctl: RunCtl, why: str) -> None:
        text = await self._wrap_up(job, ctl, why)
        usage = self._usage(ctl)
        self.store.finish_run(ctl.run_id, self.clock(), "budget", text, usage)
        self.store.log(job["id"], "note", f"budget used up ({why})", self.clock())
        if job.get("rule"):
            self._reschedule(job, ctl, result=text)                    # a repeating job tries again at its next time
            tail = "I will try again at the next scheduled time."
        else:
            self.store.update(job["id"], status="stopped", last_result=text[:1000], runs=job["runs"] + 1, last_run_ts=self.clock(),
                              resume=1, usage=usage, park=None)
            tail = "Say 'continue' (or press Continue in Settings > Tasks) to give it more budget."
        msg = (f"⏱️ **Task #{job['id']}: {job['title']}** - stopped, the {why} budget ({ctl.budget.describe()}) is used up.\n"
               f"{text[:int(self.cfg.get('report_max_chars', 1200))]}\n{tail}")
        await self.deliver("task", msg + "".join(f"\nMEDIA:{m}" for m in ctl.media), key=f"job:{job['id']}", source_id=job["id"])

    async def _on_error(self, job: dict, ctl: RunCtl, e: Exception) -> None:
        self.counters["errors"] += 1
        err = f"{type(e).__name__}: {e}"[:200]
        logger.warning(f"jobs: #{job['id']} run failed: {err}")
        usage = self._usage(ctl)
        try:
            self._sync(ctl)
        except Exception:                                              # noqa: BLE001
            pass
        self.store.finish_run(ctl.run_id, self.clock(), "error", err, usage)
        self.store.log(job["id"], "error", err, self.clock())
        attempts = int(job.get("attempts") or 0) + 1
        if attempts < int(self.cfg.get("max_attempts", 3)):
            keep = job["kind"] == "goal" and self.store.get_checkpoint(job["id"]) is not None
            self.store.update(job["id"], status="scheduled", attempts=attempts, last_error=err, usage=usage,
                              resume=1 if keep else 0, due_ts=self.clock() + float(self.cfg.get("retry_minutes", 10)) * 60)
            return
        nxt = self._next_due(job)
        self.store.update(job["id"], status="scheduled" if nxt else "failed", due_ts=nxt or job["due_ts"], attempts=0,
                          last_error=err, resume=0, park=None, usage=None)
        self.store.set_checkpoint(job["id"], None)
        if job["source"] != "system":
            await self.deliver("alert", _compose.template("alert", {"text": f"I could not finish task #{job['id']} \"{job['title']}\": {err}."}),
                               key=f"job-fail:{job['id']}", source_id=job["id"])
