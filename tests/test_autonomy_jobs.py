"""P8: the task queue (lucy.autonomy) - cron rules, budgets, the store, and the runner driving the REAL AgentLoop against the
scripted fake llama-server with the REAL safety layer (no GPU, no model): start rules, schedules, budgets, parking an
approval and resuming, restart recovery, pause / stop / cancel, retries, deadlines, builtin upkeep jobs, the `task` tool,
the HTTP handlers and the proactive engine as the scheduler."""
import asyncio, json, pathlib, sys, time, types
from datetime import datetime, timedelta, timezone

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from _safety_helpers import Env, safety  # noqa: E402
from test_agent_loop import Brain, start_server, tool, CALL, ANS, S  # noqa: E402
from lucy.server.agent_loop import AgentLoop, LoopConfig  # noqa: E402
from lucy.tools import registry as R  # noqa: E402
from lucy.proactive import cron, schedule, ProactiveEngine, ProactiveHost, set_engine  # noqa: E402
from lucy.proactive.config import DEFAULTS, deep_merge  # noqa: E402
from lucy.proactive.engine import UserError  # noqa: E402
from lucy.autonomy import Budget, JobHostHooks, JobRunner, JobStore, run_tool  # noqa: E402
from lucy.autonomy import web as jobs_web  # noqa: E402
from lucy.autonomy.store import STATUS_LABELS  # noqa: E402

if R.default.get("run_shell_command") is None:
    R.default.discover()                                       # real tiers for the tool names below

TZ = timezone(timedelta(hours=8))
MON_10AM = datetime(2026, 10, 19, 10, 0, 0, tzinfo=TZ).timestamp()        # a Monday morning
SCHEMAS = [tool("run_shell_command", {"command": S}, ["command"]), tool("web_search", {"query": S}, ["query"]),
           tool("read_local_file", {"file_path": S}, ["file_path"]), tool("task", {"action": S}, ["action"])]
SHELL = lambda cmd="dir": CALL("run_shell_command", {"command": cmd})
SEARCH = lambda q="price": CALL("web_search", {"query": q})


def arun(coro):
    return asyncio.run(coro)


# ------------------------------------------------------------------------------------------------ cron
def test_cron_next_matches_classic_cron_rules():
    mon = datetime(2026, 10, 19, 9, 0, tzinfo=TZ)
    nxt = lambda e, a=mon: cron.next_after(e, a).strftime("%a %Y-%m-%d %H:%M")
    assert nxt("30 8 * * mon-fri") == "Tue 2026-10-20 08:30"
    assert nxt("*/15 * * * *") == "Mon 2026-10-19 09:15"
    assert nxt("0 22 * * sun") == "Sun 2026-10-25 22:00"
    assert nxt("0 9 31 * *") == "Sat 2026-10-31 09:00"
    assert nxt("0 9 1 * mon") == "Mon 2026-10-26 09:00"          # day-of-month AND day-of-week restricted -> either
    assert nxt("0 9 * * 7") == "Sun 2026-10-25 09:00" and nxt("@hourly") == "Mon 2026-10-19 10:00"
    assert nxt("5 0 29 feb *") == "Tue 2028-02-29 00:05"
    assert nxt("0 7 * jan,jul *") == "Fri 2027-01-01 07:00"
    assert cron.next_after("0 9 * * *", mon) > mon                  # strictly later, never "now"
    assert cron.next_after("0 9 * * *", datetime(2026, 10, 19, 9, 0, 30, tzinfo=TZ)).day == 20


def test_cron_rejects_nonsense_with_a_readable_reason():
    for bad in ["", "* * * *", "61 * * * *", "* 24 * * *", "* * 0 * *", "* * * 13 *", "a b c d e", "*/0 * * * *", "5-1 * * * *", "* * * * funday"]:
        try:
            cron.validate(bad); assert False, bad
        except cron.CronError as e:
            assert str(e)
    try:
        cron.next_after("0 0 31 feb *", datetime(2026, 1, 1, tzinfo=TZ)); assert False
    except cron.CronError as e:
        assert "never matches" in str(e)
    assert cron.describe("30 8 * * mon-fri").endswith("08:30") and cron.describe("@daily") == "daily"


def test_cron_is_a_repeat_rule_for_reminders_and_jobs():
    first = datetime(2026, 10, 19, 9, 0, tzinfo=TZ)
    r = schedule.make_rule(None, None, first, cron=" 0  7 * * 1-5 ")
    assert r == {"type": "cron", "expr": "0 7 * * 1-5"}
    assert schedule.next_after(r, first).isoformat() == "2026-10-20T07:00:00+08:00"
    assert "07:00" in schedule.describe_rule(r)
    try:
        schedule.make_rule(None, None, first, cron="nope"); assert False
    except ValueError:
        pass
    assert schedule.make_rule("daily", None, first)["type"] == "daily"          # nothing else changed


# ------------------------------------------------------------------------------------------------ budget + store
def test_budget_is_clamped_and_reports_what_ran_out():
    b = Budget.make({"seconds": 1, "tool_calls": 10 ** 9, "tokens": "junk"})
    assert b.seconds == 60 and b.tool_calls == 600 and b.tokens == 80000
    assert Budget.make({"seconds": 99999999}, None, {"seconds": 7200}).seconds == 7200
    assert Budget(seconds=100, tool_calls=5, tokens=9000).exceeded({"seconds": 99, "tool_calls": 5, "tokens": 1}) == "tool calls"
    assert Budget().exceeded({"seconds": 1, "tool_calls": 1, "tokens": 1}) is None


