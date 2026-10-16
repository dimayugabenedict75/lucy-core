"""The agent keeps going until the task is DONE: no step budget, no giving up on a failing tool,
no endless repeating. (Scripted fake llama-server, see test_agent_loop.py.)"""
import pathlib, sys, types
ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.modules.setdefault("lucy", types.ModuleType("lucy")); sys.modules["lucy"].__path__ = [str(ROOT / "src/lucy")]
sys.modules.setdefault("lucy.server", types.ModuleType("lucy.server")); sys.modules["lucy.server"].__path__ = [str(ROOT / "src/lucy/server")]
sys.path.insert(0, str(ROOT / "tests"))
from test_agent_loop import run, CALL, ANS, tool_msgs, tool, TOOLS, S           # noqa: E402
from lucy.server.agent_loop import LoopConfig, looks_failed                       # noqa: E402
from lucy.server.multistep import expected_actions                                # noqa: E402

FILE_TOOLS = TOOLS + [tool("write_local_file", {"file_path": S, "content": S}, ["file_path", "content"]),
                      tool("send_file", {"file_path": S}, ["file_path"]),
                      tool("run_shell_command", {"command": S}, ["command"])]
EDIT = lambda n="a": CALL("edit_local_file", {"file_path": f"{n}.py", "old_string": "x", "new_string": "y"})
STOP = lambda t: {"content": [t], "finish": "stop"}


# ---- no step budget -----------------------------------------------------------------------------
def test_a_long_task_is_never_cut_short_by_a_round_budget():
    steps = [CALL("web_search", {"query": f"step {i}"}) for i in range(25)]      # the old limit was 8 rounds
    r = run(steps + [ANS("All 25 steps done.")])
    assert len(r.calls) == 25 and r.text == "All 25 steps done."
    assert all(q["tools"] for q in r.requests)                                    # tools were offered to the very end


def test_the_optional_safety_ceiling_reports_honestly_instead_of_going_silent():
    r = run([CALL("web_search", {"query": f"s{i}"}) for i in range(3)] + [ANS("Got this far; say continue.")],
            cfg=LoopConfig(max_rounds=3))
    last = r.requests[-1]
    assert last["tools"] == [] and "safety limit of 3" in last["messages"][-1]["content"]
    assert r.text.endswith("Got this far; say continue.")


# ---- blockers: workarounds, never stopping --------------------------------------------------------
def test_failing_tool_gets_a_workaround_playbook_and_the_turn_continues():
    async def ex(n, a):
        return "Error: 'old_string' not found in file: a.py" if n == "edit_local_file" else "ok, written"
    r = run([EDIT("a"), EDIT("b"), EDIT("c"), CALL("write_local_file", {"file_path": "a.py", "content": "new"}),
             ANS("Rewrote the file instead.")], execute=ex, tools=FILE_TOOLS)
    assert [c[0] for c in r.calls] == ["edit_local_file"] * 3 + ["write_local_file"]
    hint = tool_msgs(r, 3)[-1]["content"]
    assert "Blocked: edit_local_file has failed 3 times" in hint and "write_local_file" in hint and "run_shell_command" in hint
    assert r.requests[3]["tools"] and r.text.endswith("Rewrote the file instead.")     # many failures never force an end


def test_eight_failures_no_longer_end_the_turn():
    async def bad(n, a): return "Error: boom"
    r = run([CALL("web_search", {"query": f"q{i}"}) for i in range(10)] + [CALL("read_local_file", {"file_path": "x"}), ANS("Found another way.")],
            execute=bad)
    assert r.text.endswith("Found another way.") and r.metrics["tool_calls"] == 11
    assert "Only after you have tried at least two genuinely different approaches" in tool_msgs(r, 9)[-1]["content"]


def test_an_identical_failing_call_is_not_re_run_forever():
    async def bad(n, a): return "Error: nope"
    r = run([EDIT("a"), EDIT("a"), EDIT("a"), EDIT("a"), ANS("Switching approach.")], execute=bad, tools=FILE_TOOLS)
    assert len(r.calls) == 2                                                          # 3rd/4th identical call short-circuited
    assert "already failed 2 times" in tool_msgs(r)[-1]["content"]


def test_unknown_tool_suggests_the_closest_real_one():
    r = run([CALL("send_files", {"file_path": "a"}), ANS("ok")], tools=FILE_TOOLS)
    assert "Did you mean: send_file" in tool_msgs(r)[0]["content"]


def test_unchanged_world_vs_changed_world_for_failed_calls():
    state = {"made": False}
    async def ex(n, a):
        if n == "write_local_file": state["made"] = True; return "written"
        return "ok" if state["made"] else "Error: File not found: a.py"
    r = run([CALL("read_local_file", {"file_path": "a.py"}), CALL("read_local_file", {"file_path": "a.py"}),
             CALL("write_local_file", {"file_path": "a.py", "content": "hi"}),
             CALL("read_local_file", {"file_path": "a.py"}), ANS("done")], execute=ex, tools=FILE_TOOLS)
    assert [c[0] for c in r.calls] == ["read_local_file", "read_local_file", "write_local_file", "read_local_file"]


def test_looks_failed_catches_refusals_and_no_ops():
    for t in ["Refused: this change would leave x.json as invalid JSON", "No change: new_string is identical",
              "Not a file: foo", "[error reading file: boom]", "Error: x", "Permission denied: y"]:
        assert looks_failed(t), t
    for t in ["Replaced 1 occurrence(s) in a.py.", "MEDIA:out/a.pdf", "Here are the results"]:
        assert not looks_failed(t), t


