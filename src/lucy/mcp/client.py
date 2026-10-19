"""A small MCP client: JSON-RPC 2.0 over a child process (stdio) or streamable HTTP. Standard library only.

Why hand-rolled: it works on any asyncio event loop (Windows included), needs no extra install, and is easy to test
with a fake server. Everything here is synchronous and thread-based, like tools/jobs.py; the manager calls it from
worker threads, so the event loop is never blocked.

Supported: initialize, tools/list (+ pagination, + list_changed), tools/call, ping, cancellation on timeout.
A server that asks US for something (sampling, roots, elicitation) gets a clean "not supported" error, never silence.
Not supported (on purpose, for now): the legacy SSE transport, OAuth login for remote servers (use a token header),
server-initiated streams on HTTP, resources and prompts.
"""
from __future__ import annotations

import codecs
import itertools
import json
import os
import shutil
import subprocess
import threading
import urllib.error
import urllib.request
from collections import deque
from concurrent.futures import Future, TimeoutError as FutureTimeout
from typing import Any, Callable, Deque, Dict, List, Optional

PROTOCOL = "2025-06-18"
KNOWN_PROTOCOLS = {"2025-06-18", "2025-03-26", "2024-11-05"}
CLIENT_INFO = {"name": "lucy-core", "version": "1.0"}
_WIN = os.name == "nt"
_HIDDEN_ENV = {"DEV_API_KEY", "LUCY_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GOOGLE_CLIENT_SECRET"}


class McpError(RuntimeError):
    def __init__(self, message: str, code: Optional[int] = None):
        super().__init__(message)
        self.code = code


# ===================================================================================================== transports
class _Transport:
    def __init__(self) -> None:
        self.on_message: Callable[[dict], None] = lambda m: None
        self.on_error: Callable[[Optional[int], McpError], None] = lambda i, e: None   # (request id or None, error)
        self.on_close: Callable[[str], None] = lambda why: None
        self.log: Deque[str] = deque(maxlen=30)

    def send(self, msg: dict) -> None: raise NotImplementedError
    def close(self) -> None: raise NotImplementedError
    @property
    def alive(self) -> bool: raise NotImplementedError
    def set_protocol(self, version: str) -> None: pass


