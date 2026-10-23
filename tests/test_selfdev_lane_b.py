"""P7 Lane B: copy -> submit -> checks -> user approval -> swap -> rollback. Checks are injected (the real ones run the
full suite + a canary of the copy; that needs FastAPI/uvicorn and is exercised on the user's PC)."""
import json, pathlib, sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "src"))
from lucy import selfdev  # noqa: E402
from lucy.selfdev import lane_b as LB, smoke  # noqa: E402
from lucy.selfdev import __main__ as CLI  # noqa: E402
from lucy.tools import registry as R  # noqa: E402
import pytest  # noqa: E402


def make_repo(root):
    files = {"src/lucy/server/agent_loop.py": "RETRIES = 1\n", "src/lucy/safety/policy.py": "SAFE = True\n",
             "src/lucy/tools/web.py": "TIMEOUT = 10\n", ".core/permissions.yaml": "mode: ask_for_risky\n",
             "ui/chat.js": "// ui\r\n", "README.md": "hi\n", "google_client_secret.json": "{secret}",
             "runtime/sessions.sqlite": "db", "models/x.gguf": "big", "src/lucy/__pycache__/a.pyc": "c"}
    for rel, txt in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(txt.encode())
    (root / ".core" / "constitution.lock").write_text(json.dumps({"files": {"src/lucy/safety/policy.py": "x"}}))
    return root


@pytest.fixture
def lb(tmp_path):
    root = make_repo(tmp_path / "live")
    calls = {"tests": 0, "canary": 0, "tests_ok": True, "canary_ok": True}

    def tests(ws, log):
        calls["tests"] += 1
        return calls["tests_ok"], "12 passed" if calls["tests_ok"] else "1 failed"

    def canary(ws, log):
        calls["canary"] += 1
        return calls["canary_ok"], "ok   1 health" if calls["canary_ok"] else "FAIL 4 chat"
    b = LB.LaneB(root=root, state_dir=root / "runtime" / "selfdev", workspaces=root / "runtime" / "workspaces",
                 run_tests=tests, run_canary=canary)
    b.calls = calls
    return b


def edit(b, pid, rel, text):
    p = pathlib.Path(b.get(pid)["workspace"]) / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


def test_the_copy_leaves_out_runtime_models_caches_and_secrets(lb):
    rec = lb.start("retry more", "web search fails on slow sites", session="s1")
    ws = pathlib.Path(rec["workspace"])
    assert (ws / "src/lucy/server/agent_loop.py").exists() and (ws / "ui/chat.js").read_bytes() == b"// ui\r\n"
    for gone in ("google_client_secret.json", "runtime/sessions.sqlite", "models/x.gguf", "src/lucy/__pycache__/a.pyc"):
        assert not (ws / gone).exists(), gone
    assert rec["status"] == "editing" and "src/lucy/server/agent_loop.py" in rec["base"]


def test_happy_path_submit_checks_approve_rollback(lb):
    pid = lb.start("retry more", "web search fails")["id"]
    edit(lb, pid, "src/lucy/server/agent_loop.py", "RETRIES = 3\n")
    edit(lb, pid, "src/lucy/server/new_helper.py", "X = 1\n")
    (pathlib.Path(lb.get(pid)["workspace"]) / "README.md").unlink()
    rec = lb.submit(pid, background=False)
    assert rec["status"] == "ready" and lb.calls == {**lb.calls, "tests": 1, "canary": 1}
    assert "-RETRIES = 1" in rec["diff"] and "+RETRIES = 3" in rec["diff"]
    live = lb.root
    assert (live / "src/lucy/server/agent_loop.py").read_text() == "RETRIES = 1\n"      # nothing live before approval
    rec = lb.approve(pid)
    assert rec["status"] == "applied" and (live / "src/lucy/server/agent_loop.py").read_text() == "RETRIES = 3\n"
    assert (live / "src/lucy/server/new_helper.py").exists() and not (live / "README.md").exists()
    m = lb.rollback()
    assert m["status"] == "rolled_back"
    assert (live / "src/lucy/server/agent_loop.py").read_text() == "RETRIES = 1\n"
    assert not (live / "src/lucy/server/new_helper.py").exists() and (live / "README.md").read_text() == "hi\n"
    assert lb.get(pid)["status"] == "rolled_back"


@pytest.mark.parametrize("which", ["tests", "canary"])
def test_a_failed_check_never_touches_live_lucy(lb, which):
    lb.calls[which + "_ok"] = False
    pid = lb.start("t", "r")["id"]
    edit(lb, pid, "src/lucy/tools/web.py", "TIMEOUT = 99\n")
    rec = lb.submit(pid, background=False)
    assert rec["status"] == "failed" and which in rec["failure"]
    assert (lb.root / "src/lucy/tools/web.py").read_text() == "TIMEOUT = 10\n"
    with pytest.raises(LB.LaneBError, match="only a change that passed"):
        lb.approve(pid)
    assert lb.releases() == []


