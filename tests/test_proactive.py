"""Tests for lucy.proactive (no model, no network). Run: pytest tests/test_proactive.py"""
import asyncio, sys, tempfile, random
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from lucy.proactive import ProactiveHost, ProactiveEngine, normalize_history, run_tool, set_engine  # noqa: E402
from lucy.proactive import compose, schedule  # noqa: E402
from lucy.proactive.chat_format import LEAD_PLACEHOLDER  # noqa: E402
from lucy.proactive.config import in_quiet_hours, resolve_tz  # noqa: E402
from lucy.proactive.timeparse import parse_when, parse_duration  # noqa: E402

TZ = timezone(timedelta(hours=8))
NOW = datetime(2026, 10, 13, 14, 5, 30, tzinfo=TZ)        # a Tuesday


def run(coro):
    return asyncio.run(coro)


# ------------------------------------------------------------------ time parsing
def test_parse_relative_and_absolute():
    f = lambda s: parse_when(s, NOW)
    assert f("in 20 minutes") == NOW + timedelta(minutes=20)
    assert f("1h30m") == NOW + timedelta(minutes=90)
    assert f("in an hour and a half") == NOW + timedelta(minutes=90)
    assert f("tomorrow 9am").isoformat() == "2026-10-14T09:00:00+08:00"
    assert f("tomorrow evening 7").hour == 19
    assert f("friday 5pm").isoformat() == "2026-10-16T17:00:00+08:00"
    assert f("tuesday 9am").day == 20                    # today's 9am is past -> next week
    assert f("tuesday 3pm").day == 13
    assert f("oct 20 8:30").isoformat() == "2026-10-20T08:30:00+08:00"
    assert f("june 7").year == 2027                      # already passed this year
    assert f("2026-10-14T01:00Z").isoformat() == "2026-10-14T09:00:00+08:00"
    assert f("in 3 days 9am").day == 16
    assert f("6:30").isoformat() == "2026-10-13T18:30:00+08:00"      # soonest future 6:30
    assert f("noon").day == 14


def test_parse_rejects_nonsense():
    for bad in ["", "blah", "today", "25:00", "13pm", "feb 30", "next week", "in 0 minutes", "remind me"]:
        assert parse_when(bad, NOW) is None, bad
    assert parse_duration("5pm") is None


# ------------------------------------------------------------------ repeat rules
def test_schedule_rules():
    first = datetime(2026, 1, 31, 9, 0, tzinfo=TZ)
    m = schedule.make_rule("monthly", None, first)
    assert schedule.next_after(m, datetime(2026, 1, 31, 9, 0, tzinfo=TZ)).date().isoformat() == "2026-02-28"
    assert schedule.next_after(m, datetime(2026, 2, 28, 9, 0, tzinfo=TZ)).date().isoformat() == "2026-03-31"   # no drift
    y = schedule.make_rule("yearly", None, datetime(2024, 2, 29, 8, 0, tzinfo=TZ))
    assert schedule.next_after(y, datetime(2026, 3, 1, tzinfo=TZ)).date().isoformat() == "2027-02-28"
    wd = schedule.make_rule("weekdays", None, datetime(2026, 10, 16, 8, 30, tzinfo=TZ))     # a Friday
    assert schedule.next_after(wd, datetime(2026, 10, 16, 9, 0, tzinfo=TZ)).date().isoformat() == "2026-10-19"
    iv = schedule.make_rule(None, 90, NOW)
    nxt = schedule.next_after(iv, NOW + timedelta(hours=10))
    assert nxt > NOW + timedelta(hours=10) and (nxt - NOW).total_seconds() % 5400 == 0
    for bad in ((None, 5), ("sometimes", None)):
        try:
            schedule.make_rule(bad[0], bad[1], NOW); assert False
        except ValueError:
            pass


