"""Lane B canary: start the CHANGED copy of Lucy Core on another port and check that it still works. Stdlib only.

The canary never touches the real models: it gets its own runtime/ inside the copy with an llm_models.json that points
at a scripted fake LLM on a free port (autostart off, so it cannot stop the real llama-servers on 8080/8081), a temp
LUCY_SAFE_ROOT, its own API key, LUCY_REFLECT=0 and LUCY_CANARY=1.

Smoke script (all must pass):
  1. /api/health answers                          the app imports and starts
  2. /api/selfdev/status says root = the copy     it really is the copy's code that runs
  3. /api/safety/status is not halted             the safety layer starts and the constitution lock matches
  4. chat round trip WITH a tool call             model -> read_local_file -> model -> answer reaches the client
  5. /api/library answers                         Library view
  6. /api/proactive/status answers                the proactive engine (scheduler) is up
"""
from __future__ import annotations

import http.server
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable, List, Optional, Tuple

ANSWER = "Smoke check passed."


def free_port(avoid=(8080, 8081, 8090, 8091)) -> int:
    for _ in range(50):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        if port not in avoid:
            return port
    raise RuntimeError("no free port")


class FakeLLM:
    """Scripted OpenAI-compatible server: 1st chat round asks for read_local_file, 2nd round answers."""

    def __init__(self, smoke_file: str, marker: str):
        self.smoke_file, self.marker = smoke_file, marker
        self.requests: List[dict] = []
        self.saw_tool_result = False
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _json(self, obj, code=200):
                b = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)

            def do_GET(self):
                if self.path.rstrip("/").endswith("/models"):
                    self._json({"data": [{"id": "canary"}]})
                else:
                    self._json({"status": "ok"})

            def do_POST(self):
                try:
                    body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                except ValueError:
                    body = {}
                outer.requests.append(body)
                msgs = body.get("messages") or []
                tool_seen = any(m.get("role") == "tool" and outer.marker in str(m.get("content")) for m in msgs)
                if tool_seen:
                    outer.saw_tool_result = True
                if not body.get("stream"):
                    self._json({"choices": [{"message": {"role": "assistant", "content": "ok"}}]})
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()

                def w(delta, fin=None):
                    self.wfile.write(b"data: " + json.dumps({"choices": [{"delta": delta, "finish_reason": fin}]}).encode() + b"\n\n")
                if tool_seen or not body.get("tools"):
                    w({"content": ANSWER})
                    w({}, "stop")
                else:
                    w({"tool_calls": [{"index": 0, "id": "smoke1", "function": {
                        "name": "read_local_file", "arguments": json.dumps({"file_path": outer.smoke_file})}}]})
                    w({}, "tool_calls")
                self.wfile.write(b"data: [DONE]\n\n")

        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


