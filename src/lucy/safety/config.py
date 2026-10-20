"""Load `.core/permissions.yaml` (the user's rules) on top of safe built-in defaults.

The file is a constitution file: the agent can read it but never change it (see constitution.py).
Placeholders in paths: {LUCY_ROOT} {HOME} {SAFE_ROOT} {RUNTIME}.
"""
from __future__ import annotations

import copy
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    import yaml
except ImportError:                       # pragma: no cover - pyyaml is in requirements.txt
    yaml = None

MODES = ("ask_every_time", "ask_for_risky", "auto_in_allowed", "full_auto")
MODE_LABELS = {
    "ask_every_time": "Ask every time",
    "ask_for_risky": "Ask for risky",
    "auto_in_allowed": "Auto within allowed folders",
    "full_auto": "Full auto",
}
DEFAULT_MODE = "ask_for_risky"

# Whole-drive rules (P5). A drive letter not listed gets `other` (a new F:, a USB stick). Profiles:
#   off      not part of Lucy's reach (reading asks, changes ask)
#   ask      read freely; every change asks
#   storage  read and change freely; DELETING (or moving something out) always asks
#   full     read, change and delete freely; in "auto_in_allowed" mode commands that stay on the drive run too
DRIVE_PROFILES = ("off", "ask", "storage", "full")

# Built-in defaults. permissions.yaml overrides keys it sets; lists REPLACE the default list (so the user can remove an
# entry), except the hard-wired protections in policy.py / constitution.py, which no config can remove.
DEFAULTS: Dict[str, Any] = {
    "mode": DEFAULT_MODE,
    "approval_timeout_s": 300,
    "folders": {
        "read": ["{SAFE_ROOT}", "{LUCY_ROOT}"],
        "write": ["{LUCY_ROOT}", "{HOME}/Documents"],
    },
    "ask_paths": ["{LUCY_ROOT}/src", "{LUCY_ROOT}/ui", "{LUCY_ROOT}/.core"],
    "denied_paths": [
        "{LUCY_ROOT}/google_client_secret.json", "{LUCY_ROOT}/.env", "{HOME}/.ssh", "{HOME}/.aws", "{HOME}/.gnupg",
        "{HOME}/AppData/Roaming/Microsoft/Credentials", "{HOME}/AppData/Local/Microsoft/Credentials",
        "{HOME}/AppData/Local/Google/Chrome/User Data", "{HOME}/AppData/Local/Microsoft/Edge/User Data",
        "{HOME}/AppData/Roaming/Mozilla/Firefox/Profiles",
    ],
    "readonly_paths": ["C:/Windows", "C:/Program Files", "C:/Program Files (x86)", "C:/ProgramData/Microsoft"],
    "commands": {"deny": [], "ask": []},          # extra patterns on top of the built-in ones in policy.py
    "network": {
        "allow_hosts": ["*"],
        "deny_hosts": ["localhost", "*.localhost", "127.*", "0.0.0.0", "::1", "[::1]", "169.254.*", "metadata.google.internal"],
    },
    "tools": {"send_tts": "auto", "send_sticker": "auto"},
    "untrusted": {
        "tools": ["web_search", "web_extract", "get_doc_content", "list_drive_files"],
        "paths": ["{HOME}/Downloads"],
    },
    "backups": {"max_file_mb": 50, "max_total_mb": 2048},
    "drives": {},                                  # e.g. {"C": "ask", "D": "full", "other": "storage"}
}