def test_quiet_hours_and_tz():
    assert in_quiet_hours(datetime(2026, 1, 1, 23, 30), "23:00", "07:00")
    assert in_quiet_hours(datetime(2026, 1, 1, 6, 59), "23:00", "07:00")
    assert not in_quiet_hours(datetime(2026, 1, 1, 7, 0), "23:00", "07:00")
    assert not in_quiet_hours(datetime(2026, 1, 1, 12, 0), "09:00", "09:00")
    assert resolve_tz("+08:00").utcoffset(None) == timedelta(hours=8)
    assert resolve_tz("app", "utc").utcoffset(None) == timedelta(0)


# ------------------------------------------------------------------ sanitizing / history
def test_sanitize():
    S = compose.sanitize
    assert S("<think>hmm</think>Morning, Benny.") == "Morning, Benny."
    assert S("SKIP") == compose.SKIP and S("skip.") == compose.SKIP
    assert S("SKIP", allow_skip=False) is None
    assert S("Action: web_search(x)") is None and S('{"a":1}') is None
    assert S("Lucy: hi there") == "hi there"
    assert len(S("word. " * 200)) <= 501
    assert compose.clean_report("[link](http://x)\nAction: foo\n- item") == "[link](http://x)\n- item"


def test_normalize_history():
    h = [{"role": "assistant", "content": "a"}, {"role": "user", "content": "u"},
         {"role": "assistant", "content": "r1"}, {"role": "assistant", "content": "r2"}]
    out = normalize_history(h)
    assert [m["role"] for m in out] == ["user", "assistant", "user", "assistant"]
    assert out[0]["content"] == LEAD_PLACEHOLDER and out[3]["content"] == "r1\n\nr2"
    assert normalize_history(h, truncated=True)[0]["role"] == "user"
    assert h[2]["content"] == "r1"                       # input not mutated


# ------------------------------------------------------------------ engine harness
class Fake:
    def __init__(self, tmp):
        self.t = NOW.timestamp()
        self.msgs = []            # (session, role, content)
        self.busy = False
        self.llm_up = False
        self.llm_reply = "Back already. Nice.\nCoffee first? ☕"
        self.llm_calls = 0
        self.agent_reply = "Price is **₱1,200** now (was ₱1,350)."
        self.agent_calls = []
        self.agent_fail = 0
        self.health = {}
        self.spoken = []
        self.history = [{"role": "user", "content": "working on the beeswax paper"}, {"role": "assistant", "content": "ok"}]
        host = ProactiveHost(
            root=Path(tmp), runtime=Path(tmp) / "runtime",
            append_message=lambda s, r, c: self.msgs.append((s, r, c)),
            pick_session=lambda: "sess1", recent_messages=lambda s, n: self.history[-n:],
            is_busy=lambda: self.busy, health=lambda: self.health, app_timezone=lambda: "+08:00",
            persona=lambda: "You are Lucy.", llm_ready=self._ready, llm_complete=self._complete,
            run_agent=self._agent, memory_context=lambda q: "- Benny likes concise answers",
            sticker=lambda e: f"assets/stickers/Dark/{e}.png", speak=self._speak)
        self.eng = ProactiveEngine(host, clock=lambda: self.t, rng=random.Random(1))
        self.eng.cfg["delivery"]["sticker_chance"] = 0           # deterministic unless a test wants stickers
        set_engine(self.eng)

    async def _ready(self): return self.llm_up
    async def _complete(self, msgs, opts):
        self.llm_calls += 1; return self.llm_reply
    async def _agent(self, msgs, tools, timeout, rounds):
        self.agent_calls.append((msgs, tools))
        if self.agent_fail:
            self.agent_fail -= 1; raise RuntimeError("boom")
        return self.agent_reply
    async def _speak(self, text): self.spoken.append(text)

    def at(self, hhmm, day=13):
        h, m = map(int, hhmm.split(":"))
        self.t = datetime(2026, 10, day, h, m, 0, tzinfo=TZ).timestamp()
    def advance(self, **kw): self.t += timedelta(**kw).total_seconds()
    def sent(self, kind=None): return [m for m in self.msgs if kind is None or kind in m[2]]
    def tick(self): run(self.eng.tick())


