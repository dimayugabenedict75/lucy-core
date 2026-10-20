"""Runs ONE user-tool call or its tests inside a separate, restricted Python process. Stdlib only.

Started by sandbox.py as `python -I -B _runner.py` with a JSON job on stdin. It installs an audit hook BEFORE any user
code is loaded. The hook cannot be removed from Python code and blocks, unless the job's caps allow it:
  * writing files outside the job's temp folder (and outside the paths the call named, for tools that need "files")
  * reading denied paths (secrets, browser data, the admin helper's folder)
  * starting programs (subprocess, os.system, os.startfile ...)            -> cap "programs"
  * the network (socket connect / bind / DNS)                              -> cap "internet"
  * the registry, ctypes / native code                                     -> cap "system" (ctypes: never)
This is defence in depth, not a perfect jail: the real limits are the tier (the user's approval card), the static
check, the time limit and the probation period. A tool with cap "programs" can escape the hook through a child process
(that is why such a tool runs at tier exec, i.e. it asks).

Output: the LAST line on the real stdout is  \\x1eLUCY_RESULT {json}.
"""
import io
import json
import os
import sys
import traceback

MARK = "\x1eLUCY_RESULT "
_real_stdout = sys.stdout


def _emit(obj):
    _real_stdout.write("\n" + MARK + json.dumps(obj, ensure_ascii=False) + "\n")
    _real_stdout.flush()


def _norm(p):
    try:
        p = os.fspath(p)
    except TypeError:
        return None
    if isinstance(p, bytes):
        p = p.decode("utf-8", "replace")
    if not isinstance(p, str):
        return None
    return os.path.normcase(os.path.realpath(os.path.abspath(p)))


def _inside(p, roots):
    for r in roots:
        if p == r or p.startswith(r.rstrip("\\/") + os.sep):
            return True
    return False


