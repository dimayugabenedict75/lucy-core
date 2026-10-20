"""The FIXED menu of PC operations (P5). Standard library only: the admin broker runs it ELEVATED with `python -I -S`
(no site-packages, no environment tricks), so nothing here may import a third-party package.

Every op: strict argument checks, a plain-text result a small model can read, `dry_run` (say what would happen, do
nothing), and NO free-form command text: values reach PowerShell through environment variables (never pasted into the
script) or reach a program as separate argv items.

`admin` = the op needs administrator rights and runs in the broker; everything else runs in Lucy's own process.
Constitution file (hash-locked): the agent can never change what the elevated helper is able to do.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from typing import Any, Callable, Dict, List, Optional, Tuple

IS_WIN = os.name == "nt"
Runner = Callable[[List[str], float, Dict[str, str]], Tuple[int, str, str]]


class OpError(ValueError):
    """Bad arguments or a refused target. The text goes back to the model as-is."""


# =============================================================================================== running programs
def default_runner(argv: List[str], timeout: float, env: Dict[str, str]) -> Tuple[int, str, str]:
    flags = 0x08000000 if IS_WIN else 0                       # CREATE_NO_WINDOW: no console flashes on screen
    try:
        r = subprocess.run(argv, capture_output=True, timeout=timeout, env={**os.environ, **env},
                           stdin=subprocess.DEVNULL, creationflags=flags)
    except FileNotFoundError:
        return 127, "", f"{argv[0]} is not installed or not on PATH"
    except subprocess.TimeoutExpired:
        return 124, "", f"timed out after {int(timeout)} s"
    dec = lambda b: (b or b"").decode("utf-8", "replace")
    return r.returncode, dec(r.stdout), dec(r.stderr)


def ps(script: str, env: Dict[str, str], ctx: "Ctx", timeout: float = 60) -> Tuple[int, str, str]:
    """PowerShell with the values in $env:LUCY_* (never formatted into the script text)."""
    pre = "$ErrorActionPreference='Stop'; $ProgressPreference='SilentlyContinue'; [Console]::OutputEncoding=[Text.Encoding]::UTF8; "
    return ctx.runner(["powershell", "-NoProfile", "-NonInteractive", "-Command", pre + script], timeout,
                      {k: str(v) for k, v in env.items()})


def _clean(text: str, limit: int = 4000) -> str:
    """Strip progress-bar noise (\\r rewrites, spinners, control chars); keep the tail if long."""
    lines = []
    for raw in (text or "").replace("\r\n", "\n").split("\n"):
        line = raw.split("\r")[-1]
        line = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]|[\u2580-\u259f]", "", line).rstrip()
        if line.strip() and not re.fullmatch(r"[\s\-\\|/]+", line):
            lines.append(line)
    out = "\n".join(lines)
    return out if len(out) <= limit else "...\n" + out[-limit:]


def _json_rows(text: str) -> List[dict]:
    text = (text or "").strip()
    if not text:
        return []
    try:
        v = json.loads(text)
    except ValueError:
        raise OpError("unexpected output: " + _clean(text, 400))
    return v if isinstance(v, list) else [v]


def _table(rows: List[dict], cols: List[str], empty: str = "(nothing found)") -> str:
    if not rows:
        return empty
    return "\n".join(" | ".join(str(r.get(c, "") if r.get(c) is not None else "") for c in cols) for r in rows)


def _fail(rc: int, out: str, err: str, what: str) -> str:
    msg = _clean(err or out, 800) or f"exit code {rc}"
    if re.search(r"(?i)access is denied|requires elevation|administrator|0x80070005|PermissionDenied", msg):
        msg += " (needs administrator rights)"
    return f"Failed: {what}: {msg}"


# =============================================================================================== argument checks
_NAME = re.compile(r"^[\w.\-$@+ ()]{1,120}$")
_WINGET = re.compile(r"^[\w.\-+]{1,120}$")
_REG_ROOTS = {"HKLM": "HKEY_LOCAL_MACHINE", "HKEY_LOCAL_MACHINE": "HKEY_LOCAL_MACHINE", "HKCU": "HKEY_CURRENT_USER",
              "HKEY_CURRENT_USER": "HKEY_CURRENT_USER", "HKCR": "HKEY_CLASSES_ROOT", "HKEY_CLASSES_ROOT": "HKEY_CLASSES_ROOT",
              "HKU": "HKEY_USERS", "HKEY_USERS": "HKEY_USERS", "HKCC": "HKEY_CURRENT_CONFIG",
              "HKEY_CURRENT_CONFIG": "HKEY_CURRENT_CONFIG"}
_REG_TYPES = ("REG_SZ", "REG_EXPAND_SZ", "REG_DWORD", "REG_QWORD", "REG_MULTI_SZ")

# Never touched, whoever asks (the broker enforces these again on its side).
CRITICAL_PROCESSES = {"system", "registry", "smss", "csrss", "wininit", "winlogon", "services", "lsass", "lsaiso",
                      "fontdrvhost", "memory compression", "secure system", "msmpeng", "sgrmbroker"}
CRITICAL_SERVICES = {"rpcss", "rpceptmapper", "dcomlaunch", "lsm", "samss", "windefend", "mpssvc", "bfe", "eventlog",
                     "wdnissvc", "securityhealthservice", "sense", "brokerinfrastructure", "systemeventsbroker",
                     "power", "plugplay", "profsvc", "schedule", "winmgmt"}
REG_DENY = [r"^HKEY_LOCAL_MACHINE\\(SAM|SECURITY)(\\|$)", r"\\Control\\Lsa(\\|$)",
            r"\\Image File Execution Options(\\|$)", r"\\Windows NT\\CurrentVersion\\Winlogon(\\|$)",
            r"\\Policies\\Microsoft\\Windows Defender(\\|$)", r"\\Windows Defender(\\|$)",
            r"\\CurrentControlSet\\Services\\(WinDefend|MpsSvc|BFE|EventLog|SamSs)(\\|$)"]


def need(args: dict, key: str, rx: Optional[re.Pattern] = None, what: str = "") -> str:
    v = str(args.get(key) or "").strip()
    if not v:
        raise OpError(f"'{key}' is required" + (f" ({what})" if what else ""))
    if rx is not None and not rx.match(v):
        raise OpError(f"'{key}' has characters that are not allowed: {v[:80]!r}")
    return v


def opt(args: dict, key: str, rx: Optional[re.Pattern] = None) -> str:
    v = str(args.get(key) or "").strip()
    if v and rx is not None and not rx.match(v):
        raise OpError(f"'{key}' has characters that are not allowed: {v[:80]!r}")
    return v


def reg_key(raw: str) -> Tuple[str, str]:
    """'HKLM\\Software\\X' / 'HKLM:/Software/X' -> ('HKEY_LOCAL_MACHINE', 'Software\\X')."""
    s = str(raw or "").strip().replace("/", "\\")
    s = re.sub(r"^(\w+):\\?", r"\1\\", s)
    head, _, rest = s.partition("\\")
    root = _REG_ROOTS.get(head.upper())
    if not root:
        raise OpError("key must start with HKLM, HKCU, HKCR, HKU or HKCC, e.g. HKCU\\Software\\MyApp")
    if "\x00" in rest or len(rest) > 250:
        raise OpError("key is not valid")
    return root, rest.strip("\\")


# =============================================================================================== protected paths
def _norm(p: str) -> str:
    return os.path.normcase(os.path.realpath(str(p))).replace("\\", "/").rstrip("/")


def _inside(p: str, folder: str) -> bool:
    return p == folder or p.startswith(folder.rstrip("/") + "/")


_SYS_DENY = ["{SYS}/system32", "{SYS}/syswow64", "{SYS}/winsxs", "{SYS}/boot", "{SYS}/servicing", "{SYS}/security",
             "{SD}/boot", "{SD}/bootmgr", "{SD}/bootnxt", "{SD}/recovery", "{SD}/system volume information",
             "{SD}/$recycle.bin", "{SD}/pagefile.sys", "{SD}/hiberfil.sys", "{SD}/swapfile.sys", "{SD}/efi"]


def denied_paths_from_yaml(root: str) -> List[str]:
    """`denied_paths` of permissions.yaml without pyyaml (the broker runs with -S): the simple list form only."""
    out, inside = [], False
    try:
        lines = open(os.path.join(root, ".core", "permissions.yaml"), encoding="utf-8").read().splitlines()
    except OSError:
        return out
    home = os.path.expanduser("~").replace("\\", "/")
    for line in lines:
        if re.match(r"^denied_paths:\s*(#.*)?$", line):
            inside = True
            continue
        if inside:
            m = re.match(r"^\s+-\s+[\"']?([^\"'#]+?)[\"']?\s*(#.*)?$", line)
            if m:
                out.append(m.group(1).replace("{HOME}", home).replace("{LUCY_ROOT}", root.replace("\\", "/")))
            elif line.strip() and not line.lstrip().startswith("#"):
                break
    return out


class Guard:
    """Targets nobody may change through the PC tools: Lucy Core itself, drive roots, the core of Windows, secrets."""

    def __init__(self, lucy_root: Optional[str], extra_denied: Optional[List[str]] = None):
        """lucy_root=None: Lucy's own process (the safety policy already guards Lucy Core and the denied paths)."""
        sysroot = os.environ.get("SystemRoot", "C:/Windows")
        sd = os.environ.get("SystemDrive", "C:") + "/"
        self.lucy_root = _norm(lucy_root) if lucy_root else None
        if lucy_root is None and extra_denied is None:
            extra_denied = []
        self.deny = [_norm(x.replace("{SYS}", sysroot).replace("{SD}", sd).replace("//", "/")) for x in _SYS_DENY]
        self.deny += [_norm(x) for x in (extra_denied if extra_denied is not None else denied_paths_from_yaml(lucy_root))]

    def check(self, raw: str) -> str:
        if not raw or not str(raw).strip():
            raise OpError("a path is required")
        bare = str(raw).strip().strip("\"'").replace("\\", "/").rstrip("/")
        # A bare drive or root, tested BEFORE realpath (on Windows realpath("C:") expands to the current dir on C:).
        if re.fullmatch(r"(?i)[a-z]:", bare) or bare in ("", "/"):
            raise OpError(f"{raw} is a whole drive; pick a folder or file inside it")
        p = _norm(str(raw).strip().strip("\"'"))
        if re.fullmatch(r"[a-z]:", p) or p in ("", "/"):
            raise OpError(f"{raw} is a whole drive; pick a folder or file inside it")
        if self.lucy_root and _inside(p, self.lucy_root):
            raise OpError(f"{raw} is inside Lucy Core; the PC tools never change Lucy's own files")
        for d in self.deny:
            if _inside(p, d):
                raise OpError(f"{raw} is a protected Windows/secret location; refused")
        return p


