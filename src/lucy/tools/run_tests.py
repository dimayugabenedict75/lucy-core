"""run_tests: run a project's test suite and hand the model a SHORT list of what failed, not 500 lines of log.

The framework is detected (pytest, npm test, dotnet test). There is deliberately no free-form `command` argument:
the safety policy inspects shell commands only for run_shell_command, so an arbitrary command here would sidestep
those rules. `target` / `filter` are passed to the test program as separate argv items (never through a shell) and may
not start with '-'.

The work is done by the same job runner as the shell tool (jobs.py): output is captured, Stop kills the process tree, and
a run that outlasts the tool timeout keeps going as a background job that run_shell_command(action='poll') can read.
"""
from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path
from typing import List, Optional, Tuple

from . import _code, jobs

DEFAULT_MAX_RUNTIME = 1800          # a test run is killed after 30 minutes
_LOOP_MARGIN = 10                   # the agent loop abandons a tool after LUCY_TOOL_TIMEOUT: stop waiting a bit before
OUT_CHARS = 3500

SCHEMA = {
    "type": "function",
    "function": {
        "name": "run_tests",
        "description": ("Run the project's tests (pytest, npm test or dotnet test is detected automatically) and get a short "
                        "summary of what failed. If it takes long it keeps running as a job: check it with run_shell_command "
                        "action 'poll'."),
        "parameters": {
            "type": "object",
            "properties": {
                "cwd": {"type": "string", "description": "Project folder (default: the Lucy Core folder)."},
                "target": {"type": "string", "description": "Only this test file, folder or test id (optional)."},
                "filter": {"type": "string", "description": "Only tests whose name contains this (optional)."},
            },
            "required": [],
        },
    },
}
TIER = "exec"
LABEL = "run"


# ------------------------------------------------------------------------------------------------ command
def build_command(kind: str, cwd: Path, target: Optional[str], flt: Optional[str]) -> List[str]:
    if kind == "pytest":
        argv = [_code.python_for(cwd), "-m", "pytest", "-q", "--tb=short", "--color=no", "-p", "no:cacheprovider"]
        if flt:
            argv += ["-k", flt]
        return argv + ([target] if target else [])
    if kind == "npm":
        argv = [_code.npm_exe(), "test"]
        extra = (["-t", flt] if flt else []) + ([target] if target else [])
        return argv + (["--"] + extra if extra else [])
    if kind == "dotnet":
        argv = ["dotnet", "test", "--nologo"] + ([target] if target else [])
        return argv + (["--filter", flt] if flt else [])
    raise ValueError(kind)


# ------------------------------------------------------------------------------------------------ condensing
_PY_SUMMARY = re.compile(r"^=*\s*((?:\d+ (?:passed|failed|errors?|skipped|deselected|xfailed|xpassed|warnings?)(?:, )?)+|no tests ran)"
                         r"(?: in [\d.]+s.*?)?\s*=*$")
_PY_SECTION = re.compile(r"^_{3,} (.+?) _{3,}$")
_PY_LOC = re.compile(r"^(\S+\.py):(\d+)")


def _pytest_summary(lines: List[str]) -> str:
    for ln in reversed(lines):
        m = _PY_SUMMARY.match(ln.strip())
        if m:
            tail = re.search(r" in ([\d.]+s)", ln)
            return m.group(1).strip() + (f" in {tail.group(1)}" if tail else "")
    return ""


def _condense_pytest(text: str) -> str:
    lines = text.splitlines()
    summary = _pytest_summary(lines)
    short = [l.strip() for l in lines if l.startswith(("FAILED ", "ERROR "))][:12]
    sections: List[Tuple[str, List[str]]] = []
    cur = None
    in_failures = False
    for ln in lines:
        if re.match(r"^=+ (FAILURES|ERRORS) =+$", ln):
            in_failures = True
            continue
        if in_failures and re.match(r"^=+ .* =+$", ln):
            in_failures = False
        if in_failures:
            m = _PY_SECTION.match(ln)
            if m:
                cur = (m.group(1), [])
                sections.append(cur)
            elif cur is not None:
                cur[1].append(ln)
    out: List[str] = []
    if short:
        out.append("Failed:\n" + "\n".join("  " + s[:200] for s in short))
    for name, body in sections[:5]:
        errs = [l[1:].strip() for l in body if l.startswith("E ")][:4]
        locs = [m.group(0) for m in (_PY_LOC.match(l) for l in body) if m]
        out.append(f"-- {name}" + (f"  (at {locs[-1]})" if locs else "") + ("\n   " + "\n   ".join(e[:200] for e in errs) if errs else ""))
    if len(sections) > 5:
        out.append(f"... and {len(sections) - 5} more failing test(s)")
    return (summary + ("\n" if summary and out else "") + "\n".join(out)).strip()