@dataclass
class Config:
    mode: str = DEFAULT_MODE
    approval_timeout_s: float = 300.0
    read_folders: List[str] = field(default_factory=list)
    write_folders: List[str] = field(default_factory=list)
    ask_paths: List[str] = field(default_factory=list)
    denied_paths: List[str] = field(default_factory=list)
    readonly_paths: List[str] = field(default_factory=list)
    deny_commands: List[str] = field(default_factory=list)
    ask_commands: List[str] = field(default_factory=list)
    allow_hosts: List[str] = field(default_factory=list)
    deny_hosts: List[str] = field(default_factory=list)
    tool_overrides: Dict[str, str] = field(default_factory=dict)
    untrusted_tools: List[str] = field(default_factory=list)
    untrusted_paths: List[str] = field(default_factory=list)
    max_file_bytes: int = 50 * 1024 * 1024
    max_total_bytes: int = 2048 * 1024 * 1024
    drives: Dict[str, str] = field(default_factory=dict)   # "c" -> profile (lower-case letter)
    other_drive: str = "off"                                # profile of drives not listed
    problems: List[str] = field(default_factory=list)      # config mistakes (shown in the UI, never fatal)

    def drive_profile(self, normed: str) -> Optional[str]:
        """Profile of the drive a normalised path is on ("c:/x" -> drives["c"]); None for non-drive paths."""
        letter = drive_letter(normed)
        if letter is None:
            return None
        return self.drives.get(letter, self.other_drive)

    def drive_roots(self) -> List[str]:
        """Drive roots Lucy can reach right now ("c:" style): listed drives + present unlisted ones if `other` allows."""
        out = [f"{k}:" for k, v in sorted(self.drives.items()) if v != "off"]
        if self.other_drive != "off":
            out += [f"{k}:" for k in present_drives() if k not in self.drives]
        return out


def norm_path(p: str | os.PathLike) -> str:
    """Absolute, symlink-free, forward-slash, case-folded on Windows. Used for every path comparison."""
    s = os.path.realpath(os.path.expanduser(str(p)))
    s = os.path.normcase(s).replace("\\", "/")
    return s.rstrip("/") or "/"


def is_inside(path: str, folder: str) -> bool:
    """Both already normalised with norm_path."""
    return path == folder or path.startswith(folder.rstrip("/") + "/")


_DRIVE = re.compile(r"^([a-z]):(/|$)")


def drive_letter(normed: str) -> Optional[str]:
    m = _DRIVE.match(normed or "")
    return m.group(1) if m else None


_present_cache: Dict[str, Any] = {"t": 0.0, "v": []}