# =============================================================================================== context + jobs
class Ctx:
    def __init__(self, runner: Runner = default_runner, guard: Optional[Guard] = None, dry_run: bool = False,
                 elevated: bool = False, protect_pids: Tuple[int, ...] = (), trash: Optional[Callable[[str], str]] = None):
        self.runner, self.guard, self.dry_run, self.elevated = runner, guard, dry_run, elevated
        self.protect_pids = set(int(p) for p in protect_pids if p) | {os.getpid()}
        self.trash = trash


class Jobs:
    """Long ops (installs, big copies) keep running in a thread; the caller polls with the job id."""

    def __init__(self):
        self._jobs: Dict[str, dict] = {}
        self._lock = threading.Lock()

    def start(self, fn: Callable[[], str], what: str) -> str:
        jid = uuid.uuid4().hex[:6]
        job = {"what": what, "done": threading.Event(), "result": None, "t0": time.monotonic()}

        def run():
            try:
                job["result"] = fn()
            except OpError as e:
                job["result"] = f"Failed: {e}"
            except Exception as e:                                # noqa: BLE001 - reported to the model, never crashes
                job["result"] = f"Failed: {type(e).__name__}: {e}"
            finally:
                job["done"].set()
        with self._lock:
            self._jobs = {k: v for k, v in self._jobs.items()
                          if not (v["done"].is_set() and time.monotonic() - v["t0"] > 3600)}
            self._jobs[jid] = job
        threading.Thread(target=run, daemon=True, name=f"lucy-op-{jid}").start()
        return jid

    def wait(self, jid: str, seconds: float) -> dict:
        job = self._jobs.get(jid)
        if job is None:
            return {"done": True, "result": f"Failed: no job {jid!r} (finished jobs are kept for 1 hour)"}
        job["done"].wait(max(0.0, seconds))
        if job["done"].is_set():
            return {"done": True, "result": job["result"]}
        return {"done": False, "job": jid, "what": job["what"], "running_s": int(time.monotonic() - job["t0"])}


