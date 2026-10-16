"""Tool routing: a small tool list per turn, tool_search, and auto-adding a real tool the model calls by name.
Pure routing tests + the agent loop against the scripted fake llama-server (no GPU)."""
import asyncio, pathlib, sys, types
ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.modules.setdefault("lucy", types.ModuleType("lucy")); sys.modules["lucy"].__path__ = [str(ROOT / "src/lucy")]
sys.modules.setdefault("lucy.server", types.ModuleType("lucy.server")); sys.modules["lucy.server"].__path__ = [str(ROOT / "src/lucy/server")]
sys.path.insert(0, str(ROOT / "tests"))
from test_agent_loop import Brain, start_server, tool, CALL, ANS, S                    # reuse the scripted fake llama-server
from lucy.server.agent_loop import AgentLoop, LoopConfig                               # noqa: E402
from lucy.tools import routing as RT                                                   # noqa: E402

ALL = [tool("read_local_file", {"file_path": S}, ["file_path"]), tool("write_local_file", {"file_path": S, "content": S}, ["file_path", "content"]),
       tool("edit_local_file", {"file_path": S}, ["file_path"]), tool("remember", {"content": S}, ["content"]),
       tool("recall_memory", {"query": S}, []), tool("send_sticker", {"emotion": S}, ["emotion"]),
       tool("web_search", {"query": S}, ["query"]), tool("web_extract", {"urls": S}, ["urls"]),
       tool("run_shell_command", {"command": S}, ["command"]), tool("set_reminder", {"message": S, "when": S}, ["message", "when"]),
       tool("list_drive_files", {}, [])]
ALL[8]["function"]["description"] = "Run a command in the terminal. Returns its output."
ALL[10]["function"]["description"] = "List the most recent files in your Google Drive."


def names(tools):
    return [t["function"]["name"] for t in tools]


# ---------------------------------------------------------------- pure routing
def test_a_plain_chat_turn_gets_only_the_small_always_set():
    n = RT.select_tools("hi, how are you?")
    assert n == list(RT.ALWAYS) and len(n) + 2 <= 10          # + update_plan + tool_search


def test_families_follow_the_users_words():
    assert {"web_search", "web_extract"} <= set(RT.select_tools("what's the weather in Manila today?"))
    assert "set_reminder" in RT.select_tools("remind me tomorrow at 5pm to call mom")
    assert "run_shell_command" in RT.select_tools("run pip install requests")
    assert "analyze_image" in RT.select_tools("what is in this screenshot.png")
    assert {"cua_mouse", "cua_keyboard"} <= set(RT.select_tools("click the start button"))
    assert "list_drive_files" in RT.select_tools("find the budget doc on my google drive")
    assert "search_conversations" in RT.select_tools("what did we talk about yesterday")
    assert "send_file" in RT.select_tools("send me the report.pdf")
    assert "web_search" not in RT.select_tools("edit config.yaml and save it")


def test_short_follow_ups_keep_the_context_of_the_last_messages():
    hist = [{"role": "user", "content": "search for the latest iPhone"}, {"role": "assistant", "content": "ok"}]
    assert "web_search" in RT.select_tools("do it", hist)
    assert "web_search" not in RT.select_tools("do it", [{"role": "user", "content": "hello"}])
    assert "web_search" in RT.select_tools("go on", [{"role": "user", "content": [{"type": "text", "text": "google the news"}]}])


def test_available_filter_drops_unknown_names():
    assert RT.select_tools("search the web", available={"read_local_file", "web_search"}) == ["read_local_file", "web_search"]


def test_catalog_search_get_and_describe():
    cat = RT.ToolCatalog(ALL + [RT.TOOL_SEARCH])
    assert cat.get("run_shell_command") and cat.get(RT.TOOL_SEARCH_NAME) is None          # the meta tool is not in the catalog
    assert "list_drive_files" in names(cat.search("google drive"))
    assert names(cat.search("run a command"))[0] == "run_shell_command"
    assert cat.search("qqqq zzzz") == [] and cat.search("") == []
    assert RT.describe_tools(cat.search("terminal command")).startswith("- run_shell_command: Run a command in the terminal.")


