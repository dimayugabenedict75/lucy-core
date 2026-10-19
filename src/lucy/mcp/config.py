"""Reads `.core/mcp.json` (the server list) and `runtime/data/mcp_state.json` (UI toggles + the tool cache).

mcp.json uses the same shape as Claude Desktop / VS Code, so existing snippets paste straight in:

    {"mcpServers": {
        "playwright": {"command": "npx", "args": ["-y", "@playwright/mcp@latest", "--isolated"],
                       "tier": "network", "tool_tiers": {"browser_click": "network", "browser_type": "exec"}},
        "notes":      {"url": "https://example.com/mcp", "headers": {"Authorization": "Bearer ${NOTES_TOKEN}"}}
    }}

Lucy-only extras (other apps ignore them): enabled, tier, tool_tiers, read_only_hint, trusted, hint, core, idle_minutes,
timeout. `${VAR}` / `${VAR:-default}` in env, headers, args and url come from Lucy's environment, so tokens
never need to be written into the file. Nothing here raises on a bad file: problems are collected and shown in the UI.
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

VALID_TIERS = ("read", "write", "exec", "system", "network")
DEFAULT_TIER = "network"
DEFAULT_TIMEOUT = 60.0
DEFAULT_IDLE_MIN = 10.0
_NAME_OK = re.compile(r"[^a-z0-9]+")
_VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def server_key(name: str) -> str:
    """'My GitHub!' -> 'my_github' (used in tool names: my_github__create_issue)."""
    return _NAME_OK.sub("_", str(name).lower()).strip("_")[:24]


def expand(text: str, env: Optional[dict] = None) -> str:
    env = os.environ if env is None else env

    def sub(m):
        v = env.get(m.group(1))
        return v if v not in (None, "") else (m.group(2) or "")
    return _VAR.sub(sub, str(text))


@dataclass
class ServerConfig:
    name: str                                   # key as written in the file
    key: str                                    # sanitised, unique
    transport: str                              # "stdio" | "http"
    command: str = ""
    args: List[str] = field(default_factory=list)
    env: Dict[str, str] = field(default_factory=dict)
    cwd: str = ""
    url: str = ""
    headers: Dict[str, str] = field(default_factory=dict)
    enabled: bool = True
    tier: str = DEFAULT_TIER
    tool_tiers: Dict[str, str] = field(default_factory=dict)      # tool name or glob -> tier
    read_only_hint: bool = False                # a tool the server marks readOnlyHint becomes tier "read"
    trusted: bool = False                       # True: results are NOT framed as untrusted content
    hint: str = ""                              # a few words for the tool_search description ("browser, web pages")
    core: List[str] = field(default_factory=list)   # the server's main tools (names or globs), shown first by tool_search
    idle_minutes: float = DEFAULT_IDLE_MIN      # stop an idle server after this long (0 = keep running)
    timeout: float = DEFAULT_TIMEOUT            # seconds per tool call

    def fingerprint(self) -> str:
        """Changes when what the server IS changes (so the cached tool list is dropped). Secrets are not hashed."""
        d = {"t": self.transport, "c": self.command, "a": self.args, "u": self.url, "cwd": self.cwd,
             "e": sorted(self.env), "h": sorted(self.headers)}
        return hashlib.sha256(json.dumps(d, sort_keys=True).encode()).hexdigest()[:16]

    def tier_for(self, tool: str, read_only: bool = False) -> str:
        if tool in self.tool_tiers:
            return self.tool_tiers[tool]
        for pat, t in self.tool_tiers.items():
            if any(ch in pat for ch in "*?[") and fnmatch.fnmatchcase(tool, pat):
                return t
        if read_only and self.read_only_hint:
            return "read"
        return self.tier


def _as_list(v) -> List[str]:
    return [str(x) for x in v] if isinstance(v, list) else []


def _as_map(v) -> Dict[str, str]:
    return {str(k): str(x) for k, x in v.items()} if isinstance(v, dict) else {}


def parse(data: object) -> Tuple[Dict[str, ServerConfig], List[str]]:
    """(servers by key, problems). Bad entries are skipped and reported, never raised."""
    servers: Dict[str, ServerConfig] = {}
    problems: List[str] = []
    block = data.get("mcpServers") if isinstance(data, dict) else None
    if block is None and isinstance(data, dict):
        block = data.get("servers")                          # VS Code spelling
    if not isinstance(block, dict):
        return servers, ["mcp.json needs a top-level \"mcpServers\" object"]
    for name, raw in block.items():
        if not isinstance(raw, dict):
            problems.append(f"{name}: must be an object")
            continue
        key = server_key(name)
        if not key:
            problems.append(f"{name}: the name needs letters or digits")
            continue
        if key in servers:
            problems.append(f"{name}: clashes with another server called '{key}'")
            continue
        kind = str(raw.get("type") or "").lower()
        if raw.get("url"):
            if kind == "sse":
                problems.append(f"{name}: the old SSE transport is not supported; use the server's streamable HTTP url")
                continue
            transport = "http"
        elif raw.get("command"):
            transport = "stdio"
        else:
            problems.append(f"{name}: needs either \"command\" (a local program) or \"url\" (a remote server)")
            continue
        tier = str(raw.get("tier") or DEFAULT_TIER).lower()
        if tier not in VALID_TIERS:
            problems.append(f"{name}: tier must be one of {', '.join(VALID_TIERS)} (got {tier!r}); using {DEFAULT_TIER}")
            tier = DEFAULT_TIER
        tool_tiers = {}
        for t, v in _as_map(raw.get("tool_tiers")).items():
            if v.lower() in VALID_TIERS:
                tool_tiers[t] = v.lower()
            else:
                problems.append(f"{name}: tool_tiers[{t}] = {v!r} is not a tier; ignored")
        try:
            idle = float(raw.get("idle_minutes", DEFAULT_IDLE_MIN))
            timeout = float(raw.get("timeout", DEFAULT_TIMEOUT))
        except (TypeError, ValueError):
            problems.append(f"{name}: idle_minutes / timeout must be numbers; using defaults")
            idle, timeout = DEFAULT_IDLE_MIN, DEFAULT_TIMEOUT
        enabled = raw.get("enabled")
        if enabled is None:
            enabled = not bool(raw.get("disabled", False))   # Claude Desktop spelling
        servers[key] = ServerConfig(
            name=str(name), key=key, transport=transport,
            command=str(raw.get("command") or ""), args=_as_list(raw.get("args")), env=_as_map(raw.get("env")),
            cwd=str(raw.get("cwd") or ""), url=str(raw.get("url") or ""), headers=_as_map(raw.get("headers")),
            enabled=bool(enabled), tier=tier, tool_tiers=tool_tiers, read_only_hint=bool(raw.get("read_only_hint", False)),
            trusted=bool(raw.get("trusted", False)), hint=str(raw.get("hint") or "")[:60],
            core=_as_list(raw.get("core")),
            idle_minutes=max(0.0, idle), timeout=min(max(timeout, 1.0), 3600.0))
    return servers, problems


def config_path(root: Path) -> Path:
    return Path(root) / ".core" / "mcp.json"


def load(root: Path) -> Tuple[Dict[str, ServerConfig], List[str]]:
    p = config_path(root)
    if not p.is_file():
        return {}, []
    try:
        data = json.loads(p.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as e:
        return {}, [f"mcp.json could not be read: {e}"]
    return parse(data)


class State:
    """runtime/data/mcp_state.json: which servers the user switched on/off in the UI, last errors, cached tool lists.

    The agent could in theory edit this file, but all it can do is switch a server the USER already declared in the
    (locked) mcp.json on or off. It can never add a command."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.enabled: Dict[str, bool] = {}
        self.errors: Dict[str, str] = {}
        self.cache: Dict[str, dict] = {}            # key -> {"fp": fingerprint, "tools": [...], "server": {...}, "at": ts}
        self._load()

    def _load(self) -> None:
        try:
            d = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if isinstance(d, dict):
            self.enabled = {str(k): bool(v) for k, v in (d.get("enabled") or {}).items()}
            self.errors = {str(k): str(v) for k, v in (d.get("errors") or {}).items()}
            self.cache = {str(k): v for k, v in (d.get("cache") or {}).items() if isinstance(v, dict)}

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_text(json.dumps({"enabled": self.enabled, "errors": self.errors, "cache": self.cache}, indent=1),
                           encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError:
            pass                                    # the state is a convenience; never fail a chat over it
