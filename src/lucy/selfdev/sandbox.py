"""Run user-tool code away from Lucy: a static check first, then a separate restricted Python process.

User code is NEVER imported into Lucy's own process. Each call / test run gets: a fresh temp folder as working
directory, a small environment without API keys or tokens, a time limit, an output cap and the audit hook of
_runner.py (see there for what it blocks). Constitution file (src/lucy/selfdev/ is hash-locked).
"""
from __future__ import annotations

import ast
import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, List, Optional

RUNNER = Path(__file__).resolve().parent / "_runner.py"
MARK = "\x1eLUCY_RESULT "

NEEDS = ("files", "programs", "internet", "system")
# The lowest tier a tool with these needs may run at (the user can raise it, never lower it below this).
NEED_TIER = {"files": "write", "programs": "exec", "system": "system", "internet": "network"}
TIER_ORDER = ("read", "write", "exec", "system", "network")

_ALWAYS_BANNED_MODULES = {"ctypes", "_ctypes", "cffi", "importlib", "imp", "gc", "inspect", "builtins", "code",
                          "codeop", "marshal", "pickle", "shelve", "dill", "_thread", "threading", "signal", "atexit",
                          "sysconfig", "site", "lucy", "runpy", "zipimport", "pkgutil"}
_NET_MODULES = {"socket", "ssl", "http", "urllib", "ftplib", "smtplib", "poplib", "imaplib", "telnetlib", "xmlrpc",
                "socketserver", "asyncio", "requests", "httpx", "aiohttp", "websocket", "websockets", "paramiko"}
_SPAWN_MODULES = {"subprocess", "multiprocessing", "pty", "concurrent", "_posixsubprocess", "_winapi", "pywinauto"}
_SYSTEM_MODULES = {"winreg", "_winreg", "win32api", "win32con", "pythoncom", "wmi"}
_BANNED_CALLS = {"eval", "exec", "compile", "__import__", "globals", "locals", "vars", "breakpoint", "input",
                 "memoryview", "setattr", "delattr"}
_SPAWN_ATTRS = {"system", "popen", "startfile", "execv", "execve", "execl", "execle", "execlp", "execvp", "execvpe",
                "spawnl", "spawnv", "spawnve", "posix_spawn", "posix_spawnp", "fork", "forkpty", "kill", "killpg"}
_BANNED_SYS = {"addaudithook", "settrace", "setprofile", "modules", "meta_path", "path_hooks", "_getframe",
               "setrecursionlimit", "exit", "stdout", "stderr", "stdin", "__stdout__", "__stderr__"}
_OK_DUNDERS = {"__name__", "__doc__", "__init__", "__len__", "__iter__", "__next__", "__enter__", "__exit__"}


def tier_floor(needs: Iterable[str]) -> str:
    level = 0
    for n in needs or ():
        level = max(level, TIER_ORDER.index(NEED_TIER.get(n, "read")))
    return TIER_ORDER[level]


def static_check(code: str, needs: Iterable[str] = (), *, is_tests: bool = False) -> List[str]:
    """Problems with the code (empty list = fine). Clear, fixable messages: a small model reads them."""
    needs = set(needs or ())
    probs: List[str] = []
    try:
        tree = ast.parse(code or "", filename="<tests>" if is_tests else "<tool>")
    except SyntaxError as e:
        return [f"syntax error on line {e.lineno}: {e.msg}"]
    banned = set(_ALWAYS_BANNED_MODULES)
    if "internet" not in needs:
        banned |= _NET_MODULES
    if "programs" not in needs:
        banned |= _SPAWN_MODULES
    if "system" not in needs:
        banned |= _SYSTEM_MODULES
    if is_tests:
        banned |= _NET_MODULES | _SPAWN_MODULES | _SYSTEM_MODULES
    has_run = has_assert = False
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            mods = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""]
            for m in mods:
                top = m.split(".")[0]
                if top in banned:
                    hint = ""
                    if top in _NET_MODULES:
                        hint = " (add 'internet' to needs if the tool really must use the web; tests can never)"
                    elif top in _SPAWN_MODULES:
                        hint = " (add 'programs' to needs if the tool must start programs; tests can never)"
                    elif top in _SYSTEM_MODULES:
                        hint = " (add 'system' to needs)"
                    probs.append(f"line {node.lineno}: import {top} is not allowed{hint}")
            if isinstance(node, ast.ImportFrom) and node.level:
                probs.append(f"line {node.lineno}: relative imports are not allowed")
        elif isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Name) and f.id in _BANNED_CALLS:
                probs.append(f"line {node.lineno}: {f.id}() is not allowed")
            if isinstance(f, ast.Attribute) and f.attr in _SPAWN_ATTRS and isinstance(f.value, ast.Name) \
                    and f.value.id == "os" and "programs" not in needs:
                probs.append(f"line {node.lineno}: os.{f.attr}() needs 'programs' in needs")
        elif isinstance(node, ast.Attribute):
            a = node.attr
            if a.startswith("__") and a.endswith("__") and a not in _OK_DUNDERS:
                probs.append(f"line {node.lineno}: the attribute {a} is not allowed")
            if isinstance(node.value, ast.Name) and node.value.id == "sys" and a in _BANNED_SYS:
                probs.append(f"line {node.lineno}: sys.{a} is not allowed")
        elif isinstance(node, ast.Name) and node.id in ("__builtins__", "__loader__", "__spec__"):
            probs.append(f"line {node.lineno}: {node.id} is not allowed")
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "run" and node in tree.body:
            has_run = True
            if isinstance(node, ast.AsyncFunctionDef):
                probs.append("run(args) must be a normal function, not async")
            elif len(node.args.args) != 1:
                probs.append("run must take exactly one argument: def run(args):  (args is a dict)")
        elif isinstance(node, ast.Assert):
            has_assert = True
    if is_tests:
        if not has_assert:
            probs.append("the tests need at least one assert, e.g.  assert run({'text': 'ab'}) == 'ba'")
    elif not has_run:
        probs.append("define a top-level function  def run(args):  that returns a string")
    seen, out = set(), []
    for p in probs:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out[:12]