# ---- "send the file" / "edit the file" really get done ----------------------------------------------
def test_user_asked_to_send_a_file_but_the_model_only_talks_gets_pushed_to_call_send_file():
    r = run([STOP("All set, the report looks great."), CALL("send_file", {"file_path": "report.pdf"}), ANS("Sent it!")],
            messages=[{"role": "user", "content": "Please send me the report file"}], tools=FILE_TOOLS)
    assert [c[0] for c in r.calls] == ["send_file"] and r.text.endswith("Sent it!")
    assert "asked you to send the file" in r.requests[1]["messages"][-1]["content"]


def test_edit_request_is_not_considered_done_without_an_edit_tool():
    r = run([STOP("The function is fine as it is."), EDIT("a"), ANS("Edited it.")],
            messages=[{"role": "user", "content": "edit api.py and fix the loop bug"}], tools=FILE_TOOLS)
    assert [c[0] for c in r.calls] == ["edit_local_file"]


def test_coverage_nudge_is_capped_and_ignores_questions_and_negations():
    r = run([STOP("Nothing to send."), STOP("Still nothing."), STOP("Really nothing here.")],
            messages=[{"role": "user", "content": "send me the file"}], tools=FILE_TOOLS)
    assert not r.calls and r.text.endswith("Really nothing here.")                    # 2 nudges, then the model may finish
    for q in ["How do I send a file?", "don't send the file yet", "what does the edit button do?", "hi"]:
        assert expected_actions(q) == [], q
    assert expected_actions("send me the report as a file")[0][1] == frozenset({"send_file"})


# ---- repeating itself ------------------------------------------------------------------------------
LOOP = ["I'm patching agent_loop.py to redirect the output stream. "] * 20


def test_a_reply_stuck_in_a_loop_is_shown_once_cut_and_retried_with_anti_repeat_sampling():
    r = run([{"content": ["Starting now. "] + LOOP, "finish": "stop"}, ANS("Patched it and verified.")])
    assert r.text.count("I'm patching agent_loop.py to redirect the output stream.") == 1
    assert r.text.endswith("Patched it and verified.")
    assert "repeat_penalty" not in r.requests[0] and r.requests[1]["repeat_penalty"] >= 1.1
    assert r.requests[1]["dry_multiplier"] > 0
    note = r.requests[1]["messages"][-1]["content"]
    assert "Carry on with the next concrete step" in note and "repeating" not in note      # nothing the model can quote back


def test_a_nudge_round_that_restates_the_previous_text_is_not_shown_twice():
    r = run([STOP("Let me read that file for you right now."), STOP("Let me read that file for you right now."),
             CALL("read_local_file", {"file_path": "a"}), ANS("It says hi.")])
    assert r.text.count("Let me read that file for you right now.") == 1
    assert [c[0] for c in r.calls] == ["read_local_file"]


def test_a_model_that_never_stops_looping_ends_with_an_honest_message():
    r = run([{"content": LOOP, "finish": "stop"}] * 8)
    assert "stuck repeating myself" in r.text and r.errors == []
    assert r.text.count("I'm patching agent_loop.py to redirect the output stream.") == 1


def test_repeated_lines_in_code_are_left_alone():
    code = "Here:\n```python\n" + "print('hello world, this is a line')\n" * 6 + "```\nDone."
    r = run([{"content": [code], "finish": "stop"}])
    assert r.text == code


# ---- the exact failure from the chat screenshot ------------------------------------------------------
FIND = tool("find_file", {"query": S}, ["query"])


def test_screenshot_in_progress_narration_and_malformed_action_line_end_in_a_real_call():
    """The model wrote "I'm searching...", "I'm executing find_file..." and "Action: [Sticker: Focused]" and the old
    loop took that for a final answer. Now: the harness gets a real find_file call out of it and the chat is clean."""
    r = run([STOP("I'll search for `repeat_guard.py` within the known safe paths."),
             STOP("I'm searching for `repeat_guard.py` across the entire user directory.\n\n"
                  "I'm executing `find_file` for that script name.\n\nAction: [Sticker: Focused]"),
             {"forced": {"tool": "find_file", "arguments": {"query": "repeat_guard.py"}}},
             ANS("Found it: src/lucy/server/repeat_guard.py.")],
            tools=FILE_TOOLS + [FIND], messages=[{"role": "user", "content": "i meant repeat_guard.py"}])
    assert [c[0] for c in r.calls] == ["find_file"] and r.errors == []
    assert r.text.endswith("Found it: src/lucy/server/repeat_guard.py.")
    assert "executing" not in r.text and "Action:" not in r.text and "Sticker" not in r.text    # no narration clutter
    assert "web_search" not in [c[0] for c in r.calls]                                         # not mistaken for a web search


def test_present_progressive_narration_alone_is_enough_to_trigger_a_push():
    for sentence in ["I'm listing the files in the current directory.", "Checking the directory now.",
                     "Okay, executing find_file for repeat_guard.py."]:
        r = run([STOP(sentence), CALL("find_file", {"query": "x"}), ANS("Done, found it.")], tools=FILE_TOOLS + [FIND])
        assert [c[0] for c in r.calls] == ["find_file"], sentence


def test_ordinary_replies_are_not_mistaken_for_narration():
    for sentence in ["I'm happy to help with that.", "I'm looking forward to it.", "Reading files can be slow.",
                     "I searched the folder and found it.", "Done! The report is in your chat."]:
        r = run([STOP(sentence)], tools=FILE_TOOLS + [FIND])
        assert not r.calls and r.text == sentence and len(r.requests) == 1, sentence
