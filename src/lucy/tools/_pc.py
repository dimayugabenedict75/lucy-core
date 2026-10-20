"""Shared plumbing for the P5 PC tools: run an op from lucy.broker.ops in Lucy's own process, or in the elevated
admin broker when it needs administrator rights; long ops become jobs the model polls.

Helper module (leading underscore): never treated as a tool module.
"""
from __future__ import annotations

import os
from typing import Optional

from lucy import paths as _paths
from lucy.broker import ops

LOCAL_JOBS = ops.Jobs()
_client = None
_runner: ops.Runner = ops.default_runner
# Ops that run as the user first and go to the broker only when Windows says "access denied" (already approved:
# these are hard-ask actions, so the user saw a card for this exact call).
ADMIN_RETRY = {"process.kill"}


def get_client():
    global _client
    if _client is None:
        from lucy.broker.client import BrokerClient
        idle = float(os.environ.get("LUCY_BROKER_IDLE", "900"))
        _client = BrokerClient(_paths.RUNTIME / "broker", _paths.ROOT, idle_s=idle)
    return _client


def set_client(c) -> None:
    """Tests: a client talking to an in-process broker (or a fake)."""
    global _client
    _client = c


def set_runner(r: Optional[ops.Runner]) -> None:
    """Tests: fake program runner for the ops that run in Lucy's process."""
    global _runner
    _runner = r or ops.default_runner


def wait_s() -> float:
    """How long a call may block before it turns into a job (the loop abandons a tool after LUCY_TOOL_TIMEOUT)."""
    cap = float(os.environ.get("LUCY_TOOL_TIMEOUT", "120")) - 15
    return max(3.0, min(100.0, cap))


def _job_text(r: dict, tool: str, jid: str) -> str:
    if r.get("error"):
        return f"Failed: {r['error']}"
    if r.get("done"):
        return str(r.get("result"))
    return (f"Still running ({r.get('what', 'job')}, {r.get('running_s', 0)} s so far). Job id: {jid}. Call {tool} with "
            f"action=poll and job_id={jid} to get the result; you can do other things meanwhile.")


def _broker(op: str, args: dict, dry_run: bool, tool: str) -> str:
    r = get_client().call(op, args, dry_run=dry_run, wait=wait_s())
    if r.get("error"):
        return (f"Failed: {op} needs administrator rights and the admin helper is not available: {r['error']}. "
                "Tell the user (they may need to accept the Windows UAC prompt).")
    return _job_text(r, tool, "B-" + str(r.get("job", "")))


def execute(op: str, args: dict, *, tool: str, dry_run: bool = False, force_admin: bool = False) -> str:
    spec = ops.OPS.get(op)
    if spec is None:
        return f"Failed: unknown operation {op}"
    if force_admin or spec.admin(args):
        return _broker(op, args, dry_run, tool)
    ctx = ops.Ctx(runner=_runner, guard=ops.Guard(None), dry_run=dry_run, protect_pids=(os.getppid(),),
                  trash=_trash)
    if spec.long and not dry_run:
        jid = LOCAL_JOBS.start(lambda: ops.run(op, args, ctx), op)
        return _job_text(LOCAL_JOBS.wait(jid, wait_s()), tool, "L-" + jid)
    res = ops.run(op, args, ctx)
    if op in ADMIN_RETRY and ops.needs_admin_retry(res) and not dry_run:
        return _broker(op, args, dry_run, tool)
    return res


def poll(job_id: str, tool: str) -> str:
    jid = str(job_id or "").strip()
    if jid.startswith("L-"):
        return _job_text(LOCAL_JOBS.wait(jid[2:], wait_s()), tool, jid)
    if jid.startswith("B-"):
        r = get_client().poll(jid[2:], wait_s())
        return _job_text(r, tool, jid)
    return f"Failed: job_id must be the id from an earlier answer (like L-1a2b3c or B-1a2b3c), got {jid!r}"


def _trash(path: str) -> str:
    """Recycle Bin for deletes done in Lucy's own process (fallback: runtime/trash, never a hard delete)."""
    from pathlib import Path
    from lucy.safety.backups import move_to_trash
    return move_to_trash(Path(path), _paths.RUNTIME / "trash")


def action_of(args: dict, default: str = "") -> str:
    return str((args or {}).get("action") or default).strip().lower()


def schema(name: str, description: str, actions: list, props: dict, required_extra: Optional[list] = None,
           dry_run: bool = True) -> dict:
    p = {"action": {"type": "string", "enum": actions}}
    p.update(props)
    if dry_run:
        p["dry_run"] = {"type": "boolean", "description": "true = only say what would happen"}
    return {"type": "function", "function": {"name": name, "description": description,
                                             "parameters": {"type": "object", "properties": p,
                                                            "required": ["action"] + list(required_extra or [])}}}
