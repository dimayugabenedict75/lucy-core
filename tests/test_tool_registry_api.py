"""api.py <-> tool registry wiring. Same stubs as test_proactive_integration.py (no GPU/Windows needed)."""
import asyncio, os, shutil, sys, tempfile, types
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
TMP = Path(tempfile.mkdtemp(prefix="lucy_reg_"))
os.environ.setdefault("LUCY_ROOT", str(TMP))
os.environ.setdefault("LUCY_SAFE_ROOT", str(TMP))
(TMP / ".core" / "personas").mkdir(parents=True, exist_ok=True)
(TMP / "ui").mkdir(exist_ok=True)
(TMP / "ui" / "chat.html").write_text("<html><body>key=__API_KEY_PLACEHOLDER__</body></html>")
shutil.copy(HERE / ".core" / "personas" / "lucy.md", TMP / ".core" / "personas" / "lucy.md")
sys.path.insert(0, str(HERE / "src"))

tk = types.ModuleType("tiktoken")
tk.get_encoding = lambda name: types.SimpleNamespace(encode=lambda t: t.split())
sys.modules.setdefault("tiktoken", tk)
if "lucy.cua.lucy_cua" not in sys.modules:
    cua = types.ModuleType("lucy.cua.lucy_cua")
    for n in ("get_cursor_pos", "set_cursor_pos", "mouse_click", "mouse_drag", "key_combo", "type_text", "minimize_all"):
        setattr(cua, n, lambda *a, **k: {})
    sys.modules["lucy.cua.lucy_cua"] = cua

from lucy.server import api  # noqa: E402
from lucy.tools import registry as R  # noqa: E402


def _dummy():
    schema = {"type": "function", "function": {"name": "zz_dummy", "description": "Test tool.",
              "parameters": {"type": "object", "properties": {}, "required": []}}}
    return R.ToolSpec("zz_dummy", schema, lambda a, c: f"dummy:{a.get('v')}", "read", "search", "test")


def test_execute_tool_call_dispatches_to_registry_and_labels_come_from_it():
    R.default.register(_dummy())
    try:
        assert asyncio.run(api.execute_tool_call("zz_dummy", {"v": "ok"})) == "dummy:ok"
        assert api._tool_to_activity("zz_dummy", {}) == "search"
    finally:
        R.default.unregister("zz_dummy")


def test_every_registry_tool_is_in_the_prompt_tool_list():
    names = [t["function"]["name"] for t in api.TOOLS]
    for s in R.default.all_specs():
        assert s.name in names and s.name in api.TOOL_REGISTRY
    assert len(names) == len(set(names)), "duplicate tool names in api.TOOLS"


def test_google_tools_work_through_execute_tool_call():
    from lucy.tools import google_drive
    T = google_drive.Toolset
    saved = T.get_doc_content
    T.get_doc_content = lambda self, title: f"content:{title}"
    try:
        assert asyncio.run(api.execute_tool_call("get_doc_content", {"title": "Plan"})) == "content:Plan"
        assert api._tool_to_activity("get_doc_content", {}) == "docs"
        assert api._tool_to_activity("list_drive_files", {}) == "drive"
    finally:
        T.get_doc_content = saved
    names = [t["function"]["name"] for t in api.TOOLS]
    for n in ("list_drive_files", "create_google_doc", "get_doc_content"):
        assert names.count(n) == 1


def test_file_tools_work_through_execute_tool_call():
    probe = api._paths.SAFE_ROOT / "_registry_probe.txt"
    try:
        out = asyncio.run(api.execute_tool_call("write_local_file", {"file_path": probe.as_posix(), "content": "abc\n"}))
        assert "Error" not in out and "Refused" not in out, out
        assert "abc" in asyncio.run(api.execute_tool_call("read_local_file", {"file_path": probe.as_posix()}))
        assert api._tool_to_activity("list_local_files", {}) == "list"
        assert api._tool_to_activity("send_file", {}) == "send"
    finally:
        probe.unlink(missing_ok=True)
    names = [t["function"]["name"] for t in api.TOOLS]
    for n in ("read_local_file", "edit_local_file", "write_local_file", "list_local_files", "find_file", "send_file"):
        assert names.count(n) == 1