def test_store_orders_by_deadline_and_recovers_running_jobs(tmp_path):
    s = JobStore(tmp_path / "j.sqlite")
    a = s.add(title="late", goal="g" * 10, due_ts=100, now=1)
    b = s.add(title="urgent", goal="g" * 10, due_ts=200, deadline_ts=500, now=1)
    c = s.add(title="future", goal="g" * 10, due_ts=10 ** 9, now=1)
    assert [j["id"] for j in s.due(300)] == [b, a]                  # nearest deadline first, a not-yet-due job never
    s.update(a, status="running", usage={"seconds": 3, "tool_calls": 1, "tokens": 5})
    s.set_checkpoint(a, [{"role": "user", "content": "x"}])
    assert s.recover() == [a]
    j = s.get(a)
    assert j["status"] == "scheduled" and j["resume"] == 1 and j["usage"]["tool_calls"] == 1
    assert s.get_checkpoint(a) == [{"role": "user", "content": "x"}]
    assert "checkpoint" not in s.get(a) and s.get(c)["rule"] is None


def test_store_counts_goal_runs_only_and_keeps_a_short_log(tmp_path):
    s = JobStore(tmp_path / "j.sqlite")
    g = s.add(title="g", goal="goal goal", now=1)
    u = s.add(title="u", kind="builtin", handler="h", source="system", now=1)
    for jid in (g, u):
        rid = s.start_run(jid, 1000.0); s.finish_run(rid, 1010.0, "done", "ok", {"tool_calls": 4, "seconds": 9, "tokens": 1})
    assert s.usage_since(0) == {"runs": 1, "tool_calls": 4, "seconds": 9.0}      # upkeep does not eat the daily caps
    for i in range(200):
        s.log(g, "step", f"line {i}", 1.0)
    tail = s.log_tail(g, 500)
    assert len(tail) == 150 and tail[-1]["text"] == "line 199"


def test_followup_daily_cap_ignores_job_reports(tmp_path):
    from lucy.proactive.store import Store
    st = Store(tmp_path / "p.sqlite")
    st.add_event("task", "a follow-up", key="task:1", ts=100.0)
    st.add_event("task", "a job report", key="job:7", ts=100.0)
    st.add_event("task", "a job notice", key="job-park:7", ts=100.0)
    assert st.task_runs_since(0) == 1


