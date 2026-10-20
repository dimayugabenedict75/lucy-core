"""core_change: propose a change to Lucy Core ITSELF (P7 Lane B, lucy.selfdev.lane_b). Gated.

Lucy edits a COPY (never the live files), submits it, the harness runs the full test suite + a canary instance + a
smoke script, and only then the user sees a diff with Approve / Reject in Settings > Evolve. Lucy cannot approve,
cannot change constitution files through it, and the previous version is kept for rollback.
Tier exec (submit runs the test suite, i.e. code); start / status / ideas are lower (policy.ACTION_TIERS).
"""
from lucy import selfdev
from lucy.selfdev.lane_b import LaneBError

SCHEMA = {
    "type": "function",
    "function": {
        "name": "core_change",
        "description": ("Change Lucy Core's own code safely: start makes a copy to edit, submit tests it, the user approves. "
                        "status shows progress; ideas shows weekly improvement ideas from your trace."),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["start", "submit", "status", "discard", "ideas"],
                           "description": "start | submit | status | discard | ideas"},
                "id": {"type": "string", "description": "The change id that start gave you (submit, status, discard)."},
                "title": {"type": "string", "description": "start: short title of the improvement."},
                "reason": {"type": "string", "description": "start: why, in a sentence (what went wrong / what it fixes)."},
            },
            "required": ["action"],
        },
    },
}
TIER = "exec"
LABEL = "run"

_HINT = {"editing": "Edit the files in the copy, then call core_change action=submit.",
         "checking": "Checks are running (they take minutes). Carry on with other work or finish your answer; "
                     "call status again later.",
         "ready": "All checks passed. It now waits for the USER: tell them to open Settings > Evolve and Approve or Reject.",
         "failed": "Fix the problem in the copy and submit again, or discard it.",
         "applied": "Applied. Lucy Core must be restarted to use it.",
         }


def _status(rec):
    ch = rec.get("changes") or {}
    files = sum(len(v) for v in ch.values()) if ch else 0
    lines = [f"Change {rec['id']} '{rec['title']}': {rec['status']}" + (f" ({rec['failure']})" if rec.get("failure") else ""),
             f"Copy: {rec['workspace']}" + (f" ({files} files changed)" if files else "")]
    if rec.get("status") == "checking":
        lines.append(f"Stage: {(rec.get('checks') or {}).get('stage')}")
    for k in ("tests", "canary"):
        out = (rec.get("checks") or {}).get(k)
        if out and rec.get("status") == "failed":
            lines.append(f"{k} output (end):\n" + out[-1200:])
    lines.append(_HINT.get(rec["status"], ""))
    return "\n".join(x for x in lines if x)


def handler(args, ctx):
    action = str(args.get("action") or "").strip().lower()
    lb = selfdev.get_lane_b()
    pid = str(args.get("id") or "").strip()
    try:
        if action == "ideas":
            return selfdev.get_miner().text()
        if action == "start":
            rec = lb.start(args.get("title", ""), args.get("reason", ""), session=ctx.session_id)
            return (f"Change {rec['id']} started. A copy of Lucy Core is at {rec['workspace']}. Edit ONLY files inside that "
                    "folder with your file tools (same relative paths as the live tree, e.g. <copy>/src/lucy/...). "
                    "Never edit the live files. Constitution files (src/lucy/safety, broker, selfdev, permissions.yaml, "
                    f"lucy.md...) cannot be changed this way. When done: core_change action=submit id={rec['id']}.")
        if action == "status":
            if not pid:
                open_ = lb.list(include_closed=False)
                if not open_:
                    return "No open changes. Start one with action=start (title, reason)."
                return "\n\n".join(_status(lb.get(r["id"])) for r in open_)
            return _status(lb.get(pid))
        if not pid:
            return "Error: give the change id (from start), e.g. id=1019-a1b2c3."
        if action == "submit":
            rec = lb.submit(pid)
            return _status(rec) + "\nChecks started: tests in the copy, then a canary instance + smoke test."
        if action == "discard":
            lb.discard(pid, by="agent")
            return f"Discarded {pid}; its copy was deleted. The live Lucy Core was never touched."
        return "Error: action must be start, submit, status, discard or ideas."
    except LaneBError as e:
        return f"Error: {e}"
