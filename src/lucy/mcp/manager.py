"""McpManager: owns the MCP servers declared in `.core/mcp.json`.

* Lazy: nothing is launched at startup if the tool list is already cached (runtime/data/mcp_state.json). A server is
  started on its first tool call (or when its list must be (re)built) and stopped again after `idle_minutes`.
* Its tools are registered in the tool registry as `server__tool` (tier from the config, default "network"), so every
  call passes the SAME safety path as any other tool (permission gate, hard rules, audit). They are NOT in TOOLS and never
  in the prompt: `catalog_schemas()` feeds `tool_search` / auto-add only.
* Extra guards done here, because the policy only looks at a few argument names: every path-like string in the arguments
  (also inside lists) and every file: URL is checked against the user's denied / constitution / read-only rules, and all
  results are framed as untrusted data (the injection guard) unless the server is marked trusted.
"""
from __future__ import annotations

import re
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from . import config as _cfg
from . import schema as _schema
from .client import HttpTransport, McpClient, McpError, StdioTransport

_PATHISH_KEY = re.compile(r"path|file|dir|folder|source|dest|target|cwd|uri|url", re.I)
_ABS_PATH = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\|/[^/\s]|~[\\/])")
MODULE = "lucy.mcp"
MAX_TOOLS = 300


def _label_for(tier: str) -> str:
    return {"read": "read", "network": "search"}.get(tier, "run")           # activity labels the UI already knows


