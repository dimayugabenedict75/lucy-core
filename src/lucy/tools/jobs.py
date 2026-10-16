"""Process runner behind the shell tool: timeouts, background jobs, incremental output, kill (whole process tree).

Uses subprocess.Popen + reader threads (not asyncio subprocesses) so it behaves the same on every event loop,
including Windows ones, and can never block the server: callers wait on a threading.Event via a worker thread.
Not a tool module (no SCHEMA/TOOLS), so registry discovery ignores it.
"""
from __future__ import annotations

import codecs
import itertools
import os
import signal
import subprocess
import sys
import threading
import time
from typing import Dict, List, Optional, Tuple

MAX_BUFFER = 2_000_000          # characters of output kept per job (the newest ones)
KEEP_FINISHED = 30              # finished jobs remembered for poll/list
FINISHED_TTL = 3600.0           # ...for at most this long (seconds)
_WIN = os.name == "nt"


class JobError(RuntimeError):
    """The process could not be started (missing shell, bad cwd...)."""


def kill_tree(proc: subprocess.Popen) -> None:
    """Stop the process and everything it started."""
    try:
        if _WIN:
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, timeout=15)
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except Exception:                                    # noqa: BLE001 - already gone / no permission: fall back
        pass
    try:
        proc.kill()
    except Exception:                                    # noqa: BLE001
        pass


class Job:
    def __init__(self, jid: int, argv: List[str], cwd: Optional[str], env: Optional[Dict[str, str]],
                 label: str, max_runtime: Optional[float]):
        self.id, self.label, self.argv = jid, label, argv
        self.started = time.time()
        self.ended: Optional[float] = None
        self.returncode: Optional[int] = None
        self.killed = False                              # stopped on request (kill action / Stop button)
        self.timed_out = False
        self.max_runtime = max_runtime
        self._buf = ""
        self._dropped = 0                                # characters that fell off the front of the buffer
        self._cursor = 0                                 # absolute offset already handed out by read_new()
        self._lock = threading.Lock()
        self._done = threading.Event()
        full_env = dict(os.environ)
        full_env.update({str(k): str(v) for k, v in (env or {}).items()})
        kwargs: dict = {}
        if _WIN:
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        else:
            kwargs["start_new_session"] = True           # own process group, so kill_tree gets the children too
        try:
            self.proc = subprocess.Popen(argv, cwd=cwd, env=full_env, stdin=subprocess.DEVNULL,   # never wait for input
                                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, **kwargs)
        except (OSError, ValueError) as e:
            raise JobError(str(e)) from e
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()
        threading.Thread(target=self._wait_loop, daemon=True).start()
        self._timer: Optional[threading.Timer] = None
        if max_runtime:
            self._timer = threading.Timer(max_runtime, self._on_timeout)
            self._timer.daemon = True
            self._timer.start()

    # -- background threads ---------------------------------------------------------------------------
    def _read_loop(self) -> None:
        dec = codecs.getincrementaldecoder("utf-8")(errors="replace")
        try:
            while True:
                chunk = self.proc.stdout.read1(65536)
                if not chunk:
                    break
                self._append(dec.decode(chunk))
            self._append(dec.decode(b"", final=True))
        except Exception:                                # noqa: BLE001 - pipe closed under us
            pass

    def _append(self, text: str) -> None:
        if not text:
            return
        with self._lock:
            self._buf += text
            if len(self._buf) > MAX_BUFFER:
                cut = len(self._buf) - MAX_BUFFER
                self._buf, self._dropped = self._buf[cut:], self._dropped + cut

    def _wait_loop(self) -> None:
        self.returncode = self.proc.wait()
        self._reader.join(timeout=3)
        self.ended = time.time()
        if self._timer:
            self._timer.cancel()
        self._done.set()

    def _on_timeout(self) -> None:
        if not self._done.is_set():
            self.timed_out = True
            kill_tree(self.proc)

    # -- API ---------------------------------------------------------------------------------------------
    @property
    def running(self) -> bool:
        return not self._done.is_set()

    def wait(self, timeout: Optional[float] = None) -> bool:
        """Block (in the caller's worker thread) until the job ends or `timeout` seconds pass. True = ended."""
        return self._done.wait(timeout)

    def kill(self) -> None:
        if not self._done.is_set():
            self.killed = True
            kill_tree(self.proc)

    def read_new(self, max_chars: int = 8000) -> Tuple[str, int]:
        """Output produced since the last call, newest `max_chars` of it -> (text, characters skipped before it)."""
        with self._lock:
            total = self._dropped + len(self._buf)
            start = max(self._cursor, self._dropped)
            skipped = start - self._cursor
            text = self._buf[start - self._dropped:]
            if max_chars and len(text) > max_chars:
                skipped += len(text) - max_chars
                text = text[-max_chars:]
            self._cursor = total
        return text, skipped

    def status(self) -> str:
        if self.running:
            return f"running {int(time.time() - self.started)}s"
        if self.timed_out:
            return f"timed out after {int(self.max_runtime or 0)}s and was killed"
        if self.killed:
            return "killed"
        return f"exit code {self.returncode}"


class JobManager:
    def __init__(self) -> None:
        self._jobs: Dict[int, Job] = {}
        self._ids = itertools.count(1)
        self._lock = threading.Lock()

    def start(self, argv: List[str], cwd: Optional[str] = None, env: Optional[Dict[str, str]] = None,
              label: str = "", max_runtime: Optional[float] = None) -> Job:
        self.prune()
        with self._lock:
            jid = next(self._ids)
        job = Job(jid, argv, cwd, env, label or " ".join(argv), max_runtime)
        with self._lock:
            self._jobs[jid] = job
        return job

    def get(self, jid) -> Optional[Job]:
        try:
            return self._jobs.get(int(jid))
        except (TypeError, ValueError):
            return None

    def all(self) -> List[Job]:
        return sorted(self._jobs.values(), key=lambda j: j.id)

    def prune(self) -> None:
        now = time.time()
        with self._lock:
            done = sorted((j for j in self._jobs.values() if not j.running), key=lambda j: j.ended or 0)
            for j in done:
                if now - (j.ended or now) > FINISHED_TTL:
                    self._jobs.pop(j.id, None)
            done = [j for j in done if j.id in self._jobs]
            for j in done[:max(0, len(done) - KEEP_FINISHED)]:
                self._jobs.pop(j.id, None)


default = JobManager()
