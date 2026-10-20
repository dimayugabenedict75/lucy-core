"""Lane B: changes to Lucy Core itself, gated. Constitution file (src/lucy/selfdev/ is hash-locked). Stdlib only, so
`python -m lucy.selfdev rollback` works even when the rest of Lucy will not import.

  start    copy the live tree (no runtime/, models, caches, secrets) to runtime/workspaces/<id>/ and remember the hash
           of every copied file (the BASE). Lucy edits the copy with her normal file tools; the live tree is untouched.
  submit   (the agent) compute the change set copy-vs-BASE. Refused at once if it touches a constitution file or a
           protected folder. Then, in a background thread: full test suite in the copy -> canary instance of the copy on
           another port -> smoke script (smoke.py). All green -> status "ready": a proposal with diff + results waits
           for the USER. Anything red -> "failed" (the live tree was never touched; nothing to roll back).
  approve  (the user, API/UI only) re-check constitution + conflicts (live file changed since BASE?), copy the current
           live versions to runtime/selfdev/releases/<rid>/before/, then write the new files. Restart Lucy to use it.
  rollback (the user, UI or rollback.bat) put the `before` files back and remove files the release added. Works
           without the server running.
State lives in runtime/selfdev/ (the safety layer refuses agent writes there); workspaces are agent-writable.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

EXCLUDE_DIRS = {"runtime", "cache", "models", ".venv", "venv", "env", ".git", "__pycache__", "node_modules",
                ".pytest_cache", ".mypy_cache", ".ruff_cache", "build", "dist", ".idea", ".vscode"}
EXCLUDE_FILES = {"google_client_secret.json", ".env", "token.json", "credentials.json"}
EXCLUDE_SUFFIX = (".gguf", ".sqlite", ".sqlite3", ".db", ".pyc", ".pyo", ".log", ".wav", ".mp3", ".zip", ".tmp",
                  ".bak", ".exe", ".dll")
MAX_FILE = 5 * 1024 * 1024
MAX_TOTAL = 300 * 1024 * 1024
MAX_OPEN = 3
KEEP_RELEASES = 10
TEST_TIMEOUT = float(os.environ.get("LUCY_LANEB_TEST_TIMEOUT", "1800") or 1800)
# Never changed through Lane B (besides every file the constitution lock lists): the safety/broker/selfdev packages,
# Lane A's folder, and the scripts the user double-clicks.
PROTECTED_PREFIXES = ("src/lucy/safety/", "src/lucy/broker/", "src/lucy/selfdev/", "src/lucy/tools_user/", ".core/constitution.lock")
PROTECTED_FILES = {"seal.bat", "rollback.bat", "src/lucy/tools/registry.py", "src/lucy/tools/_common.py",
                   "src/lucy/server/file_guard.py", ".core/permissions.yaml", ".core/mcp.json", ".core/personas/lucy.md"}


class LaneBError(ValueError):
    pass


def _sha_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _skip(rel_parts: Tuple[str, ...], name: str) -> bool:
    if any(p in EXCLUDE_DIRS for p in rel_parts[:-1]):
        return True
    if rel_parts and rel_parts[0] == "lucy_audio" and "build" in rel_parts:
        return True
    return name in EXCLUDE_FILES or name.lower().endswith(EXCLUDE_SUFFIX)


def scan(root: Path) -> Dict[str, str]:
    """{relative posix path: sha256} of the files Lane B works on."""
    root = Path(root)
    out: Dict[str, str] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in EXCLUDE_DIRS]
        rel_dir = Path(dirpath).relative_to(root)
        for fn in filenames:
            rel = (rel_dir / fn)
            parts = rel.parts
            if _skip(parts, fn):
                continue
            p = Path(dirpath) / fn
            try:
                if p.is_symlink() or p.stat().st_size > MAX_FILE:
                    continue
                out[rel.as_posix()] = _sha_file(p)
            except OSError:
                continue
    return out


def protected(rel: str, locked: Optional[set] = None) -> bool:
    rel = rel.replace("\\", "/")
    while rel.startswith("./"):
        rel = rel[2:]                                   # NOT lstrip("./"): that would eat the dot of ".core/"
    low = rel.lower()
    if rel in PROTECTED_FILES or (locked and rel in locked):
        return True
    return any(low.startswith(p.lower()) for p in PROTECTED_PREFIXES)


def locked_files(root: Path) -> set:
    """Relative paths of every file the constitution lock covers (falls back to the built-in list)."""
    try:
        data = json.loads((Path(root) / ".core" / "constitution.lock").read_text(encoding="utf-8"))
        return {str(k) for k in (data.get("files") or {})}
    except (OSError, ValueError, AttributeError):
        return set()


class LaneB:
    def __init__(self, *, root: Path, state_dir: Path, workspaces: Path, python: Optional[str] = None,
                 run_tests: Optional[Callable[[Path, Callable[[str], None]], Tuple[bool, str]]] = None,
                 run_canary: Optional[Callable[[Path, Callable[[str], None]], Tuple[bool, str]]] = None,
                 clock: Callable[[], float] = time.time):
        self.root = Path(root).resolve()
        self.state_dir = Path(state_dir)
        self.props_dir = self.state_dir / "proposals"
        self.rel_dir = self.state_dir / "releases"
        self.workspaces = Path(workspaces)
        self.python = python or sys.executable
        self._run_tests = run_tests or self._default_tests
        self._run_canary = run_canary or self._default_canary
        self.clock = clock
        self._lock = threading.RLock()
        self._threads: Dict[str, threading.Thread] = {}

    # ============================================================================== records
    def _ppath(self, pid: str) -> Path:
        if not re.fullmatch(r"[a-z0-9\-]{4,40}", str(pid or "")):
            raise LaneBError(f"unknown change id {pid!r}")
        return self.props_dir / f"{pid}.json"

    def get(self, pid: str) -> dict:
        try:
            return json.loads(self._ppath(pid).read_text(encoding="utf-8"))
        except OSError:
            raise LaneBError(f"unknown change id {pid!r}") from None

    def _put(self, rec: dict) -> dict:
        self.props_dir.mkdir(parents=True, exist_ok=True)
        rec["updated"] = self.clock()
        p = self._ppath(rec["id"])
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_text(json.dumps(rec, indent=1, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, p)
        return rec

    def _update(self, pid: str, **kw) -> dict:
        with self._lock:
            rec = self.get(pid)
            rec.update(kw)
            return self._put(rec)

    def _event(self, pid: str, text: str) -> None:
        with self._lock:
            rec = self.get(pid)
            ev = list(rec.get("events") or [])
            ev.append(time.strftime("%H:%M:%S", time.localtime(self.clock())) + " " + text[:300])
            rec["events"] = ev[-40:]
            self._put(rec)

    def list(self, include_closed: bool = True) -> List[dict]:
        out = []
        if self.props_dir.is_dir():
            for p in self.props_dir.glob("*.json"):
                try:
                    rec = json.loads(p.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                if include_closed or rec.get("status") in ("editing", "checking", "ready"):
                    out.append({k: v for k, v in rec.items() if k not in ("base", "diff")})
        return sorted(out, key=lambda r: -float(r.get("created", 0)))

    # ============================================================================== agent: start / submit / discard
    def start(self, title: str, reason: str, session: Optional[str] = None) -> dict:
        title, reason = " ".join(str(title or "").split())[:120], " ".join(str(reason or "").split())[:400]
        if not title or not reason:
            raise LaneBError("give a short title and a reason (what you want to improve in Lucy Core, and why)")
        with self._lock:
            open_ = [r for r in self.list(False) if r.get("status") in ("editing", "checking")]
            if len(open_) >= MAX_OPEN:
                raise LaneBError(f"{len(open_)} changes are already open ({', '.join(r['id'] for r in open_)}); "
                                 "submit or discard one first")
            pid = time.strftime("%m%d", time.localtime(self.clock())) + "-" + secrets.token_hex(3)
            ws = self.workspaces / pid
            base = scan(self.root)
            total = 0
            for rel in base:
                total += (self.root / rel).stat().st_size
                if total > MAX_TOTAL:
                    raise LaneBError("Lucy Core is too big to copy for a change (over 300 MB without runtime/models)")
            for rel in base:
                dst = ws / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(self.root / rel, dst)
            rec = {"id": pid, "title": title, "reason": reason, "session": session, "status": "editing",
                   "workspace": ws.as_posix(), "created": self.clock(), "base": base, "events": [],
                   "changes": {}, "checks": {}}
            self._put(rec)
            self._event(pid, f"copy made ({len(base)} files)")
            return rec

    def changes(self, pid: str) -> Dict[str, List[str]]:
        rec = self.get(pid)
        ws = Path(rec["workspace"])
        if not ws.is_dir():
            raise LaneBError(f"the copy for {pid} is gone")
        now, base = scan(ws), rec["base"]
        return {"changed": sorted(k for k in now if k in base and now[k] != base[k]),
                "added": sorted(k for k in now if k not in base),
                "deleted": sorted(k for k in base if k not in now)}

    def diff_text(self, pid: str, ch: Optional[dict] = None, limit: int = 400_000) -> str:
        rec = self.get(pid)
        ws = Path(rec["workspace"])
        ch = ch or self.changes(pid)
        out: List[str] = []
        for rel in ch["changed"] + ch["added"] + ch["deleted"]:
            a = self._text(self.root / rel) if rel not in ch["added"] else ""
            b = self._text(ws / rel) if rel not in ch["deleted"] else ""
            if a is None or b is None:
                out.append(f"--- {rel}\n(binary file {'added' if rel in ch['added'] else 'changed'})\n")
                continue
            out.extend(difflib.unified_diff(a.splitlines(True), b.splitlines(True), f"live/{rel}", f"new/{rel}", n=3))
            if sum(len(x) for x in out) > limit:
                out.append("\n...[diff cut: too long]\n")
                break
        return "".join(out)

    @staticmethod
    def _text(p: Path) -> Optional[str]:
        try:
            b = p.read_bytes()
        except OSError:
            return ""
        if b"\x00" in b[:4096]:
            return None
        return b.decode("utf-8", "replace").replace("\r\n", "\n")

    def _forbidden(self, ch: dict) -> List[str]:
        locked = locked_files(self.root)
        return [r for r in ch["changed"] + ch["added"] + ch["deleted"] if protected(r, locked)]

    def submit(self, pid: str, background: bool = True) -> dict:
        with self._lock:
            rec = self.get(pid)
            if rec["status"] == "checking":
                raise LaneBError(f"{pid} is already being checked; call status")
            if rec["status"] not in ("editing", "failed"):
                raise LaneBError(f"{pid} is {rec['status']}; only an open change can be submitted")
            ch = self.changes(pid)
            n = sum(len(v) for v in ch.values())
            if not n:
                raise LaneBError(f"nothing changed in the copy yet ({rec['workspace']}); edit files there first")
            bad = self._forbidden(ch)
            if bad:
                self._event(pid, "refused: touches protected files " + ", ".join(bad[:6]))
                raise LaneBError("refused: these are constitution / protected files that only the user may change: "
                                 + ", ".join(bad[:8]) + ". Undo those edits in the copy (or discard the change).")
            ws = Path(rec["workspace"])
            frozen = {rel: _sha_file(ws / rel) for rel in ch["changed"] + ch["added"]}
            rec = self._update(pid, status="checking", changes=ch, frozen=frozen,
                               checks={"stage": "tests", "started": self.clock()}, diff=self.diff_text(pid, ch))
        self._event(pid, f"submitted: {len(ch['changed'])} changed, {len(ch['added'])} added, {len(ch['deleted'])} deleted")
        if background:
            t = threading.Thread(target=self._pipeline, args=(pid,), daemon=True, name=f"laneb-{pid}")
            self._threads[pid] = t
            t.start()
        else:
            self._pipeline(pid)
        return self.get(pid)

    def wait(self, pid: str, timeout: float = 60.0) -> dict:
        t = self._threads.get(pid)
        if t:
            t.join(timeout)
        return self.get(pid)

    def _pipeline(self, pid: str) -> None:
        rec = self.get(pid)
        ws = Path(rec["workspace"])
        checks = dict(rec.get("checks") or {})
        try:
            ok, out = self._run_tests(ws, lambda s: self._event(pid, s))
            checks.update(tests_ok=ok, tests=out[-4000:])
            if not ok:
                checks["stage"] = "done"
                self._update(pid, status="failed", checks=checks, failure="tests failed in the copy")
                self._event(pid, "tests FAILED: live Lucy was not touched")
                return
            self._event(pid, "tests passed")
            checks["stage"] = "canary"
            self._update(pid, checks=checks)
            ok, out = self._run_canary(ws, lambda s: self._event(pid, s))
            checks.update(canary_ok=ok, canary=out[-4000:], stage="done")
            if not ok:
                self._update(pid, status="failed", checks=checks, failure="canary / smoke check failed")
                self._event(pid, "canary FAILED: live Lucy was not touched")
                return
            # Final re-check: the submitted files are still exactly what was tested.
            moved = self._drift(self.get(pid))
            if moved:
                self._update(pid, status="failed", checks=checks,
                             failure="files of the change were edited while it was being checked: " + ", ".join(moved[:4]))
                self._event(pid, "copy changed during the checks: submit again")
                return
            self._update(pid, status="ready", checks=checks, failure="")
            self._event(pid, "all checks passed: waiting for the user's approval")
        except Exception as e:                                      # noqa: BLE001 - a crash is a failed check
            checks["stage"] = "done"
            self._update(pid, status="failed", checks=checks, failure=f"checks crashed: {type(e).__name__}: {e}")
            self._event(pid, f"checks crashed: {e}")

    def _drift(self, rec: dict) -> List[str]:
        """Submitted files whose content is no longer what was checked (or submitted deletions that came back)."""
        ws = Path(rec["workspace"])
        out = []
        for rel, sha in (rec.get("frozen") or {}).items():
            p = ws / rel
            if not p.exists() or _sha_file(p) != sha:
                out.append(rel)
        for rel in (rec.get("changes") or {}).get("deleted", []):
            if (ws / rel).exists():
                out.append(rel)
        return out

    def discard(self, pid: str, by: str = "agent") -> dict:
        with self._lock:
            rec = self.get(pid)
            if rec["status"] in ("applied", "rolled_back"):
                raise LaneBError(f"{pid} is {rec['status']}; use rollback for applied changes")
            if rec["status"] == "checking":
                raise LaneBError(f"{pid} is being checked right now; wait until it finishes")
            shutil.rmtree(rec["workspace"], ignore_errors=True)
            rec = self._update(pid, status="discarded" if by == "agent" else "rejected", closed_by=by)
            self._event(pid, f"{rec['status']} by {by}")
            return rec

    # ============================================================================== user: approve / rollback
    def approve(self, pid: str, by: str = "user") -> dict:
        with self._lock:
            rec = self.get(pid)
            if rec["status"] != "ready":
                raise LaneBError(f"{pid} is {rec['status']}: only a change that passed every check can be applied")
            ch = rec["changes"]                       # exactly the set that was checked (files tests left behind are ignored)
            moved = self._drift(rec)
            if moved:
                self._update(pid, status="editing", failure="edited after the checks: " + ", ".join(moved[:4]))
                raise LaneBError("the copy changed after the checks (" + ", ".join(moved[:4]) + "); submit it again")
            bad = self._forbidden(ch)
            if bad:
                raise LaneBError("refused: touches protected files: " + ", ".join(bad[:8]))
            base, ws = rec["base"], Path(rec["workspace"])
            conflicts = []
            for rel in ch["changed"] + ch["deleted"]:
                live = self.root / rel
                if not live.exists() or _sha_file(live) != base.get(rel):
                    conflicts.append(rel)
            for rel in ch["added"]:
                if (self.root / rel).exists():
                    conflicts.append(rel)
            if conflicts:
                self._update(pid, status="failed", failure="live files changed since the copy: " + ", ".join(conflicts[:6]))
                raise LaneBError("conflict: these live files changed since the copy was made: " + ", ".join(conflicts[:8])
                                 + ". Start a new change from the current version.")
            rid = time.strftime("%Y%m%d-%H%M%S", time.localtime(self.clock())) + "-" + pid
            rdir = self.rel_dir / rid
            before = rdir / "before"
            for rel in ch["changed"] + ch["deleted"]:
                dst = before / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(self.root / rel, dst)
            manifest = {"id": rid, "proposal": pid, "title": rec["title"], "reason": rec["reason"], "by": by,
                        "applied": self.clock(), "status": "applying", "changes": ch,
                        "after": {rel: _sha_file(ws / rel) for rel in ch["changed"] + ch["added"]}}
            self._write_manifest(rdir, manifest)
            done: List[str] = []
            try:
                for rel in ch["changed"] + ch["added"]:
                    dst = self.root / rel
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    tmp = dst.with_name(dst.name + ".laneb-tmp")
                    shutil.copy2(ws / rel, tmp)
                    os.replace(tmp, dst)
                    done.append(rel)
                for rel in ch["deleted"]:
                    (self.root / rel).unlink(missing_ok=True)
                    done.append(rel)
            except OSError as e:
                manifest["status"] = "applying"
                self._write_manifest(rdir, manifest)
                self.rollback(rid, by="auto")
                self._update(pid, status="failed", failure=f"could not write {e}; rolled back")
                raise LaneBError(f"writing the new files failed ({e}); everything was put back") from e
            manifest["status"] = "applied"
            self._write_manifest(rdir, manifest)
            self._prune_releases()
            rec = self._update(pid, status="applied", release=rid, closed_by=by)
            self._event(pid, f"applied by {by} as release {rid}: restart Lucy to load it")
            shutil.rmtree(ws, ignore_errors=True)
            return rec

    def _write_manifest(self, rdir: Path, m: dict) -> None:
        rdir.mkdir(parents=True, exist_ok=True)
        tmp = rdir / "manifest.json.tmp"
        tmp.write_text(json.dumps(m, indent=1, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, rdir / "manifest.json")

    def releases(self) -> List[dict]:
        out = []
        if self.rel_dir.is_dir():
            for d in self.rel_dir.iterdir():
                try:
                    out.append(json.loads((d / "manifest.json").read_text(encoding="utf-8")))
                except (OSError, ValueError):
                    continue
        return sorted(out, key=lambda m: -float(m.get("applied", 0)))

    def rollback(self, rid: Optional[str] = None, by: str = "user") -> dict:
        """Put the previous files back. rid=None -> the newest applied release."""
        with self._lock:
            rels = self.releases()
            if rid is None:
                cand = [m for m in rels if m.get("status") in ("applied", "applying")]
                if not cand:
                    raise LaneBError("there is no applied change to roll back")
                m = cand[0]
            else:
                m = next((x for x in rels if x.get("id") == rid), None)
                if m is None:
                    raise LaneBError(f"unknown release {rid!r}")
                if m.get("status") == "rolled_back":
                    raise LaneBError(f"{rid} was already rolled back")
                newer = [x for x in rels if x.get("status") == "applied" and float(x.get("applied", 0)) > float(m.get("applied", 0))
                         and set(sum(x["changes"].values(), [])) & set(sum(m["changes"].values(), []))]
                if newer:
                    raise LaneBError(f"roll back the newer release {newer[0]['id']} first (it changes the same files)")
            rdir = self.rel_dir / m["id"]
            ch = m["changes"]
            for rel in ch["changed"] + ch["deleted"]:
                src = rdir / "before" / rel
                if src.exists():
                    dst = self.root / rel
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    tmp = dst.with_name(dst.name + ".laneb-tmp")
                    shutil.copy2(src, tmp)
                    os.replace(tmp, dst)
            for rel in ch["added"]:
                (self.root / rel).unlink(missing_ok=True)
            m.update(status="rolled_back", rolled_back=self.clock(), rolled_back_by=by)
            self._write_manifest(rdir, m)
            try:
                self._update(m["proposal"], status="rolled_back")
            except LaneBError:
                pass
            return m

    def _prune_releases(self) -> None:
        rels = self.releases()
        for m in rels[KEEP_RELEASES:]:
            shutil.rmtree(self.rel_dir / m["id"], ignore_errors=True)

    # ============================================================================== default checks
    def _default_tests(self, ws: Path, log: Callable[[str], None]) -> Tuple[bool, str]:
        log("running the full test suite in the copy")
        env = {k: v for k, v in os.environ.items() if not k.startswith("LUCY_") and k not in ("DEV_API_KEY", "PYTHONPATH")}
        env.update({"PYTHONPATH": str(ws / "src"), "LUCY_ROOT": str(ws), "LUCY_REFLECT": "0", "LUCY_CANARY": "1",
                    "PYTHONDONTWRITEBYTECODE": "1"})
        flags = 0x08000000 if sys.platform == "win32" else 0
        try:
            p = subprocess.run([self.python, "-m", "pytest", "tests", "-q", "-p", "no:cacheprovider"], cwd=str(ws), env=env,
                               capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=TEST_TIMEOUT,
                               creationflags=flags)
        except subprocess.TimeoutExpired:
            return False, f"the test suite did not finish within {int(TEST_TIMEOUT)} s"
        except OSError as e:
            return False, f"could not start the tests: {e}"
        out = (p.stdout or "") + ("\n" + p.stderr if p.stderr else "")
        lines = [ln for ln in out.splitlines() if ln.strip()]
        return p.returncode == 0, "\n".join(lines[-60:])

    def _default_canary(self, ws: Path, log: Callable[[str], None]) -> Tuple[bool, str]:
        from . import smoke
        return smoke.run_canary(ws, python=self.python, log=log)