@dataclass
class Outcome:
    ok: bool
    text: str = ""
    error: str = ""
    blocked: bool = False
    timed_out: bool = False
    stdout: str = ""
    ms: int = 0
    extra: dict = field(default_factory=dict)


_SECRET_WORDS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "PASSWD", "CREDENTIAL", "AUTH", "COOKIE", "SESSION")
_KEEP_ENV = ("SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE", "LANG",
             "LC_ALL", "TZ")


def _env(tmp: str, caps: Iterable[str]) -> dict:
    caps = set(caps)
    env = {k: v for k, v in os.environ.items() if k.upper() in _KEEP_ENV}
    if "programs" in caps or "system" in caps:
        env["PATH"] = os.environ.get("PATH", "")
        for k in ("USERPROFILE", "HOMEDRIVE", "HOMEPATH", "APPDATA", "LOCALAPPDATA", "PROGRAMFILES", "PROGRAMDATA"):
            if k in os.environ:
                env[k] = os.environ[k]
    env.update({"TEMP": tmp, "TMP": tmp, "TMPDIR": tmp, "HOME": tmp, "PYTHONIOENCODING": "utf-8",
                "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUTF8": "1"})
    return {k: v for k, v in env.items() if not any(w in k.upper() for w in _SECRET_WORDS)}


def run_job(module: Path, *, mode: str = "call", args: Optional[dict] = None, tests: str = "",
            caps: Iterable[str] = (), write_paths: Iterable[str] = (), deny_read: Iterable[str] = (),
            timeout: float = 30.0, max_out: int = 12000, python: Optional[str] = None) -> Outcome:
    """Run module's run(args) (mode='call') or the tests against it (mode='test') in a child process."""
    import time
    caps = [c for c in caps if c in NEEDS] if mode == "call" else []        # tests never get extra powers
    tmp = tempfile.mkdtemp(prefix="lucy_tool_")
    job = {"module": str(Path(module).resolve()), "mode": mode, "args": args or {}, "tests": tests, "caps": caps,
           "tmp": tmp, "write_paths": list(write_paths) if mode == "call" else [], "deny_read": list(deny_read)}
    flags = 0x08000000 if sys.platform == "win32" else 0                    # CREATE_NO_WINDOW
    t0 = time.monotonic()
    try:
        try:
            p = subprocess.run([python or sys.executable, "-I", "-B", str(RUNNER)], input=json.dumps(job),
                               capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
                               cwd=tmp, env=_env(tmp, caps), creationflags=flags)
        except subprocess.TimeoutExpired:
            return Outcome(False, error=f"timed out after {int(timeout)} s (make it faster, or it loops forever)",
                           timed_out=True, ms=int((time.monotonic() - t0) * 1000))
        ms = int((time.monotonic() - t0) * 1000)
        out = p.stdout or ""
        i = out.rfind(MARK)
        if i < 0:
            err = (p.stderr or "").strip()[-800:] or f"the tool process ended without an answer (exit {p.returncode})"
            return Outcome(False, error=err, ms=ms)
        try:
            res = json.loads(out[i + len(MARK):].strip().splitlines()[0])
        except (ValueError, IndexError):
            return Outcome(False, error="the tool process gave an unreadable answer", ms=ms)
        text = str(res.get("result") or "")
        if len(text) > max_out:
            text = text[: max_out // 2] + f"\n...[{len(text) - max_out} characters cut]...\n" + text[-max_out // 2:]
        return Outcome(bool(res.get("ok")), text=text, error=str(res.get("error") or ""),
                       blocked=bool(res.get("blocked")), stdout=str(res.get("stdout") or ""), ms=ms,
                       extra={"trace": res.get("trace", "")})
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