# ---------------------------------------------------------------- the loop
def run_routed(script, shown, catalog, execute=None):
    srv = start_server(); srv.script = list(script)
    calls = []
    async def ex(name, args):
        calls.append((name, args)); return f"result of {name}"
    loop = AgentLoop(brain=Brain(srv.server_address[1]), tools=shown, execute=execute or ex, activity_for=lambda n, a: f"{n}:x",
                     count_tokens=lambda t: max(1, len(t) // 4), temperature=0.5, cfg=LoopConfig(), tool_catalog=catalog)
    async def go():
        return [e async for e in loop.run([{"role": "user", "content": "hi"}])]
    events = asyncio.run(go()); srv.shutdown()
    return types.SimpleNamespace(events=events, calls=calls, requests=srv.requests, loop=loop,
                                 text="".join(e.data for e in events if e.kind == "content"),
                                 errors=[e.data for e in events if e.kind == "error"],
                                 activities=[e.data for e in events if e.kind == "activity"])


def req_tools(r, i):
    return names(r.requests[i]["tools"])


def test_tool_search_adds_the_tool_for_the_rest_of_the_turn():
    shown = ALL[:2] + [RT.TOOL_SEARCH]
    r = run_routed([CALL("tool_search", {"query": "run a command"}), CALL("run_shell_command", {"command": "echo hi"}), ANS("Done.")],
                   shown, RT.ToolCatalog(ALL + [RT.TOOL_SEARCH]))
    assert "run_shell_command" not in req_tools(r, 0) and "run_shell_command" in req_tools(r, 1)
    assert r.calls == [("run_shell_command", {"command": "echo hi"})]                    # tool_search never reaches execute()
    tool_result = [m for m in r.requests[1]["messages"] if m["role"] == "tool"][0]["content"]
    assert "run_shell_command" in tool_result and "Run a command in the terminal" in tool_result
    assert r.text.endswith("Done.") and r.errors == [] and "think" in r.activities
    assert names(shown) == ["read_local_file", "write_local_file", RT.TOOL_SEARCH_NAME]      # the caller's list is not mutated


def test_a_real_tool_called_without_being_shown_is_added_and_run():
    r = run_routed([CALL("web_search", {"query": "cats"}), ANS("Found.")], ALL[:2], RT.ToolCatalog(ALL))
    assert r.calls == [("web_search", {"query": "cats"})] and r.text.endswith("Found.")
    assert "web_search" in req_tools(r, 1)


def test_without_a_catalog_an_unknown_tool_still_fails_as_before():
    r = run_routed([CALL("web_search", {"query": "cats"}), ANS("ok")], ALL[:2], None)
    assert r.calls == []
    assert "Unknown tool" in [m for m in r.requests[1]["messages"] if m["role"] == "tool"][0]["content"]


def test_tool_search_with_no_match_says_so_and_lists_what_the_model_has():
    r = run_routed([CALL("tool_search", {"query": "qqqq zzzz"}), ANS("ok")], ALL[:2] + [RT.TOOL_SEARCH], RT.ToolCatalog(ALL))
    out = [m for m in r.requests[1]["messages"] if m["role"] == "tool"][0]["content"]
    assert "No tool matches" in out and "read_local_file" in out


def test_endless_tool_search_cannot_hang_a_turn():
    script = [CALL("tool_search", {"query": f"run a command {i}"}) for i in range(8)] + [ANS("gave up searching")]
    r = run_routed(script, ALL[:2] + [RT.TOOL_SEARCH], RT.ToolCatalog(ALL))
    assert r.text.endswith("gave up searching") and r.errors == []


def test_the_loop_reports_tools_added_mid_turn():
    seen = []
    async def ex(name, args): return "ok"
    loop = AgentLoop(brain=Brain(1), tools=ALL[:2], execute=ex, activity_for=lambda n, a: n, count_tokens=lambda t: len(t) // 4,
                     temperature=0.5, cfg=LoopConfig(), tool_catalog=RT.ToolCatalog(ALL), on_tools_added=seen.append)
    assert loop._add_tools([ALL[6], ALL[0]]) == ["web_search"] and seen == [["web_search"]]      # read_local_file was already there
    assert loop._add_tools([ALL[6]]) == [] and seen == [["web_search"]]                          # nothing new -> no callback