@pytest.mark.parametrize("rel", ["src/lucy/safety/policy.py", ".core/permissions.yaml", "src/lucy/safety/new.py",
                                 "src/lucy/selfdev/sandbox.py", "src/lucy/tools/registry.py", "seal.bat",
                                 ".core/constitution.lock", "src/lucy/tools_user/x.py"])
def test_constitution_and_protected_files_are_refused_at_submit(lb, rel):
    pid = lb.start("t", "r")["id"]
    edit(lb, pid, rel, "SAFE = False\n")
    with pytest.raises(LB.LaneBError, match="constitution / protected"):
        lb.submit(pid, background=False)
    assert lb.calls["tests"] == 0 and lb.get(pid)["status"] == "editing"


def test_nothing_changed_and_too_many_open_changes(lb):
    pid = lb.start("t", "r")["id"]
    with pytest.raises(LB.LaneBError, match="nothing changed"):
        lb.submit(pid, background=False)
    lb.start("t2", "r")
    lb.start("t3", "r")
    with pytest.raises(LB.LaneBError, match="already open"):
        lb.start("t4", "r")
    with pytest.raises(LB.LaneBError, match="title and a reason"):
        lb.discard(pid) and lb.start("", "")


def test_a_conflicting_live_edit_blocks_the_swap(lb):
    pid = lb.start("t", "r")["id"]
    edit(lb, pid, "src/lucy/tools/web.py", "TIMEOUT = 20\n")
    lb.submit(pid, background=False)
    (lb.root / "src/lucy/tools/web.py").write_text("TIMEOUT = 15\n")             # the user edited it meanwhile
    with pytest.raises(LB.LaneBError, match="conflict"):
        lb.approve(pid)
    assert (lb.root / "src/lucy/tools/web.py").read_text() == "TIMEOUT = 15\n"


def test_edits_after_the_checks_require_a_new_submit_and_stray_files_are_not_applied(lb):
    pid = lb.start("t", "r")["id"]
    edit(lb, pid, "src/lucy/tools/web.py", "TIMEOUT = 20\n")
    lb.submit(pid, background=False)
    edit(lb, pid, "tests/leftover_from_a_test_run.txt", "x")                     # not part of the checked set
    edit(lb, pid, "src/lucy/tools/web.py", "TIMEOUT = 0  # sneaky\n")
    with pytest.raises(LB.LaneBError, match="changed after the checks"):
        lb.approve(pid)
    edit(lb, pid, "src/lucy/tools/web.py", "TIMEOUT = 20\n")
    rec = lb.submit(pid, background=False)
    assert rec["status"] == "ready"
    lb.approve(pid)
    assert (lb.root / "src/lucy/tools/web.py").read_text() == "TIMEOUT = 20\n"


def test_the_rollback_command_works_without_the_server(lb, monkeypatch, capsys):
    pid = lb.start("t", "r")["id"]
    edit(lb, pid, "src/lucy/tools/web.py", "TIMEOUT = 20\n")
    lb.submit(pid, background=False)
    lb.approve(pid)
    monkeypatch.setattr(CLI, "_root", lambda: lb.root)
    monkeypatch.setattr(CLI, "state_dir", lambda: lb.state_dir)
    monkeypatch.setattr(CLI, "workspaces_dir", lambda: lb.workspaces)
    assert CLI.main(["releases"]) == 0 and "applied" in capsys.readouterr().out
    assert CLI.main(["rollback"]) == 0 and "Rolled back" in capsys.readouterr().out
    assert (lb.root / "src/lucy/tools/web.py").read_text() == "TIMEOUT = 10\n"
    assert CLI.main(["rollback"]) == 1


def test_background_submit_and_the_core_change_tool(lb, monkeypatch):
    from lucy.tools import core_change
    monkeypatch.setattr(selfdev, "_laneb", lb)
    ctx = R.ToolContext("s1")
    out = core_change.handler({"action": "start", "title": "retry", "reason": "slow sites"}, ctx)
    assert "copy of Lucy Core is at" in out
    pid = lb.list()[0]["id"]
    edit(lb, pid, "src/lucy/tools/web.py", "TIMEOUT = 30\n")
    assert "Checks started" in core_change.handler({"action": "submit", "id": pid}, ctx)
    assert lb.wait(pid, 30)["status"] == "ready"
    st = core_change.handler({"action": "status", "id": pid}, ctx)
    assert "waits for the USER" in st and "Settings > Evolve" in st
    actions = core_change.SCHEMA["function"]["parameters"]["properties"]["action"]["enum"]
    assert "approve" not in actions and "rollback" not in actions        # the agent has no way to approve / swap
    assert core_change.handler({"action": "discard", "id": "bad id!"}, ctx).startswith("Error")


