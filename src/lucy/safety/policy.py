"""Decide what happens to one tool call: run it (auto), ask the user (ask), or refuse it (deny).

Pure function of (config, tool, tier, args, extra rule hits). No I/O except resolving paths, so it is easy to test.

Levels (the strictest one wins):
    AUTO      run it
    ASK       ask the user; "Always allow this session" may skip the question next time
    ASK_HARD  ask the user every time, in EVERY mode (risky commands, Lucy's own safety files, injection suspects)
    DENY      never run it (secrets, the OS folder, constitution files, wipe-the-disk commands)
"""
from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass, field
from fnmatch import fnmatch
from typing import Any, Callable, Iterable, List, Optional, Tuple
from urllib.parse import urlparse

from .config import Config, is_inside, norm_path

AUTO, ASK, ASK_HARD, DENY = 0, 1, 2, 3
LEVEL_NAMES = {AUTO: "auto", ASK: "ask", ASK_HARD: "ask", DENY: "deny"}

PATH_KEYS = ("file_path", "dir_path", "image_path", "path", "cwd", "folder", "directory", "source", "destination",
             "dest", "target")
SHELL_TOOLS = {"run_shell_command"}

# P5 PC tools: harmless actions get a lower tier than the tool's TIER (listing, reading, focusing a window...).
ACTION_TIERS = {
    "service": {"list": "read", "status": "read"},
    "process": {"list": "read"},
    "winget": {"search": "read", "list": "read", "poll": "read"},
    "scheduled_task": {"list": "read", "info": "read"},
    "registry": {"read": "read"},
    "system_info": {"disk": "read", "firewall": "read", "events": "read", "updates": "read", "": "read"},
    "window": {"list": "read"},                               # focus/min/max = system (control)
    "desktop": {"clipboard_get": "read"},                     # the rest control the PC
    "file_ops": {"poll": "read"},
    "user_tool": {"list": "read"},                            # P7: create/improve/disable = write (sandboxed tests)
    "core_change": {"status": "read", "ideas": "read", "start": "write", "discard": "write"},   # submit = exec
}
# Ask every time, in every mode (no "Always allow"): changes a small model should never make alone.
HARD_ACTIONS = {("registry", "write"), ("service", "stop"), ("service", "restart"), ("winget", "uninstall"),
                ("scheduled_task", "disable"), ("process", "kill")}
# Tools whose `dry_run: true` only describes what would happen (each handler honours it; checked by tests).
DRY_RUN_TOOLS = {"service", "process", "winget", "scheduled_task", "registry", "file_ops", "window", "desktop",
                 "click_on_screen"}
# file_ops: which path argument is read, changed or removed, per action. A removed path is also a changed one.
FILE_OPS = "file_ops"
_FILE_OPS_ROLES = {
    "copy": {"source": "read", "destination": "write"},
    "move": {"source": "delete", "destination": "write"},
    "delete": {"path": "delete", "source": "delete"},
    "mkdir": {"path": "write", "destination": "write"},
}
_DELETE_VERBS = re.compile(r"(?i)\b(remove-item|ri|del|erase|rd|rmdir|rm|move-item|mi|mv|move|clear-content)\b|\s/(mov|move|purge|mir)\b")
WEB_READ_TOOLS = {"web_search", "web_extract"}