# =============================================================================================== the ops
def _service_name(args) -> str:
    return need(args, "name", _NAME, "service name, e.g. Spooler")


def op_service_list(a, ctx):
    rc, out, err = ps("$f=$env:LUCY_F; Get-Service | Where-Object { -not $f -or $_.Name -like \"*$f*\" -or "
                      "$_.DisplayName -like \"*$f*\" } | Select-Object -First 60 Name,DisplayName,"
                      "@{n='Status';e={\"$($_.Status)\"}},@{n='Start';e={\"$($_.StartType)\"}} | ConvertTo-Json -Compress",
                      {"LUCY_F": opt(a, "filter", _NAME)}, ctx)
    if rc:
        return _fail(rc, out, err, "list services")
    return "name | display name | status | start type\n" + _table(_json_rows(out), ["Name", "DisplayName", "Status", "Start"])


def op_service_status(a, ctx):
    n = _service_name(a)
    rc, out, err = ps("Get-Service -Name $env:LUCY_N | Select-Object Name,DisplayName,@{n='Status';e={\"$($_.Status)\"}},"
                      "@{n='Start';e={\"$($_.StartType)\"}} | ConvertTo-Json -Compress", {"LUCY_N": n}, ctx)
    if rc:
        return _fail(rc, out, err, f"service {n}")
    return _table(_json_rows(out), ["Name", "DisplayName", "Status", "Start"])


def _service_change(verb: str):
    cmd = {"start": "Start-Service", "stop": "Stop-Service", "restart": "Restart-Service"}[verb]

    def op(a, ctx):
        n = _service_name(a)
        if verb != "start" and n.lower() in CRITICAL_SERVICES:
            raise OpError(f"{n} is a core Windows/security service; Lucy never stops it")
        if ctx.dry_run:
            return f"DRY RUN: would {verb} the service {n}. Nothing was changed."
        rc, out, err = ps(cmd + " -Name $env:LUCY_N; (Get-Service -Name $env:LUCY_N).Status.ToString()", {"LUCY_N": n},
                          ctx, timeout=90)
        if rc:
            return _fail(rc, out, err, f"{verb} {n}")
        return f"Done: {verb} {n}. Status now: {_clean(out, 200) or 'unknown'}"
    return op


def op_process_list(a, ctx):
    rc, out, err = ps("$f=$env:LUCY_F; Get-Process | Where-Object { -not $f -or $_.ProcessName -like \"*$f*\" -or "
                      "$_.MainWindowTitle -like \"*$f*\" } | Sort-Object WS -Descending | Select-Object -First 40 Id,"
                      "ProcessName,@{n='MB';e={[int]($_.WS/1MB)}},@{n='CPU_s';e={[int]$_.CPU}},MainWindowTitle | "
                      "ConvertTo-Json -Compress", {"LUCY_F": opt(a, "filter", _NAME)}, ctx)
    if rc:
        return _fail(rc, out, err, "list processes")
    return "pid | name | memory MB | cpu s | window\n" + _table(_json_rows(out), ["Id", "ProcessName", "MB", "CPU_s",
                                                                              "MainWindowTitle"])


def op_process_kill(a, ctx):
    pid = a.get("pid")
    name = opt(a, "name", _NAME)
    if pid in (None, "") and not name:
        raise OpError("give 'pid' (best, from process list) or 'name'")
    if pid not in (None, ""):
        try:
            pid = int(pid)
        except (TypeError, ValueError):
            raise OpError("'pid' must be a number")
        if pid in (0, 4) or pid in ctx.protect_pids:
            raise OpError(f"process {pid} is Lucy herself or a core Windows process; refused")
        argv, what = ["taskkill", "/PID", str(pid), "/F"], f"process {pid}"
    else:
        base = re.sub(r"(?i)\.exe$", "", name)
        if base.lower() in CRITICAL_PROCESSES:
            raise OpError(f"{name} is a core Windows process; killing it would crash the PC")
        if base.lower() in ("python", "pythonw", "py", "llama-server"):
            raise OpError(f"killing every '{base}' would stop Lucy herself; use 'pid' from process list instead")
        argv, what = ["taskkill", "/IM", base + ".exe", "/F"], f"every {base}.exe"
    if ctx.dry_run:
        return f"DRY RUN: would force-close {what}. Nothing was changed."
    rc, out, err = ctx.runner(argv, 30, {})
    if rc:
        return _fail(rc, out, err, f"close {what}")
    return f"Done: closed {what}. {_clean(out, 300)}"


def _task_env(a) -> Dict[str, str]:
    n = need(a, "name", _NAME, "task name from scheduled_task list")
    p = opt(a, "path", re.compile(r"^\\[\w.\-\\ ]{0,200}$"))
    return {"LUCY_N": n, "LUCY_P": p or ""}


_TASK_GET = "$t = if ($env:LUCY_P) { Get-ScheduledTask -TaskName $env:LUCY_N -TaskPath $env:LUCY_P } else { Get-ScheduledTask -TaskName $env:LUCY_N }; "


