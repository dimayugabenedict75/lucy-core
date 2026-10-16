"""run_shell_command: PowerShell (default on Windows), cmd or bash, with timeouts, background jobs and incremental output.

One tool with an `action` (run | start | poll | kill | list) so a small model never juggles five near-identical tools.
The work is done by jobs.py (Popen + reader threads): nothing here can block the event loop, a command that asks for
input gets EOF instead of hanging, and Stop (task cancellation) kills the process tree.
"""
import asyncio
import os
import shutil

from lucy import paths as _paths

from . import jobs
from ._common import outside_root, resolve_path

SHELLS = ("powershell", "cmd", "bash")
DEFAULT_TIMEOUT = 60
MAX_TIMEOUT = 3600
OUT_CHARS = 8000                 # newest characters of output returned per call
_LOOP_MARGIN = 10                # the agent loop abandons a tool after LUCY_TOOL_TIMEOUT: stop waiting a bit before that

_SCHEMA_RUN_SHELL_COMMAND = {
    "type": "function",
    "function": {
        "name": "run_shell_command",
        "description": ("Run a command on this PC (PowerShell by default; shell can be cmd or bash). For long jobs use "
                        "action 'start', then 'poll' for new output and 'kill' to stop it."),
        "parameters": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "The command line to run (action run/start)."},
                "action": {"type": "string", "enum": ["run", "start", "poll", "kill", "list"],
                           "description": "run = wait for the result (default); start = background job; poll/kill need job_id."},
                "job_id": {"type": "integer", "description": "Job number returned by action 'start'."},
                "shell": {"type": "string", "enum": list(SHELLS), "description": "Which shell (default: powershell)."},
                "timeout": {"type": "integer", "description": f"Seconds before the command is killed (default {DEFAULT_TIMEOUT}, max {MAX_TIMEOUT})."},
                "cwd": {"type": "string", "description": "Folder to run in (default: the file-tool root)."},
                "env": {"type": "object", "description": "Extra environment variables, name -> value."},
                "wait": {"type": "integer", "description": "For poll: seconds to wait for more output (max 30)."},
            },
            "required": [],
        },
    },
}


def default_shell() -> str:
    env = (os.environ.get("LUCY_SHELL") or "").strip().lower()
    if env in SHELLS:
        return env
    return "powershell" if os.name == "nt" else "bash"


def build_argv(shell: str, command: str):
    """The process to launch for `command`. (A plain string for cmd on Windows: cmd has its own quoting rules.)"""
    if shell == "powershell":
        exe = "powershell.exe" if os.name == "nt" else (shutil.which("pwsh") or "pwsh")
        prefix = "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; $ProgressPreference='SilentlyContinue'; "
        return [exe, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", prefix + command]
    if shell == "cmd":
        return 'cmd.exe /d /s /c "chcp 65001>nul & ' + command + '"'
    return ["bash", "-c", command]


def _int(value, default):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _show(job, max_chars=OUT_CHARS) -> str:
    text, skipped = job.read_new(max_chars)
    note = f"[... {skipped} earlier characters skipped ...]\n" if skipped else ""
    return note + text


def _result(job, text: str, extra: str = "") -> str:
    """Final answer for a finished job. Failures start with 'Command failed' so the agent loop counts them as failed calls."""
    if job.timed_out:
        head = (f"Command failed: timed out after {int(job.max_runtime or 0)}s and was killed. For long jobs use "
                "action 'start', then 'poll'.\n")
    elif job.returncode not in (0, None):
        head = f"Command failed (exit code {job.returncode}).\n"
    else:
        head = ""
    body = text if text.strip() else "[no output]"
    return f"{head}{body.rstrip()}\n[{job.status()}]{extra}"


def _job_or_error(args):
    job = jobs.default.get(args.get("job_id"))
    if job is None:
        known = ", ".join(f"#{j.id}" for j in jobs.default.all()) or "none"
        return None, f"Error: no job {args.get('job_id')!r}. Known jobs: {known}. Use action 'list'."
    return job, ""


async def _h_run_shell_command(args, ctx):
    action = str(args.get("action") or "run").strip().lower()

    if action == "list":
        rows = [f"#{j.id} [{j.status()}] {j.label[:70]}" for j in jobs.default.all()]
        return "\n".join(rows) if rows else "No jobs."

    if action in ("poll", "kill"):
        job, err = _job_or_error(args)
        if job is None:
            return err
        if action == "kill":
            await asyncio.to_thread(job.kill)
            await asyncio.to_thread(job.wait, 5)
            return f"Job #{job.id}: {job.status()}.\n{_show(job)}".rstrip()
        wait = max(0, min(_int(args.get("wait"), 0), 30))
        if wait and job.running:
            await asyncio.to_thread(job.wait, wait)
        text = _show(job)
        if not text and not job.running:
            text = "[no new output]"
        elif not text:
            text = "[no new output yet]"
        return f"Job #{job.id} [{job.status()}]\n{text}".rstrip()

    if action not in ("run", "start"):
        return f"Error: unknown action {action!r}. Use run, start, poll, kill or list."
    command = str(args.get("command") or "").strip()
    if not command:
        return f"Error: 'command' is required for action '{action}'."
    shell = str(args.get("shell") or default_shell()).strip().lower()
    if shell not in SHELLS:
        return f"Error: unknown shell {shell!r}. Use one of: {', '.join(SHELLS)}."
    cwd = _paths.SAFE_ROOT
    if args.get("cwd"):
        cwd = resolve_path(str(args["cwd"]))
        if cwd is None:
            return outside_root(str(args["cwd"]))
        if not cwd.is_dir():
            return f"Error: folder not found: {args['cwd']}"
    env = args.get("env") or {}
    if not isinstance(env, dict):
        return "Error: 'env' must be an object like {\"NAME\": \"value\"}."

    timeout = max(1, min(_int(args.get("timeout"), DEFAULT_TIMEOUT), MAX_TIMEOUT))
    extra = ""
    if action == "run":
        cap = max(5.0, float(os.environ.get("LUCY_TOOL_TIMEOUT", "120")) - _LOOP_MARGIN)
        if timeout > cap:
            timeout, extra = int(cap), f"\n[a foreground run is limited to {int(cap)}s here: use action 'start' for longer jobs]"
    else:
        timeout = _int(args.get("timeout"), MAX_TIMEOUT)             # background jobs: up to an hour unless told otherwise
        timeout = max(1, min(timeout, MAX_TIMEOUT))

    try:
        job = await asyncio.to_thread(jobs.default.start, build_argv(shell, command), str(cwd), env, command, timeout)
    except jobs.JobError as e:
        return f"Error: could not start the command ({shell}): {e}"

    if action == "start":
        await asyncio.to_thread(job.wait, 0.5)                       # catches "command not found" and instant output
        first = _show(job)
        head = f"Started job #{job.id} ({shell}): {command[:80]}. Check it with action 'poll' (job_id {job.id}); stop it with 'kill'."
        if not job.running:
            return f"{head}\nIt already finished:\n{_result(job, first)}"
        return f"{head}\n{first}".rstrip()

    try:
        await asyncio.to_thread(job.wait, timeout + 5)               # the job's own timer kills it at `timeout`
    except asyncio.CancelledError:                                   # the user pressed Stop: don't leave it running
        job.kill()
        raise
    return _result(job, _show(job), extra)


TOOLS = [
    {"schema": _SCHEMA_RUN_SHELL_COMMAND, "handler": _h_run_shell_command, "tier": "exec", "label": "run"},
]