def make(tmp_path):
    f = Fake(str(tmp_path)); f.eng.last_user_ts = f.t - 30; return f


def ui(f):                                                   # pretend a chat window is open for a while
    f.eng.bus.subscribe(); f.eng._connected_at = f.t - 60


# ------------------------------------------------------------------ reminders
def test_reminder_fires_once_in_template(tmp_path):
    f = make(tmp_path)
    out = run(run_tool("set_reminder", {"message": "Stretch", "when": "in 20 minutes"}, "sess1"))
    assert out.startswith("Scheduled reminder #1") and "today 14:25" in out
    f.tick(); assert not f.msgs                              # not due yet
    f.advance(minutes=21); f.tick()
    assert len(f.msgs) == 1 and f.msgs[0][:2] == ("sess1", "assistant") and "**Reminder:** Stretch" in f.msgs[0][2]
    f.tick(); assert len(f.msgs) == 1                        # done, never repeats
    assert f.eng.store.get_reminder(1)["status"] == "done"
    assert f.eng.store.recent_events(1)[0]["kind"] == "reminder"


def test_reminder_bad_input_is_error_not_guess(tmp_path):
    f = make(tmp_path)
    assert run(run_tool("set_reminder", {"message": "x", "when": "sometime soon"})).startswith("Error:")
    assert "past" in run(run_tool("set_reminder", {"message": "x", "when": "today 9am"}))
    assert run(run_tool("set_reminder", {"message": "x", "when": "tomorrow 9am", "every_minutes": 3})).startswith("Error:")
    assert run(run_tool("manage_reminders", {"action": "list"})) == "Nothing is scheduled."


def test_reminder_overdue_and_busy_deferral(tmp_path):
    f = make(tmp_path)
    run(run_tool("set_reminder", {"message": "Call mom", "when": "in 10 minutes"}))
    f.busy = True; f.advance(minutes=10, seconds=5); f.tick()
    assert not f.msgs                                        # waits for the running reply
    f.advance(seconds=200); f.tick()
    assert len(f.msgs) == 1                                  # ... but not forever
    f2 = make(tmp_path / "b"); run(run_tool("set_reminder", {"message": "Pay bill", "when": "in 10 minutes"}))
    f2.advance(hours=3); f2.tick()
    assert "was due today 14:15" in f2.msgs[0][2]            # server was "off": still delivered, honestly labelled


def test_recurring_advances_and_skips_stale_catchup(tmp_path):
    f = make(tmp_path)
    run(run_tool("set_reminder", {"message": "Standup", "when": "tomorrow 9am", "repeat": "weekdays"}))
    f.at("09:00", 14); f.tick()
    assert len(f.msgs) == 1 and f.eng.store.get_reminder(1)["status"] == "active"
    nxt = datetime.fromtimestamp(f.eng.store.get_reminder(1)["due_ts"], TZ)
    assert nxt.day == 15 and nxt.hour == 9
    f.at("20:00", 15); f.tick()                              # missed by 11h: skipped, not replayed
    assert len(f.msgs) == 1
    assert datetime.fromtimestamp(f.eng.store.get_reminder(1)["due_ts"], TZ).day == 16
    assert f.eng.store.recent_events(1)[0]["reason"] == "missed_recurring"


def test_reminder_fires_in_quiet_hours_but_task_waits(tmp_path):
    f = make(tmp_path)
    f.at("22:00"); f.eng.last_user_ts = f.t - 30
    run(run_tool("set_reminder", {"message": "Wake up", "when": "tomorrow 6am"}))
    f.at("06:00", 14); f.tick(); assert len(f.msgs) == 1
    f.llm_up = True
    run(run_tool("schedule_task", {"task": "check price of X", "when": "in 30 minutes"}))
    f.at("06:30", 14); f.eng.last_user_ts = f.t - 600; f.tick()
    assert not f.agent_calls                                 # quiet hours: unattended work waits
    f.at("07:01", 14); f.eng.last_user_ts = f.t - 600; f.tick()
    assert len(f.agent_calls) == 1 and "Follow-up" in f.msgs[-1][2]