class McpManager:
    def __init__(self, root: Path, runtime: Path, registry=None,
                 client_factory: Optional[Callable[[_cfg.ServerConfig], McpClient]] = None,
                 max_result_chars: int = 20000):
        self.root, self.runtime = Path(root), Path(runtime)
        if registry is None:
            from lucy.tools import registry as _r
            registry = _r.default
        self.registry = registry
        self._factory = client_factory or self._make_client
        self.max_result_chars = max_result_chars
        self.servers: Dict[str, _cfg.ServerConfig] = {}
        self.problems: List[str] = []
        self.state = _cfg.State(self.runtime / "data" / "mcp_state.json")
        self._raw_tools: Dict[str, List[dict]] = {}         # key -> tools/list entries (from cache or live)
        self._registered: Dict[str, List[str]] = {}         # key -> registry names we own
        self._by_name: Dict[str, tuple] = {}                # full name -> (key, original tool name)
        self._clients: Dict[str, McpClient] = {}
        self._starting: Dict[str, threading.Lock] = {}
        self._last_used: Dict[str, float] = {}
        self._busy: Dict[str, int] = {}
        self._lock = threading.RLock()
        self._mtime: Optional[tuple] = None
        self._stop = threading.Event()
        self._reaper: Optional[threading.Thread] = None

    # ================================================================================================ lifecycle
    def start(self) -> None:
        """Startup: read the config, register cached tools, build missing lists in the background. Never raises."""
        try:
            self.reload(force=True)
        except Exception as e:                              # noqa: BLE001
            self.problems.append(f"MCP could not start: {e}")
            return
        todo = [k for k, c in self.servers.items() if self.is_enabled(k) and k not in self._raw_tools]
        if todo:
            threading.Thread(target=self._list_all, args=(todo,), daemon=True, name="mcp-list").start()
        if self._reaper is None:
            self._reaper = threading.Thread(target=self._reap_loop, daemon=True, name="mcp-reaper")
            self._reaper.start()

    def shutdown(self) -> None:
        """Stop every server and take its tools out of the registry."""
        self._stop.set()
        with self._lock:
            clients, self._clients = list(self._clients.values()), {}
            for key in list(self._registered):
                self._unregister(key)
        for c in clients:
            try:
                c.close()
            except Exception:                               # noqa: BLE001
                pass

    def _list_all(self, keys: List[str]) -> None:
        for k in keys:
            try:
                self.refresh(k)
            except McpError:
                pass                                        # the error is stored for the UI (status)

    def _reap_loop(self) -> None:
        while not self._stop.wait(30):
            self.reap_idle()

    def reap_idle(self, now: Optional[float] = None) -> List[str]:
        """Stop servers that have been idle longer than their idle_minutes. Returns the keys stopped."""
        now = time.monotonic() if now is None else now
        stopped = []
        with self._lock:
            for key, client in list(self._clients.items()):
                cfg = self.servers.get(key)
                idle = (cfg.idle_minutes if cfg else 0) * 60
                if cfg and idle > 0 and not self._busy.get(key) and now - self._last_used.get(key, now) > idle:
                    self._clients.pop(key, None)
                    stopped.append(key)
                    threading.Thread(target=client.close, daemon=True).start()
        return stopped

    # ================================================================================================ config
    def _stat(self) -> Optional[tuple]:
        try:
            st = _cfg.config_path(self.root).stat()
            return (st.st_mtime_ns, st.st_size)
        except OSError:
            return None

    def reload(self, force: bool = False) -> None:
        """(Re)read mcp.json when it changed and bring the registry in line with it."""
        with self._lock:
            stat = self._stat()
            if not force and stat == self._mtime:
                return
            self._mtime = stat
            self.servers, self.problems = _cfg.load(self.root)
            for key in list(self._registered):                          # servers removed from the file
                if key not in self.servers:
                    self._unregister(key)
                    self._drop(key)
            for key, cfg in self.servers.items():
                if not self.is_enabled(key):
                    self._unregister(key)
                    continue
                cached = self.state.cache.get(key)
                if cached and cached.get("fp") == cfg.fingerprint() and isinstance(cached.get("tools"), list):
                    self._raw_tools[key] = cached["tools"]
                    self._register(key, cached["tools"])
                elif cached:                                            # the server changed: forget its old tools
                    self._raw_tools.pop(key, None)
                    self._unregister(key)

    def is_enabled(self, key: str) -> bool:
        cfg = self.servers.get(key)
        return bool(cfg) and self.state.enabled.get(key, cfg.enabled)

    def _drop(self, key: str) -> None:
        self._raw_tools.pop(key, None)
        client = self._clients.pop(key, None)
        if client:
            threading.Thread(target=client.close, daemon=True).start()

    # ================================================================================================ servers
    def _make_client(self, cfg: _cfg.ServerConfig) -> McpClient:
        key = cfg.key
        on_changed = lambda: threading.Thread(target=self._safe_refresh, args=(key,), daemon=True).start()   # noqa: E731
        if cfg.transport == "http":
            headers = {k: _cfg.expand(v) for k, v in cfg.headers.items()}
            t = HttpTransport(_cfg.expand(cfg.url), headers, timeout=max(cfg.timeout, 30.0))
        else:
            t = StdioTransport(_cfg.expand(cfg.command), [_cfg.expand(a) for a in cfg.args],
                               {k: _cfg.expand(v) for k, v in cfg.env.items()}, _cfg.expand(cfg.cwd))
        return McpClient(t, on_tools_changed=on_changed)

    def _safe_refresh(self, key: str) -> None:
        try:
            self.refresh(key)
        except McpError:
            pass

    def ensure_started(self, key: str) -> McpClient:
        cfg = self.servers.get(key)
        if cfg is None:
            raise McpError(f"no MCP server called '{key}'")
        if not self.is_enabled(key):
            raise McpError(f"the MCP server '{key}' is switched off (Settings > Connectors)")
        with self._lock:
            lock = self._starting.setdefault(key, threading.Lock())
        with lock:
            client = self._clients.get(key)
            if client is not None and client.alive:
                return client
            from lucy import safety
            halt = safety.get().halt_reason()                           # a changed safety file / mcp.json: launch nothing
            if halt:
                self._note_error(key, f"not started: {halt}")           # the UI shows why
                raise McpError(f"not started: {halt}")
            self._clients.pop(key, None)
            client = None
            try:
                client = self._factory(cfg)
                client.initialize(timeout=min(max(cfg.timeout, 30.0), 120.0))
            except McpError as e:
                if client is not None:
                    client.close()
                self._note_error(key, str(e))
                raise
            except Exception as e:                                      # noqa: BLE001
                if client is not None:
                    client.close()
                self._note_error(key, f"{type(e).__name__}: {e}")
                raise McpError(f"could not start '{key}': {e}") from e
            self._clients[key] = client
            self._last_used[key] = time.monotonic()
            self._note_error(key, "")
            return client

    def _note_error(self, key: str, text: str) -> None:
        if text:
            self.state.errors[key] = text[:300]
        else:
            self.state.errors.pop(key, None)
        self.state.save()

    def refresh(self, key: str) -> int:
        """Start the server if needed, read its tool list, cache it, register the tools. Returns the tool count."""
        client = self.ensure_started(key)
        cfg = self.servers[key]
        try:
            tools = client.list_tools(timeout=min(max(cfg.timeout, 30.0), 120.0))
        except McpError as e:
            self._note_error(key, f"tools/list failed: {e}")
            raise
        tools = tools[:MAX_TOOLS]                                       # a server with thousands of tools is not useful to a small model
        with self._lock:
            self._raw_tools[key] = tools
            self.state.cache[key] = {"fp": cfg.fingerprint(), "tools": tools, "at": time.time(),
                                     "server": dict(client.server_info)}
            self.state.save()
            self._register(key, tools)
        self._last_used[key] = time.monotonic()
        return len(tools)

    def set_enabled(self, key: str, on: bool) -> None:
        if key not in self.servers:
            raise KeyError(key)
        with self._lock:
            self.state.enabled[key] = bool(on)
            self.state.save()
            if not on:
                self._unregister(key)
                client = self._clients.pop(key, None)
                if client:
                    threading.Thread(target=client.close, daemon=True).start()
            else:
                cached = self.state.cache.get(key)
                if cached and cached.get("fp") == self.servers[key].fingerprint():
                    self._raw_tools[key] = cached["tools"]
                    self._register(key, cached["tools"])

    def stop(self, key: str) -> None:
        with self._lock:
            client = self._clients.pop(key, None)
        if client:
            client.close()

    # ================================================================================================ registry
    def _unregister(self, key: str) -> None:
        for name in self._registered.pop(key, []):
            self.registry.unregister(name)
            self._by_name.pop(name, None)

    def _register(self, key: str, tools: List[dict]) -> None:
        from lucy.tools.registry import ToolSpec, ToolSpecError
        cfg = self.servers[key]
        self._unregister(key)
        names: List[str] = []
        for t in tools:
            orig = str(t.get("name") or "")
            if not orig:
                continue
            full = _schema.tool_name(key, orig)
            tier = cfg.tier_for(orig, _schema.is_read_only(t))
            spec = ToolSpec(full, _schema.to_openai(key, t, full), self._handler(key, orig, full, tier),
                            tier, _label_for(tier), MODULE)
            try:
                self.registry.register(spec)
            except ToolSpecError as e:                                  # same name twice: keep the first, say so
                self.problems.append(str(e))
                continue
            self._by_name[full] = (key, orig)
            names.append(full)
        self._registered[key] = names

    # ================================================================================================ calling
    def _handler(self, key: str, tool: str, full: str, tier: str):
        def handler(args: dict, ctx) -> str:
            return self._run(key, tool, full, tier, args or {}, getattr(ctx, "session_id", None))
        return handler

    def call(self, key: str, tool: str, args: dict, timeout: Optional[float] = None) -> dict:
        cfg = self.servers.get(key)
        if cfg is None:
            raise McpError(f"no MCP server called '{key}'")
        with self._lock:
            self._busy[key] = self._busy.get(key, 0) + 1
        try:
            client = self.ensure_started(key)
            try:
                return client.call_tool(tool, args, timeout or cfg.timeout)
            except McpError as e:
                if client.alive or "timed out" in str(e):
                    raise
                client = self.ensure_started(key)                       # it died under us: restart once and retry
                return client.call_tool(tool, args, timeout or cfg.timeout)
        finally:
            with self._lock:
                self._busy[key] = max(0, self._busy.get(key, 1) - 1)
                self._last_used[key] = time.monotonic()

    def _run(self, key: str, tool: str, full: str, tier: str, args: dict, session: Optional[str]) -> str:
        cfg = self.servers.get(key)
        if cfg is None or not self.is_enabled(key):
            return f"Tool failed: the MCP server '{key}' is switched off."
        refusal = self.check_args(full, tier, args)
        if refusal:
            return refusal
        try:
            raw = self.call(key, tool, args, cfg.timeout)
        except McpError as e:
            return "Tool failed: " + self._frame(full, f"[server message] {full}: {e}", session, cfg.trusted)
        text, is_error = _schema.format_result(raw, self.runtime / "tmp" / "mcp", self.max_result_chars)
        if is_error:
            return "Tool failed: " + self._frame(full, f"[server message] {text}", session, cfg.trusted)
        return self._frame(full, text, session, cfg.trusted)

    @staticmethod
    def _frame(name: str, text: str, session: Optional[str], trusted: bool) -> str:
        """Results from a server are DATA: framed as untrusted and remembered by the injection guard (unless trusted)."""
        if trusted:
            return ("(result) " + text) if text.startswith("MEDIA:") else text    # a server can never make the chat send a file
        from lucy import safety
        from lucy.safety import taint
        safety.get().taint.add(session, text)
        framed = taint.wrap(name, text)
        return framed if framed != text else taint.wrap(name, "(result) " + text)

    # ------------------------------------------------------------------------------------------------ argument guard
    @staticmethod
    def _path_strings(args: Any, key_hint: bool = False, depth: int = 0) -> List[str]:
        out: List[str] = []
        if depth > 6:
            return out
        if isinstance(args, dict):
            for k, v in args.items():
                out += McpManager._path_strings(v, bool(_PATHISH_KEY.search(str(k))), depth + 1)
        elif isinstance(args, (list, tuple)):
            for v in args:
                out += McpManager._path_strings(v, key_hint, depth + 1)
        elif isinstance(args, str):
            s = args.strip()
            if s[:5].lower() == "file:":
                try:
                    p = urllib.request.url2pathname(urllib.parse.urlparse(s).path)
                    out.append(p or s)
                except Exception:                                       # noqa: BLE001
                    out.append(s)
            elif s and len(s) < 1000 and (_ABS_PATH.match(s) or (key_hint and ("/" in s or "\\" in s))):
                out.append(s)
        return out

    def check_args(self, name: str, tier: str, args: dict) -> Optional[str]:
        """The user's DENY rules (secrets, Lucy's safety files, read-only system folders on writes) for every path in the
        arguments, including paths the permission policy does not look at (lists, nested objects, file: URLs)."""
        from lucy import safety
        layer = safety.get()
        for p in dict.fromkeys(self._path_strings(args)):
            d = layer.policy.decide(name, tier, {"path": p})
            if d.level >= safety.DENY:
                return layer._refuse(name, d.tier, args, None, d.rule, d.reason)
        return None

    # ================================================================================================ for the prompt / UI
    def catalog_schemas(self) -> List[dict]:
        """Schemas of every enabled MCP tool, for tool_search / auto-add (never for the prompt)."""
        self.reload()
        out = []
        for key, names in self._registered.items():
            if self.is_enabled(key):
                for n in names:
                    spec = self.registry.get(n)
                    if spec is not None:
                        out.append(spec.schema)
        return out

    def keywords(self) -> Dict[str, str]:
        """full tool name -> the server's hint words (so a search for 'browser' finds the browser tools)."""
        kw: Dict[str, str] = {}
        for key, names in self._registered.items():
            cfg = self.servers.get(key)
            if cfg:
                words = f"{cfg.name} {cfg.hint}"
                for n in names:
                    kw[n] = words
        return kw

    def core_names(self) -> set:
        """Tools a config marks as the server's main ones (shown first when its server matches a search)."""
        out = set()
        for key, names in self._registered.items():
            cfg = self.servers.get(key)
            if cfg and cfg.core:
                import fnmatch
                for n in names:
                    short = self._by_name[n][1]
                    if any(fnmatch.fnmatchcase(short, p) for p in cfg.core):
                        out.add(n)
        return out

    def more_hint(self, found_names: List[str], limit: int = 14) -> str:
        """After a tool_search: the other tools of the same server(s), so the model can ask for them by name."""
        lines = []
        shown = set(found_names)
        for key in dict.fromkeys(self._by_name[n][0] for n in found_names if n in self._by_name):
            rest = [self._by_name[n][1] for n in self._registered.get(key, []) if n not in shown]
            if rest:
                more = ", ".join(rest[:limit]) + (" ..." if len(rest) > limit else "")
                lines.append(f"More from {key} (call as {key}__<name>): {more}")
        return "\n".join(lines)

    def hint(self) -> str:
        """A few words for the tool_search description, e.g. 'browser, github'. Empty when nothing is connected."""
        self.reload()
        words = []
        for key, cfg in self.servers.items():
            if self.is_enabled(key) and self._registered.get(key):
                words.append(cfg.hint or cfg.name)
        return ", ".join(words)[:120]

    def status(self) -> dict:
        self.reload()
        rows = []
        for key, cfg in self.servers.items():
            client = self._clients.get(key)
            rows.append({"key": key, "name": cfg.name, "transport": cfg.transport, "enabled": self.is_enabled(key),
                         "running": bool(client and client.alive), "tools": len(self._registered.get(key, [])),
                         "tier": cfg.tier, "trusted": cfg.trusted, "error": self.state.errors.get(key, ""),
                         "server": (self.state.cache.get(key) or {}).get("server", {}).get("name", "")})
        return {"servers": rows, "problems": list(self.problems), "config": str(_cfg.config_path(self.root))}
