"""Tests for the multi-step system: promise detection, the escalation ladder and the update_plan checklist."""
import pathlib, sys, types
ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.modules.setdefault("lucy", types.ModuleType("lucy")); sys.modules["lucy"].__path__ = [str(ROOT / "src/lucy")]
sys.modules.setdefault("lucy.server", types.ModuleType("lucy.server")); sys.modules["lucy.server"].__path__ = [str(ROOT / "src/lucy/server")]
sys.path.insert(0, str(ROOT / "tests"))
from test_agent_loop import run, CALL, ANS, tool_msgs                       # reuse the scripted fake llama-server
from lucy.server.multistep import find_promise, extract_queries, Plan

# the exact reply from the bug report
SCREENSHOT = ("That makes sense. Sticking with a known brand is always a safe bet. \U0001F91D\n"
              "Since you're looking for a general recommendation within the INGCO line, I'm going to search for "
              "\"INGCO soldering station review\" and \"INGCO soldering station specifications\" to see if any "
              "aggregated data or product listings provide a good overview of their typical offerings.\n"
              "I'll run the search now. \U0001F50D")
STOP = lambda t: {"content": [t], "finish": "stop"}
PLAN = lambda *steps: CALL("update_plan", {"steps": [{"text": t, "status": s} for t, s in steps]})

# ---- detection -----------------------------------------------------------------------------------
def test_trailing_emoji_no_longer_hides_the_promise():
    p = find_promise(SCREENSHOT)
    assert p and p.kind == "search"
    assert p.queries == ["INGCO soldering station review", "INGCO soldering station specifications"]

def test_decoration_only_fragments_are_ignored():
    assert find_promise("Okay. Let me check that file. \U0001F50D\U0001F50D") is not None
    assert find_promise("Found it. The answer is 42. \U0001F44D") is None

def test_finished_answers_and_offers_are_not_promises():
    for t in ["I'll search the web if you want. Want me to?", "Here is the result. I'll keep it short.",
              "Done - the file is saved.", "Should I search for it?"]:
        assert find_promise(t) is None, t

def test_query_extraction():
    assert extract_queries('search for "a b c" and "d e f"') == ["a b c", "d e f"]
    assert extract_queries("Now I'll search for INGCO soldering iron price in PH to compare") == ["INGCO soldering iron price in PH"]
    assert extract_queries("let me look that up") == []

# ---- ladder --------------------------------------------------------------------------------------
def test_screenshot_scenario_ends_with_a_real_search_and_answer():
    r = run([STOP(SCREENSHOT), STOP(SCREENSHOT), ANS("INGCO's station is 60W.")])
    assert [c[0] for c in r.calls] == ["web_search", "web_search"]
    assert [c[1]["query"] for c in r.calls] == ["INGCO soldering station review", "INGCO soldering station specifications"]
    assert r.text.endswith("INGCO's station is 60W.")
    assert r.metrics["tool_calls"] == 2

def test_first_broken_promise_is_just_a_nudge_and_a_real_call_clears_it():
    r = run([STOP(SCREENSHOT), CALL("web_search", {"query": "ingco soldering station"}), ANS("Found it.")])
    assert r.calls == [("web_search", {"query": "ingco soldering station"})]
    assert "did not call" in r.requests[1]["messages"][-1]["content"]

def test_second_nudge_is_firmer_for_non_search_promises():
    r = run([STOP("I'll read the file now."), STOP("I'll read the file now."),
             CALL("read_local_file", {"file_path": "a.md"}), ANS("It says hi.")])
    assert r.calls == [("read_local_file", {"file_path": "a.md"})]
    assert "NEXT message must be the tool call" in r.requests[2]["messages"][-1]["content"]

def test_promise_that_is_never_kept_ends_honestly_not_silently():
    r = run([STOP("I'll read the file now.")] * 4)
    assert not r.calls and "never actually ran it" in r.text and r.errors == []

def test_streak_resets_after_a_kept_promise():
    r = run([STOP("Let me search for ingco."), CALL(), STOP("Now I'll read the first result."),
             CALL("read_local_file", {"file_path": "x"}), ANS("Done.")])
    assert [c[0] for c in r.calls] == ["web_search", "read_local_file"] and r.text.endswith("Done.")