def test_pause_all_holds_reminders_initiative_only_does_not(tmp_path):
    f = make(tmp_path)
    run(run_tool("set_reminder", {"message": "R", "when": "in 5 minutes"}))
    run(run_tool("proactive_control", {"action": "pause", "minutes": 120}))
    f.advance(minutes=6); f.tick(); assert len(f.msgs) == 1
    run(run_tool("set_reminder", {"message": "R2", "when": "in 5 minutes"}))
    run(run_tool("proactive_control", {"action": "pause", "minutes": 120, "scope": "all"}))
    f.advance(minutes=6); f.tick(); assert len(f.msgs) == 1
    run(run_tool("proactive_control", {"action": "resume"})); f.tick(); assert len(f.msgs) == 2


def test_manage_replace_cancel(tmp_path):
    f = make(tmp_path)
    run(run_tool("set_reminder", {"message": "A", "when": "in 1 hour"}))
    out = run(run_tool("set_reminder", {"message": "A2", "when": "in 2 hours", "replaces_id": 1}))
    assert out.startswith("Updated reminder #1")
    assert "A2" in run(run_tool("manage_reminders", {"action": "list"}))
    assert run(run_tool("manage_reminders", {"action": "cancel", "id": 1})).startswith("Cancelled #1")
    assert run(run_tool("manage_reminders", {"action": "cancel", "id": 1})).startswith("Error:")
    assert run(run_tool("manage_reminders", {"action": "cancel"})).startswith("Error:")


# ------------------------------------------------------------------ tasks
def test_task_runs_with_readonly_tools_and_retries(tmp_path):
    f = make(tmp_path); f.llm_up = True
    run(run_tool("schedule_task", {"task": "Check the ingco soldering station price on example.com", "when": "in 30 minutes"}))
    f.agent_fail = 1
    f.advance(minutes=31); f.eng.last_user_ts = f.t - 600; f.tick()
    assert not f.msgs and f.eng.store.get_reminder(1)["attempts"] == 1      # failed once -> retry scheduled
    f.advance(minutes=11); f.tick()
    assert "₱1,200" in f.msgs[-1][2] and f.eng.store.get_reminder(1)["status"] == "done"
    msgs, tools = f.agent_calls[-1]
    assert "run_shell_command" not in tools and "edit_local_file" not in tools and "web_search" in tools
    assert "ingco" in msgs[1]["content"]


def test_task_gives_up_honestly(tmp_path):
    f = make(tmp_path); f.llm_up = True; f.agent_fail = 99
    run(run_tool("schedule_task", {"task": "do thing", "when": "in 5 minutes"}))
    for _ in range(4):
        f.advance(minutes=12); f.eng.last_user_ts = f.t - 600; f.tick()
    assert "could not finish" in f.msgs[-1][2] and f.eng.store.get_reminder(1)["status"] == "failed"


def test_task_waits_while_user_chats_or_model_down(tmp_path):
    f = make(tmp_path)
    run(run_tool("schedule_task", {"task": "t", "when": "in 5 minutes"}))
    f.advance(minutes=6); f.tick(); assert not f.agent_calls            # model not running: never started for us
    f.llm_up = True; f.eng.last_user_ts = f.t - 5; f.tick(); assert not f.agent_calls   # user active
    f.advance(minutes=12); f.eng.last_user_ts = f.t - 600; f.tick(); assert len(f.agent_calls) == 1


