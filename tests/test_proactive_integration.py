"""Integration: the patched api.py + the proactive package, through real HTTP routes (TestClient).
Stubs only what cannot exist off-Windows / offline: tiktoken's vocab download and the Win32 cursor module.
Run: pytest tests/test_proactive_integration.py"""
import asyncio, json, os, sys, tempfile, types, shutil
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
TMP = Path(tempfile.mkdtemp(prefix="lucy_it_"))
os.environ["LUCY_ROOT"] = str(TMP)
os.environ["LUCY_SAFE_ROOT"] = str(TMP)
(TMP / ".core" / "personas").mkdir(parents=True)
(TMP / "ui").mkdir()
(TMP / "ui" / "chat.html").write_text("<html><body>key=__API_KEY_PLACEHOLDER__</body></html>")
shutil.copy(HERE / ".core" / "personas" / "lucy.md", TMP / ".core" / "personas" / "lucy.md")
sys.path.insert(0, str(HERE / "src"))

# --- stubs
tk = types.ModuleType("tiktoken")
tk.get_encoding = lambda name: types.SimpleNamespace(encode=lambda t: t.split())
sys.modules.setdefault("tiktoken", tk)
cua = types.ModuleType("lucy.cua.lucy_cua")
for n in ("get_cursor_pos", "set_cursor_pos", "mouse_click", "mouse_drag", "key_combo", "type_text", "minimize_all"):
    setattr(cua, n, lambda *a, **k: {})
sys.modules["lucy.cua.lucy_cua"] = cua

from fastapi.testclient import TestClient  # noqa: E402
from lucy.server import api  # noqa: E402
from lucy import proactive  # noqa: E402

H = {"X-API-Key": "dev-harness"}


def client():
    return TestClient(api.app)


def _purge(*sids):
    with api._db_lock:
        for sid in sids:
            for table in ("messages", "summaries", "sessions"):
                try:
                    api._conn.execute(f"DELETE FROM {table} WHERE session_id = ?", (sid,))
                except Exception:                                # noqa: BLE001 - a table this version does not have
                    pass
        api._conn.commit()


class _session:
    """A session id nobody used before, deleted afterwards. When the whole suite runs, `lucy.paths` is already imported with the REAL
    project root before this file sets LUCY_ROOT, so these tests write into the real runtime/sessions.sqlite: a fixed id piled up
    4 messages per run (the alternation test then failed) and left test sessions in the chat sidebar."""
    def __init__(self, prefix):
        import uuid
        self.sid = f"{prefix}-{uuid.uuid4().hex[:8]}"
    def __enter__(self):
        return self.sid
    def __exit__(self, *a):
        _purge(self.sid)


def test_tools_registered_everywhere():
    names = [t["function"]["name"] for t in api.TOOLS]
    for n in proactive.TOOL_NAMES:
        assert n in names and n in api.TOOL_REGISTRY
    assert set(proactive.TOOL_NAMES) <= {t["function"]["name"] for t in api._resolve_skills_to_tools([{"tools": []}])}


def test_routes_need_key_but_js_is_public():
    with client() as c:
        assert c.get("/api/proactive/status").status_code == 401
        assert c.get("/api/proactive/status", headers=H).json()["running"] is True
        js = c.get("/proactive.js")
        assert js.status_code == 200 and "EventSource" in js.text


def test_root_injects_client_once():
    with client() as c:
        html = c.get("/").text
        assert html.count("/proactive.js") == 1 and 'LUCY_PROACTIVE_KEY="dev-harness"' in html
        assert proactive.inject_client(html, "k") == html          # idempotent


def test_tool_roundtrip_through_execute_tool_call():
    with client():
        out = asyncio.run(api.execute_tool_call("set_reminder", {"message": "Test ping", "when": "in 15 minutes"}))
        assert out.startswith("Scheduled reminder #")
        lst = asyncio.run(api.execute_tool_call("manage_reminders", {"action": "list"}))
        assert "Test ping" in lst
        r = TestClient(api.app).get("/api/proactive/reminders", headers=H).json()["reminders"]
        assert r and r[0]["text"] == "Test ping"
        asyncio.run(api.execute_tool_call("manage_reminders", {"action": "cancel_all"}))