# ------------------------------------------------------------------------------------------------ the world: runner + real loop
class World:
    """JobRunner wired to the real AgentLoop (scripted fake llama-server), the real SafetyLayer and a fake clock."""

    def __init__(self, tmp_path, monkeypatch, mode="ask_for_risky", jobs=None):
        self.env = Env(mode=mode, monkeypatch=monkeypatch)
        self.srv = start_server()
        self.t = MON_10AM
        self.calls, self.delivered = [], []
        self.busy = self.paused = self.quiet = self.rest = False
        self.idle, self.llm_up, self.last_user = True, True, 0.0
        self.on_exec = None
        self.cfg = deep_merge(DEFAULTS, {"jobs": {"poll_seconds": 0.02, "retry_minutes": 1, **(jobs or {})}})
        self.store = JobStore(tmp_path / "jobs.sqlite")
        self.summary = "Partial report."
        hooks = JobHostHooks(make_agent=self._make_agent, execute=self._exec, layer=lambda: self.env.layer,
                             tier_of=self.env.layer._tier_of, paused=lambda: self.paused, llm_ready=self._ready,
                             llm_complete=self._complete, persona=lambda: "You are Lucy.")
        pol = types.SimpleNamespace(pause_state=lambda: (self.pro_paused, "all", 0.0), is_quiet=lambda: self.quiet,
                                    is_rest_day=lambda: self.rest)
        self.pro_paused = False
        self.runner = JobRunner(self.store, hooks, cfg=lambda: self.cfg, clock=lambda: self.t,
                                now_local=lambda ts=None: datetime.fromtimestamp(self.t if ts is None else ts, TZ), policy=pol,
                                deliver=self._deliver, llm_idle=lambda now: self.idle, busy=lambda: self.busy,
                                last_user_ts=lambda: self.last_user)

    async def _ready(self): return self.llm_up
    async def _complete(self, msgs, opts): return self.summary
    async def _deliver(self, kind, text, *, key="", source_id=None, **kw): self.delivered.append((kind, text, key))

    async def _exec(self, name, args):
        self.calls.append((name, args))
        if self.on_exec:
            r = self.on_exec(name, args)
            r = await r if asyncio.iscoroutine(r) else r
            if r is not None:
                return r
        return f"result of {name}"

    def _make_agent(self, job, execute, gate, session_id, access, read_tools):
        return AgentLoop(brain=Brain(self.srv.server_address[1]), tools=SCHEMAS, execute=execute, activity_for=lambda n, a: n,
                         count_tokens=lambda t: max(1, len(t) // 4), temperature=0.3, cfg=LoopConfig(plan_tool=False),
                         gate=gate, session_id=session_id)

    def script(self, *steps): self.srv.script = list(steps)
    def add(self, goal="Check the price of the RTX 4090 and report it.", **kw): return self.runner.add_job(goal=goal, **kw)["id"]
    def job(self, jid=1): return self.store.get(jid)
    def texts(self): return [t for _, t, _ in self.delivered]

    async def step(self):
        """One scheduler tick; waits for the run it started (if any) to end."""
        await self.runner.tick(self.t)
        task = self.runner._task
        if task is not None and not task.done():
            await asyncio.wait_for(asyncio.shield(task), 30)

    def run(self, coro):
        try:
            return asyncio.run(coro)
        finally:
            self.srv.shutdown(); self.env.close()


def world(tmp_path, monkeypatch, **kw):
    return World(tmp_path, monkeypatch, **kw)


# ------------------------------------------------------------------------------------------------ a normal run
def test_a_job_runs_through_the_real_loop_and_posts_its_report(tmp_path, monkeypatch):
    w = world(tmp_path, monkeypatch)
    w.script(SEARCH(), ANS("RTX 4090 costs 1,799 at the main shop."))

    async def go():
        jid = w.add(title="RTX price")
        await w.step()
        return jid
    jid = w.run(go())
    j = w.job(jid)
    assert j["status"] == "done" and j["runs"] == 1 and "1,799" in j["last_result"] and j["resume"] == 0
    assert w.calls == [("web_search", {"query": "price"})]
    assert [k for k, _, _ in w.delivered] == ["task"] and "Task #1: RTX price" in w.texts()[0] and "1,799" in w.texts()[0]
    sys_msg, brief = w.srv.requests[0]["messages"][0], w.srv.requests[0]["messages"][1]
    assert "BACKGROUND JOB" in sys_msg["content"] and "RTX 4090" in brief["content"] and "Monday 2026-10-19 10:00" in brief["content"]
    run = w.store.recent_runs(jid)[0]
    assert run["outcome"] == "done" and run["tool_calls"] == 1 and run["tokens"] > 0
    assert w.store.get_checkpoint(jid) is None and w.job(jid)["usage"] is None
    assert any(r["kind"] == "step" and "web_search" in r["text"] for r in w.store.log_tail(jid))


def test_a_repeating_job_is_rescheduled_and_changes_only_stays_silent(tmp_path, monkeypatch):
    w = world(tmp_path, monkeypatch)
    w.script(SEARCH("a"), ANS("Price 100."), SEARCH("b"), ANS("NOTHING NEW"), SEARCH("c"), ANS("Price dropped to 90."))

    async def go():
        jid = w.add(every_minutes=30, notify="changes", goal="Watch the price of the RTX 4090 and report changes.")
        await w.step()
        first = w.job(jid)
        w.t = first["due_ts"] + 5
        await w.step()
        second = w.job(jid)
        w.t = second["due_ts"] + 5
        await w.step()
        return jid, first, second
    jid, first, second = w.run(go())
    assert first["status"] == "scheduled" and first["due_ts"] > MON_10AM and first["runs"] == 1 and "100" in first["last_result"]
    assert second["runs"] == 2 and second["last_result"] == first["last_result"]               # "nothing new" keeps the last REPORT
    assert len(w.delivered) == 2 and "Price 100" in w.texts()[0] and "dropped to 90" in w.texts()[1]
    assert "compare with it" in w.srv.requests[2]["messages"][1]["content"]                    # run 2 saw the previous report
    assert "NOTHING NEW" in w.srv.requests[2]["messages"][1]["content"]


# ------------------------------------------------------------------------------------------------ when a job may start
def test_start_rules_quiet_pause_busy_chatting_model_idle_and_caps(tmp_path, monkeypatch):
    w = world(tmp_path, monkeypatch, jobs={"max_runs_per_day": 1})
    w.script(ANS("one."), ANS("two."), ANS("three."))

    async def go():
        a = w.add(goal="First job: summarize today's tech news.")
        why = {}
        for name, flip in [("quiet", lambda v: setattr(w, "quiet", v)), ("busy", lambda v: setattr(w, "busy", v)),
                           ("chatting", lambda v: setattr(w, "idle", not v)), ("paused", lambda v: setattr(w, "paused", v)),
                           ("proactive pause", lambda v: setattr(w, "pro_paused", v)), ("model down", lambda v: setattr(w, "llm_up", not v))]:
            flip(True)
            await w.step()
            why[name] = w.job(a)["status"]
            flip(False)
        assert set(why.values()) == {"scheduled"}                         # nothing started under any blocker
        assert w.calls == [] and w.srv.requests == []
        await w.step()                                                    # all clear: it runs
        assert w.job(a)["status"] == "done"
        b = w.add(goal="Second job: summarize the sports news.")
        await w.step()
        capped = w.job(b)["status"]                                       # daily cap of 1 run reached
        w.runner.run_now(b)                                               # the user's "Run now" ignores the cap
        await w.step()
        c = w.add(goal="Third job when quiet hours: check the weather.")
        w.quiet = True
        await w.step()
        quiet_wait = w.job(c)["status"]
        w.runner.run_now(c)                                               # ... and quiet hours
        await w.step()
        return capped, w.job(b)["status"], quiet_wait, w.job(c)["status"]
    capped, b_after, quiet_wait, c_after = w.run(go())
    assert (capped, b_after, quiet_wait, c_after) == ("scheduled", "done", "scheduled", "done")


def test_idle_jobs_wait_until_the_user_has_been_quiet(tmp_path, monkeypatch):
    w = world(tmp_path, monkeypatch)
    w.script(ANS("Tidied."))

    async def go():
        jid = w.add(goal="Re-index my notes folder when I am not around.", idle=True)
        w.last_user = w.t - 60                                            # chatted a minute ago; needs 10 minutes
        await w.step()
        waiting = w.job(jid)["status"]
        w.last_user = w.t - 11 * 60
        await w.step()
        return waiting, w.job(jid)["status"]
    assert w.run(go()) == ("scheduled", "done")


def test_goal_jobs_run_on_rest_days_but_upkeep_jobs_do_not(tmp_path, monkeypatch):
    w = world(tmp_path, monkeypatch)
    w.script(ANS("done"))
    ran = []
    w.runner.register_builtin("tidy", lambda: ran.append(1) or "tidied", label="Tidy", needs_llm=False)

    async def go():
        w.runner.ensure_system_jobs()
        w.store.update(1, quiet_ok=1, rest_ok=0)                          # (a system job that must respect rest days)
        w.rest = True
        jid = w.add(goal="Summarize the news for the user.")
        await w.step(); await w.step()
        return jid
    jid = w.run(go())
    assert w.job(jid)["status"] == "done" and ran == []


# ------------------------------------------------------------------------------------------------ tool limits inside a job
def test_read_jobs_only_get_read_tools_and_no_job_can_spawn_jobs(tmp_path, monkeypatch):
    w = world(tmp_path, monkeypatch, mode="full_auto")
    w.script(SHELL("whoami"), CALL("task", {"action": "add"}), SEARCH(), ANS("Looked it up."))

    async def go():
        jid = w.add(goal="Look something up on the web for me.")
        await w.step()
        return jid
    jid = w.run(go())
    tool_msgs = [m["content"] for m in w.srv.requests[-1]["messages"] if m["role"] == "tool"]
    assert "read-only tools only" in tool_msgs[0] and "Not available in a background job: task" in tool_msgs[1]
    assert w.calls == [("web_search", {"query": "price"})]                 # shell and task never executed
    assert w.job(jid)["status"] == "done"


def test_a_full_job_cannot_use_blocked_tools_even_in_full_auto(tmp_path, monkeypatch):
    w = world(tmp_path, monkeypatch, mode="full_auto")
    w.script(CALL("task", {"action": "list"}), ANS("Done without it."))

    async def go():
        jid = w.runner.add_job(goal="Do some housekeeping for the user.", access="full", source="user")["id"]
        await w.step()
        return jid
    jid = w.run(go())
    assert w.calls == [] and w.job(jid)["status"] == "done"


# ------------------------------------------------------------------------------------------------ approvals: park -> answer -> resume
def test_a_risky_call_parks_the_job_and_the_harness_runs_it_once_after_approval(tmp_path, monkeypatch):
    w = world(tmp_path, monkeypatch)
    w.script(SHELL("dir"), ANS("Listed the folder."))

    async def go():
        jid = w.runner.add_job(goal="List the files in the work folder for me.", access="full", source="user")["id"]
        await w.step()
        parked = w.job(jid)
        card = w.runner.card(parked)
        await w.runner.tick(w.t)                                           # parked jobs wait: nothing else starts
        assert w.calls == [] and w.runner._task.done()
        w.runner.decide(jid, "approve", parked["park"]["id"])
        await w.step()
        return jid, parked, card
    jid, parked, card = w.run(go())
    assert parked["status"] == "parked" and parked["park"]["tool"] == "run_shell_command" and parked["park"]["details"] == "dir"
    assert card["parked"]["summary"].startswith("Run a") and card["parked"]["can_always"] is True and "args" not in card["parked"]
    assert w.calls == [("run_shell_command", {"command": "dir"})]          # ran ONCE, by the harness, with the approved args
    tm = [m for m in w.srv.requests[-1]["messages"] if m["role"] == "tool"]
    assert tm[0]["content"] == "result of run_shell_command"
    assert "paused and is now continuing" in w.srv.requests[-1]["messages"][-1]["content"]
    j = w.job(jid)
    assert j["status"] == "done" and "Listed the folder" in j["last_result"] and j["park"] is None
    assert [k for k, _, _ in w.delivered].count("task") == 2 and "needs your OK" in w.texts()[0]   # the notice, then the report
    assert [r["outcome"] for r in reversed(w.store.recent_runs(jid))] == ["parked", "done"]


def test_a_denied_call_is_a_result_the_job_works_around_and_is_not_asked_again(tmp_path, monkeypatch):
    w = world(tmp_path, monkeypatch)
    w.script(SHELL("dir"), SHELL("dir"), ANS("I could not list it; the user said no."))

    async def go():
        jid = w.runner.add_job(goal="List the files in the work folder for me.", access="full", source="user")["id"]
        await w.step()
        w.runner.decide(jid, "deny")
        await w.step()
        return jid
    jid = w.run(go())
    assert w.calls == []
    tm = [m["content"] for m in w.srv.requests[-1]["messages"] if m["role"] == "tool"]
    assert "DENIED" in tm[0] and "another tool" in tm[0] and "DENIED" in tm[1]      # the identical retry is refused, not parked again
    assert w.job(jid)["status"] == "done" and w.job(jid)["runs"] == 1


def test_an_unanswered_approval_expires_into_a_denial_and_the_job_finishes(tmp_path, monkeypatch):
    w = world(tmp_path, monkeypatch, jobs={"park_timeout_hours": 1})
    w.script(SHELL("dir"), ANS("Reported without the listing."))

    async def go():
        jid = w.runner.add_job(goal="List the files in the work folder for me.", access="full", source="user")["id"]
        await w.step()
        w.t += 30 * 60
        await w.step()
        still = w.job(jid)["status"]
        w.t += 40 * 60
        await w.step()
        return jid, still
    jid, still = w.run(go())
    assert still == "parked" and w.calls == []
    tm = [m["content"] for m in w.srv.requests[-1]["messages"] if m["role"] == "tool"]
    assert "nobody answered" in tm[0] and w.job(jid)["status"] == "done"


def test_always_allow_covers_the_same_tool_for_the_rest_of_the_job(tmp_path, monkeypatch):
    w = world(tmp_path, monkeypatch)
    w.script(SHELL("dir"), SHELL("echo two"), ANS("Both ran."))

    async def go():
        jid = w.runner.add_job(goal="Run two harmless commands for me.", access="full", source="user")["id"]
        await w.step()
        w.runner.decide(jid, "always")
        await w.step()
        return jid
    jid = w.run(go())
    assert [c[1]["command"] for c in w.calls] == ["dir", "echo two"]        # no second card
    assert w.job(jid)["allow_tools"] == ["run_shell_command"] and w.job(jid)["status"] == "done"


def test_a_paused_safety_layer_refuses_instead_of_parking(tmp_path, monkeypatch):
    w = world(tmp_path, monkeypatch)
    w.script(SHELL("dir"), ANS("Could not run it."))

    async def go():
        jid = w.runner.add_job(goal="List the files in the work folder for me.", access="full", source="user")["id"]
        w.env.layer.set_paused(True)
        w.paused = False                                                   # (the runner's own pause check is separate)
        await w.step()
        return jid
    jid = w.run(go())
    tm = [m["content"] for m in w.srv.requests[-1]["messages"] if m["role"] == "tool"]
    assert "paused by the user" in tm[0] and w.calls == [] and w.job(jid)["status"] == "done"


def test_the_parked_notice_waits_for_quiet_hours_to_end_and_answers_check_the_card(tmp_path, monkeypatch):
    w = world(tmp_path, monkeypatch)
    w.script(SHELL("dir"), ANS("ok"))

    async def go():
        jid = w.runner.add_job(goal="List the files in the work folder for me.", access="full", source="user")["id"]
        await w.step()
        w.quiet = True
        await w.runner.tick(w.t)
        held = len(w.delivered)
        w.quiet = False
        await w.runner.tick(w.t); await w.runner.tick(w.t)                 # sent once, not repeatedly
        for bad in [("nonsense", None), ("approve", "pk_wrong")]:
            try:
                w.runner.decide(jid, bad[0], bad[1]); assert False
            except UserError:
                pass
        return held, len(w.delivered)
    held, after = w.run(go())
    assert (held, after) == (0, 1)


# ------------------------------------------------------------------------------------------------ budgets
def test_the_tool_call_budget_stops_the_job_with_an_honest_report_and_continue_finishes_it(tmp_path, monkeypatch):
    w = world(tmp_path, monkeypatch)
    w.summary = "Done: searched a, b, c. Not done: the comparison."
    w.script(SEARCH("a"), SEARCH("b"), SEARCH("c"), SEARCH("d"), ANS("Finished the comparison."), ANS("Finished the comparison."))

    async def go():
        jid = w.add(goal="Compare prices of three shops for the RTX 4090.")
        w.store.update(jid, budget={"seconds": 600, "tool_calls": 3, "tokens": 80000})
        await w.step()
        stopped, calls_at_stop = w.job(jid), list(w.calls)
        w.runner.extend(jid)
        await w.step()
        return jid, stopped, calls_at_stop
    jid, stopped, calls_at_stop = w.run(go())
    assert stopped["status"] == "stopped" and stopped["resume"] == 1 and stopped["usage"]["tool_calls"] == 3
    assert "tool calls budget" in w.texts()[0] and "Not done: the comparison" in w.texts()[0] and "Continue" in w.texts()[0]
    assert len(calls_at_stop) == 3                                         # never more than the budget before it stopped
    j = w.job(jid)
    assert j["status"] == "done" and "Finished the comparison" in j["last_result"] and j["budget"]["tool_calls"] > 3
    assert [r["outcome"] for r in reversed(w.store.recent_runs(jid))] == ["budget", "done"]


def test_the_time_budget_uses_the_clock_and_waiting_for_the_user_is_not_billed(tmp_path, monkeypatch):
    w = world(tmp_path, monkeypatch)
    w.script(SEARCH("a"), SEARCH("b"), ANS("never"))

    def slow(name, args):
        if name == "web_search" and args["query"] == "a":
            w.t += 90                                                      # the first tool "took" 90 s of a 60 s budget
    w.on_exec = slow

    async def go():
        jid = w.add(goal="Research something thoroughly for me.", minutes=1)
        await w.step()
        return jid
    jid = w.run(go())
    assert w.job(jid)["status"] == "stopped" and "time budget" in w.texts()[0]


def test_chat_has_priority_between_tool_calls_and_the_wait_is_free(tmp_path, monkeypatch):
    w = world(tmp_path, monkeypatch)
    w.script(SEARCH("a"), ANS("Done."))

    async def go():
        jid = w.add(goal="Look up a thing and report it to me.")
        w.busy = False
        await w.runner.tick(w.t)                                           # starts
        await asyncio.sleep(0.01)
        w.busy = True                                                      # the user starts chatting while it works
        await asyncio.sleep(0.25)
        mid = list(w.calls)
        w.busy = False
        await asyncio.wait_for(asyncio.shield(w.runner._task), 20)
        return jid, mid
    jid, mid = w.run(go())
    assert w.job(jid)["status"] == "done" and len(w.calls) == 1
    assert w.store.recent_runs(jid)[0]["seconds"] < 1                      # the 0.25 s wait was not billed (fake clock never moved)


# ------------------------------------------------------------------------------------------------ pause / stop / restart / cancel
def test_pausing_mid_run_keeps_the_work_and_continues_when_resumed(tmp_path, monkeypatch):
    w = world(tmp_path, monkeypatch)
    w.script(SEARCH("a"), SEARCH("b"), ANS("All done."))

    async def pause_after_first(name, args):
        if args["query"] == "a":
            w.paused = True
            await asyncio.sleep(0.3)                                       # still working when the supervisor notices the pause
    w.on_exec = pause_after_first

    async def go():
        jid = w.add(goal="Research two things and report both.")
        await w.step()
        mid, mid_calls = w.job(jid), list(w.calls)
        w.paused = False
        w.on_exec = None
        await w.step()
        return jid, mid, mid_calls
    jid, mid, mid_calls = w.run(go())
    assert mid["status"] == "scheduled" and mid["resume"] == 1 and [c[1]["query"] for c in mid_calls] == ["a"]
    assert w.job(jid)["status"] == "done" and w.job(jid)["runs"] == 1 and [c[1]["query"] for c in w.calls] == ["a", "b"]
    assert any("Not run: the job was paused" in str(m.get("content")) for m in w.srv.requests[1]["messages"])
    assert [r["outcome"] for r in reversed(w.store.recent_runs(jid))] == ["paused", "done"]


def test_server_stop_checkpoints_and_the_next_start_continues(tmp_path, monkeypatch):
    w = world(tmp_path, monkeypatch)
    w.script(SEARCH("a"), SEARCH("b"), ANS("Finished after the restart."))
    gate = {"hold": True}

    async def hold_second(name, args):
        if args["query"] == "b" and gate["hold"]:
            await asyncio.sleep(30)                                        # a slow tool: the server stops while it runs
    w.on_exec = hold_second

    async def go():
        jid = w.add(goal="Research two things and report both.")
        await w.runner.tick(w.t)
        for _ in range(200):
            await asyncio.sleep(0.02)
            if len(w.calls) >= 2:
                break
        await w.runner.stop()
        stopped = w.job(jid)
        gate["hold"] = False
        w.runner.recover()
        await w.step()
        return jid, stopped
    jid, stopped = w.run(go())
    assert stopped["status"] == "scheduled" and stopped["resume"] == 1
    j = w.job(jid)
    assert j["status"] == "done" and "after the restart" in j["last_result"]
    last = w.srv.requests[-1]["messages"]
    assert any("Not run: the job was paused" in str(m.get("content")) for m in last if m["role"] == "tool")   # the interrupted call is told so


def test_a_crash_leaves_a_running_job_that_recover_continues_from_its_checkpoint(tmp_path, monkeypatch):
    w = world(tmp_path, monkeypatch)
    w.script(ANS("Continued and finished."))

    async def go():
        jid = w.add(goal="Research something and report it to me.")
        w.store.update(jid, status="running", usage={"seconds": 40, "tool_calls": 1, "tokens": 100})
        w.store.set_checkpoint(jid, [
            {"role": "user", "content": "the brief"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "web_search", "arguments": "{\"query\": \"x\"}"}}]},
            {"role": "tool", "tool_call_id": "c1", "content": "found it"}])
        w.runner.recover()                                                 # what engine.start() does
        await w.step()
        return jid
    jid = w.run(go())
    msgs = w.srv.requests[0]["messages"]
    assert [m["role"] for m in msgs] == ["system", "user", "assistant", "tool", "user"] and msgs[3]["content"] == "found it"
    assert w.job(jid)["status"] == "done"


