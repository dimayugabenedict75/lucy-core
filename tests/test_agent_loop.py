"""Tests for lucy.server.agent_loop against a scripted fake llama-server (no GPU, no model)."""
import asyncio, json, sys, threading, http.server, pathlib, time
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
import types
sys.modules.setdefault("lucy", types.ModuleType("lucy")); sys.modules["lucy"].__path__ = [str(pathlib.Path(__file__).resolve().parents[1] / "src/lucy")]
sys.modules.setdefault("lucy.server", types.ModuleType("lucy.server")); sys.modules["lucy.server"].__path__ = [str(pathlib.Path(__file__).resolve().parents[1] / "src/lucy/server")]
from lucy.server.agent_loop import AgentLoop, LoopConfig, parse_args, coerce_args, narrates_without_acting, looks_failed

def tool(name, props, required):
    return {"type": "function", "function": {"name": name, "description": name,
            "parameters": {"type": "object", "properties": props, "required": required}}}
S, I = {"type": "string"}, {"type": "integer"}
TOOLS = [tool("web_search", {"query": S, "limit": I}, ["query"]),
         tool("read_local_file", {"file_path": S}, ["file_path"]),
         tool("edit_local_file", {"file_path": S, "old_string": S, "new_string": S}, ["file_path", "old_string", "new_string"])]

class Fake(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        srv = self.server; srv.requests.append(body)
        step = srv.script.pop(0) if srv.script else {"content": ["(script exhausted)"], "finish": "stop"}
        if "status" in step:
            self.send_response(step["status"]); self.end_headers(); self.wfile.write(step.get("body", "").encode()); return
        self.send_response(200); self.send_header("Content-Type", "text/event-stream"); self.end_headers()
        w = lambda d, f=None: self.wfile.write(b"data: " + json.dumps({"choices": [{"delta": d, "finish_reason": f}]}).encode() + b"\n\n")
        if step.get("drop"): return
        for piece in step.get("content", []): w({"content": piece})
        for i, tc in enumerate(step.get("tool_calls", [])):
            w({"tool_calls": [{"index": i, "id": tc.get("id", f"c{i}"), "function": {"name": tc["name"], "arguments": tc["arguments"] if isinstance(tc["arguments"], str) else json.dumps(tc["arguments"])}}]})
        w({}, step.get("finish", "tool_calls" if step.get("tool_calls") else "stop"))
        self.wfile.write(b"data: [DONE]\n\n")
    def log_message(self, *a): pass

class Brain:
    label = "Lucy 12B"; model = "m"
    def __init__(self, port): self.base_url = f"http://127.0.0.1:{port}/v1"; self.up = True; self.ensured = 0; self._err = None
    def extras(self): return {}
    def loading(self): return False
    def error(self): return self._err
    async def ready(self): return self.up
    async def ensure(self): self.ensured += 1

def start_server():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Fake); srv.requests, srv.script = [], []
    threading.Thread(target=srv.serve_forever, daemon=True).start(); return srv

