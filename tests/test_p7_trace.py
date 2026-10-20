"""P7 trace mining: failure patterns in agent_trace.jsonl -> concrete, ranked ideas (no model call)."""
import json, pathlib, sys, time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
from lucy.selfdev import trace_mining as TM  # noqa: E402

NOW = 1_760_000_000.0


def ts(dt=0):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(NOW - dt))


def write(path, events):
    path.write_text("\n".join(json.dumps(e) for e in events) + "\nnot json\n", encoding="utf-8")


def sample():
    ev = []
    for i in range(5):
        ev.append({"ts": ts(100), "event": "tool", "tool": "web_extract", "ok": False,
                   "error": f"Error: missing required argument 'url' (call {i})"})
    ev += [{"ts": ts(100), "event": "tool", "tool": "web_extract", "ok": True}] * 5
    ev += [{"ts": ts(50), "event": "forced_call", "tool": "send_file"}] * 3
    ev += [{"ts": ts(50), "event": "tool_auto_added", "tool": "winget"}] * 4
    ev += [{"ts": ts(50), "event": "tool_search", "query": "convert units", "found": []}] * 2
    ev += [{"ts": ts(50), "event": "broken_promise", "sentence": "I'll check that now."}] * 3
    ev += [{"ts": ts(30 * 86400), "event": "tool", "tool": "old_tool", "ok": False, "error": "x"}] * 9   # outside the week
    return ev


def test_patterns_become_ranked_ideas_with_the_right_lane():
    ideas = TM.analyse([e for e in sample() if e["ts"] >= ts(TM.WEEK)])
    by = {i["title"].split(" ")[0]: i for i in ideas}
    assert ideas[0]["title"].startswith("web_extract failed 5x (50%") and ideas[0]["kind"] == "lane_b"
    assert "wrong arguments" in ideas[0]["suggestion"] and "url" in ideas[0]["evidence"]
    assert any(i["kind"] == "lane_a" and "convert units" in i["title"] for i in ideas)
    assert any(i["kind"] == "routing" and i["title"].startswith("winget") for i in ideas)
    assert any("force send_file" in i["title"] for i in ideas)
    assert any("said she would act" in i["title"] and "check that now" in i["evidence"] for i in ideas)
    assert "old_tool" not in by and [i["n"] for i in ideas] == list(range(1, len(ideas) + 1))


def test_the_miner_runs_weekly_and_writes_ideas_and_a_note(tmp_path):
    trace = tmp_path / "agent_trace.jsonl"
    write(trace, sample())
    clock = [NOW]
    m = TM.TraceMiner(trace_path=trace, state_dir=tmp_path / "selfdev", notes_dir=tmp_path / "learned", clock=lambda: clock[0])
    assert m.due() and "No trace review yet" in m.text()
    res = m.run()
    assert res["events"] > 10 and res["ideas"] and not m.due()
    assert (tmp_path / "selfdev" / "ideas.json").exists() and list((tmp_path / "learned").glob("trace-*.md"))
    assert m.text().splitlines()[1].startswith("1. [lane_b] web_extract failed")
    clock[0] += TM.WEEK + 1
    assert m.due()


def test_a_quiet_week_and_a_missing_trace_are_fine(tmp_path):
    m = TM.TraceMiner(trace_path=tmp_path / "none.jsonl", state_dir=tmp_path / "s", clock=lambda: NOW)
    assert m.run()["ideas"] == [] and "nothing stood out" in m.text()
