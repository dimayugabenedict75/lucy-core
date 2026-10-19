"""python: a persistent Python session per chat, for quick calculations and data work (variables survive between calls).

Why it is confined: the safety policy can read a shell command before it runs, but it cannot read Python code. So the
child process installs an audit hook (sys.addaudithook) that enforces the user's permissions.yaml from the INSIDE:
  * it cannot start programs (subprocess / os.system / exec / spawn / ctypes): use run_shell_command, which has rules;
  * it can write only inside `folders.write` (plus the temp folder) and never into denied, read-only, ask-first or
    constitution locations; it cannot read denied (secret) locations;
  * it cannot connect to this PC's own network addresses (Lucy's own API).
An audit hook is a strong guard rail, not a perfect sandbox: the tool is still tier "exec", so it asks for approval in
every mode except Full auto / Auto-in-allowed-folders. The child never sees Lucy's API keys.
"""
from __future__ import annotations

import asyncio
import atexit
import json
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Dict, Optional

from . import _code, jobs

DEFAULT_TIMEOUT = 30
MAX_TIMEOUT = 120
IDLE_TTL = 1800.0
MAX_SESSIONS = 3
OUT_HEAD, OUT_TAIL = 2000, 3500
_LOOP_MARGIN = 10

SCHEMA = {
    "type": "function",
    "function": {
        "name": "python",
        "description": ("Run Python code in a persistent session (variables stay between calls) for quick calculations and data "
                        "work. It can't start programs or write outside the allowed folders: use run_shell_command or the file "
                        "tools for that."),
        "parameters": {
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "Python code. The value of the last expression is shown."},
                "timeout": {"type": "integer", "description": f"Seconds before it is stopped (default {DEFAULT_TIMEOUT}, max {MAX_TIMEOUT})."},
                "cwd": {"type": "string", "description": "Folder the session works in (changing it starts a fresh session)."},
                "reset": {"type": "boolean", "description": "Start a fresh session (forget all variables)."},
            },
            "required": ["code"],
        },
    },
}
TIER = "exec"
LABEL = "run"

# ------------------------------------------------------------------------------------------------ the child process
_SERVER = r'''
import sys, os, io, json, ast, traceback, contextlib, tempfile, linecache, tokenize, threading  # noqa
sys.dont_write_bytecode = True
_proto_in = sys.stdin
_proto_out = os.fdopen(os.dup(1), "w", encoding="utf-8", newline="\n")
_null = os.open(os.devnull, os.O_WRONLY)
os.dup2(_null, 1)
_rules = json.loads(os.environ.pop("LUCY_PY_RULES", "{}"))
G = {"__name__": "__main__", "__builtins__": __builtins__}

def _norm(p):
    s = os.path.normcase(os.path.realpath(os.path.expanduser(str(p)))).replace("\\", "/")
    return s.rstrip("/") or "/"

def _inside(p, f):
    return p == f or p.startswith(f.rstrip("/") + "/")

_W = [_norm(x) for x in _rules.get("write", [])]
_TMP = _norm(tempfile.gettempdir())
_NO = [_norm(x) for x in _rules.get("no_write", [])]
_DENY = [_norm(x) for x in _rules.get("deny", [])]
_BLOCK_EVENTS = ("os.exec", "os.spawn", "os.posix_spawn", "os.fork", "os.system", "os.forkpty", "subprocess.", "_winapi.", "ctypes.",
                 "msvcrt.", "winreg.", "pty.", "os.startfile")
_LOOP = ("127.", "localhost", "::1", "0.0.0.0")

def _check(path, writing, what):
    try:
        if isinstance(path, int) or path is None:
            return
        if isinstance(path, bytes):
            path = os.fsdecode(path)
        n = _norm(path)
    except Exception:
        return
    if any(_inside(n, d) for d in _DENY):
        raise PermissionError("blocked location (secrets / private data): " + str(path))
    if writing:
        if _inside(n, _TMP):
            return                                  # scratch files in the temp folder are harmless (secrets were checked above)
        if any(_inside(n, d) for d in _NO):
            raise PermissionError("Lucy may not change this location (read-only, asks first, or a safety file): " + str(path)
                                  + ". Use the file tools for it: they ask the user when needed.")
        if not any(_inside(n, w) for w in _W):
            raise PermissionError("writing outside the allowed folders is not allowed from the python tool: " + str(path)
                                  + ". Use the file tools (write_local_file / apply_patch).")

def _hook(event, args):
    try:
        if event == "open":
            path, mode, flags = args[0], args[1], args[2]
            w = False
            if isinstance(mode, str) and any(c in mode for c in "wax+"):
                w = True
            if isinstance(flags, int) and flags & (os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_TRUNC):
                w = True
            _check(path, w, event)
        elif event in ("os.remove", "os.rmdir", "os.mkdir", "os.truncate", "os.chmod", "os.chown", "os.mkfifo", "os.mknod",
                       "os.utime", "os.symlink", "os.link"):
            _check(args[0] if event not in ("os.symlink", "os.link") else args[1], True, event)
        elif event in ("os.rename", "shutil.move", "shutil.copyfile", "shutil.copytree"):
            if event == "os.rename":
                _check(args[0], True, event)
                _check(args[1], True, event)
            else:
                _check(args[1], True, event)
        elif event in ("os.listdir", "os.scandir"):
            _check(args[0], False, event)
        elif event == "socket.connect":
            host = str(args[1][0]) if isinstance(args[1], tuple) and args[1] else ""
            if host.startswith(_LOOP) or host in ("localhost", "::1"):
                raise PermissionError("connecting to this PC's own network addresses is not allowed from the python tool")
        elif event.startswith(_BLOCK_EVENTS):
            raise PermissionError("starting programs / system calls is not allowed from the python tool (" + event
                                  + "). Use run_shell_command: it has approval rules.")
    except PermissionError:
        raise
    except Exception:
        return

sys.addaudithook(_hook)

def _tb(exc):
    frames = [f for f in traceback.extract_tb(exc.__traceback__) if f.filename == "<python>"]
    lines = ["Traceback (most recent call last):\n"] + traceback.format_list(frames)
    return "".join(lines) + "".join(traceback.format_exception_only(type(exc), exc)).rstrip("\n")

def _run(code):
    buf = io.StringIO()
    ok = True
    sys.stdin = io.StringIO("")
    try:
        tree = ast.parse(code, "<python>", "exec")
        last = None
        if tree.body and isinstance(tree.body[-1], ast.Expr):
            last = ast.Expression(tree.body.pop().value)
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            exec(compile(tree, "<python>", "exec"), G)
            if last is not None:
                v = eval(compile(last, "<python>", "eval"), G)
                if v is not None:
                    print(repr(v))
    except SystemExit as e:
        buf.write("[SystemExit(%r) ignored: the session keeps running]\n" % (e.code,))
    except BaseException as e:
        ok = False
        buf.write(_tb(e) + "\n")
    finally:
        sys.stdin = _proto_in
    return ok, buf.getvalue()

if _rules.get("cwd"):
    try:
        os.chdir(_rules["cwd"])
    except OSError:
        pass
_proto_out.write(json.dumps({"ready": True}) + "\n")
_proto_out.flush()
for _line in _proto_in:
    try:
        _req = json.loads(_line)
    except ValueError:
        continue
    _ok, _out = _run(_req.get("code", ""))
    _proto_out.write(json.dumps({"id": _req.get("id"), "ok": _ok, "out": _out[:300000]}) + "\n")
    _proto_out.flush()
'''