def _req(url: str, key: str, data: Optional[dict] = None, timeout: float = 15.0) -> Tuple[int, str]:
    req = urllib.request.Request(url, data=json.dumps(data).encode() if data is not None else None,
                                 headers={"X-API-Key": key, "Content-Type": "application/json"},
                                 method="POST" if data is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, (e.read() or b"").decode("utf-8", "replace")
    except (urllib.error.URLError, OSError) as e:
        return 0, str(e)


def prepare_runtime(copy: Path, llm_port: int) -> None:
    data = Path(copy) / "runtime" / "data"
    data.mkdir(parents=True, exist_ok=True)
    (data / "llm_models.json").write_text(json.dumps({
        "llama_server": "canary-has-no-llama-server", "autostart": False, "host": "127.0.0.1",
        "models": [{"id": "canary", "label": "Canary fake model", "port": llm_port, "temperature": 0.5,
                    "gguf": "canary", "mmproj": "", "extra_args": []}]}), encoding="utf-8")
    (data / "llm_state.json").write_text(json.dumps({"active": "canary", "temperature": 0.5, "reasoning": False,
                                                     "kv_cache": "q8_0"}), encoding="utf-8")


def run_canary(copy: Path, *, python: Optional[str] = None, log: Callable[[str], None] = lambda s: None,
               start_timeout: float = 150.0, chat_timeout: float = 150.0) -> Tuple[bool, str]:
    """Start the copy on a free port, run the smoke script, stop it. (ok, report)."""
    copy = Path(copy).resolve()
    safe = Path(tempfile.mkdtemp(prefix="lucy_canary_safe_"))
    marker = "canary-" + secrets.token_hex(4)
    smoke_file = safe / "smoke.txt"
    smoke_file.write_text(f"This file is part of the canary smoke test ({marker}).\n", encoding="utf-8")
    fake = FakeLLM(smoke_file.as_posix(), marker)
    port, key = free_port(), secrets.token_hex(16)
    prepare_runtime(copy, fake.port)
    env = {k: v for k, v in os.environ.items() if not k.startswith("LUCY_") and k not in ("DEV_API_KEY", "PYTHONPATH")}
    env.update({"PYTHONPATH": str(copy / "src"), "LUCY_ROOT": str(copy), "LUCY_SAFE_ROOT": str(safe),
                "LUCY_CACHE_DIR": str(copy / "cache"), "DEV_API_KEY": key, "LUCY_REFLECT": "0", "LUCY_CANARY": "1",
                "PYTHONDONTWRITEBYTECODE": "1"})
    logf = open(copy / "runtime" / "canary.log", "w", encoding="utf-8", errors="replace")
    flags = 0x08000000 if sys.platform == "win32" else 0
    proc = subprocess.Popen([python or sys.executable, "-m", "uvicorn", "lucy.server.api:app", "--host", "127.0.0.1",
                             "--port", str(port)], cwd=str(copy / "src"), env=env, stdout=logf, stderr=subprocess.STDOUT,
                            creationflags=flags)
    base = f"http://127.0.0.1:{port}"
    report: List[str] = []

    def fail(msg: str) -> Tuple[bool, str]:
        report.append("FAIL " + msg)
        return False, "\n".join(report)

    try:
        log(f"canary starting on port {port}")
        t0 = time.monotonic()
        while True:
            if proc.poll() is not None:
                return fail(f"the canary exited during startup (code {proc.returncode}); see runtime/canary.log in the copy:\n"
                            + _tail(copy / "runtime" / "canary.log"))
            code, _ = _req(base + "/api/health", key, timeout=3)
            if code == 200:
                break
            if time.monotonic() - t0 > start_timeout:
                return fail(f"no answer on /api/health after {int(start_timeout)} s:\n" + _tail(copy / "runtime" / "canary.log"))
            time.sleep(1.0)
        report.append(f"ok   1 health ({time.monotonic() - t0:.0f} s to start)")
        code, body = _req(base + "/api/selfdev/status", key)
        try:
            root = json.loads(body).get("root", "")
        except ValueError:
            root = ""
        if code != 200 or Path(root).resolve() != copy:
            return fail(f"2 /api/selfdev/status: code {code}, root {root!r} (expected the copy {copy})")
        report.append("ok   2 the copy's code is running")
        code, body = _req(base + "/api/safety/status", key)
        try:
            halted = json.loads(body).get("halted")
        except ValueError:
            halted = "unreadable"
        if code != 200 or halted:
            return fail(f"3 safety layer: code {code}, halted={halted!r}")
        report.append("ok   3 safety layer up, constitution matches")
        code, body = _req(base + "/api/chat", key, {"message": "Canary smoke test: please read smoke.txt.",
                                                     "session_id": "canary-smoke", "stream": True}, timeout=chat_timeout)
        if code != 200:
            return fail(f"4 chat: HTTP {code}: {body[:300]}")
        text = "".join(_sse_content(body))
        if ANSWER not in text or not fake.saw_tool_result:
            return fail(f"4 chat round trip: answer seen={ANSWER in text}, tool result reached the model={fake.saw_tool_result}; "
                        f"stream: {body[:400]}")
        report.append("ok   4 chat round trip with a tool call (read_local_file)")
        code, body = _req(base + "/api/library", key)
        if code != 200:
            return fail(f"5 library: HTTP {code}: {body[:200]}")
        report.append("ok   5 library")
        code, body = _req(base + "/api/proactive/status", key)
        if code != 200:
            return fail(f"6 proactive: HTTP {code}: {body[:200]}")
        report.append("ok   6 proactive engine")
        return True, "\n".join(report)
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=15)
        except Exception:                                    # noqa: BLE001
            try:
                proc.kill()
            except Exception:                                # noqa: BLE001
                pass
        logf.close()
        fake.close()
        shutil.rmtree(safe, ignore_errors=True)


def _sse_content(body: str):
    for line in body.splitlines():
        if line.startswith("data: ") and not line.startswith("data: [METRICS]"):
            try:
                d = json.loads(line[6:])
            except ValueError:
                continue
            if isinstance(d, dict) and isinstance(d.get("content"), str):
                yield d["content"]


def _tail(p: Path, n: int = 2500) -> str:
    try:
        return Path(p).read_text(encoding="utf-8", errors="replace")[-n:]
    except OSError:
        return "(no log)"