class StdioTransport(_Transport):
    def __init__(self, command: str, args: List[str], env: Dict[str, str], cwd: str = ""):
        super().__init__()
        full_env = {k: v for k, v in os.environ.items() if k.upper() not in _HIDDEN_ENV}   # Lucy's own secrets stay home
        full_env.update(env)
        exe = shutil.which(command, path=full_env.get("PATH") or full_env.get("Path")) or shutil.which(command)
        if exe is None and os.path.isfile(command):
            exe = command
        if exe is None:
            hint = " (install Node.js to get npx)" if command.lower() in ("npx", "node", "npm") else ""
            raise McpError(f"program not found: {command}{hint}")
        kwargs: dict = {}
        if _WIN:
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        else:
            kwargs["start_new_session"] = True
        try:
            self.proc = subprocess.Popen([exe] + list(args), cwd=cwd or None, env=full_env, stdin=subprocess.PIPE,
                                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kwargs)
        except (OSError, ValueError) as e:
            raise McpError(f"could not start {command}: {e}") from e
        self._wlock = threading.Lock()
        self._closed = False
        threading.Thread(target=self._read_stdout, daemon=True, name="mcp-out").start()
        threading.Thread(target=self._read_stderr, daemon=True, name="mcp-err").start()

    @property
    def alive(self) -> bool:
        return not self._closed and self.proc.poll() is None

    def send(self, msg: dict) -> None:
        data = (json.dumps(msg, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")
        try:
            with self._wlock:
                self.proc.stdin.write(data)
                self.proc.stdin.flush()
        except (OSError, ValueError) as e:
            raise McpError(f"the server is not running ({e})") from e

    def _read_stdout(self) -> None:
        try:
            for raw in iter(self.proc.stdout.readline, b""):
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except ValueError:
                    self.log.append(line[:300])                 # a banner / stray print: not protocol, ignore
                    continue
                for m in (msg if isinstance(msg, list) else [msg]):
                    if isinstance(m, dict):
                        try:
                            self.on_message(m)
                        except Exception:                       # noqa: BLE001 - one bad message must not kill the reader
                            pass
        except Exception:                                       # noqa: BLE001
            pass
        finally:
            self._closed = True
            self.on_close(self.stderr_tail() or f"the server exited (code {self.proc.poll()})")

    def _read_stderr(self) -> None:
        dec = codecs.getincrementaldecoder("utf-8")(errors="replace")
        buf = ""
        try:
            while True:
                chunk = self.proc.stderr.read1(8192)
                if not chunk:
                    break
                buf += dec.decode(chunk)
                *lines, buf = buf.split("\n")
                for ln in lines:
                    if ln.strip():
                        self.log.append(ln.strip()[:300])
        except Exception:                                       # noqa: BLE001
            pass
        if buf.strip():
            self.log.append(buf.strip()[:300])

    def stderr_tail(self, n: int = 6) -> str:
        return " | ".join(list(self.log)[-n:])

    def close(self) -> None:
        if self._closed and self.proc.poll() is not None:
            return
        self._closed = True
        try:
            self.proc.stdin.close()                             # the polite way: EOF on stdin
        except Exception:                                       # noqa: BLE001
            pass
        try:
            self.proc.wait(timeout=2)
        except Exception:                                       # noqa: BLE001
            pass
        if self.proc.poll() is None:
            from lucy.tools.jobs import kill_tree
            kill_tree(self.proc)


class HttpTransport(_Transport):
    """Streamable HTTP: every message is a POST; the answer is JSON or an SSE stream carrying the response."""

    def __init__(self, url: str, headers: Dict[str, str], timeout: float = 60.0):
        super().__init__()
        if not url.lower().startswith(("http://", "https://")):
            raise McpError(f"not a web address: {url}")
        self.url, self.headers, self.timeout = url, dict(headers), timeout
        self.session_id: Optional[str] = None
        self._protocol: Optional[str] = None
        self._closed = False

    @property
    def alive(self) -> bool:
        return not self._closed

    def set_protocol(self, version: str) -> None:
        self._protocol = version

    def send(self, msg: dict) -> None:
        if self._closed:
            raise McpError("the connection is closed")
        threading.Thread(target=self._post, args=(msg,), daemon=True, name="mcp-http").start()

    def _request(self, body: Optional[bytes], method: str) -> urllib.request.Request:
        h = {"Accept": "application/json, text/event-stream", **self.headers}
        if body is not None:
            h["Content-Type"] = "application/json"
        if self.session_id:
            h["Mcp-Session-Id"] = self.session_id
        if self._protocol:
            h["MCP-Protocol-Version"] = self._protocol
        return urllib.request.Request(self.url, data=body, headers=h, method=method)

    def _post(self, msg: dict) -> None:
        mid = msg.get("id") if "method" in msg else None
        body = json.dumps(msg, ensure_ascii=False).encode("utf-8")
        try:
            with urllib.request.urlopen(self._request(body, "POST"), timeout=self.timeout) as resp:
                sid = resp.headers.get("Mcp-Session-Id")
                if sid:
                    self.session_id = sid
                if resp.status == 202 or mid is None and resp.status < 300 and not resp.headers.get("Content-Type"):
                    return
                ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
                if ctype == "text/event-stream":
                    self._read_sse(resp, mid)
                else:
                    raw = resp.read().decode("utf-8", errors="replace").strip()
                    if raw:
                        data = json.loads(raw)
                        for m in (data if isinstance(data, list) else [data]):
                            if isinstance(m, dict):
                                self.on_message(m)
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", errors="replace")[:200]
            except Exception:                                   # noqa: BLE001
                pass
            why = {401: "needs authentication (401): put a token in \"headers\" in mcp.json",
                   403: "refused access (403)", 404: "not found (404)" + (" - the session expired" if self.session_id else "")
                   }.get(e.code, f"HTTP {e.code}")
            self.on_error(mid, McpError(f"{self.url} {why} {detail}".strip(), e.code))
        except Exception as e:                                  # noqa: BLE001 - network down, bad JSON, timeout
            self.on_error(mid, McpError(f"{type(e).__name__}: {e}"))

    def _read_sse(self, resp, mid) -> None:
        data: List[str] = []
        for raw in resp:
            line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
            if line == "":
                if data:
                    try:
                        m = json.loads("\n".join(data))
                    except ValueError:
                        m = None
                    data = []
                    if isinstance(m, dict):
                        self.on_message(m)
                        if mid is not None and m.get("id") == mid and ("result" in m or "error" in m):
                            return                              # our answer arrived; stop reading the stream
            elif line.startswith("data:"):
                data.append(line[5:].lstrip(" "))

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self.session_id:
            try:
                urllib.request.urlopen(self._request(None, "DELETE"), timeout=3).close()
            except Exception:                                   # noqa: BLE001 - many servers answer 405: fine
                pass


# ===================================================================================================== client
class McpClient:
    def __init__(self, transport: _Transport, on_tools_changed: Optional[Callable[[], None]] = None,
                 on_close: Optional[Callable[[str], None]] = None):
        self.t = transport
        self.on_tools_changed = on_tools_changed
        self.on_close = on_close
        self.server_info: dict = {}
        self.capabilities: dict = {}
        self.protocol = ""
        self.closed_reason = ""
        self._ids = itertools.count(1)
        self._pending: Dict[int, Future] = {}
        self._lock = threading.Lock()
        transport.on_message = self._on_message
        transport.on_error = self._on_error
        transport.on_close = self._on_closed

    # ------------------------------------------------------------------------------------------ incoming
    def _on_message(self, m: dict) -> None:
        method, mid = m.get("method"), m.get("id")
        if method is not None and mid is not None:                       # the server asks us something
            if method == "ping":
                self._reply(mid, result={})
            else:
                self._reply(mid, error={"code": -32601, "message": f"Lucy does not support '{method}'"})
        elif method is not None:                                         # notification
            if method == "notifications/tools/list_changed" and self.on_tools_changed:
                try:
                    self.on_tools_changed()
                except Exception:                                        # noqa: BLE001
                    pass
            elif method == "notifications/message":
                p = m.get("params") or {}
                self.t.log.append(f"[{p.get('level', 'log')}] {str(p.get('data', ''))[:200]}")
        elif mid is not None:                                            # a response to one of our requests
            with self._lock:
                fut = self._pending.pop(mid, None) if isinstance(mid, int) else None
            if fut is None or fut.done():
                return
            if isinstance(m.get("error"), dict):
                e = m["error"]
                fut.set_exception(McpError(str(e.get("message", "error")), e.get("code")))
            else:
                fut.set_result(m.get("result"))

    def _reply(self, mid, result=None, error=None) -> None:
        msg = {"jsonrpc": "2.0", "id": mid}
        msg.update({"error": error} if error else {"result": result})
        try:
            self.t.send(msg)
        except McpError:
            pass

    def _on_error(self, mid: Optional[int], err: McpError) -> None:
        if mid is None:
            self.t.log.append(str(err))
            return
        with self._lock:
            fut = self._pending.pop(mid, None)
        if fut is not None and not fut.done():
            fut.set_exception(err)

    def _on_closed(self, why: str) -> None:
        self.closed_reason = why
        with self._lock:
            pending, self._pending = list(self._pending.values()), {}
        for fut in pending:
            if not fut.done():
                fut.set_exception(McpError(f"the server stopped: {why}"))
        if self.on_close:
            try:
                self.on_close(why)
            except Exception:                                            # noqa: BLE001
                pass

    # ------------------------------------------------------------------------------------------ outgoing
    @property
    def alive(self) -> bool:
        return self.t.alive

    def request(self, method: str, params: Optional[dict] = None, timeout: float = 60.0) -> Any:
        """Blocks the calling (worker) thread until the answer arrives. Raises McpError."""
        if not self.t.alive:
            raise McpError(f"the server is not running: {self.closed_reason or 'stopped'}")
        mid = next(self._ids)
        fut: Future = Future()
        with self._lock:
            self._pending[mid] = fut
        msg = {"jsonrpc": "2.0", "id": mid, "method": method}
        if params is not None:
            msg["params"] = params
        try:
            self.t.send(msg)
        except McpError:
            with self._lock:
                self._pending.pop(mid, None)
            raise
        try:
            return fut.result(timeout=timeout)
        except FutureTimeout:
            with self._lock:
                self._pending.pop(mid, None)
            self.notify("notifications/cancelled", {"requestId": mid, "reason": "timed out"})
            raise McpError(f"{method} timed out after {int(timeout)} s") from None

    def notify(self, method: str, params: Optional[dict] = None) -> None:
        msg = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        try:
            self.t.send(msg)
        except McpError:
            pass

    # ------------------------------------------------------------------------------------------ protocol
    def initialize(self, timeout: float = 30.0) -> dict:
        res = self.request("initialize", {"protocolVersion": PROTOCOL, "capabilities": {}, "clientInfo": CLIENT_INFO}, timeout)
        res = res if isinstance(res, dict) else {}
        self.protocol = str(res.get("protocolVersion") or "")
        if self.protocol not in KNOWN_PROTOCOLS:
            raise McpError(f"the server speaks protocol version {self.protocol or '?'}, which Lucy does not know")
        self.server_info = res.get("serverInfo") if isinstance(res.get("serverInfo"), dict) else {}
        self.capabilities = res.get("capabilities") if isinstance(res.get("capabilities"), dict) else {}
        self.t.set_protocol(self.protocol)
        self.notify("notifications/initialized")
        return res

    def list_tools(self, timeout: float = 30.0) -> List[dict]:
        tools: List[dict] = []
        cursor: Optional[str] = None
        for _ in range(20):                                              # pagination, with a sanity limit
            res = self.request("tools/list", {"cursor": cursor} if cursor else {}, timeout)
            res = res if isinstance(res, dict) else {}
            tools += [t for t in (res.get("tools") or []) if isinstance(t, dict) and t.get("name")]
            cursor = res.get("nextCursor")
            if not cursor:
                break
        return tools

    def call_tool(self, name: str, arguments: dict, timeout: float = 60.0) -> dict:
        res = self.request("tools/call", {"name": name, "arguments": arguments or {}}, timeout)
        return res if isinstance(res, dict) else {"content": [{"type": "text", "text": str(res)}]}

    def close(self) -> None:
        self.t.close()