class ReplDied(RuntimeError):
    pass


class Repl:
    def __init__(self, cwd: Path, rules: dict) -> None:
        self.cwd = cwd
        self.last_used = time.time()
        self.dead = False
        self._q: "queue.Queue[Optional[dict]]" = queue.Queue()
        self._n = 0
        env = {k: v for k, v in os.environ.items() if k.upper() not in jobs._HIDDEN_ENV}
        env.update({"LUCY_PY_RULES": json.dumps({**rules, "cwd": str(cwd)}), "PYTHONUNBUFFERED": "1",
                    "PYTHONIOENCODING": "utf-8", "PYTHONDONTWRITEBYTECODE": "1"})
        kwargs: dict = {}
        if os.name == "nt":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        else:
            kwargs["start_new_session"] = True
        try:
            self.proc = subprocess.Popen([sys.executable, "-u", "-c", _SERVER], cwd=str(cwd), env=env,
                                         stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, **kwargs)
        except (OSError, ValueError) as e:
            raise ReplDied(f"could not start Python: {e}") from e
        threading.Thread(target=self._read, daemon=True).start()
        ready = self._get(15)
        if not ready or not ready.get("ready"):
            self.kill()
            raise ReplDied("the Python session did not start")

    def _read(self) -> None:
        try:
            for raw in self.proc.stdout:
                try:
                    self._q.put(json.loads(raw.decode("utf-8", "replace")))
                except ValueError:
                    continue
        except Exception:                                    # noqa: BLE001
            pass
        self._q.put(None)

    def _get(self, timeout: float) -> Optional[dict]:
        try:
            return self._q.get(timeout=timeout)
        except queue.Empty:
            return {"timeout": True}

    def run(self, code: str, timeout: float) -> dict:
        if self.dead or self.proc.poll() is not None:
            self.dead = True
            raise ReplDied("the Python session ended")
        self._n += 1
        self.last_used = time.time()
        try:
            self.proc.stdin.write((json.dumps({"id": self._n, "code": code}) + "\n").encode("utf-8"))
            self.proc.stdin.flush()
        except OSError as e:
            self.dead = True
            raise ReplDied(f"the Python session ended ({e})") from e
        while True:
            msg = self._get(timeout)
            if msg is None:
                self.dead = True
                raise ReplDied("the Python session ended unexpectedly")
            if msg.get("timeout"):
                self.kill()
                return {"timeout": True}
            if msg.get("id") == self._n:
                self.last_used = time.time()
                return msg

    def kill(self) -> None:
        self.dead = True
        try:
            jobs.kill_tree(self.proc)
        except Exception:                                    # noqa: BLE001
            pass
        self._q.put(None)                                    # wakes a thread that is waiting for an answer


