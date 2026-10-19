"""P2 inside the agent loop (scripted fake llama-server): parallel read-only calls, the harness VERIFY step after edits,
coding routing + prompt note. No GPU, no network."""
import asyncio, json, os, pathlib, shutil, sys, tempfile, time
HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from test_agent_loop import run, CALL, ANS, tool, S, tool_msgs  # noqa: E402
from lucy import paths as _paths  # noqa: E402
from lucy.tools import routing, _verify  # noqa: E402

READ = lambda n="read_local_file", **a: {"name": n, "arguments": a or {"file_path": "x"}}
TOOLS = [tool("read_local_file", {"file_path": S}, ["file_path"]), tool("code_search", {"query": S}, ["query"]),
         tool("edit_local_file", {"file_path": S, "old_string": S, "new_string": S}, ["file_path", "old_string", "new_string"]),
         tool("apply_patch", {"file_path": S}, ["file_path"]), tool("run_tests", {}, [])]


def test_independent_reads_run_side_by_side_and_answers_keep_their_order():
    live, peak = [0], [0]

    async def slow(name, args):                                          # counts overlap instead of timing it: machine-speed proof
        live[0] += 1; peak[0] = max(peak[0], live[0]); await asyncio.sleep(0.3); live[0] -= 1
        return f"out-{args.get('file_path') or args.get('query')}"
    r = run([{"tool_calls": [READ(file_path="a"), READ("code_search", query="q"), READ(file_path="b")]}, ANS("done")], execute=slow, tools=TOOLS)
    assert peak[0] == 3                                                  # all three were running at the same moment
    assert [m["content"] for m in tool_msgs(r)] == ["out-a", "out-q", "out-b"] and r.text.endswith("done")


def test_duplicates_writes_and_the_off_switch_stay_sequential(monkeypatch):
    live, peak = [0], [0]

    async def counted(name, args):
        live[0] += 1; peak[0] = max(peak[0], live[0]); await asyncio.sleep(0.2); live[0] -= 1; return "ok"
    edit = {"name": "edit_local_file", "arguments": {"file_path": "a", "old_string": "x", "new_string": "y"}}
    run([{"tool_calls": [READ(file_path="a"), edit, READ(file_path="b")]}, ANS()], execute=counted, tools=TOOLS)
    assert peak[0] == 1                                                  # a write splits the group: nothing overlaps
    run([{"tool_calls": [READ(file_path="a"), READ(file_path="a")]}, ANS()], execute=counted, tools=TOOLS)
    assert peak[0] == 1                                                  # identical calls: the second reuses the first's result
    monkeypatch.setenv("LUCY_PARALLEL", "0")
    run([{"tool_calls": [READ(file_path="a"), READ(file_path="b")]}, ANS()], execute=counted, tools=TOOLS)
    assert peak[0] == 1


def test_one_failing_parallel_read_does_not_hurt_the_others():
    async def mixed(name, args):
        if args.get("file_path") == "bad":
            raise RuntimeError("boom")
        return "fine"
    r = run([{"tool_calls": [READ(file_path="a"), READ(file_path="bad")]}, ANS("ok")], execute=mixed, tools=TOOLS)
    c = [m["content"] for m in tool_msgs(r)]
    assert c[0] == "fine" and c[1].startswith("Tool failed: read_local_file raised RuntimeError")


# ---------------------------------------------------------------------------------------------- VERIFY step
class tmp_root:
    def __enter__(self):
        self.saved = (_paths.SAFE_ROOT, _paths.ROOT)
        self.t = pathlib.Path(tempfile.mkdtemp()).resolve(); _paths.SAFE_ROOT = _paths.ROOT = self.t
        return self.t
    def __exit__(self, *a):
        _paths.SAFE_ROOT, _paths.ROOT = self.saved; shutil.rmtree(self.t, ignore_errors=True)


def _edit(path):
    return {"name": "edit_local_file", "arguments": {"file_path": str(path), "old_string": "a", "new_string": "b"}}


def test_verify_appends_a_syntax_problem_the_model_must_fix(monkeypatch):
    monkeypatch.delenv("LUCY_VERIFY", raising=False)
    with tmp_root() as t:
        f = t / "bad.py"

        async def write_broken(name, args):
            f.write_text("def x(:\n"); return "Replaced 1 occurrence(s) in bad.py."
        r = run([{"tool_calls": [_edit(f)]}, ANS("fixed")], execute=write_broken, tools=TOOLS)
        out = tool_msgs(r)[0]["content"]
        assert out.startswith("Replaced 1 occurrence") and "[Harness check] SYNTAX PROBLEM in bad.py" in out and "fix this before" in out

        async def write_ok(name, args):
            f.write_text("x = 1\n"); return "Replaced 1 occurrence(s) in bad.py."
        r = run([{"tool_calls": [_edit(f)]}, ANS("ok")], execute=write_ok, tools=TOOLS)
        assert "[Harness check] syntax OK (bad.py)" in tool_msgs(r)[0]["content"]


