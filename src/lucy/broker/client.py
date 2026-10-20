"""Lucy's side of the admin broker: start it on demand (UAC prompt), talk to it, stop it.

Lucy itself never runs as administrator. The first admin op of a session starts server.py elevated (Windows shows its
UAC prompt AFTER Lucy's own approval card); the broker then stays up until it has been idle for `idle_s` or Lucy exits.
"""
from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
import threading
import time
from multiprocessing.connection import Client
from pathlib import Path
from typing import Callable, List, Optional

Launcher = Callable[[str, List[str], str], Optional[str]]       # (python, args, cwd) -> error text or None
SERVER = Path(__file__).resolve().parent / "server.py"


def uac_launch(python: str, args: List[str], cwd: str) -> Optional[str]:
    """Windows: ShellExecute 'runas' = the UAC prompt; hidden window. Returns an error text or None."""
    if os.name != "nt":
        return "the admin helper only exists on Windows"
    import ctypes
    exe = python
    if exe.lower().endswith("pythonw.exe"):
        exe = exe[:-len("pythonw.exe")] + "python.exe"
    rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, subprocess.list2cmdline(args), cwd, 0)
    if rc <= 32:
        return ("the Windows administrator prompt (UAC) was declined or failed" if rc == 5
                else f"could not start the admin helper (ShellExecute code {rc})")
    return None


class BrokerClient:
    def __init__(self, broker_dir: Path, root: Path, *, launcher: Optional[Launcher] = None,
                 python: Optional[str] = None, idle_s: float = 900, start_timeout: float = 120,
                 extra_args: Optional[List[str]] = None):
        self.dir, self.root = Path(broker_dir), Path(root)
        self.launcher = launcher or uac_launch
        self.python = python or sys.executable
        self.idle_s, self.start_timeout = idle_s, start_timeout
        self.extra_args = list(extra_args or [])
        self._secret: Optional[bytes] = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------------------------------------ connection
    def _port(self) -> Optional[int]:
        try:
            return int(json.loads((self.dir / "port.json").read_text(encoding="utf-8"))["port"])
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def _request(self, req: dict, timeout_s: float) -> dict:
        port = self._port()
        if port is None or self._secret is None:
            raise ConnectionError("broker not running")
        conn = Client(("127.0.0.1", port), authkey=self._secret)          # mutual HMAC: a fake broker fails here
        try:
            conn.send_bytes(json.dumps(req).encode("utf-8"))
            if not conn.poll(timeout_s):
                raise TimeoutError("the admin helper did not answer")
            return json.loads(conn.recv_bytes().decode("utf-8"))
        finally:
            conn.close()

    def running(self) -> bool:
        try:
            return bool(self._request({"op": "ping"}, 5).get("ok"))
        except Exception:                                               # noqa: BLE001
            return False

    def _start(self) -> Optional[str]:
        self.dir.mkdir(parents=True, exist_ok=True)
        for n in ("port.json", "error.txt", "secret"):
            try:
                (self.dir / n).unlink()
            except OSError:
                pass
        secret = secrets.token_bytes(32)
        sp = self.dir / "secret"
        sp.write_text(secret.hex(), encoding="utf-8")
        args = ["-I", "-S", str(SERVER), "--dir", str(self.dir), "--root", str(self.root),
                "--parent-pid", str(os.getpid()), "--idle", str(int(self.idle_s))] + self.extra_args
        err = self.launcher(self.python, args, str(self.root))
        if err:
            try:
                sp.unlink()
            except OSError:
                pass
            return err
        deadline = time.monotonic() + self.start_timeout
        while time.monotonic() < deadline:
            if (self.dir / "error.txt").exists():
                return (self.dir / "error.txt").read_text(encoding="utf-8", errors="replace")[:600]
            if self._port() is not None:
                self._secret = secret
                return None
            time.sleep(0.2)
        try:
            sp.unlink()
        except OSError:
            pass
        return "the admin helper did not start in time (UAC prompt not answered?)"

    def ensure(self) -> Optional[str]:
        with self._lock:
            if self._secret is not None and self.running():
                return None
            self._secret = None
            return self._start()

    # ------------------------------------------------------------------------------------------ calls
    def call(self, op: str, args: dict, *, dry_run: bool = False, wait: float = 100) -> dict:
        """{'done': True, 'result': text} | {'done': False, 'job': id, ...} | {'error': text}."""
        err = self.ensure()
        if err:
            return {"error": err}
        try:
            r = self._request({"op": op, "args": args, "dry_run": dry_run, "wait": wait}, wait + 30)
        except Exception as e:                                          # noqa: BLE001
            return {"error": f"lost contact with the admin helper ({e})"}
        return r if r.get("ok") else {"error": r.get("error") or "the admin helper refused"}

    def poll(self, job: str, wait: float = 100) -> dict:
        if self._secret is None:
            return {"error": "the admin helper is not running (was it restarted?); the job result is lost"}
        try:
            r = self._request({"op": "job.poll", "job": job, "wait": wait}, wait + 30)
        except Exception as e:                                          # noqa: BLE001
            return {"error": f"lost contact with the admin helper ({e})"}
        return r if r.get("ok") else {"error": r.get("error")}

    def stop(self) -> bool:
        try:
            self._request({"op": "shutdown"}, 5)
            return True
        except Exception:                                               # noqa: BLE001
            return False
        finally:
            self._secret = None

    def status(self) -> dict:
        return {"running": self._secret is not None and self.running(), "dir": self.dir.as_posix()}