# Never runs, whatever the mode (Windows + POSIX). Kept short and obvious; the user adds more in permissions.yaml.
BUILTIN_DENY = [
    r"\bformat(\.com)?\s+[a-z]:",                                   # format C:
    r"\bformat-volume\b", r"\bclear-disk\b", r"\binitialize-disk\b", r"\bdiskpart\b",
    r"\bbcdedit\b", r"\bvssadmin\b.*\bdelete\b", r"\bwbadmin\b.*\bdelete\b", r"\bcipher\b.*\s/w",
    r"\b(del|erase)\b[^|;&\n]*\s/s\b[^|;&\n]*\s\"?[a-z]:[\\/]?\*?(\*\.\*)?\"?(\s|$)",   # del /s C:\
    r"\b(rd|rmdir)\b[^|;&\n]*\s/s\b[^|;&\n]*\s\"?[a-z]:[\\/]?\"?(\s|$)",              # rd /s C:\
    r"\bremove-item\b[^|;\n]*\s['\"]?[a-z]:[\\/]?\*?['\"]?(\s[^|;\n]*)?-recurse",   # Remove-Item C:\ -Recurse
    r"\bremove-item\b[^|;\n]*-recurse[^|;\n]*\s['\"]?[a-z]:[\\/]?\*?['\"]?(\s|$)",
    r"\brm\s+-[a-z]*r[a-z]*\s+(-[a-z]+\s+)*(/|~|\$home|[a-z]:[\\/]?)\*?(\s|$)",      # rm -rf / ~ C:/
    r"\bmkfs(\.\w+)?\b", r"\bdd\b[^|;\n]*\bof=/dev/", r"\breg(\.exe)?\s+delete\s+hk(lm|ey_local_machine)\\?(\s|$)",
]
# Always asks, whatever the mode (even Full auto): things a small model should never decide alone.
BUILTIN_ASK = [
    r"\bshutdown\b", r"\b(stop|restart)-computer\b", r"\bnet\s+(user|localgroup)\b", r"\b(add|remove)-localgroupmember\b",
    r"\breg(\.exe)?\s+(add|delete|import|load|restore)\b", r"\b(set|new|remove)-itemproperty\b", r"\bhk(lm|cu|cr|u|cc):",
    r"\bhkey_", r"\bset-executionpolicy\b", r"\btakeown\b", r"\bicacls\b", r"\bschtasks\b", r"\bregister-scheduledtask\b",
    r"\bsc(\.exe)?\s+(create|delete|config|stop)\b", r"\b(stop|set|new)-service\b", r"\bnetsh\b", r"\bset-mppreference\b",
    r"\bbitsadmin\b", r"\bcertutil\b", r"(iwr|irm|invoke-webrequest|invoke-restmethod|curl|wget)\b[^\n]*\|\s*(iex|invoke-expression|sh|bash|python|pwsh|powershell|cmd)\b",
    r"\biex\b", r"\binvoke-expression\b", r"downloadstring", r"-enc(odedcommand)?\s", r"\bstart-process\b[^\n]*-verb\s+runas",
    r"\bremove-item\b[^\n]*-recurse", r"\brm\s+-[a-z]*r", r"\b(rd|rmdir)\b[^\n]*\s/s", r"\b(del|erase)\b[^\n]*\s/[sq]\b",
    r"\bgit\s+(push\b[^\n]*(--force|-f\b)|reset\s+--hard|clean\s+-[a-z]*f)", r"\bwinget\s+(uninstall|remove)\b",
    r"\bpip\s+uninstall\b", r"\bsetx\b", r"\[environment\]::setenvironmentvariable",
]
# Text that points at Lucy's own safety layer. A shell command that mentions it always asks (the file tools refuse).
SAFETY_MARKERS = ["permissions.yaml", "constitution.lock", "lucy/safety", "lucy\\safety", "lucy.safety", "file_guard",
                  "audit.jsonl", "safety_state.json", "tools/registry.py", "tools\\registry.py", "tools/_common.py",
                  "tools\\_common.py", "/api/safety", "runtime/backups", "runtime\\backups", "dev_api_key",
                  "localhost:8090", "127.0.0.1:8090", ":8090/api"]

_WIN_ABS = re.compile(r"(?i)(?<![\w])[a-z]:[\\/][^\s\"'|;&<>`]*")
_POSIX_ABS = re.compile(r"(?<![\w:./\\-])/(?:[^\s/\"'|;&<>`]+/)+[^\s\"'|;&<>`]*")
_HOME_REF = re.compile(r"(?i)(~[\\/]|\$home\b|\$env:userprofile|%userprofile%)([^\s\"'|;&<>`]*)")
_URL = re.compile(r"(?i)\bhttps?://[^\s\"'<>`)\]]+")