def test_test_endpoint_stores_message_in_active_session_and_streams():
    with client() as c, _session("s-active") as sid:
        api._append_message(sid, "user", "hello")
        api._append_message(sid, "assistant", "hi")
        res = c.post("/api/proactive/test", headers=H, json={"text": "ping from test"}).json()
        assert res["ok"]
        msgs = api._get_messages(sid)
        assert msgs[-1]["role"] == "assistant" and "ping from test" in msgs[-1]["content"]
        # no window was open -> replayed to the next connection (poll variant of the same data)
        ev = c.get("/api/proactive/pending?since=0", headers=H).json()["events"]
        assert ev and ev[-1]["text"] == "ping from test"
        # feedback + control
        assert c.post("/api/proactive/feedback", headers=H, json={"id": ev[-1]["id"], "action": "dismiss"}).json()["ok"]
        ctl = c.post("/api/proactive/control", headers=H, json={"action": "pause", "minutes": 30}).json()
        assert ctl["status"]["paused"] is True
        assert c.post("/api/proactive/control", headers=H, json={"action": "bogus"}).status_code == 400
        c.post("/api/proactive/control", headers=H, json={"action": "resume"})


def test_cli_sessions_never_receive_proactive_messages():
    with client(), _session("cli-dev") as sid:
        api._append_message(sid, "user", "x")
        assert not api._proactive_pick_session().startswith("cli-")


def test_build_messages_alternates_and_carries_clock():
    with client(), _session("s-alt") as sid:
        api._append_message(sid, "assistant", "unprompted hello")                 # Lucy spoke first
        api._append_message(sid, "user", "hey")
        api._append_message(sid, "assistant", "reply")
        api._append_message(sid, "assistant", "second unprompted")                # two in a row
        msgs = api._build_messages(sid, "what's next today?")
        convo = [m["role"] for m in msgs if m["role"] in ("user", "assistant")]
        assert convo == ["user", "assistant", "user", "assistant", "user"]
        assert "Current local time: " in msgs[-1]["content"] and msgs[-1]["content"].startswith("what's next today?")
        assert any("REMINDERS & FOLLOW-UPS" in str(m["content"]) for m in msgs if m["role"] == "system")
        # the clock note is only on the final message (keeps the llama KV cache valid)
        assert all("Current local time" not in str(m["content"]) for m in msgs[:-1])


def test_background_task_agent_is_restricted(monkeypatch=None):
    """_proactive_run_agent must refuse any tool outside the allowlist even if the model asks for it."""
    calls = []

    async def fake_exec(name, args):
        calls.append(name); return "ok"

    class FakeLoop:
        def __init__(self, **kw):
            self.kw = kw
        async def run(self, messages):
            res = await self.kw["execute"]("run_shell_command", {"command": "del *"})
            res2 = await self.kw["execute"]("web_search", {"query": "x"})
            yield types.SimpleNamespace(kind="content", data=f"{res} | {res2}")

    orig_exec, orig_loop = api.execute_tool_call, api.AgentLoop
    api.execute_tool_call, api.AgentLoop = fake_exec, FakeLoop
    try:
        out = asyncio.run(api._proactive_run_agent([], ["web_search"], 5, 3))
    finally:
        api.execute_tool_call, api.AgentLoop = orig_exec, orig_loop
    assert "not available in a background task" in out and calls == ["web_search"]


def test_yaml_defaults_file_matches_code_defaults():
    import yaml
    from lucy.proactive.config import DEFAULTS, deep_merge
    f = HERE / ".core" / "proactive.yaml"
    assert deep_merge(DEFAULTS, yaml.safe_load(f.read_text(encoding="utf-8"))) == DEFAULTS


def test_promise_detector_catches_announce_then_stop():
    """Regression: replies like these (seen live) ended the turn with no tool call and went undetected."""
    from lucy.server.multistep import find_promise
    for t in ["I'll start by reading the file. 🛠️",
              "I'm going to pull the contents of src/lucy/server/agent_loop.py to examine the Plan logic.\nI'll start by reading the file.",
              "I am currently executing the command to read agent_loop.py. I'll report back as soon as the file content is in hand."]:
        assert find_promise(t) is not None, t
    for t in ["Want me to read the file?", "The file reads fine; nothing to change.", "Done. The loop already handles that."]:
        assert find_promise(t) is None, t


def test_clock_note_only_when_about_time():
    with client(), _session("s-clock") as sid:
        api._append_message(sid, "user", "hi"); api._append_message(sid, "assistant", "hey")
        assert "Current local time" not in api._build_messages(sid, "read agent_loop.py")[-1]["content"]
        assert "Current local time" in api._build_messages(sid, "remind me tomorrow at 9")[-1]["content"]