# ------------------------------------------------------------------ initiative
def test_greeting_on_open_with_llm_then_template_fallback(tmp_path):
    f = make(tmp_path); f.at("09:00"); f.llm_up = True
    f.eng.last_user_ts = f.t - 10 * 3600; ui(f)
    f.tick(); assert f.llm_calls == 1 and "Coffee first" in f.msgs[0][2]
    f.tick(); assert len(f.msgs) == 1                                    # once per gap
    g = make(tmp_path / "b"); g.at("09:00"); g.eng.last_user_ts = g.t - 10 * 3600; ui(g)
    run(run_tool("set_reminder", {"message": "Dentist", "when": "today 15:00"}))
    g.tick(); assert "Morning, Benny" in g.msgs[0][2] and "Dentist" in g.msgs[0][2]


def test_no_greeting_when_active_or_no_window_or_quiet_or_rest_variant(tmp_path):
    f = make(tmp_path); f.at("09:00"); f.eng.last_user_ts = f.t - 600; ui(f); f.tick(); assert not f.msgs   # just chatted
    g = make(tmp_path / "b"); g.at("09:00"); g.eng.last_user_ts = g.t - 9 * 3600; g.tick(); assert not g.msgs  # no window
    h = make(tmp_path / "c"); h.at("02:00"); h.eng.last_user_ts = h.t - 9 * 3600; ui(h); h.tick(); assert not h.msgs
    r = make(tmp_path / "d"); r.at("10:00", 15); r.eng.last_user_ts = r.t - 9 * 3600; ui(r); r.tick()          # Thursday
    assert "Rest day" in r.msgs[0][2]


def test_checkin_only_with_llm_skip_cooldown_and_rest_day(tmp_path):
    f = make(tmp_path); f.at("15:00"); f.eng.last_user_ts = f.t - 4 * 3600; ui(f)
    f.tick(); assert not f.msgs                                          # model down: silence beats a canned message
    f.llm_up = True; f.llm_reply = "SKIP"; f.eng._last_watch = 0; f.tick()
    assert not f.msgs and f.eng.store.recent_events(1)[0]["reason"] == "llm_skip"
    f.llm_reply = "Beeswax paper still open?"; f.advance(minutes=30); f.eng._last_watch = 0; f.tick()
    assert not f.msgs                                                    # skip cooldown (2h)
    f.advance(hours=2); f.eng._last_watch = 0; f.eng.last_user_ts = f.t - 4 * 3600; f.tick()
    assert f.msgs and "Beeswax" in f.msgs[0][2]
    r = make(tmp_path / "r"); r.at("15:00", 15); r.llm_up = True; r.eng.last_user_ts = r.t - 4 * 3600; ui(r); r.tick()
    assert not r.msgs                                                    # Thursday: no work-ish nudges


def test_ignored_backoff_stops_checkins_until_user_writes(tmp_path):
    f = make(tmp_path); f.llm_up = True; ui(f)
    for i in range(4):                                                   # four unanswered check-ins, days apart
        f.at("15:00", 13 + 0); f.t += i * 86400 + (86400 if False else 0)
        f.eng.store.add_event("checkin", "x", ts=f.t - 3 * 3600)
    f.t += 4 * 86400; f.eng.last_user_ts = f.t - 4 * 3600
    assert f.eng.policy.ignored_streak() == 4
    f.eng._last_watch = 0; f.tick(); assert not f.msgs
    f.eng.touch(); assert f.eng.policy.ignored_streak() == 0


def test_rate_limit_and_mute(tmp_path):
    f = make(tmp_path); f.at("12:00"); ui(f)
    for i in range(3):
        f.eng.store.add_event("alert", "x", key=str(i), ts=f.t - 100 * i)
    assert f.eng.policy.check("alert", ui_connected=True, busy=False).reason == "rate_hour"
    f.advance(hours=2)
    assert f.eng.policy.check("alert", ui_connected=True, busy=False).ok
    run(run_tool("proactive_control", {"action": "mute", "kind": "alert"}))
    assert f.eng.policy.check("alert", ui_connected=True, busy=False).reason == "muted"