def test_cancel_stops_a_running_job_and_removes_a_waiting_one(tmp_path, monkeypatch):
    w = world(tmp_path, monkeypatch)
    w.script(SEARCH("a"), ANS("never"))

    async def slow(name, args):
        await asyncio.sleep(30)
    w.on_exec = slow

    async def go():
        running = w.add(goal="Research something slowly for me.")
        waiting = w.add(goal="Another job that will be cancelled.", when="in 3 hours")
        await w.runner.tick(w.t)
        for _ in range(200):
            await asyncio.sleep(0.02)
            if w.calls:
                break
        w.runner.cancel(running)
        w.runner.cancel(waiting)
        await asyncio.wait_for(asyncio.shield(w.runner._task), 20)
        try:
            w.runner.cancel(running); assert False
        except UserError as e:
            assert "already cancelled" in str(e)
        return running, waiting
    running, waiting = w.run(go())
    assert w.job(running)["status"] == "cancelled" and w.job(waiting)["status"] == "cancelled" and w.delivered == []
    assert w.store.get_checkpoint(running) is None


# ------------------------------------------------------------------------------------------------ errors, deadlines, validation
def test_a_failing_run_is_retried_from_its_checkpoint_then_reported_as_failed(tmp_path, monkeypatch):
    w = world(tmp_path, monkeypatch, jobs={"max_attempts": 2, "retry_minutes": 1})
    bad = {"status": 400, "body": "model exploded"}

    async def go():
        jid = w.add(goal="Research something and report it to me.")
        w.script(bad); await w.step()
        first = w.job(jid)
        w.t += 120
        w.script(bad); await w.step()
        return jid, first
    jid, first = w.run(go())
    assert first["status"] == "scheduled" and first["attempts"] == 1 and first["last_error"] and first["due_ts"] > MON_10AM
    j = w.job(jid)
    assert j["status"] == "failed" and j["last_error"]
    assert [k for k, _, _ in w.delivered] == ["alert"] and "could not finish task #1" in w.texts()[0]