def _condense_npm(text: str) -> str:
    lines = text.splitlines()
    summary = [l.strip() for l in lines if re.match(r"^\s*(Tests|Test Suites|Test Files):", l)]
    out: List[str] = []
    i = 0
    while i < len(lines) and len(out) < 40:
        ln = lines[i]
        if ln.startswith("FAIL ") or (ln.strip().startswith(("●", "✕", "×")) and "›" in ln):
            out.append(ln.strip()[:200])
            for k in range(1, 7):
                if i + k < len(lines) and lines[i + k].strip():
                    out.append("   " + lines[i + k].strip()[:200])
            i += 7
            continue
        i += 1
    return ("\n".join(summary) + ("\n" if summary and out else "") + "\n".join(out)).strip()


def _condense_dotnet(text: str) -> str:
    lines = text.splitlines()
    out: List[str] = []
    for i, ln in enumerate(lines):
        s = ln.strip()
        if s.startswith("Failed ") and "[" in s:
            out.append(s[:200])
        elif s.startswith("Error Message:") and i + 1 < len(lines):
            out.append("   " + lines[i + 1].strip()[:200])
        elif re.match(r"^(Passed|Failed)!\s+-", s):
            out.insert(0, s[:200])
    return "\n".join(out[:30]).strip()


def condense(kind: str, text: str, returncode: Optional[int]) -> Tuple[bool, str]:
    """-> (passed, text for the model). Always ends up non-empty: falls back to the tail of the raw output."""
    passed = returncode == 0
    body = {"pytest": _condense_pytest, "npm": _condense_npm, "dotnet": _condense_dotnet}[kind](text)
    if not body:
        body = _code.tail_lines(text, 25, 2000) or "[no output]"
    if passed and kind == "pytest":
        body = _pytest_summary(text.splitlines()) or body
    head = f"Tests PASSED ({kind})" if passed else f"Tests FAILED ({kind}, exit code {returncode})"
    return passed, f"{head}\n{body}"[:OUT_CHARS]


_NOT_INSTALLED = [
    ("pytest", re.compile(r"No module named '?pytest'?", re.I)),
]


# ------------------------------------------------------------------------------------------------ running
def plan(args: dict) -> Tuple[Optional[dict], str]:
    """Validate the arguments and work out what to run -> ({kind, cwd, argv, label}, "") or (None, error)."""
    cwd, err = _code.resolve_dir(args.get("cwd"))
    if cwd is None:
        return None, err
    kind = _code.detect_test_kind(cwd)
    if kind is None:
        return None, (f"Error: no tests found in {cwd.as_posix()}: I looked for pytest, npm test and dotnet test. "
                      "If they live in a sub-folder, pass it as cwd; otherwise run your test command with run_shell_command.")
    target, e1 = _code.safe_target(args.get("target"))
    flt, e2 = _code.safe_target(args.get("filter"))
    if e1 or e2:
        return None, e1 or e2
    if kind == "dotnet" and shutil_which("dotnet") is None:
        return None, "Error: dotnet is not installed (or not on PATH), so these tests cannot run."
    if kind == "npm" and shutil_which(_code.npm_exe()) is None:
        return None, "Error: npm is not installed (or not on PATH), so these tests cannot run."
    argv = build_command(kind, cwd, target, flt)
    return {"kind": kind, "cwd": cwd, "argv": argv, "label": "tests: " + " ".join(argv[-4:])}, ""


def shutil_which(name: str) -> Optional[str]:
    return _code.which(name)


def start(p: dict, max_runtime: float = DEFAULT_MAX_RUNTIME) -> jobs.Job:
    env = {"PYTHONUNBUFFERED": "1", "CI": "true", "NO_COLOR": "1"}
    return jobs.default.start(p["argv"], str(p["cwd"]), env, p["label"], max_runtime)


def finish(p: dict, job: jobs.Job) -> str:
    """Model-facing text for a job that has ended."""
    text, _skipped = job.read_new(0)
    if job.timed_out:
        return (f"Command failed: the tests timed out after {int(job.max_runtime or 0)}s and were killed. Run a smaller part "
                "with target= or filter=.")
    for tool, rx in _NOT_INSTALLED:
        if rx.search(text) and job.returncode not in (0, None):
            py = p["argv"][0]
            return (f"Error: {tool} is not installed for {py}. Install it with run_shell_command: "
                    f"{py} -m pip install {tool}")
    if job.killed:
        return "Tests were stopped before they finished."
    if p["kind"] == "pytest" and job.returncode == 5:
        return "No tests were collected (pytest exit code 5). Check target= / filter=, or that the test files are named test_*.py."
    return condense(p["kind"], text, job.returncode)[1]


async def handler(args, ctx):
    p, err = plan(args or {})
    if p is None:
        return err
    try:
        job = await asyncio.to_thread(start, p)
    except jobs.JobError as e:
        return f"Error: could not start the tests ({p['kind']}): {e}"
    cap = max(5.0, float(os.environ.get("LUCY_TOOL_TIMEOUT", "120")) - _LOOP_MARGIN)
    try:
        done = await asyncio.to_thread(job.wait, cap)
    except asyncio.CancelledError:                                    # the user pressed Stop: don't leave it running
        job.kill()
        raise
    if not done:
        return (f"Tests are still running after {int(cap)}s (job #{job.id}, {p['kind']}). Read the output so far with "
                f"run_shell_command action 'poll' job_id {job.id}; stop them with action 'kill'.")
    return finish(p, job)