def test_policy_tiers_for_the_core_change_actions(monkeypatch):
    from _safety_helpers import Env
    env = Env("ask_for_risky", monkeypatch)
    try:
        for action, want in (("status", "auto"), ("ideas", "auto"), ("start", "auto"), ("submit", "ask")):
            assert env.layer.decide("core_change", "exec", {"action": action}, "s").action == want, action
    finally:
        env.close()


def test_the_canary_runtime_cannot_reach_the_real_models(tmp_path):
    smoke.prepare_runtime(tmp_path, 45678)
    cfg = json.loads((tmp_path / "runtime/data/llm_models.json").read_text())
    assert cfg["autostart"] is False and [m["port"] for m in cfg["models"]] == [45678]
    assert smoke.free_port() not in (8080, 8081, 8090, 8091)


def test_the_fake_llm_scripts_a_tool_round_then_an_answer():
    import urllib.request
    f = smoke.FakeLLM("C:/x/smoke.txt", "MARK")
    try:
        def post(body):
            req = urllib.request.Request(f"http://127.0.0.1:{f.port}/v1/chat/completions", data=json.dumps(body).encode(),
                                         headers={"Content-Type": "application/json"})
            return urllib.request.urlopen(req, timeout=5).read().decode()
        first = post({"stream": True, "tools": [{}], "messages": [{"role": "user", "content": "hi"}]})
        assert "read_local_file" in first and "C:/x/smoke.txt" in first
        second = post({"stream": True, "tools": [{}], "messages": [{"role": "tool", "content": "...MARK..."}]})
        assert smoke.ANSWER in second and f.saw_tool_result
        lucy_sse = 'data: {"content": "Smoke "}\n\ndata: {"content": "check passed."}\n\ndata: [METRICS]{}\n\n'
        assert "".join(smoke._sse_content(lucy_sse)) == smoke.ANSWER     # Lucy's own SSE format (what the canary sends)
    finally:
        f.close()


STUB_UVICORN = r'''
import http.server, json, os, sys, urllib.request
from pathlib import Path
root = Path(os.environ["LUCY_ROOT"])
cfg = json.loads((root / "runtime/data/llm_models.json").read_text())
llm = f"http://127.0.0.1:{cfg['models'][0]['port']}/v1/chat/completions"
port = int(sys.argv[sys.argv.index("--port") + 1])
BROKEN = os.path.exists(root / "BROKEN_CHAT")

def ask(msgs):
    req = urllib.request.Request(llm, data=json.dumps({"stream": True, "tools": [{}], "messages": msgs}).encode(),
                                 headers={"Content-Type": "application/json"})
    return urllib.request.urlopen(req, timeout=10).read().decode()

class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def out(self, obj, ctype="application/json"):
        b = (obj if isinstance(obj, str) else json.dumps(obj)).encode()
        self.send_response(200); self.send_header("Content-Type", ctype); self.send_header("Content-Length", str(len(b)))
        self.end_headers(); self.wfile.write(b)
    def do_GET(self):
        if self.headers.get("X-API-Key") != os.environ["DEV_API_KEY"]:
            self.send_response(401); self.end_headers(); return
        p = self.path
        self.out({"root": str(root)} if p == "/api/selfdev/status" else {"halted": None} if p == "/api/safety/status" else {"ok": 1})
    def do_POST(self):
        self.rfile.read(int(self.headers["Content-Length"]))
        first = ask([{"role": "user", "content": "smoke"}])
        args = json.loads(json.loads(first.split("data: ")[1])["choices"][0]["delta"]["tool_calls"][0]["function"]["arguments"])
        content = "nothing" if BROKEN else open(args["file_path"]).read()
        second = ask([{"role": "user", "content": "smoke"}, {"role": "tool", "content": content}])
        text = "".join(json.loads(l[6:])["choices"][0]["delta"].get("content") or "" for l in second.splitlines()
                       if l.startswith("data: {"))
        self.out(f"data: {json.dumps({'content': text})}\n\ndata: [METRICS]{{}}\n\n", "text/event-stream")
http.server.ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()
'''


@pytest.mark.parametrize("broken", [False, True])
def test_the_canary_harness_starts_checks_and_stops_the_copy(tmp_path, broken):
    copy = tmp_path / "copy"
    (copy / "src" / "uvicorn").mkdir(parents=True)
    (copy / "src" / "uvicorn" / "__init__.py").write_text("")
    (copy / "src" / "uvicorn" / "__main__.py").write_text(STUB_UVICORN)
    if broken:
        (copy / "BROKEN_CHAT").write_text("1")                    # the "new build" no longer passes tool results on
    ok, report = smoke.run_canary(copy, start_timeout=30, chat_timeout=30)
    if broken:
        assert not ok and "FAIL 4 chat round trip" in report and "ok   3" in report
    else:
        assert ok, report
        assert report.count("ok   ") == 6