def test_alert_model_error_debounced_and_cooled(tmp_path):
    f = make(tmp_path); f.at("12:00"); f.eng.cfg["alerts"]["disk_free_gb"] = 0
    f.health = {"llm_phase": "error", "llm_error": "CUDA out of memory\nmore", "llm_label": "Lucy 12B"}
    f.eng._last_watch = 0; f.tick(); assert not f.msgs                   # not yet 90 s
    f.advance(minutes=3); f.eng._last_watch = 0; f.tick()
    assert "Lucy 12B failed to load: CUDA out of memory" in f.msgs[0][2]
    f.advance(minutes=3); f.eng._last_watch = 0; f.tick(); assert len(f.msgs) == 1   # 6h cooldown


def test_never_speaks_while_reply_in_progress(tmp_path):
    f = make(tmp_path); f.at("09:00"); f.eng.last_user_ts = f.t - 9 * 3600; ui(f); f.busy = True
    f.tick(); assert not f.msgs
    f.busy = False; f.eng._last_watch = 0; f.tick(); assert f.msgs


# ------------------------------------------------------------------ delivery details
def test_event_bus_media_voice_and_unseen_replay(tmp_path):
    f = make(tmp_path); f.eng.cfg["delivery"]["sticker_chance"] = 1; f.eng.cfg["delivery"]["stickers"]["reminder"] = ["Excited"]
    f.eng.config._cache["delivery"]["voice"] = "reminders"
    q = f.eng.bus.subscribe()
    run(run_tool("set_reminder", {"message": "Drink water", "when": "in 5 minutes"}))
    f.advance(minutes=6)
    async def go():
        await f.eng.tick(); await asyncio.sleep(0)
    run(go())
    ev = q.get_nowait()
    assert ev["kind"] == "reminder" and ev["media"] == "assets/stickers/Dark/Excited.png"
    assert f.msgs[0][2].endswith("\nMEDIA:assets/stickers/Dark/Excited.png")
    assert f.eng.store.get_event(ev["id"])["live"] == 1 and f.spoken == ["⏰ **Reminder:** Drink water"]
    # sent with nobody connected -> replayed on connect
    g = make(tmp_path / "g"); run(run_tool("set_reminder", {"message": "Z", "when": "in 5 minutes"}))
    g.advance(minutes=6); g.tick()
    assert [e["text"] for e in g.eng.store.unseen_events(g.t - 3600)] == ["⏰ **Reminder:** Z"]


def test_feedback_snooze_mute_open(tmp_path):
    f = make(tmp_path); eid = run(f.eng.send_test()); gid = run(f.eng._deliver("greeting", "hi", sticker=False))
    f.eng.feedback(eid, "dismiss"); assert f.eng.store.get_event(eid)["acked"] == 1
    f.eng.feedback(gid, "mute"); assert "greeting" in f.eng.policy.muted_kinds()
    f.eng.feedback(gid, "snooze"); assert f.eng.policy.pause_state()[0]
    f.eng.feedback(gid, "open"); assert f.eng.store.get_event(gid)["engaged"] == 1


def test_runtime_settings_and_status(tmp_path):
    f = make(tmp_path)
    assert "Error" in run(run_tool("proactive_control", {"action": "quiet_hours", "start": "25:00"}))
    run(run_tool("proactive_control", {"action": "quiet_hours", "start": "22:00", "end": "08:00"}))
    assert f.eng.cfg["quiet_hours"]["start"] == "22:00"
    st = f.eng.status(); assert st["timezone"] == "UTC+08:00" and st["rest_day_today"] is False
    assert "Quiet hours 22:00-08:00" in run(run_tool("proactive_control", {"action": "status"}))
    assert "Current local time: Tuesday 2026-10-13 14:05 (UTC+08:00)" in f.eng.now_note()


def test_disabled_engine_is_inert(tmp_path):
    f = make(tmp_path); f.eng.config._cache["enabled"] = False
    run(run_tool("set_reminder", {"message": "x", "when": "in 5 minutes"}))
    f.advance(minutes=6); f.tick(); assert not f.msgs
