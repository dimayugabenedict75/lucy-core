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

def test_second_unkept_promise_makes_the_harness_produce_the_call_itself():
    """The model said "I'll read the file" twice and stopped: it is not asked a third time. A grammar-constrained
    request (JSON schema = exactly one valid tool call) gets the call out of it, whatever the chat template does."""
    r = run([STOP("I'll read the file now."), STOP("I'll read the file now."),
             {"forced": {"tool": "read_local_file", "arguments": {"file_path": "a.md"}}}, ANS("It says hi.")])
    assert r.calls == [("read_local_file", {"file_path": "a.md"})] and r.text.endswith("It says hi.") and r.errors == []
    forced = r.requests[2]
    assert forced["stream"] is False and "tools" not in forced
    schema = forced["response_format"]["json_schema"]["schema"]["anyOf"]
    assert {a["properties"]["tool"]["enum"][0] for a in schema} >= {"read_local_file", "web_search", "finish_task"}
    assert r.text.count("I'll read the file now.") == 1                                   # narration is shown once, not per round

def test_narration_from_rejected_rounds_is_not_shown():
    r = run([STOP("I'll read the file now."), STOP("Now I am going to read that file for you."),
             {"forced": {"tool": "read_local_file", "arguments": {"file_path": "a.md"}}}, ANS("It says hi.")])
    assert "going to read that file" not in r.text and r.text.endswith("It says hi.")

def test_promise_about_a_local_file_is_not_turned_into_a_web_search():
    r = run([STOP("I'll search for `repeat_guard.py` across the entire user directory."),
             STOP("I'm searching for repeat_guard.py in the project folder."),
             {"forced": {"tool": "read_local_file", "arguments": {"file_path": "repeat_guard.py"}}}, ANS("Found it.")])
    assert [c[0] for c in r.calls] == ["read_local_file"]                                 # no junk web_search

def test_unkept_promise_falls_back_to_required_tool_choice_when_constrained_output_is_unavailable():
    r = run([STOP("I'll read the file now.")] * 2 + [CALL("read_local_file", {"file_path": "a.md"}), ANS("It says hi.")])
    assert [c[0] for c in r.calls] == ["read_local_file"] and r.errors == []
    assert r.requests[2]["stream"] is False and r.requests[3]["stream"] is False    # constrained call, both formats (fake answers 400)
    assert r.requests[4]["tool_choice"] == "required"
    assert "finish_task" in [t["function"]["name"] for t in r.requests[4]["tools"]]

def test_finish_task_is_the_way_out_when_a_tool_call_is_required():
    r = run([STOP("I'll read the file now.")] * 2 + [{"forced": {"tool": "finish_task", "arguments": {"summary": "Nothing left to do - all done."}}}])
    assert not r.calls and r.text.endswith("Nothing left to do - all done.")

def test_finish_task_is_refused_while_plan_steps_are_open():
    r = run([PLAN(("a", "pending")), ANS("x1"), ANS("x2"), ANS("x3"),
             {"forced": {"tool": "finish_task", "arguments": {"summary": "done!"}}},
             {"forced": {"tool": "web_search", "arguments": {"query": "q"}}}, PLAN(("a", "done")), ANS("Really finished.")])
    assert r.text.endswith("Really finished.") and "done!" not in r.text and [c[0] for c in r.calls] == ["web_search"]

def test_required_tool_choice_unsupported_by_the_server_falls_back_to_nudges():
    r = run([STOP("I'll read the file now.")] * 2 + [{"status": 400, "body": "tool_choice required is not supported"},
                                                      CALL("read_local_file", {"file_path": "a.md"}), ANS("ok then.")])
    assert r.errors == [] and [c[0] for c in r.calls] == ["read_local_file"] and r.text.endswith("ok then.")
    assert r.requests[2]["stream"] is False and r.requests[4]["tool_choice"] == "required" and r.requests[5]["tool_choice"] == "auto"

def test_pending_plan_is_pushed_until_done_and_then_a_tool_call_is_required():
    r = run([PLAN(("a", "pending")), ANS("one"), ANS("two"), ANS("three"),
             {"forced": {"tool": "web_search", "arguments": {"query": "q"}}}, PLAN(("a", "done")), ANS("Finished.")])
    assert r.text.endswith("Finished.") and r.errors == [] and [c[0] for c in r.calls] == ["web_search"]
    assert r.requests[4]["stream"] is False

def test_a_model_that_ignores_every_push_ends_with_an_honest_report_not_an_endless_loop():
    r = run([PLAN(("a", "pending"))] + [ANS(f"Reply number {i} that never acts.") for i in range(14)])
    last = r.requests[-1]
    assert last["tools"] == [] and "going around in circles" in last["messages"][-1]["content"]
    assert r.metrics["rounds"] <= 14 and r.errors == []

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