@dataclass
class Decision:
    level: int = AUTO
    reasons: List[str] = field(default_factory=list)
    tier: str = "read"                 # effective tier of this call
    rule: str = "policy"               # what decided a deny / hard ask (for the audit log)
    write_paths: List[str] = field(default_factory=list)

    @property
    def action(self) -> str:
        return LEVEL_NAMES[self.level]

    @property
    def can_always_allow(self) -> bool:
        return self.level == ASK

    def raise_to(self, level: int, reason: str, rule: str = "policy") -> None:
        if level > self.level:
            self.level, self.rule = level, rule
        if reason and reason not in self.reasons:
            self.reasons.append(reason)

    @property
    def reason(self) -> str:
        return "; ".join(self.reasons)


def effective_tier(name: str, tier: str, args: dict) -> str:
    """Some actions of a tool are harmless (polling a job, reading the cursor position)."""
    action = str((args or {}).get("action") or "").strip().lower()
    if name in DRY_RUN_TOOLS and (args or {}).get("dry_run") is True:
        return "read"                   # only describes what it would do
    if name in ACTION_TIERS and action in ACTION_TIERS[name]:
        return ACTION_TIERS[name][action]
    if name in SHELL_TOOLS and action in ("poll", "list", "kill"):
        return "read"                   # kill only ever touches Lucy's own background jobs
    if name == "cua_cursor" and action in ("", "get"):
        return "read"
    if name == "restore_from_backup" and action in ("", "list"):
        return "read"
    return tier


def _strings(v: Any) -> Iterable[str]:
    if isinstance(v, str):
        yield v
    elif isinstance(v, dict):
        for x in v.values():
            yield from _strings(x)
    elif isinstance(v, (list, tuple)):
        for x in v:
            yield from _strings(x)


def command_paths(command: str, home: str) -> List[str]:
    """Absolute paths a shell command mentions (best effort; used to keep 'auto' inside the allowed folders)."""
    out = [m.group(0) for m in _WIN_ABS.finditer(command)] + [m.group(0) for m in _POSIX_ABS.finditer(command)]
    out += [home + "/" + m.group(2).lstrip("\\/") for m in _HOME_REF.finditer(command)]
    return [norm_path(p.rstrip(".,)")) for p in out]


def hosts_of(args: dict) -> List[str]:
    hosts = []
    for s in _strings(args):
        for u in _URL.findall(s):
            h = (urlparse(u).hostname or "").lower()
            if h:
                hosts.append(h)
    return hosts


def _host_match(host: str, pats: List[str]) -> bool:
    return any(p == "*" or fnmatch(host, p) for p in pats)


def _private(host: str) -> bool:
    try:
        ip = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return False
    return ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved


class Policy:
    def __init__(self, cfg: Config, *, protected: Callable[[str], bool], resolve: Callable[[str], Optional[str]],
                 home: str, mode: Optional[str] = None, default_cwd: Optional[str] = None):
        self.cfg = cfg
        self.mode = mode or cfg.mode
        self.protected = protected               # norm path -> is it a constitution file / Lucy safety state?
        self.resolve = resolve                   # what the model typed -> the path the tool will really use (norm)
        self.home = norm_path(home)
        self.default_cwd = norm_path(default_cwd) if default_cwd else self.home     # where the shell runs without a cwd
        self._deny = [re.compile(p, re.I) for p in BUILTIN_DENY + cfg.deny_commands]
        self._ask = [re.compile(p, re.I) for p in BUILTIN_ASK + cfg.ask_commands]

    # --------------------------------------------------------------------------------------------- helpers
    def _in(self, p: str, folders: List[str]) -> bool:
        return any(is_inside(p, f) for f in folders)

    def _readable(self, p: str) -> bool:
        return self._in(p, self.cfg.read_folders) or self.cfg.drive_profile(p) in ("ask", "storage", "full")

    def _writable(self, p: str) -> bool:
        return self._in(p, self.cfg.write_folders) or self.cfg.drive_profile(p) in ("storage", "full")

    def _cmd_ok(self, p: str) -> bool:
        """auto_in_allowed: a command may run alone where it stays inside these."""
        return self._in(p, self.cfg.write_folders) or self.cfg.drive_profile(p) == "full"

    def _paths(self, args: dict) -> List[Tuple[str, str]]:
        out = []
        for k in PATH_KEYS:
            v = (args or {}).get(k)
            if isinstance(v, str) and v.strip():
                r = self.resolve(v)
                if r:
                    out.append((k, r))
        return out

    # --------------------------------------------------------------------------------------------- decide
    def decide(self, name: str, tier: str, args: dict, *, session_allowed: bool = False) -> Decision:
        args = args or {}
        cfg, mode = self.cfg, self.mode
        eff = effective_tier(name, tier, args)
        d = Decision(tier=eff)
        override = cfg.tool_overrides.get(name)
        if override == "deny":
            d.raise_to(DENY, f"{name} is switched off in permissions.yaml", "tool_override")
            return d

        paths = self._paths(args)
        writes = eff in ("write", "exec", "system") or (name == "restore_from_backup" and eff != "read")
        roles = _FILE_OPS_ROLES.get(str(args.get("action") or "").strip().lower(), {}) if name == FILE_OPS else {}
        for key, p in paths:
            if self._in(p, cfg.denied_paths):
                d.raise_to(DENY, f"{p} is a blocked location (secrets / private data)", "denied_path")
            role = roles.get(key, "write") if name == FILE_OPS else "write"
            if role == "read" and writes:
                if not self._readable(p):
                    d.raise_to(ASK, f"reads outside the allowed folders ({p})", "mode")
                continue
            if writes and role == "delete" and not self.protected(p):
                prof = cfg.drive_profile(p)
                if prof == "storage":
                    d.raise_to(ASK_HARD, f"deletes or moves something out of {p[:2].upper()} (that drive always asks "
                                         f"before removing anything)", "delete_storage")
                elif prof in ("ask", "off") and not self._in(p, cfg.write_folders):
                    d.raise_to(ASK, f"removes {p}", "mode")
            if writes and key != "cwd":
                d.write_paths.append(p)
                if self.protected(p):
                    d.raise_to(DENY, f"{p} is one of Lucy's safety files; only the user can change it", "constitution")
                elif self._in(p, cfg.readonly_paths) and name == FILE_OPS:
                    d.raise_to(ASK_HARD, f"{p} is a protected system location: changing it needs administrator rights "
                                         "(Windows will show a UAC prompt)", "admin_path")
                elif self._in(p, cfg.readonly_paths):
                    d.raise_to(DENY, f"{p} is read-only for Lucy (system folder)", "readonly_path")
                elif self._in(p, cfg.ask_paths):
                    d.raise_to(ASK, f"changes a folder that always asks ({p})", "ask_path")
            elif writes and key == "cwd" and (self.protected(p) or self._in(p, cfg.readonly_paths)):
                d.raise_to(ASK_HARD, f"runs inside a protected folder ({p})", "readonly_path")

        cmd = str(args.get("command") or "") if name in SHELL_TOOLS and eff == "exec" else ""
        if cmd:
            low = cmd.lower()
            for rx in self._deny:
                if rx.search(cmd):
                    d.raise_to(DENY, "this command could destroy data or the system", "command_deny")
                    break
            if any(m in low for m in SAFETY_MARKERS):
                d.raise_to(ASK_HARD, "the command touches Lucy's safety layer", "constitution")
            for rx in self._ask:
                if rx.search(cmd):
                    d.raise_to(ASK_HARD, "risky command (system settings, accounts, downloads-and-run or bulk delete)",
                               "command_ask")
                    break
            deletes = bool(_DELETE_VERBS.search(cmd))
            for p in command_paths(cmd, self.home):
                if deletes and cfg.drive_profile(p) == "storage":
                    d.raise_to(ASK_HARD, f"the command may delete or move files on {p[:2].upper()} (that drive always "
                                         "asks before removing anything)", "delete_storage")
                if self._in(p, cfg.denied_paths):
                    d.raise_to(ASK_HARD, f"the command mentions a blocked location ({p})", "denied_path")
                elif self.protected(p):
                    d.raise_to(ASK_HARD, "the command touches Lucy's safety layer", "constitution")

        hosts = hosts_of(args) if eff == "network" or name in WEB_READ_TOOLS else []
        for h in hosts:
            if _host_match(h, cfg.deny_hosts):
                d.raise_to(DENY, f"{h} is a local/internal address Lucy may not reach", "deny_host")
            elif _private(h):
                d.raise_to(ASK_HARD, f"{h} is a private network address", "private_host")

        action = str(args.get("action") or "").strip().lower()
        if (name, action) in HARD_ACTIONS and eff != "read":
            d.raise_to(ASK_HARD, f"{name} {action} always asks (system change)", "hard_action")
        if override == "ask":
            d.raise_to(ASK, f"{name} is set to 'ask' in permissions.yaml", "tool_override")
        if d.level >= ASK_HARD:
            return d

        base, why = self._mode_level(name, eff, args, paths, cmd, hosts)
        if override == "auto":
            base, why = AUTO, ""
        d.raise_to(base, why, "mode")
        if d.level == ASK and session_allowed:
            d.level, d.rule = AUTO, "session_rule"
        return d

    def _mode_level(self, name, eff, args, paths, cmd, hosts) -> Tuple[int, str]:
        cfg, mode = self.cfg, self.mode
        if mode == "full_auto":
            return AUTO, ""
        if eff == "read":
            outside = [p for _k, p in paths if not self._readable(p)]
            return (ASK, f"reads outside the allowed folders ({outside[0]})") if outside else (AUTO, "")
        if eff == "write":
            if mode == "ask_every_time":
                return ASK, "changes something (mode: Ask every time)"
            roles = _FILE_OPS_ROLES.get(str(args.get("action") or "").strip().lower(), {}) if name == FILE_OPS else {}
            targets = [p for k, p in paths if k != "cwd" and roles.get(k) != "read"]
            outside = [p for p in targets if not self._writable(p)]
            if not outside:
                return AUTO, ""
            if cfg.drive_profile(outside[0]) == "ask":
                return ASK, f"changes something on {outside[0][:2].upper()} (that drive asks before every change)"
            return ASK, f"writes outside the allowed folders ({outside[0]})"
        if eff == "exec":
            if mode != "auto_in_allowed":
                return ASK, "runs a program or command"
            cwd = next((p for k, p in paths if k == "cwd"), None) or self.default_cwd
            mentioned = command_paths(cmd, self.home) if cmd else []
            if cwd and self._cmd_ok(cwd) and all(self._cmd_ok(p) for p in mentioned):
                return AUTO, ""
            return ASK, "runs a command that may reach outside the allowed folders"
        if eff == "system":
            return ASK, "controls the PC (mouse, keyboard, system settings)"
        if eff == "network":
            if mode == "ask_every_time":
                return ASK, "uses the internet (mode: Ask every time)"
            if name in WEB_READ_TOOLS and not hosts:
                return (AUTO, "") if _host_match("search", cfg.allow_hosts) or "*" in cfg.allow_hosts else (ASK, "web search")
            bad = [h for h in hosts if not _host_match(h, cfg.allow_hosts)]
            if bad:
                return ASK, f"contacts {bad[0]} (not in the network allowlist)"
            return AUTO, ""
        return ASK, f"unknown tier {eff!r}"