def op_task_list(a, ctx):
    rc, out, err = ps("$f=$env:LUCY_F; Get-ScheduledTask | Where-Object { if ($f) { $_.TaskName -like \"*$f*\" } else "
                      "{ $_.TaskPath -notlike '\\Microsoft\\*' } } | Select-Object -First 60 TaskName,TaskPath,"
                      "@{n='State';e={\"$($_.State)\"}} | ConvertTo-Json -Compress", {"LUCY_F": opt(a, "filter", _NAME)}, ctx)
    if rc:
        return _fail(rc, out, err, "list scheduled tasks")
    return ("name | folder | state  (Windows' own tasks under \\Microsoft\\ are hidden unless you give a filter)\n"
            + _table(_json_rows(out), ["TaskName", "TaskPath", "State"]))


def op_task_info(a, ctx):
    rc, out, err = ps(_TASK_GET + "$i = $t | Get-ScheduledTaskInfo; [pscustomobject]@{Name=$t.TaskName; Folder=$t.TaskPath; "
                      "State=\"$($t.State)\"; LastRun=\"$($i.LastRunTime)\"; LastResult=$i.LastTaskResult; "
                      "NextRun=\"$($i.NextRunTime)\"; Runs=(($t.Actions | ForEach-Object { \"$($_.Execute) $($_.Arguments)\" }) "
                      "-join '; ')} | ConvertTo-Json -Compress", _task_env(a), ctx)
    if rc:
        return _fail(rc, out, err, "task info")
    r = (_json_rows(out) or [{}])[0]
    return "\n".join(f"{k}: {v}" for k, v in r.items())


def _task_change(verb: str):
    cmd = {"run": "Start-ScheduledTask", "enable": "Enable-ScheduledTask", "disable": "Disable-ScheduledTask"}[verb]

    def op(a, ctx):
        env = _task_env(a)
        if ctx.dry_run:
            return f"DRY RUN: would {verb} the scheduled task {env['LUCY_N']}. Nothing was changed."
        rc, out, err = ps(_TASK_GET + f"$t | {cmd} | Out-Null; (Get-ScheduledTask -TaskName $t.TaskName -TaskPath "
                          "$t.TaskPath).State.ToString()", env, ctx)
        if rc:
            return _fail(rc, out, err, f"{verb} task {env['LUCY_N']}")
        return f"Done: {verb} {env['LUCY_N']}. State now: {_clean(out, 100)}"
    return op


_WG_COMMON = ["--accept-source-agreements", "--disable-interactivity"]


def op_winget_search(a, ctx):
    q = need(a, "query", re.compile(r"^[\w.\-+ ]{1,80}$"), "app name, e.g. 7zip")
    rc, out, err = ctx.runner(["winget", "search", "--query", q] + _WG_COMMON, 90, {})
    text = _clean(out or err, 3000)
    if rc and not text:
        return _fail(rc, out, err, "winget search")
    return text + "\n(Install with winget action=install id=<the Id column>)"


def op_winget_list(a, ctx):
    q = opt(a, "query", re.compile(r"^[\w.\-+ ]{1,80}$"))
    rc, out, err = ctx.runner(["winget", "list"] + (["--query", q] if q else []) + _WG_COMMON, 90, {})
    text = _clean(out or err, 4000)
    return text if text else _fail(rc, out, err, "winget list")


def _winget_change(verb: str):
    def op(a, ctx):
        wid = need(a, "id", _WINGET, "package Id from winget search, e.g. 7zip.7zip")
        if verb == "upgrade" and wid.lower() == "all":
            target = ["--all"]
        else:
            target = ["--id", wid, "--exact"]
        if ctx.dry_run:
            return f"DRY RUN: would winget {verb} {wid}. Nothing was changed."
        extra = ["--silent"] + (["--accept-package-agreements"] if verb != "uninstall" else [])
        rc, out, err = ctx.runner(["winget", verb] + target + extra + _WG_COMMON, 1800, {})
        text = _clean(out or err, 2500)
        if rc:
            return f"Failed: winget {verb} {wid} (exit code {rc}):\n{text}"
        return f"Done: winget {verb} {wid}.\n{text}"
    return op


def _winreg():
    try:
        import winreg                                             # Windows only, stdlib
        return winreg
    except ImportError:
        raise OpError("the registry exists only on Windows")


def _reg_type_name(wr, t: int) -> str:
    for n in _REG_TYPES + ("REG_BINARY", "REG_NONE"):
        if getattr(wr, n, None) == t:
            return n
    return str(t)


def _reg_show(v) -> str:
    if isinstance(v, bytes):
        return v[:32].hex() + ("..." if len(v) > 32 else "")
    if isinstance(v, list):
        return " | ".join(map(str, v))[:300]
    return str(v)[:300]


def op_registry_read(a, ctx):
    wr = _winreg()
    root, sub = reg_key(need(a, "key"))
    name = a.get("value")
    try:
        k = wr.OpenKey(getattr(wr, root), sub, 0, wr.KEY_READ | getattr(wr, "KEY_WOW64_64KEY", 0))
    except OSError as e:
        return f"Failed: cannot open {root}\\{sub}: {e.strerror or e}"
    with k:
        if name not in (None, ""):
            try:
                v, t = wr.QueryValueEx(k, str(name))
            except OSError:
                return f"{root}\\{sub} has no value named {name!r}"
            return f"{name} = {_reg_show(v)}  ({_reg_type_name(wr, t)})"
        lines, i = [], 0
        while i < 50:
            try:
                n, v, t = wr.EnumValue(k, i)
            except OSError:
                break
            lines.append(f"  {n or '(Default)'} = {_reg_show(v)}  ({_reg_type_name(wr, t)})")
            i += 1
        subs, j = [], 0
        while j < 50:
            try:
                subs.append(wr.EnumKey(k, j))
            except OSError:
                break
            j += 1
    return (f"{root}\\{sub}\nvalues:\n" + ("\n".join(lines) or "  (none)") + "\nsubkeys: " + (", ".join(subs) or "(none)"))


def _reg_admin(a) -> bool:
    try:
        root, _ = reg_key(a.get("key"))
    except OpError:
        return True
    return root != "HKEY_CURRENT_USER"