def run(script, execute=None, messages=None, cfg=None, tools=TOOLS, brain=None):
    srv = start_server(); srv.script = list(script)
    brain = brain or Brain(srv.server_address[1])
    calls = []
    async def default_exec(name, args): calls.append((name, args)); return f"result of {name}"
    async def wrapped(name, args):
        calls.append((name, args)); return await (execute or default_exec_noappend)(name, args)
    async def default_exec_noappend(name, args): return f"result of {name}"
    loop = AgentLoop(brain=brain, tools=tools, execute=wrapped, activity_for=lambda n, a: f"{n}:x",
                     count_tokens=lambda t: max(1, len(t) // 4), temperature=0.5, cfg=cfg or LoopConfig())
    msgs = messages if messages is not None else [{"role": "user", "content": "hi"}]
    async def go():
        return [e async for e in loop.run(msgs)]
    events = asyncio.run(go()); srv.shutdown()
    text = "".join(e.data for e in events if e.kind == "content")
    return types.SimpleNamespace(events=events, text=text, requests=srv.requests, calls=calls, msgs=msgs,
                                 errors=[e.data for e in events if e.kind == "error"],
                                 metrics=[e.data for e in events if e.kind == "metrics"][-1] if any(e.kind == "metrics" for e in events) else None,
                                 activities=[e.data for e in events if e.kind == "activity"])

CALL = lambda n="web_search", a=None, **kw: {"tool_calls": [{"name": n, "arguments": a if a is not None else {"query": "q"}, **kw}]}
ANS = lambda t="Final answer.": {"content": [t], "finish": "stop"}
tool_msgs = lambda r, i=-1: [m for m in r.requests[i]["messages"] if m["role"] == "tool"]

# ---- the happy path & content ---------------------------------------------------------------
def test_basic_tool_round_and_separator():
    r = run([{"content": ["Checking."], "tool_calls": [{"name": "web_search", "arguments": {"query": "q"}}]}, ANS("It is 5.")])
    assert r.text == "Checking.\n\nIt is 5." and r.calls == [("web_search", {"query": "q"})] and r.metrics["tool_calls"] == 1
    assert "web_search:x" in r.activities

# ---- tool call handling ------------------------------------------------------------------------
def test_tool_exception_becomes_result():
    async def boom(n, a): raise RuntimeError("boom")
    r = run([CALL(), ANS()], execute=boom)
    assert tool_msgs(r)[0]["content"].startswith("Tool failed: web_search raised RuntimeError: boom") and r.text == "Final answer." and not r.errors

def test_invalid_json_not_executed_and_history_stays_valid():
    r = run([CALL(a='{"query": "cats'), ANS()])
    assert r.calls == []
    asst = [m for m in r.requests[1]["messages"] if m.get("tool_calls")][0]
    assert json.loads(asst["tool_calls"][0]["function"]["arguments"]) == {}
    assert tool_msgs(r)[0]["content"].startswith("Invalid arguments for web_search")

def test_unknown_tool_and_missing_required():
    r = run([CALL("teleport", {}), ANS()]); assert r.calls == [] and "Available tools" in tool_msgs(r)[0]["content"]
    r = run([CALL("web_search", {}), ANS()]); assert r.calls == [] and "Missing required" in tool_msgs(r)[0]["content"]

def test_type_coercion():
    r = run([CALL(a={"query": "x", "limit": "3"}), ANS()]); assert r.calls[0][1]["limit"] == 3

def test_finish_stop_with_tool_calls_still_executes():
    r = run([{"tool_calls": [{"name": "web_search", "arguments": {"query": "q"}}], "finish": "stop"}, ANS()]); assert len(r.calls) == 1

def test_empty_ids_repaired():
    r = run([{"tool_calls": [{"id": "", "name": "web_search", "arguments": {"query": "q"}}]}, ANS()])
    asst = [m for m in r.requests[1]["messages"] if m.get("tool_calls")][0]
    assert asst["tool_calls"][0]["id"] and tool_msgs(r)[0]["tool_call_id"] == asst["tool_calls"][0]["id"]

def test_text_format_call_recovered():
    r = run([{"content": ['Action: web_search(query="x")'], "finish": "stop"}, ANS()])
    assert r.calls == [("web_search", {"query": "x"})] and "Action:" not in r.text

def test_truncated_tool_call_not_executed():
    r = run([{"tool_calls": [{"name": "edit_local_file", "arguments": '{"file_path": "a.py", "old_string": "x", "new_str'}], "finish": "length"},
             CALL(), ANS()])
    assert r.calls == [("web_search", {"query": "q"})]
    assert "cut off by the output limit" in r.requests[1]["messages"][-1]["content"]

# ---- loops, failures, budgets -------------------------------------------------------------------
def test_duplicate_readonly_call_cached_then_forced_final():
    r = run([CALL(), CALL(), CALL(), CALL(), ANS("Done.")])
    assert len(r.calls) == 1 and r.metrics["tool_calls"] == 4
    assert r.requests[-1]["tools"] == [] and "tool_choice" not in r.requests[-1] and r.text == "Done."
    assert "already ran this exact call" in tool_msgs(r, 2)[-1]["content"]

def test_mutation_invalidates_cache():
    r = run([CALL(), CALL("edit_local_file", {"file_path": "a", "old_string": "b", "new_string": "c"}), CALL(), ANS()])
    assert [c[0] for c in r.calls] == ["web_search", "edit_local_file", "web_search"]

def test_failure_streak_hint():
    async def bad(n, a): return "Error: boom"
    r = run([CALL(a={"query": "a"}), CALL(a={"query": "b"}), ANS()], execute=bad)
    assert "failure #2" in tool_msgs(r)[-1]["content"] and r.metrics["tool_errors"] == 2

def test_edit_not_found_hint():
    async def bad(n, a): return "Error: 'old_string' not found in file: a.py"
    r = run([CALL("edit_local_file", {"file_path": "a.py", "old_string": "x", "new_string": "y"}), ANS()], execute=bad)
    assert "read_local_file" in tool_msgs(r)[0]["content"]

def test_tool_timeout():
    async def slow(n, a): await asyncio.sleep(5); return "late"
    r = run([CALL(), ANS()], execute=slow, cfg=LoopConfig(tool_timeout=0.2))
    assert tool_msgs(r)[0]["content"].startswith("Tool timed out") and r.text == "Final answer."

def test_big_result_capped():
    async def big(n, a): return "x" * 50000
    r = run([CALL(), ANS()], execute=big); c = tool_msgs(r)[0]["content"]
    assert len(c) < 13000 and "characters omitted" in c

def test_max_rounds_forces_tool_free_final_round():
    r = run([CALL(a={"query": "1"}), CALL(a={"query": "2"}), ANS("Here is what I confirmed.")], cfg=LoopConfig(max_rounds=2))
    assert r.requests[2]["tools"] == [] and "used all your tool calls" in r.requests[2]["messages"][-1]["content"]
    assert r.text == "Here is what I confirmed."

# ---- replies -----------------------------------------------------------------------------------------
def test_empty_reply_after_tools_is_nudged():
    r = run([CALL(), {"content": [], "finish": "stop"}, ANS("Now I answer.")])
    assert r.text == "Now I answer." and "Use the tool results" in r.requests[2]["messages"][-1]["content"]

def test_empty_forever_falls_back_with_last_result():
    r = run([CALL(), {"content": [], "finish": "stop"}, {"content": [], "finish": "stop"}, {"content": [], "finish": "stop"}])
    assert "result of web_search" in r.text and "couldn't put a proper reply" in r.text

def test_length_cutoff_is_continued_without_breaking_words():
    r = run([{"content": ["Hello wor"], "finish": "length"}, {"content": ["ld."], "finish": "stop"}])
    assert r.text == "Hello world." and "cut off" in r.requests[1]["messages"][-1]["content"]

def test_narration_guard_nudges_once_then_acts():
    r = run([{"content": ["Sure, let me search for that."], "finish": "stop"}, CALL(), ANS("Found it.")])
    assert len(r.calls) == 1 and r.text.endswith("Found it.") and "did not call" in r.requests[1]["messages"][-1]["content"]
    r = run([{"content": ["Let me look that up for you."], "finish": "stop"}, {"content": ["Let me look that up for you."], "finish": "stop"}])
    assert r.metrics["rounds"] == 2                      # nudged only once

def test_narration_guard_ignores_offers_and_normal_text():
    for t in ["Do you want me to search for that?", "I'll keep that in mind.", "Paris is the capital of France.",
              "If you'd like, I can search the web.", "Sure. I will remember that preference."]:
        assert not narrates_without_acting(t), t
    for t in ["Sure, let me search for that.", "I'll check the file now.", "Okay, I'm going to edit api.py.", "Let me look that up for you.", "I'll take a look at the file."]:
        assert narrates_without_acting(t), t

# ---- the model itself ------------------------------------------------------------------------------------
def test_model_down_then_started():
    srv_live = start_server(); srv_live.script = [ANS("Back online.")]
    class B(Brain):
        async def ensure(self): self.ensured += 1; self.base_url = f"http://127.0.0.1:{srv_live.server_address[1]}/v1"
    b = B(1)                                           # port 1: connection refused
    r = run([], brain=b)
    assert r.text == "Back online." and b.ensured == 1 and "load:Lucy 12B" in r.activities and r.metrics["retries"] == 1

def test_model_loading_503_waits_then_retries():
    r = run([{"status": 503, "body": '{"error":{"message":"Loading model"}}'}, ANS("Ready now.")])
    assert r.text == "Ready now." and not r.errors

def test_model_start_failure_reported():
    class B(Brain):
        def __init__(self, port): super().__init__(port); self.up = False
        async def ensure(self): self._err = "llama-server not found"
    r = run([], brain=B(1), cfg=LoopConfig(ready_wait_s=3))
    assert r.errors and "llama-server not found" in r.errors[0]["detail"]

def test_context_overflow_prunes_and_retries():
    msgs = [{"role": "system", "content": "sys"}, {"role": "user", "content": "q1"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "t", "type": "function", "function": {"name": "web_search", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "t", "content": "y" * 6000},
            {"role": "assistant", "content": "a1"}, {"role": "user", "content": "q2"}]
    r = run([{"status": 400, "body": "request (9000 tokens) exceeds the available context size"}, ANS("ok")], messages=msgs,
            cfg=LoopConfig(ctx_tokens=3000, max_tokens=256))
    assert r.text == "ok" and len(r.requests) == 2
    assert "removed to save context" in json.dumps(r.requests[1]["messages"]) and "removed to save context" not in json.dumps(r.requests[0]["messages"])

def test_transient_500_and_silent_drop_are_retried():
    r = run([{"status": 500, "body": "oops"}, {"drop": True}, ANS("fine")], cfg=LoopConfig(http_retries=2))
    assert r.text == "fine" and r.metrics["retries"] == 2

def test_fatal_http_error_reported_not_raised():
    r = run([{"status": 401, "body": "nope"}])
    assert r.errors and r.errors[0]["status"] == 401 and r.errors[0]["error"] == "Failed to connect to brain"

# ---- helpers ---------------------------------------------------------------------------------------------------
def test_helpers():
    assert parse_args('{"a": 1}') == ({"a": 1}, None) and parse_args("") == ({}, None)
    assert parse_args('```json\n{"a": 1}\n```')[0] == {"a": 1} and parse_args("{'a': 2}")[0] == {"a": 2}
    assert parse_args('{"a": ')[0] is None and parse_args("[1]")[0] is None
    sch = {"properties": {"n": {"type": "integer"}, "b": {"type": "boolean"}, "l": {"type": "array"}, "s": {"type": "string"}}}
    assert coerce_args({"n": "7", "b": "true", "l": "[1,2]", "s": 5}, sch) == {"n": 7, "b": True, "l": [1, 2], "s": "5"}
    assert looks_failed("Error: x") and looks_failed("File not found: a") and looks_failed('{"error": "x"}')
    assert not looks_failed("No results found for 'x'") and not looks_failed("ok")