def test_deadlines_set_the_order_and_raise_one_alert_when_missed(tmp_path, monkeypatch):
    w = world(tmp_path, monkeypatch)
    w.script(ANS("urgent done"), ANS("relaxed done"))

    async def go():
        a = w.add(goal="The relaxed job: tidy up my notes list.")
        b = w.add(goal="The urgent job: draft the weekly report.", deadline="in 3 hours")
        await w.step()
        first_run = w.job(b)["status"], w.job(a)["status"]
        late = w.add(goal="Never runs before the deadline passes.", when="in 2 days", deadline="in 1 hour")
        w.t += 2 * 3600
        await w.runner.tick(w.t); await w.runner.tick(w.t)
        return first_run, late
    first_run, late = w.run(go())
    assert first_run == ("done", "scheduled")                              # nearest deadline first
    alerts = [t for k, t, key in w.delivered if k == "alert"]
    assert len(alerts) == 1 and "passed its deadline" in alerts[0]


def test_add_job_validates_and_a_model_job_with_full_tools_waits_for_the_users_ok(tmp_path, monkeypatch):
    w = world(tmp_path, monkeypatch, jobs={"max_active": 3})

    async def go():
        r = w.runner
        for bad in [dict(goal="short"), dict(goal="x" * 1600), dict(goal="Check the weather later.", when="blah"),
                    dict(goal="Check the weather later.", when="2020-01-01 09:00"), dict(goal="Check the weather later.", cron="nope"),
                    dict(goal="Check the weather later.", repeat="sometimes"), dict(goal="Check the weather later.", every_minutes=2)]:
            try:
                r.add_job(**bad); assert False, bad
            except UserError:
                pass
        c = r.add_job(goal="Check the weather every weekday.", cron="30 8 * * mon-fri", title="Weather")
        assert c["repeat"].endswith("08:30") and c["due"] > w.t and c["access"] == "read" and c["status"] == "scheduled"
        m = r.add_job(goal="Clean up my downloads folder, please.", access="full", source="model")
        u = r.add_job(goal="Clean up my downloads folder, please.", access="full", source="user")
        assert (m["status"], u["status"]) == ("waiting_ok", "scheduled")
        await w.step()                                                     # a job waiting for OK never starts
        assert w.job(m["id"])["status"] == "waiting_ok"
        r.ok_to_start(m["id"])
        assert w.job(m["id"])["status"] == "scheduled"
        for n in range(6):                                                 # the cap (3 active) is reached within a few more
            try:
                r.add_job(goal=f"Extra job number {n} to fill the list.")
            except UserError as e:
                assert "too many" in str(e)
                break
        else:
            assert False, "no cap"
    w.script(ANS("ok"))
    w.run(go())