def op_registry_write(a, ctx):
    root, sub = reg_key(need(a, "key"))
    full = f"{root}\\{sub}"
    for rx in REG_DENY:
        if re.search(rx, full, re.I):
            raise OpError(f"{full} protects Windows security/sign-in; Lucy never writes there")
    name = str(a.get("value") or "")
    typ = str(a.get("type") or "REG_SZ").upper()
    if typ not in _REG_TYPES:
        raise OpError(f"type must be one of {', '.join(_REG_TYPES)}")
    data = a.get("data")
    if typ in ("REG_DWORD", "REG_QWORD"):
        try:
            data = int(str(data), 0)
        except (TypeError, ValueError):
            raise OpError("data must be a number for " + typ)
        if data < 0 or data >= (2 ** 32 if typ == "REG_DWORD" else 2 ** 64):
            raise OpError("number out of range for " + typ)
    elif typ == "REG_MULTI_SZ":
        data = [str(x) for x in data] if isinstance(data, list) else str(data or "").split("\n")
    else:
        data = "" if data is None else str(data)
    wr = _winreg()
    before = "(not set)"
    try:
        with wr.OpenKey(getattr(wr, root), sub, 0, wr.KEY_READ | getattr(wr, "KEY_WOW64_64KEY", 0)) as k:
            v, t = wr.QueryValueEx(k, name)
            before = f"{_reg_show(v)} ({_reg_type_name(wr, t)})"
    except OSError:
        pass
    label = name or "(Default)"
    if ctx.dry_run:
        return f"DRY RUN: would set {full} -> {label} from {before} to {_reg_show(data)} ({typ}). Nothing was changed."
    try:
        with wr.CreateKeyEx(getattr(wr, root), sub, 0, wr.KEY_SET_VALUE | wr.KEY_READ |
                            getattr(wr, "KEY_WOW64_64KEY", 0)) as k:
            wr.SetValueEx(k, name, 0, getattr(wr, typ), data)
            now, _t = wr.QueryValueEx(k, name)
    except OSError as e:
        return f"Failed: write {full}: {e.strerror or e}" + (" (needs administrator rights)" if getattr(e, "winerror", 0) == 5 else "")
    return f"Done: {full} -> {label} was {before}, now {_reg_show(now)} ({typ})."


def op_firewall(a, ctx):
    rc, out, err = ps("$f=$env:LUCY_F; $p = Get-NetFirewallProfile | ForEach-Object { \"$($_.Name)=$($_.Enabled)\" }; "
                      "$r = Get-NetFirewallRule -Enabled True | Where-Object { -not $f -or $_.DisplayName -like \"*$f*\" } | "
                      "Select-Object -First 50 DisplayName,@{n='Dir';e={\"$($_.Direction)\"}},@{n='Action';e={\"$($_.Action)\"}},"
                      "@{n='Profile';e={\"$($_.Profile)\"}}; [pscustomobject]@{Profiles=($p -join ', '); Rules=@($r)} | "
                      "ConvertTo-Json -Compress -Depth 3", {"LUCY_F": opt(a, "filter", _NAME)}, ctx, timeout=60)
    if rc:
        return _fail(rc, out, err, "read firewall")
    r = (_json_rows(out) or [{}])[0]
    rules = r.get("Rules") or []
    rules = rules if isinstance(rules, list) else [rules]
    return (f"Firewall on: {r.get('Profiles')}\nenabled rules (first 50): name | direction | action | profile\n"
            + _table(rules, ["DisplayName", "Dir", "Action", "Profile"]))


def _human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return str(n)


def op_disk(a, ctx):
    path = str(a.get("path") or "").strip()
    if not path:
        roots = [f"{c}:\\" for c in "CDEFGHIJKLMNOPQRSTUVWXYZ" if os.path.exists(f"{c}:\\")] if IS_WIN else ["/"]
        lines = []
        for r in roots:
            try:
                u = shutil.disk_usage(r)
            except OSError:
                continue
            lines.append(f"{r[:2] if IS_WIN else r}  {_human(u.free)} free of {_human(u.total)} ({100 * u.used // max(1, u.total)}% used)")
        return "\n".join(lines) or "(no drives found)"
    if not os.path.isdir(path):
        return f"Failed: {path} is not a folder"
    budget = time.monotonic() + float(a.get("seconds") or 20)
    sizes: Dict[str, int] = {}
    partial = False
    loose = 0
    try:
        entries = list(os.scandir(path))
    except OSError as e:
        return f"Failed: cannot read {path}: {e}"
    for ent in entries:
        if ent.is_symlink():
            continue
        if ent.is_file(follow_symlinks=False):
            try:
                loose += ent.stat(follow_symlinks=False).st_size
            except OSError:
                pass
            continue
        total, stack = 0, [ent.path]
        while stack:
            if time.monotonic() > budget:
                partial = True
                break
            d = stack.pop()
            try:
                with os.scandir(d) as it:
                    for e in it:
                        try:
                            if e.is_symlink():
                                continue
                            if e.is_dir(follow_symlinks=False):
                                stack.append(e.path)
                            else:
                                total += e.stat(follow_symlinks=False).st_size
                        except OSError:
                            pass
            except OSError:
                pass
        sizes[ent.name + "/"] = total
        if partial:
            break
    top = sorted(sizes.items(), key=lambda kv: -kv[1])[:15]
    out = [f"{_human(s):>10}  {n}" for n, s in top]
    if loose:
        out.append(f"{_human(loose):>10}  (files directly in the folder)")
    note = "\n(partial: stopped counting after the time limit; numbers are at least this big)" if partial else ""
    return f"Biggest items in {path}:\n" + "\n".join(out) + note


_LEVELS = {"error": "1,2", "warning": "1,2,3", "all": "1,2,3,4,0"}