def present_drives() -> List[str]:
    """Drive letters that exist right now (Windows only; cached 20 s so a plugged-in drive shows up quickly)."""
    import time
    if os.name != "nt":
        return []
    now = time.monotonic()
    if now - _present_cache["t"] > 20:
        _present_cache["v"] = [c.lower() for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ" if os.path.exists(f"{c}:\\")]
        _present_cache["t"] = now
    return list(_present_cache["v"])


def _expand(s: str, ctx: Dict[str, str]) -> str:
    out = str(s)
    for k, v in ctx.items():
        out = out.replace("{" + k + "}", v)
    return out


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load(path: Path | None, *, root: Path, safe_root: Path, runtime: Path, home: Path | None = None) -> Config:
    raw: dict = {}
    problems: List[str] = []
    if path is not None and path.is_file():
        if yaml is None:
            problems.append("pyyaml is not installed: permissions.yaml ignored, built-in defaults used")
        else:
            try:
                raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
                if not isinstance(raw, dict):
                    problems.append("permissions.yaml must be a mapping; built-in defaults used")
                    raw = {}
            except Exception as e:                       # noqa: BLE001 - a broken config must not open everything up
                problems.append(f"permissions.yaml could not be read ({e}); built-in defaults used")
                raw = {}
    d = _merge(DEFAULTS, raw)
    ctx = {"LUCY_ROOT": Path(root).as_posix(), "SAFE_ROOT": Path(safe_root).as_posix(),
           "RUNTIME": Path(runtime).as_posix(), "HOME": Path(home or Path.home()).as_posix()}

    def paths(v) -> List[str]:
        return [norm_path(_expand(x, ctx)) for x in (v or []) if str(x).strip()]

    def pats(v, where) -> List[str]:
        good = []
        for x in v or []:
            try:
                re.compile(str(x))
                good.append(str(x))
            except re.error as e:
                problems.append(f"bad regex in {where}: {x!r} ({e})")
        return good

    mode = str(d.get("mode") or DEFAULT_MODE).strip()
    if mode not in MODES:
        problems.append(f"unknown mode {mode!r}; using {DEFAULT_MODE}")
        mode = DEFAULT_MODE
    overrides = {}
    for k, v in (d.get("tools") or {}).items():
        v = str(v).strip().lower()
        if v in ("auto", "ask", "deny"):
            overrides[str(k)] = v
        else:
            problems.append(f"tools.{k}: must be auto, ask or deny (got {v!r})")
    folders = d.get("folders") or {}
    net = d.get("network") or {}
    cmds = d.get("commands") or {}
    unt = d.get("untrusted") or {}
    bk = d.get("backups") or {}
    try:
        timeout = max(10.0, float(d.get("approval_timeout_s") or 300))
    except (TypeError, ValueError):
        timeout = 300.0
    write_folders, readonly, denied = paths(folders.get("write")), paths(d.get("readonly_paths")), paths(d.get("denied_paths"))
    for w in write_folders:                      # e.g. a junction that resolves to a parent of a write folder
        for kind, lst in (("readonly_paths", readonly), ("denied_paths", denied)):
            for r in lst:
                if is_inside(w, r):
                    problems.append(f"{kind} entry {r} covers the whole write folder {w} (check for junctions/typos)")
    drives: Dict[str, str] = {}
    other = "off"
    for k, v in (d.get("drives") or {}).items():
        prof = "off" if v is False or v is None else str(v).strip().lower()      # YAML reads a bare `off` as False
        if prof not in DRIVE_PROFILES:
            problems.append(f"drives.{k}: must be one of {', '.join(DRIVE_PROFILES)} (got {v!r}); using 'ask'")
            prof = "ask"
        key = str(k).strip().lower().rstrip("/\\").rstrip(":")
        if key == "other":
            other = prof
        elif re.fullmatch(r"[a-z]", key):
            drives[key] = prof
        else:
            problems.append(f"drives: {k!r} is not a drive letter (use C, D, ... or other)")
    return Config(
        drives=drives, other_drive=other,
        mode=mode, approval_timeout_s=timeout,
        read_folders=paths(folders.get("read")), write_folders=write_folders,
        ask_paths=paths(d.get("ask_paths")), denied_paths=denied, readonly_paths=readonly,
        deny_commands=pats(cmds.get("deny"), "commands.deny"), ask_commands=pats(cmds.get("ask"), "commands.ask"),
        allow_hosts=[str(h).lower() for h in net.get("allow_hosts") or []],
        deny_hosts=[str(h).lower() for h in net.get("deny_hosts") or []],
        tool_overrides=overrides,
        untrusted_tools=[str(t) for t in unt.get("tools") or []], untrusted_paths=paths(unt.get("paths")),
        max_file_bytes=int(float(bk.get("max_file_mb", 50)) * 1024 * 1024),
        max_total_bytes=int(float(bk.get("max_total_mb", 2048)) * 1024 * 1024),
        problems=problems)


_MODE_LINE = re.compile(r"(?m)^mode:\s*\S+.*$")


def set_mode_in_file(path: Path, mode: str) -> None:
    """Rewrite only the `mode:` line (keeps the user's comments and line endings)."""
    if mode not in MODES:
        raise ValueError(f"unknown mode {mode!r}")
    raw = path.read_bytes() if path.is_file() else b""
    crlf = b"\r\n" in raw
    text = raw.decode("utf-8").replace("\r\n", "\n")
    line = f"mode: {mode}"
    text = _MODE_LINE.sub(line, text, count=1) if _MODE_LINE.search(text) else f"{line}\n{text}"
    data = text.replace("\n", "\r\n") if crlf else text
    tmp = path.with_name(path.name + ".tmp-lucy")
    tmp.write_bytes(data.encode("utf-8"))
    os.replace(tmp, path)