def test_web_and_media_tools_work_through_execute_tool_call():
    from lucy.tools import web, media
    saved = (web._web_search, media.clean_for_speech)
    seen = {}
    async def fake_search(q, limit): return f"S:{q}:{limit}"
    web._web_search = fake_search
    media.clean_for_speech = lambda text, names: (seen.setdefault("names", names), "")[1]
    try:
        assert asyncio.run(api.execute_tool_call("web_search", {"query": "cats"})) == "S:cats:5"
        out = asyncio.run(api.execute_tool_call("send_tts", {"text": "hello"}))
        assert out.startswith("send_tts: nothing speakable")
        assert "send_tts" in seen["names"] and "web_search" in seen["names"]       # api passes the real tool-name list
    finally:
        web._web_search, media.clean_for_speech = saved
    assert api._tool_to_activity("analyze_image", {}) == "analyze" and api._tool_to_activity("send_tts", {}) == "speak"
    names = [t["function"]["name"] for t in api.TOOLS]
    for n in ("web_search", "web_extract", "analyze_image", "send_tts", "send_sticker"):
        assert names.count(n) == 1


def test_shell_and_cua_tools_work_through_execute_tool_call():
    import json
    from lucy.tools import shell
    saved = shell.build_argv
    shell.build_argv = lambda sh, command: [sys.executable, "-u", "-c", command]          # fake "shell": the command is Python code
    try:
        out = asyncio.run(api.execute_tool_call("run_shell_command", {"command": "print('hi')"}))
        assert "hi" in out and "[exit code 0]" in out, out
    finally:
        shell.build_argv = saved
    assert isinstance(json.loads(asyncio.run(api.execute_tool_call("cua_cursor", {}))), dict)   # stub from the test header
    assert api._tool_to_activity("run_shell_command", {}) == "run"
    assert api._tool_to_activity("cua_mouse", {}) == "control"
    assert asyncio.run(api.execute_tool_call("no_such_tool", {})) == "Unknown tool: no_such_tool"
    names = [t["function"]["name"] for t in api.TOOLS]
    for n in ("run_shell_command", "cua_cursor", "cua_mouse", "cua_keyboard"):
        assert names.count(n) == 1


def test_every_tool_in_the_prompt_goes_through_the_registry_with_a_tier_and_label():
    """P0 definition of done: no tool lives outside the registry."""
    for t in api.TOOLS:
        name = t["function"]["name"]
        spec = R.default.get(name)
        assert spec is not None, f"{name} is in api.TOOLS but not registered"
        assert spec.tier in R.VALID_TIERS and spec.label.strip() and callable(spec.handler), name
        assert spec.schema is t or spec.schema == t, name
    assert {s.name for s in R.default.all_specs()} == {t["function"]["name"] for t in api.TOOLS}


def test_memory_and_reminder_adapters_dispatch():
    want = {"remember": "write", "recall_memory": "read", "forget": "write", "search_conversations": "read",
            "set_reminder": "write", "schedule_task": "write", "manage_reminders": "write", "proactive_control": "write"}
    for name, tier in want.items():
        assert R.default.get(name).tier == tier, name
    secret = asyncio.run(api.execute_tool_call("remember", {"content": "my password is hunter2222"}))
    assert '"refused"' in secret                                            # real memory code ran, nothing was stored
    assert '"not_found"' in asyncio.run(api.execute_tool_call("forget", {"memory_id": 987654321}))
    assert asyncio.run(api.execute_tool_call("search_conversations", {})).startswith("Give a query")
    assert isinstance(asyncio.run(api.execute_tool_call("recall_memory", {"query": "zzz-no-such-thing"})), str)
    assert api._tool_to_activity("recall_memory", {}) == "recall" and api._tool_to_activity("forget", {}) == "forget"
    assert api._tool_to_activity("search_conversations", {}) == "history"
    try:                                                                     # the engine may not be started in this test process
        out = asyncio.run(api.execute_tool_call("manage_reminders", {"action": "list"}))
        assert isinstance(out, str) and not out.startswith("Unknown tool")
    except Exception as e:                                                   # noqa: BLE001 - reaching the proactive code is the point
        assert "Unknown tool" not in str(e)


def test_tools_for_turn_shows_a_small_set_and_keeps_the_rest_searchable():
    msgs = [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi there"}]
    shown, catalog = api._tools_for_turn(msgs)
    n = [t["function"]["name"] for t in shown]
    assert "tool_search" in n and "read_local_file" in n and "run_shell_command" not in n and len(n) + 1 <= 10
    assert catalog.get("run_shell_command") and catalog.get("web_search")
    weather = [t["function"]["name"] for t in api._tools_for_turn(msgs[:1] + [{"role": "user", "content": "weather in Manila today?"}])[0]]
    assert "web_search" in weather
    old = os.environ.get("LUCY_TOOL_ROUTING")
    os.environ["LUCY_TOOL_ROUTING"] = "0"
    try:
        all_tools, no_catalog = api._tools_for_turn(msgs)
        assert all_tools is api.TOOLS and no_catalog is None                   # escape hatch: old behaviour
    finally:
        if old is None: os.environ.pop("LUCY_TOOL_ROUTING", None)
        else: os.environ["LUCY_TOOL_ROUTING"] = old