def op_events(a, ctx):
    log = str(a.get("log") or "System").strip()
    if log.lower() not in ("system", "application", "setup"):
        raise OpError("log must be System, Application or Setup (the Security log is not on Lucy's menu)")
    level = str(a.get("level") or "error").lower()
    if level not in _LEVELS:
        raise OpError("level must be error, warning or all")
    try:
        hours = max(1, min(24 * 30, int(a.get("hours") or 24)))
        n = max(1, min(50, int(a.get("max") or 20)))
    except (TypeError, ValueError):
        raise OpError("hours and max must be numbers")
    filt = "@{LogName=$env:LUCY_L; StartTime=(Get-Date).AddHours(-[int]$env:LUCY_H)" + \
           ("" if level == "all" else "; Level=[int[]]($env:LUCY_V -split ',')") + "}"
    rc, out, err = ps(f"$e = Get-WinEvent -FilterHashtable {filt} -MaxEvents ([int]$env:LUCY_N) -ErrorAction "
                      "SilentlyContinue; @($e) | Select-Object @{n='Time';e={$_.TimeCreated.ToString('yyyy-MM-dd HH:mm')}},Id,"
                      "ProviderName,LevelDisplayName,@{n='Message';e={(($_.Message -split \"`n\")[0]).Trim()}} | "
                      "ConvertTo-Json -Compress",
                      {"LUCY_L": log, "LUCY_H": hours, "LUCY_N": n, "LUCY_V": _LEVELS[level]}, ctx, timeout=60)
    if rc:
        return _fail(rc, out, err, f"read the {log} log")
    rows = [r for r in _json_rows(out) if r]
    return (f"{log} log, last {hours} h, {level}: time | id | source | level | message\n"
            + _table(rows, ["Time", "Id", "ProviderName", "LevelDisplayName", "Message"], "(no matching events)"))


def op_updates(a, ctx):
    rc, out, err = ps(
        "$h = Get-HotFix | Sort-Object InstalledOn -Descending | Select-Object -First 5 HotFixID,"
        "@{n='On';e={\"$($_.InstalledOn)\"}}; $p=@(); try { $s=(New-Object -ComObject Microsoft.Update.Session)"
        ".CreateUpdateSearcher(); $r=$s.Search('IsInstalled=0 and IsHidden=0'); $p=@($r.Updates | ForEach-Object { $_.Title }) }"
        " catch { $p=@(\"(could not check online: $($_.Exception.Message))\") }; "
        "$rb = Test-Path 'HKLM:\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\WindowsUpdate\\Auto Update\\RebootRequired'; "
        "[pscustomobject]@{Recent=@($h); Pending=@($p); Reboot=$rb} | ConvertTo-Json -Compress -Depth 3", {}, ctx, timeout=95)
    if rc:
        return _fail(rc, out, err, "Windows Update status")
    r = (_json_rows(out) or [{}])[0]
    rec = r.get("Recent") or []
    rec = rec if isinstance(rec, list) else [rec]
    pend = r.get("Pending") or []
    pend = pend if isinstance(pend, list) else [pend]
    return ("Waiting to install: " + (("\n  - " + "\n  - ".join(map(str, pend))) if pend else "nothing") +
            f"\nRestart needed: {'yes' if r.get('Reboot') else 'no'}\nRecently installed: " +
            (", ".join(f"{x.get('HotFixID')} ({x.get('On')})" for x in rec) or "(none listed)"))


# ---- windows / desktop (never need admin; run in Lucy's own process) --------------------------------------------
def op_window_list(a, ctx):
    rc, out, err = ps("$f=$env:LUCY_F; Get-Process | Where-Object { $_.MainWindowHandle -ne 0 -and $_.MainWindowTitle } | "
                      "Where-Object { -not $f -or $_.MainWindowTitle -like \"*$f*\" -or $_.ProcessName -like \"*$f*\" } | "
                      "Select-Object -First 40 Id,ProcessName,MainWindowTitle | ConvertTo-Json -Compress",
                      {"LUCY_F": opt(a, "filter", _NAME)}, ctx)
    if rc:
        return _fail(rc, out, err, "list windows")
    return "pid | program | window title\n" + _table(_json_rows(out), ["Id", "ProcessName", "MainWindowTitle"],
                                                    "(no visible windows)")


_PINVOKE = ("$sig='[DllImport(\"user32.dll\")] public static extern bool SetForegroundWindow(IntPtr h); "
            "[DllImport(\"user32.dll\")] public static extern bool ShowWindow(IntPtr h,int n);'; "
            "$t = Add-Type -MemberDefinition $sig -Name Win -Namespace L -PassThru; ")
_SHOW = {"focus": 9, "restore": 9, "minimize": 6, "maximize": 3}


def op_window_control(a, ctx):
    action = str(a.get("control") or "focus").strip().lower()
    if action not in _SHOW:
        raise OpError("control must be focus, minimize, maximize or restore")
    pid = a.get("pid")
    title = opt(a, "title", _NAME)
    if pid in (None, "") and not title:
        raise OpError("give 'pid' (from window list) or part of the 'title'")
    sel = ("Get-Process -Id ([int]$env:LUCY_PID)" if pid not in (None, "")
           else "Get-Process | Where-Object { $_.MainWindowTitle -like \"*$($env:LUCY_T)*\" } | Select-Object -First 1")
    if ctx.dry_run:
        return f"DRY RUN: would {action} the window {title or pid}. Nothing was changed."
    rc, out, err = ps(_PINVOKE + f"$p = {sel}; if (-not $p -or $p.MainWindowHandle -eq 0) {{ Write-Error 'no such window' }} "
                      f"else {{ [void]$t::ShowWindow($p.MainWindowHandle, {_SHOW[action]}); [void]$t::SetForegroundWindow("
                      "$p.MainWindowHandle); $p.MainWindowTitle }",
                      {"LUCY_PID": pid if pid not in (None, "") else 0, "LUCY_T": title}, ctx)
    if rc:
        return _fail(rc, out, err, f"{action} window")
    return f"Done: {action} '{_clean(out, 120)}'."