# ------------------------------------------------------------------------------------------------ builtin upkeep jobs
def test_builtin_jobs_are_seeded_once_run_silently_and_do_not_count_when_idle(tmp_path, monkeypatch):
    w = world(tmp_path, monkeypatch)
    out = {"v": ""}
    w.runner.register_builtin("review", lambda: out["v"], label="Weekly review", every_minutes=10)
    w.runner.register_builtin("night", lambda: "ran at night", label="Night job", cron="0 3 * * *")

    async def go():
        assert w.runner.ensure_system_jobs() == 2 and w.runner.ensure_system_jobs() == 0
        w.quiet = True                                                     # upkeep runs in quiet hours (it says nothing)
        await w.step()
        nothing_due = (w.job(1)["runs"], w.store.recent_runs(1))
        w.t = w.job(1)["due_ts"] + 1
        out["v"] = "Reviewed: 3 ideas."
        await w.step()
        later = w.job(1)
        w.runner.cancel(2)
        assert w.runner.ensure_system_jobs() == 0                          # a cancelled upkeep job is not recreated
        return nothing_due, later
    nothing_due, later = w.run(go())
    assert nothing_due == (0, []) and later["runs"] == 1 and later["last_result"] == "Reviewed: 3 ideas." and later["status"] == "scheduled"
    assert w.delivered == [] and w.job(2)["status"] == "cancelled"
    assert w.store.usage_since(0)["runs"] == 0                             # upkeep is not part of the daily goal caps