def test_verify_json_yaml_and_off_switch(monkeypatch):
    with tmp_root() as t:
        (t / "a.json").write_text("{bad"); (t / "b.yaml").write_text("a: [1, 2"); (t / "c.txt").write_text("{bad")
        assert _verify.check_syntax(t / "a.json") and _verify.check_syntax(t / "b.yaml") and _verify.check_syntax(t / "c.txt") is None
        monkeypatch.setenv("LUCY_VERIFY", "off")
        assert asyncio.run(_verify.verify_after("edit_local_file", {"file_path": str(t / "a.json")}, "ok")) == ""
        monkeypatch.setenv("LUCY_VERIFY", "syntax")
        assert "SYNTAX PROBLEM" in asyncio.run(_verify.verify_after("apply_patch", {"file_path": str(t / "a.json")}, "ok"))
        assert asyncio.run(_verify.verify_after("code_search", {"file_path": str(t / "a.json")}, "ok")) == ""


def test_verify_can_run_the_tests_when_the_user_opts_in(monkeypatch):
    from lucy.tools import run_tests
    with tmp_root() as t:
        (t / "tests").mkdir(); (t / "tests/test_a.py").write_text("def test_a(): pass\n"); (t / "m.py").write_text("x = 1\n")
        monkeypatch.setattr(run_tests, "build_command", lambda *a: [sys.executable, "-c", "print('1 failed, 2 passed in 0.1s'); raise SystemExit(1)"])
        monkeypatch.setenv("LUCY_VERIFY", "tests")
        out = asyncio.run(_verify.verify_after("write_local_file", {"file_path": str(t / "m.py")}, "ok"))
        assert "syntax OK" in out and "Tests FAILED (pytest" in out and "1 failed, 2 passed" in out
        monkeypatch.setenv("LUCY_VERIFY", "syntax")
        assert "Tests" not in asyncio.run(_verify.verify_after("write_local_file", {"file_path": str(t / "m.py")}, "ok"))


def test_apply_patch_counts_as_backing_for_an_edit_claim():
    async def ok(name, args):
        return "Patched x.py: 1 edit applied (+1 -1 lines)."
    r = run([{"tool_calls": [{"name": "apply_patch", "arguments": {"file_path": "x.py"}}]}, ANS("I updated x.py as asked.")], execute=ok, tools=TOOLS)
    assert r.metrics["rounds"] == 2                                      # no "you claimed an edit nobody made" nudge


# ---------------------------------------------------------------------------------------------- routing
def test_coding_requests_bring_the_right_tools_and_the_prompt_note():
    pick = lambda q: [n for n in routing.select_tools(q) if n not in routing.ALWAYS]
    assert pick("tell me a joke") == []
    assert {"code_search", "project_map"} <= set(pick("where is parse_args defined in this repo?"))
    assert {"apply_patch", "run_tests"} <= set(pick("fix the failing test in foo.py"))
    assert "git" in pick("commit my changes") and "format_and_lint" in pick("lint utils.py") and "python" in pick("calculate the average in python")
    assert routing.coding_mode(routing.select_tools("fix the failing test in foo.py")) and not routing.coding_mode(routing.select_tools("what is the weather"))
    assert "apply_patch" in routing.CODING_ADDENDUM and "run_tests" in routing.CODING_ADDENDUM
    assert len(routing.select_tools("fix the failing test in foo.py")) + 2 <= 15      # worst case seen for a coding turn


def test_parallel_reads_still_ask_one_approval_card_at_a_time():
    """Two reads outside the allowed folders run in one round: each needs a card, but only one card is open at once."""
    sys.path.insert(0, str(HERE))
    from test_safety_loop import drive, Env  # noqa: E402
    e = Env()                                   # (no path monkeypatching: on POSIX a guessed path would resolve inside SAFE_ROOT)
    outside = pathlib.Path(tempfile.mkdtemp()).resolve() / "n.txt"
    other = outside.parent / "m.txt"
    open_cards, peak = [0], [0]
    try:
        import test_safety_loop as TSL
        orig = e.layer.broker.resolve

        def resolve(aid, d, session):
            open_cards[0] -= 1
            return orig(aid, d, session)
        e.layer.broker.resolve = resolve

        def answer(card):
            open_cards[0] += 1; peak[0] = max(peak[0], open_cards[0]); return "approve"
        script = [{"tool_calls": [{"name": "read_local_file", "arguments": {"file_path": str(outside)}},
                                  {"name": "read_local_file", "arguments": {"file_path": str(other)}}]}, ANS("done")]
        ran, cards, tm, text, _ = drive(e, script, answer=answer)
        got = (len(cards), peak[0], len(ran), text)
        assert got == (2, 1, 2, "done"), got
    finally:
        e.close(); shutil.rmtree(outside.parent, ignore_errors=True)