def op_clipboard_get(a, ctx):
    rc, out, err = ps("Get-Clipboard -Raw", {}, ctx)
    if rc:
        return _fail(rc, out, err, "read the clipboard")
    text = (out or "").rstrip("\n")
    return f"Clipboard:\n{text[:4000]}" if text.strip() else "The clipboard is empty (or holds something that is not text)."


def op_clipboard_set(a, ctx):
    text = str(a.get("text") or "")
    if ctx.dry_run:
        return f"DRY RUN: would put {len(text)} characters on the clipboard."
    rc, out, err = ps("Set-Clipboard -Value $env:LUCY_TXT", {"LUCY_TXT": text}, ctx)
    if rc:
        return _fail(rc, out, err, "set the clipboard")
    return f"Done: {len(text)} characters copied to the clipboard."


def op_notify(a, ctx):
    title = str(a.get("title") or "Lucy")[:120]
    msg = str(a.get("message") or a.get("text") or "")[:400]
    if not msg:
        raise OpError("'message' is required")
    if ctx.dry_run:
        return f"DRY RUN: would show a notification: {title} - {msg}"
    rc, out, err = ps("[void][Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, "
                      "ContentType=WindowsRuntime]; $x=[Windows.UI.Notifications.ToastNotificationManager]::"
                      "GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02); "
                      "$t=$x.GetElementsByTagName('text'); $t.Item(0).AppendChild($x.CreateTextNode($env:LUCY_TITLE))|Out-Null; "
                      "$t.Item(1).AppendChild($x.CreateTextNode($env:LUCY_MSG))|Out-Null; "
                      "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('Lucy').Show("
                      "[Windows.UI.Notifications.ToastNotification]::new($x))",
                      {"LUCY_TITLE": title, "LUCY_MSG": msg}, ctx)
    if rc:                                                       # toast can fail on older builds: fall back to a message box
        ps("Add-Type -AssemblyName PresentationFramework; [void][System.Windows.MessageBox]::Show($env:LUCY_MSG,$env:LUCY_TITLE)",
           {"LUCY_TITLE": title, "LUCY_MSG": msg}, ctx, timeout=15)
    return f"Done: showed the notification '{title}'."


def op_volume(a, ctx):
    action = str(a.get("control") or "").strip().lower()
    if action not in ("set", "mute", "unmute"):
        raise OpError("control must be set (with level 0-100), mute or unmute")
    if action == "set":
        try:
            level = max(0, min(100, int(a.get("level"))))
        except (TypeError, ValueError):
            raise OpError("level must be a number 0-100")
    if ctx.dry_run:
        return f"DRY RUN: would {action} the volume" + (f" to {level}%" if action == "set" else "") + "."
    if action == "set":
        presses = "$w=New-Object -ComObject WScript.Shell; 1..50 | ForEach-Object { $w.SendKeys([char]174) }; " \
                  "$n=[int]([math]::Round(([int]$env:LUCY_L)/2)); 1..$n | ForEach-Object { $w.SendKeys([char]175) }"
        rc, out, err = ps(presses, {"LUCY_L": level}, ctx)
        done = f"volume set to about {level}%"
    else:
        rc, out, err = ps("(New-Object -ComObject WScript.Shell).SendKeys([char]173)", {}, ctx)
        done = "mute toggled"
    if rc:
        return _fail(rc, out, err, "change the volume")
    return f"Done: {done}."


def op_open(a, ctx):
    target = str(a.get("target") or a.get("path") or a.get("url") or "").strip()
    if not target:
        raise OpError("'target' is required (an app name, a file path, or a URL)")
    low = target.lower()
    if low.startswith(("http://", "https://")):
        kind = "URL"
    elif re.match(r"(?i)^[a-z]+://", low) and not low.startswith("file://"):
        raise OpError("only http(s) links are opened automatically")
    else:
        kind = "file/app"
        if ctx.guard and (os.path.sep in target or "/" in target or ":" in target):
            ctx.guard.check(target)                             # a path: keep it out of Lucy Core / secrets
    if ctx.dry_run:
        return f"DRY RUN: would open the {kind}: {target}"
    rc, out, err = ps("Start-Process $env:LUCY_T", {"LUCY_T": target}, ctx, timeout=30)
    if rc:
        return _fail(rc, out, err, f"open {target}")
    return f"Done: opened {target}."


def op_screenshot(a, ctx):
    """Saves a PNG to runtime/tmp and returns the path (the tool wrapper turns it into analyze_image / a MEDIA file)."""
    out_path = str(a.get("out_path") or "").strip()
    if not out_path:
        raise OpError("internal: out_path missing")
    region = a.get("region")
    if region:
        try:
            x, y, w, h = (int(region[k]) for k in ("x", "y", "w", "h"))
        except (TypeError, ValueError, KeyError, IndexError):
            raise OpError("region must be {x, y, w, h} in pixels")
        bounds = f"$b=[Drawing.Rectangle]::FromLTRB({x},{y},{x + w},{y + h})"
    else:
        bounds = "$b=[Windows.Forms.SystemInformation]::VirtualScreen"
    if ctx.dry_run:
        return "DRY RUN: would take a screenshot."
    rc, out, err = ps("Add-Type -AssemblyName System.Windows.Forms,System.Drawing; " + bounds +
                      "; $bmp=New-Object Drawing.Bitmap $b.Width,$b.Height; $g=[Drawing.Graphics]::FromImage($bmp); "
                      "$g.CopyFromScreen($b.Left,$b.Top,0,0,$bmp.Size); $bmp.Save($env:LUCY_O,"
                      "[Drawing.Imaging.ImageFormat]::Png); $g.Dispose(); $bmp.Dispose(); 'ok'",
                      {"LUCY_O": out_path}, ctx, timeout=30)
    if rc or not os.path.exists(out_path):
        return _fail(rc, out, err, "take a screenshot")
    return out_path