_REPLS: Dict[str, Repl] = {}
_LOCK = threading.Lock()


def _cleanup() -> None:
    now = time.time()
    for key in [k for k, r in _REPLS.items() if r.dead or now - r.last_used > IDLE_TTL]:
        _REPLS.pop(key).kill()
    while len(_REPLS) > MAX_SESSIONS:
        oldest = min(_REPLS, key=lambda k: _REPLS[k].last_used)
        _REPLS.pop(oldest).kill()


@atexit.register
def _kill_all() -> None:
    for r in list(_REPLS.values()):
        try:
            r.kill()
        except Exception:                                    # noqa: BLE001
            pass


def kill_session(session_id: Optional[str]) -> None:
    with _LOCK:
        r = _REPLS.pop(session_id or "_", None)
    if r:
        r.kill()


def build_rules() -> dict:
    """What the child may touch, taken from the user's permissions.yaml (see the module docstring)."""
    try:
        from lucy import safety
        layer = safety.get()
        cfg = layer.cfg
        protected = list(getattr(layer.constitution, "_protected", []))
        return {"write": list(cfg.write_folders), "deny": list(cfg.denied_paths),
                "no_write": list(cfg.denied_paths) + list(cfg.readonly_paths) + list(cfg.ask_paths) + protected}
    except Exception:                                        # noqa: BLE001 - no safety layer: be strict, write nowhere but temp
        return {"write": [], "deny": [], "no_write": []}


def _clip(text: str) -> str:
    text = text.rstrip("\n")
    if len(text) <= OUT_HEAD + OUT_TAIL:
        return text
    return f"{text[:OUT_HEAD]}\n[... {len(text) - OUT_HEAD - OUT_TAIL} characters left out ...]\n{text[-OUT_TAIL:]}"


def _execute(args: dict, session: Optional[str]) -> str:
    code = str(args.get("code") or "")
    if not code.strip():
        return "Error: 'code' is required: the Python code to run."
    cwd, err = _code.resolve_dir(args.get("cwd"))
    if cwd is None:
        return err
    try:
        timeout = max(1, min(int(args.get("timeout") or DEFAULT_TIMEOUT), MAX_TIMEOUT))
    except (TypeError, ValueError):
        timeout = DEFAULT_TIMEOUT
    cap = max(5.0, float(os.environ.get("LUCY_TOOL_TIMEOUT", "120")) - _LOOP_MARGIN)
    timeout = min(timeout, int(cap))
    reset = args.get("reset")
    reset = reset.strip().lower() == "true" if isinstance(reset, str) else bool(reset)
    key = session or "_"
    fresh = ""
    with _LOCK:
        _cleanup()
        repl = _REPLS.get(key)
        wants_cwd = bool(str(args.get("cwd") or "").strip())
        replaced = False
        if repl is not None and (reset or repl.dead or (wants_cwd and repl.cwd != cwd)):
            _REPLS.pop(key, None)
            repl.kill()
            repl, replaced = None, True
        if repl is None:
            try:
                repl = Repl(cwd, build_rules())
            except ReplDied as e:
                return f"Error: {e}."
            _REPLS[key] = repl
            fresh = "[new Python session" + (": earlier variables are gone" if replaced else "") + "]\n"
    try:
        res = repl.run(code, timeout)
    except ReplDied as e:
        with _LOCK:
            if _REPLS.get(key) is repl:
                _REPLS.pop(key, None)
        return f"Error: {e}. Variables from earlier calls are lost: send the code again (it starts a new session)."
    if res.get("timeout"):
        with _LOCK:
            if _REPLS.get(key) is repl:
                _REPLS.pop(key, None)
        return (f"Command failed: the Python code ran longer than {timeout}s and was stopped; variables from earlier calls are "
                "lost. Do less per call, or run a long job with run_shell_command action 'start'.")
    out = _clip(str(res.get("out") or ""))
    if not out:
        out = "(no output)"
    return fresh + out


async def handler(args, ctx):
    session = getattr(ctx, "session_id", None)
    try:
        return await asyncio.to_thread(_execute, args or {}, session)
    except asyncio.CancelledError:                           # the user pressed Stop: stop the code too
        kill_session(session)
        raise