def test_a_builtin_that_raises_is_retried_and_never_breaks_the_queue(tmp_path, monkeypatch):
    w = world(tmp_path, monkeypatch, jobs={"max_attempts": 2})

    def boom():
        raise RuntimeError("disk on fire")
    w.runner.register_builtin("boom", boom, label="Boom", every_minutes=10)

    async def go():
        w.runner.ensure_system_jobs()
        await w.step()
        first = w.job(1)
        w.t += 120
        await w.step()
        return first, w.job(1)
    first, second = w.run(go())
    assert first["attempts"] == 1 and "disk on fire" in first["last_error"]
    assert second["status"] == "scheduled" and second["attempts"] == 0 and w.delivered == []      # system jobs never nag


# ------------------------------------------------------------------------------------------------ tool + routes + engine
class Eng:
    """The real ProactiveEngine as the scheduler, with the job runner on the real loop + safety layer."""

    def __init__(self, tmp_path, monkeypatch):
        self.w = World(tmp_path, monkeypatch)
        self.msgs = []
        self.t = MON_10AM
        host = ProactiveHost(root=tmp_path, runtime=tmp_path / "runtime", append_message=lambda s, r, c: self.msgs.append((s, r, c)),
                             pick_session=lambda: "sess1", tz=lambda: TZ, persona=lambda: "You are Lucy.",
                             job_hooks=self.w.runner.hooks)
        self.eng = ProactiveEngine(host, clock=lambda: self.t)
        self.eng.cfg["jobs"]["poll_seconds"] = 0.02
        self.eng.cfg["delivery"]["sticker_chance"] = 0
        self.eng.last_user_ts = self.t - 600
        set_engine(self.eng)

    def close(self):
        set_engine(None)
        self.w.srv.shutdown(); self.w.env.close()


def test_the_proactive_engine_is_the_scheduler_and_posts_the_report(tmp_path, monkeypatch):
    e = Eng(tmp_path, monkeypatch)
    e.w.script(SEARCH("x"), ANS("Found the answer: 42."))

    async def go():
        await e.eng.start()
        await e.eng.stop()                                                 # (start + stop work with an idle queue)
        job = e.eng.jobs.add_job(goal="Find the answer to everything.", title="Answer")
        await e.eng.tick()
        await asyncio.wait_for(asyncio.shield(e.eng.jobs._task), 20)
        return job
    try:
        job = arun(go())
        assert e.eng.jobs.store.get(job["id"])["status"] == "done"
        posted = [c for s, r, c in e.msgs if "Task #1" in c]
        assert posted and "Found the answer: 42." in posted[0]
        st = e.eng.status()["jobs"]
        assert st["enabled"] and st["running"] is None and st["today"]["runs"] == 1
    finally:
        e.close()