# ---- files (copy / move / delete / mkdir). Lucy's own process does normal places; the broker does protected ones.
def recycle(path: str, ctx: Ctx) -> str:
    if ctx.trash is not None:
        return ctx.trash(path)
    if not IS_WIN:
        raise OpError("no Recycle Bin here; nothing was deleted")
    kind = "DeleteDirectory" if os.path.isdir(path) else "DeleteFile"
    rc, out, err = ps("Add-Type -AssemblyName Microsoft.VisualBasic; [Microsoft.VisualBasic.FileIO.FileSystem]::"
                      f"{kind}($env:LUCY_T, 'OnlyErrorDialogs', 'SendToRecycleBin')", {"LUCY_T": path}, ctx, timeout=300)
    if rc or os.path.exists(path):
        raise OpError("could not move it to the Recycle Bin; nothing was deleted (" + _clean(err or out, 300) + ")")
    return "the Recycle Bin"


def _fs_paths(a, ctx, *keys) -> List[str]:
    out = []
    for k in keys:
        v = str(a.get(k) or "").strip().strip("\"'")
        if not v:
            raise OpError(f"'{k}' is required")
        out.append(ctx.guard.check(v) if ctx.guard else v)
    return out


def op_fs_copy(a, ctx):
    src, dst = _fs_paths(a, ctx, "source", "destination")
    if not os.path.exists(src):
        raise OpError(f"{a.get('source')} does not exist")
    if os.path.isdir(dst):
        dst = os.path.join(dst, os.path.basename(src.rstrip("/\\")))
    if os.path.exists(dst) and not a.get("overwrite"):
        raise OpError(f"{dst} already exists; pass overwrite=true to replace it")
    if ctx.dry_run:
        return f"DRY RUN: would copy {a.get('source')} to {dst}. Nothing was changed."
    os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
    if os.path.isdir(src):
        shutil.copytree(src, dst, dirs_exist_ok=bool(a.get("overwrite")))
    else:
        shutil.copy2(src, dst)
    return f"Done: copied {a.get('source')} to {dst}."


def op_fs_move(a, ctx):
    src, dst = _fs_paths(a, ctx, "source", "destination")
    if not os.path.exists(src):
        raise OpError(f"{a.get('source')} does not exist")
    if os.path.isdir(dst):
        dst = os.path.join(dst, os.path.basename(src.rstrip("/\\")))
    if os.path.exists(dst):
        raise OpError(f"{dst} already exists; pick another destination or delete it first")
    if ctx.dry_run:
        return f"DRY RUN: would move {a.get('source')} to {dst}. Nothing was changed."
    os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
    shutil.move(src, dst)
    return f"Done: moved {a.get('source')} to {dst}."


def op_fs_delete(a, ctx):
    raw = a.get("path") or a.get("source")
    (p,) = _fs_paths({"path": raw}, ctx, "path")
    if not os.path.exists(p):
        raise OpError(f"{raw} does not exist")
    if ctx.dry_run:
        return f"DRY RUN: would move {raw} to the Recycle Bin. Nothing was changed."
    where = recycle(p, ctx)
    return f"Done: {raw} moved to {where} (it can be restored from there)."


def op_fs_mkdir(a, ctx):
    raw = a.get("path") or a.get("destination")
    (p,) = _fs_paths({"path": raw}, ctx, "path")
    if ctx.dry_run:
        return f"DRY RUN: would create the folder {raw}. Nothing was changed."
    os.makedirs(p, exist_ok=True)
    return f"Done: folder {raw} exists."


# =============================================================================================== the menu
class Op:
    def __init__(self, fn, admin: Any = False, long: bool = False):
        self.fn, self._admin, self.long = fn, admin, long

    def admin(self, args: dict) -> bool:
        return bool(self._admin(args)) if callable(self._admin) else bool(self._admin)


OPS: Dict[str, Op] = {
    "service.list": Op(op_service_list), "service.status": Op(op_service_status),
    "service.start": Op(_service_change("start"), True), "service.stop": Op(_service_change("stop"), True),
    "service.restart": Op(_service_change("restart"), True),
    "process.list": Op(op_process_list), "process.kill": Op(op_process_kill),            # admin retry when denied
    "task.list": Op(op_task_list), "task.info": Op(op_task_info),
    "task.run": Op(_task_change("run"), True), "task.enable": Op(_task_change("enable"), True),
    "task.disable": Op(_task_change("disable"), True),
    "winget.search": Op(op_winget_search), "winget.list": Op(op_winget_list),
    "winget.install": Op(_winget_change("install"), True, long=True),
    "winget.uninstall": Op(_winget_change("uninstall"), True, long=True),
    "winget.upgrade": Op(_winget_change("upgrade"), True, long=True),
    "registry.read": Op(op_registry_read), "registry.write": Op(op_registry_write, _reg_admin),
    "info.firewall": Op(op_firewall), "info.disk": Op(op_disk),
    "info.events": Op(op_events),
    "info.updates": Op(op_updates),
    "fs.copy": Op(op_fs_copy, long=True), "fs.move": Op(op_fs_move, long=True), "fs.delete": Op(op_fs_delete),
    "fs.mkdir": Op(op_fs_mkdir),
    "window.list": Op(op_window_list), "window.control": Op(op_window_control),
    "desktop.clipboard_get": Op(op_clipboard_get), "desktop.clipboard_set": Op(op_clipboard_set),
    "desktop.notify": Op(op_notify), "desktop.volume": Op(op_volume), "desktop.open": Op(op_open),
    "desktop.screenshot": Op(op_screenshot),
}


def run(op: str, args: dict, ctx: Ctx) -> str:
    """Run one op now (validation errors come back as 'Failed: ...' text, never as exceptions)."""
    spec = OPS.get(op)
    if spec is None:
        return f"Failed: unknown operation {op!r}"
    try:
        return spec.fn(dict(args or {}), ctx)
    except OpError as e:
        return f"Failed: {e}"
    except OSError as e:
        return f"Failed: {e.strerror or e}" + (" (needs administrator rights)" if getattr(e, "winerror", 0) == 5 or
                                                isinstance(e, PermissionError) else "")


def needs_admin_retry(result: str) -> bool:
    return isinstance(result, str) and result.startswith("Failed") and "needs administrator rights" in result