def test_auto_act_is_capped_per_turn():
    r = run([STOP("Let me search for a.")] * 2 + [STOP("Now I'll search for b.")] * 2 + [STOP("Let me search for c.")] * 4)
    assert len([c for c in r.calls if c[0] == "web_search"]) <= 2

# ---- plan ----------------------------------------------------------------------------------------
def test_plan_flow_runs_every_step_and_the_tool_is_handled_by_the_loop():
    r = run([PLAN(("search A", "in_progress"), ("search B", "pending")),
             CALL("web_search", {"query": "A"}),
             PLAN(("search A", "done"), ("search B", "in_progress")),
             CALL("web_search", {"query": "B"}),
             PLAN(("search A", "done"), ("search B", "done")),
             ANS("Compared A and B.")])
    assert [c[0] for c in r.calls] == ["web_search", "web_search"]          # update_plan never reaches execute()
    assert r.text.endswith("Compared A and B.") and r.metrics["tool_calls"] == 5
    first = tool_msgs(r, 1)[0]["content"]
    assert "1. [>] search A" in first and "2. [ ] search B" in first and "Next: search A" in first

def test_plan_bookkeeping_does_not_eat_the_tool_round_budget():
    from lucy.server.agent_loop import LoopConfig
    steps = [("one", "pending"), ("two", "pending")]
    r = run([PLAN(*steps), CALL("web_search", {"query": "1"}), PLAN(("one", "done"), ("two", "pending")),
             CALL("web_search", {"query": "2"}), PLAN(("one", "done"), ("two", "done")), ANS("ok")],
            cfg=LoopConfig(max_rounds=2))
    assert len(r.calls) == 2 and r.text.endswith("ok")                       # 2 real rounds fit in max_rounds=2

def test_cannot_finish_with_pending_steps():
    r = run([PLAN(("look", "pending"), ("summarise", "pending")), ANS("All done!"),
             CALL("web_search", {"query": "x"}), PLAN(("look", "done"), ("summarise", "skipped")), ANS("Really done.")])
    assert "unfinished steps" in r.requests[2]["messages"][-1]["content"] and r.text.endswith("Really done.")
    assert len(r.calls) == 1

def test_plan_nudges_are_capped_so_a_stubborn_model_still_finishes():
    r = run([PLAN(("a", "pending")), ANS("one"), ANS("two"), ANS("three")])
    assert r.text.endswith("three") and r.errors == []

def test_bad_plan_is_rejected_with_a_message():
    r = run([CALL("update_plan", {"steps": []}), ANS("fine")])
    assert "Invalid plan" in tool_msgs(r)[0]["content"] and not r.calls

def test_plan_status_normalisation():
    p = Plan(); assert p.set([{"text": "a", "status": "completed"}, "b", {"text": "c", "status": "doing"}, {"text": "d", "status": "in_progress"}]) is None
    assert [s["status"] for s in p.steps] == ["done", "pending", "in_progress", "pending"]
    assert p.next_step() == "b"

# ---- UI liveness: the chat UI hides its indicator while text streams, so the loop must say "think" again ----
def _think_after_text(events):
    """True if a 'think' activity follows a content chunk before the next tool label appears."""
    seen_text = False
    for e in events:
        if e.kind == "content": seen_text = True
        elif e.kind == "activity" and seen_text:
            return e.data == "think"
    return False

def test_think_is_announced_for_every_model_round_including_nudge_rounds():
    r = run([STOP("Let me search for ingco."), CALL(), ANS("Done.")])
    assert r.activities.count("think") >= r.metrics["rounds"]

def test_think_is_sent_when_a_tool_call_starts_after_streamed_text():
    r = run([{"content": ["Checking."], "tool_calls": [{"name": "web_search", "arguments": {"query": "q"}}]}, ANS("ok")])
    assert _think_after_text(r.events)

def test_silent_retry_rounds_are_visible():
    # text streamed -> judged a broken promise -> nudge round runs: a 'think' must follow the text immediately
    r = run([STOP("I'll read the file now."), CALL("read_local_file", {"file_path": "a"}), ANS("ok")])
    assert _think_after_text(r.events)