def test_the_task_tool_adds_lists_shows_status_cancels_and_cannot_approve(tmp_path, monkeypatch):
    e = Eng(tmp_path, monkeypatch)

    async def go():
        out = {}
        out["bad"] = await run_tool("task", {"action": "add", "goal": "x"})
        out["add"] = await run_tool("task", {"action": "add", "goal": "Check the RTX 4090 price every morning.", "when": "tomorrow 9am",
                                             "repeat": "daily", "notify": "changes", "title": "RTX price"}, "chat1")
        out["full"] = await run_tool("task", {"action": "add", "goal": "Tidy my downloads folder for me.", "access": "full"})
        out["list"] = await run_tool("task", {"action": "list"})
        out["status"] = await run_tool("task", {"action": "status", "id": 1})
        out["noid"] = await run_tool("task", {"action": "cancel"})
        out["resume_wait"] = await run_tool("task", {"action": "resume", "id": 2})
        out["unknown"] = await run_tool("task", {"action": "cancel", "id": 99})
        out["cancel"] = await run_tool("task", {"action": "cancel", "id": 1})
        out["list2"] = await run_tool("task", {"action": "list"})
        out["weird"] = await run_tool("task", {"action": "dance"})
        return out
    try:
        o = arun(go())
        assert o["bad"].startswith("Error:") and "complete instruction" in o["bad"]
        assert o["add"].startswith("Task #1 created: RTX price.") and "repeating daily" in o["add"] and "Budget: 20 min" in o["add"]
        assert "will NOT start until the user presses OK" in o["full"]
        assert "#1 RTX price [scheduled; next in" in o["list"] and "#2 Tidy my downloads folder for me. [needs your OK to start" in o["list"]
        assert "Goal: Check the RTX 4090 price" in o["status"]
        assert o["noid"].startswith("Error:") and "USER to press OK" in o["resume_wait"] and "no task #99" in o["unknown"]
        assert o["cancel"] == "Cancelled task #1: RTX price." and "#1" not in o["list2"] and o["weird"].startswith("Error:")
        assert e.eng.jobs.store.get(1)["source"] == "model" and e.eng.jobs.store.get(1)["session"] == "chat1"
        e.eng.cfg["jobs"]["enabled"] = False
        assert arun(run_tool("task", {"action": "list"})).startswith("Error: background tasks are switched off")
    finally:
        e.close()


def test_the_tasks_panel_routes_do_what_the_user_asks(tmp_path, monkeypatch):
    e = Eng(tmp_path, monkeypatch)
    e.w.script(SHELL("dir"), ANS("Listed."))

    class Req:
        def __init__(self, body): self.body = body
        async def json(self): return self.body
    HTTPException = jobs_web.HTTPException

    async def go():
        made = await jobs_web.create_job(Req({"goal": "List the files in the work folder for me.", "access": "full", "title": "Files"}))
        listing = await jobs_web.list_jobs()
        await e.eng.tick(); await asyncio.wait_for(asyncio.shield(e.eng.jobs._task), 20)
        parked = await jobs_web.one_job(made["id"])
        try:
            await jobs_web.decide(made["id"], Req({"decision": "maybe"})); bad = None
        except HTTPException as ex:
            bad = ex.status_code
        approved = await jobs_web.decide(made["id"], Req({"decision": "approve", "approval_id": parked["parked"]["id"]}))
        await e.eng.tick(); await asyncio.wait_for(asyncio.shield(e.eng.jobs._task), 20)
        done = await jobs_web.one_job(made["id"])
        try:
            await jobs_web.cancel_job(made["id"]); again = None
        except HTTPException as ex:
            again = ex.status_code
        await jobs_web.delete_job(made["id"])
        try:
            await jobs_web.one_job(made["id"]); gone = None
        except HTTPException as ex:
            gone = ex.status_code
        return made, listing, parked, bad, approved, done, again, gone
    try:
        made, listing, parked, bad, approved, done, again, gone = arun(go())
        assert made["status"] == "scheduled" and made["source"] == "user" and listing["jobs"][0]["title"] == "Files"
        assert parked["status"] == "parked" and parked["parked"]["tool"] == "run_shell_command" and parked["progress"]
        assert bad == 400 or bad == 409
        assert approved["status"] == "scheduled" and done["status"] == "done" and done["history"][0]["outcome"] == "done"
        assert again == 409 and gone == 400 or gone == 409
    finally:
        e.close()


def test_routing_shows_the_task_tool_for_background_work_but_not_for_the_word_task(tmp_path):
    from lucy.tools import routing
    avail = {n: {} for n in ["web_search", "web_extract", "read_local_file", "write_local_file", "edit_local_file", "remember",
                             "recall_memory", "send_sticker", "task", "set_reminder", "schedule_task", "manage_reminders",
                             "proactive_control", "list_local_files", "find_file", "send_file"]}
    pick = lambda t: routing.select_tools(t, [], available=avail)
    for yes in ["keep an eye on the RTX price and tell me when it drops", "what are my tasks?", "monitor the build overnight",
                "finish the report by friday", "cancel the task"]:
        assert "task" in pick(yes), yes
    for no in ["my task is to write a poem", "hello there", "what is the capital of France"]:
        assert "task" not in pick(no), no


def test_the_shipped_defaults_follow_the_pc_clock_and_describe_the_jobs_section():
    root = HERE.parent
    import yaml
    y = yaml.safe_load((root / ".core" / "proactive.yaml").read_text(encoding="utf-8"))
    assert "timezone" not in y and "timezone" not in DEFAULTS                  # no timezone setting: the PC's clock is used
    assert y["jobs"]["auto_start_model_full"] is False and y["jobs"]["system_jobs"] is True
    assert set(DEFAULTS["jobs"]["blocked_tools"]) >= {"task", "core_change", "user_tool"}
    assert all(s in STATUS_LABELS for s in ("scheduled", "running", "parked", "waiting_ok", "stopped"))
