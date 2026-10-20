"""Lucy's admin broker: a small ELEVATED helper that runs the fixed op menu in ops.py for Lucy, nothing else.

Started on demand by client.py through a UAC prompt, as   python -I -S server.py --dir <runtime/broker> --root <Lucy Core>
(-I -S: no site-packages, no PYTHON* environment variables: only the standard library and these locked files run as admin).

  * Talks ONLY on 127.0.0.1 with mutual HMAC authentication (multiprocessing.connection authkey = a 32-byte secret Lucy
    writes to <dir>/secret right before launching; the broker reads and deletes it).
  * Refuses to start if its own files differ from Lucy's constitution.lock (so an edited broker never runs as admin).
  * Serves the ops menu only (ops.OPS), re-checks every argument and its own deny lists (ops.Guard, critical
    processes/services, protected registry keys), and logs every op to <dir>/broker.log.
  * Exits when idle (default 15 min) or when the Lucy process that started it is gone.

Why TCP loopback and not a named pipe: a pipe created by an elevated process is not writable by Lucy's normal-user
process without a custom security descriptor (pywin32 or raw ctypes); loopback + mutual HMAC is stdlib-only and testable.
Constitution file (hash-locked, together with ops.py).
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import threading
import time
from multiprocessing.connection import Listener

HERE = os.path.dirname(os.path.abspath(__file__))
try:
    from . import ops                         # imported as lucy.broker.server (tests, the client's status checks)
except ImportError:                           # run as a script by the UAC launch: -I leaves this (locked) folder out
    if HERE not in sys.path:
        sys.path.insert(0, HERE)
    import ops  # noqa: E402

MAX_WAIT = 110.0


def _hash(path: str) -> str:
    with open(path, "rb") as f:
        return hashlib.sha256(f.read().replace(b"\r\n", b"\n")).hexdigest()


def verify_self(root: str) -> str:
    """'' if every broker file matches .core/constitution.lock, else why not."""
    try:
        with open(os.path.join(root, ".core", "constitution.lock"), encoding="utf-8") as f:
            locked = (json.load(f) or {}).get("files") or {}
    except (OSError, ValueError) as e:
        return f"cannot read constitution.lock ({e})"
    rel_dir = os.path.relpath(HERE, root).replace("\\", "/")
    for name in sorted(os.listdir(HERE)):
        if not name.endswith(".py"):
            continue
        key = f"{rel_dir}/{name}"
        if locked.get(key) != _hash(os.path.join(HERE, name)):
            return f"{key} is not sealed (changed or new); run seal.bat if YOU changed it"
    return ""


def _alive(pid: int) -> bool:
    if pid <= 0:
        return True
    if os.name == "nt":
        import ctypes
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(0x1000, False, pid)                  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        code = ctypes.c_ulong()
        ok = k32.GetExitCodeProcess(h, ctypes.byref(code))
        k32.CloseHandle(h)
        return bool(ok) and code.value == 259                    # STILL_ACTIVE
    try:
        os.kill(pid, 0)                                          # POSIX only: signal 0 = "does it exist" (on Windows it would KILL)
        return True
    except OSError:
        return False


class Broker:
    def __init__(self, secret: bytes, *, root: str, log_path: str = "", parent_pid: int = 0, idle_s: float = 900,
                 runner: ops.Runner = ops.default_runner, guard: "ops.Guard | None" = None):
        self.secret, self.root, self.log_path = secret, root, log_path
        self.parent_pid, self.idle_s = parent_pid, idle_s
        self.runner = runner
        self.guard = guard or ops.Guard(root)
        self.jobs = ops.Jobs()
        self.last = time.monotonic()
        self.stopping = threading.Event()
        self.listener = Listener(("127.0.0.1", 0), authkey=secret)

    @property
    def port(self) -> int:
        return self.listener.address[1]

    def _log(self, **kw) -> None:
        if not self.log_path:
            return
        try:
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps({"ts": time.strftime("%Y-%m-%d %H:%M:%S"), **kw}, ensure_ascii=False, default=str)[:2000] + "\n")
        except OSError:
            pass

    def handle(self, req: dict) -> dict:
        op = str(req.get("op") or "")
        self.last = time.monotonic()
        wait = max(0.0, min(MAX_WAIT, float(req.get("wait") or 0)))
        if op == "ping":
            return {"ok": True, "pid": os.getpid()}
        if op == "shutdown":
            self.stopping.set()
            return {"ok": True}
        if op == "job.poll":
            r = self.jobs.wait(str(req.get("job") or ""), wait)
            self._log(op=op, job=req.get("job"), done=r.get("done"))
            return {"ok": True, **r}
        if op not in ops.OPS:
            self._log(op=op, refused="not on the menu")
            return {"ok": False, "error": f"operation {op!r} is not on the broker's menu"}
        args = req.get("args") if isinstance(req.get("args"), dict) else {}
        ctx = ops.Ctx(runner=self.runner, guard=self.guard, dry_run=bool(req.get("dry_run")), elevated=True,
                      protect_pids=(self.parent_pid,))
        self._log(op=op, args=args, dry_run=ctx.dry_run)
        jid = self.jobs.start(lambda: ops.run(op, args, ctx), op)
        r = self.jobs.wait(jid, wait)
        if r.get("done"):
            self._log(op=op, result=str(r.get("result"))[:300])
        return {"ok": True, **r}

    def _serve_conn(self, conn) -> None:
        try:
            while not self.stopping.is_set():
                try:
                    raw = conn.recv_bytes(1 << 20)
                except (EOFError, OSError):
                    return
                try:
                    req = json.loads(raw.decode("utf-8"))
                    resp = self.handle(req if isinstance(req, dict) else {})
                except Exception as e:                           # noqa: BLE001 - a bad request never kills the broker
                    resp = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                conn.send_bytes(json.dumps(resp, default=str).encode("utf-8"))
        finally:
            conn.close()

    def _watchdog(self) -> None:
        while not self.stopping.is_set():
            time.sleep(1.0)
            busy = any(not j["done"].is_set() for j in list(self.jobs._jobs.values()))
            if (not busy and time.monotonic() - self.last > self.idle_s) or not _alive(self.parent_pid):
                self._log(event="exit", reason="idle" if _alive(self.parent_pid) else "lucy gone")
                self.stopping.set()
        try:                                                     # unblock accept()
            from multiprocessing.connection import Client
            Client(("127.0.0.1", self.port), authkey=self.secret).close()
        except Exception:                                        # noqa: BLE001
            pass

    def serve_forever(self) -> None:
        threading.Thread(target=self._watchdog, daemon=True, name="broker-watchdog").start()
        self._log(event="start", pid=os.getpid(), port=self.port, parent=self.parent_pid)
        while not self.stopping.is_set():
            try:
                conn = self.listener.accept()
            except Exception:                                    # noqa: BLE001 - failed auth etc.: keep serving
                self._log(event="rejected_connection")
                continue
            if self.stopping.is_set():
                conn.close()
                break
            threading.Thread(target=self._serve_conn, args=(conn,), daemon=True).start()
        self.listener.close()


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True)
    ap.add_argument("--root", required=True)
    ap.add_argument("--parent-pid", type=int, default=0)
    ap.add_argument("--idle", type=float, default=900)
    ap.add_argument("--dev-no-verify", action="store_true")     # tests on Linux only (ignored on Windows)
    a = ap.parse_args(argv)
    d = a.dir

    def fail(msg: str) -> int:
        try:
            with open(os.path.join(d, "error.txt"), "w", encoding="utf-8") as f:
                f.write(msg)
        except OSError:
            pass
        return 2
    if os.name == "nt" or not a.dev_no_verify:
        why = verify_self(a.root)
        if why:
            return fail("The admin helper refused to start: " + why)
    sp = os.path.join(d, "secret")
    try:
        with open(sp, encoding="utf-8") as f:
            secret = bytes.fromhex(f.read().strip())
        os.remove(sp)
    except (OSError, ValueError) as e:
        return fail(f"no secret from Lucy ({e})")
    if len(secret) < 16:
        return fail("secret too short")
    b = Broker(secret, root=a.root, log_path=os.path.join(d, "broker.log"), parent_pid=a.parent_pid, idle_s=a.idle)
    tmp = os.path.join(d, "port.json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"port": b.port, "pid": os.getpid()}, f)
    os.replace(tmp, os.path.join(d, "port.json"))
    try:
        b.serve_forever()
    finally:
        try:
            os.remove(os.path.join(d, "port.json"))
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