def main():
    job = json.loads(sys.stdin.read() or "{}")
    caps = set(job.get("caps") or ())
    tmp = _norm(job["tmp"])
    write_ok = [tmp] + [x for x in (_norm(p) for p in job.get("write_paths") or ()) if x]
    if "files" not in caps:
        write_ok = [tmp]
    deny_read = [x for x in (_norm(p) for p in job.get("deny_read") or ()) if x]
    own = _norm(job["module"])                  # the tool's own file is always readable, wherever it is staged
    net, spawn, system = "internet" in caps, "programs" in caps, "system" in caps
    _W = getattr(os, "O_WRONLY", 1) | getattr(os, "O_RDWR", 2) | getattr(os, "O_APPEND", 8) | \
        getattr(os, "O_CREAT", 64) | getattr(os, "O_TRUNC", 512)
    blocked_mods = {"ctypes", "_ctypes", "cffi", "_cffi_backend"}
    if not spawn:
        blocked_mods |= {"_posixsubprocess", "_winapi", "pty", "multiprocessing"}
    if not system:
        blocked_mods |= {"winreg", "_winreg"}
    path_events = {"os.remove", "os.unlink", "os.rmdir", "os.mkdir", "os.chmod", "os.chown", "os.truncate", "os.utime",
                   "os.removexattr", "os.setxattr", "shutil.rmtree", "shutil.copyfile", "shutil.copymode",
                   "shutil.copystat", "shutil.copytree", "shutil.move", "shutil.chown", "os.chflags", "os.lchflags",
                   "os.lchmod", "os.mkfifo", "os.mknod"}
    two_path = {"os.rename", "os.replace", "os.link", "os.symlink"}
    spawn_events = ("os.system", "os.exec", "os.posix_spawn", "os.spawn", "os.startfile", "os.fork", "os.forkpty",
                    "subprocess.Popen", "os.kill", "os.killpg", "pty.spawn", "_winapi.CreateProcess",
                    "_winapi.TerminateProcess", "os.popen")
    net_events = ("socket.connect", "socket.bind", "socket.getaddrinfo", "socket.gethostbyname",
                  "socket.gethostbyaddr", "socket.getnameinfo", "socket.sendto", "socket.sendmsg",
                  "socket.gethostbyname_ex")

    def deny(why):
        raise PermissionError(f"[sandbox] blocked: {why}")

    def check_write(p, what):
        n = _norm(p)
        if n is None:
            return
        if not _inside(n, write_ok):
            deny(f"{what} {p} (this tool may only write in its temp folder"
                 + (" and the paths it was given)" if "files" in caps else "; it did not declare needs=files)"))

    def hook(event, args):
        if event == "open":
            path, mode, flags = (list(args) + [None, None, 0])[:3]
            if isinstance(path, int) or path is None:
                return
            n = _norm(path)
            if n and n != own and _inside(n, deny_read):
                deny(f"reading {path} (blocked location)")
            writing = (isinstance(mode, str) and any(c in mode for c in "wax+")) or (isinstance(flags, int) and flags & _W)
            if writing:
                check_write(path, "writing")
        elif event in path_events:
            if args:
                check_write(args[0], event)
        elif event in two_path:
            for a in args[:2]:
                check_write(a, event)
        elif event.startswith(spawn_events):
            if not spawn:
                deny(f"{event} (starting programs needs needs=programs)")
        elif event.startswith(net_events):
            if not net:
                deny(f"{event} (the internet needs needs=internet)")
        elif event == "import":
            name = str(args[0] or "").split(".")[0] if args else ""
            if name in blocked_mods:
                raise ImportError(f"[sandbox] blocked: import {name}")
        elif event.startswith("ctypes."):
            deny(event)
        elif event.startswith("winreg.") and not system:
            deny(f"{event} (the registry needs needs=system)")
        elif event in ("sys.settrace", "sys.setprofile", "sys._getframe"):
            if event != "sys._getframe":
                deny(event)
        elif event == "builtins.input":
            deny("input() (tools cannot ask questions)")

    # Everything the runner itself needs is imported above / here, BEFORE the hook.
    import importlib.util
    import types
    os.chdir(tmp)
    sys.addaudithook(hook)

    captured = io.StringIO()
    sys.stdout = captured
    try:
        spec = importlib.util.spec_from_file_location("lucy_user_tool", job["module"])
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        run = getattr(mod, "run", None)
        if not callable(run):
            raise TypeError("the code must define run(args)")
        if job.get("mode") == "test":
            ns = {"run": run, "tool": mod, "__name__": "lucy_user_tool_tests"}
            code = compile(job.get("tests") or "", "<tests>", "exec")
            exec(code, ns)                                      # noqa: S102 - top-level asserts run here
            names = sorted(k for k, v in ns.items() if k.startswith("test") and isinstance(v, types.FunctionType))
            failed = []
            for k in names:
                try:
                    ns[k]()
                except Exception as e:                          # noqa: BLE001
                    failed.append(f"{k}: {type(e).__name__}: {e}")
            if failed:
                _emit({"ok": False, "error": "tests failed: " + "; ".join(failed)[:1500],
                       "stdout": captured.getvalue()[-1500:]})
                return
            _emit({"ok": True, "result": f"tests passed ({len(names)} test functions + top-level asserts)",
                   "stdout": captured.getvalue()[-1500:]})
            return
        out = run(dict(job.get("args") or {}))
        if not isinstance(out, str):
            try:
                out = json.dumps(out, ensure_ascii=False, default=str)
            except (TypeError, ValueError):
                out = str(out)
        _emit({"ok": True, "result": out, "stdout": captured.getvalue()[-2000:]})
    except BaseException as e:                                  # noqa: BLE001 - everything is reported, nothing escapes
        tb = traceback.format_exc(limit=6)
        _emit({"ok": False, "error": f"{type(e).__name__}: {e}", "trace": tb[-2500:],
               "blocked": "[sandbox] blocked" in str(e), "stdout": captured.getvalue()[-1500:]})


if __name__ == "__main__":
    main()
