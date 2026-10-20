"""The model-facing tool: ONE `task` tool with an `action` (small models handle that better than five near-duplicates).

  add     "keep an eye on X" / "finish the report by Friday" / "every morning check Y"   -> a background job
  list    what is scheduled, running, waiting for approval
  status  one task in detail (progress, budget used, last result, what it is waiting for)
  cancel  stop and remove a task
  resume  continue a task that ran out of budget

The model can NOT approve anything: approvals, "OK to start" and extra budget are the user's decisions in the Tasks panel.
Failures start with "Error:" so the agent loop counts them as failed calls.
"""

from __future__ import annotations

from lucy.proactive.engine import UserError
from lucy.proactive.schedule import REPEAT_CHOICES

TOOL_DEFS = [
    {"type": "function", "function": {
        "name": "task",
        "description": (
            "Long-running or repeating background work for yourself: add (a complete goal), list, status, cancel, resume. "
            "It runs later without the user, may ask them to approve risky steps in the Tasks panel, and posts the result "
            "in the chat; for a plain reminder use set_reminder."),
        "parameters": {"type": "object", "properties": {
            "action": {"type": "string", "enum": ["add", "list", "status", "cancel", "resume"]},
            "goal": {"type": "string", "description": "add: the COMPLETE instruction to your future self (names, links, what counts as news)."},
            "title": {"type": "string", "description": "add: short name."},
            "when": {"type": "string", "description": "add: first run ('in 2 hours', 'tomorrow 9am'); leave out to start soon."},
            "repeat": {"type": "string", "enum": list(REPEAT_CHOICES)},
            "every_minutes": {"type": "integer", "description": "add: repeat every N minutes (min 10)."},
            "cron": {"type": "string", "description": "add: cron rule 'min hour day month weekday', e.g. '30 8 * * mon-fri'."},
            "deadline": {"type": "string", "description": "add: when it must be finished by."},
            "access": {"type": "string", "enum": ["read", "full"],
                       "description": "read = look things up only (default). full = may use all tools; the user must OK it first."},
            "minutes": {"type": "integer", "description": "add: time budget in minutes (default 20)."},
            "idle": {"type": "boolean", "description": "add: only start when the user has been quiet for a while."},
            "notify": {"type": "string", "enum": ["always", "changes"], "description": "changes = say nothing when nothing is new."},
            "id": {"type": "integer", "description": "status / cancel / resume: the task number."}},
            "required": ["action"]}}},
]
TOOL_NAMES = tuple(t["function"]["name"] for t in TOOL_DEFS)


def _line(runner, c: dict) -> str:
    bits = [c["status_label"]]
    if c["due_in"]:
        bits.append(f"next in {c['due_in']}")
    if c["repeat"]:
        bits.append(c["repeat"])
    u, b = c["usage"], c["budget"]
    if c["running"] or c["status"] in ("parked", "stopped"):
        bits.append(f"used {u['tool_calls']}/{b['tool_calls']} calls, {int(u['seconds'] // 60)}/{b['seconds'] // 60} min")
    return f"#{c['id']} {c['title']} [{'; '.join(bits)}]"


async def run_tool(name: str, args: dict, session: str | None = None) -> str:
    from . import get_runner
    runner = get_runner()
    if runner is None:
        return "Error: the task system is not running."
    if not runner.enabled():
        return "Error: background tasks are switched off (jobs.enabled in proactive.yaml)."
    action = str(args.get("action") or "list").lower()
    try:
        if action == "add":
            c = runner.add_job(goal=args.get("goal", ""), title=args.get("title"), when=args.get("when"),
                               repeat=args.get("repeat"), every_minutes=args.get("every_minutes"), cron=args.get("cron"),
                               deadline=args.get("deadline"), access=args.get("access") or "read",
                               minutes=args.get("minutes"), idle=bool(args.get("idle")),
                               notify=args.get("notify") or "always", source="model", session=session)
            head = f"Task #{c['id']} created: {c['title']}."
            if c["status"] == "waiting_ok":
                return (head + " It wants full tools, so it will NOT start until the user presses OK in Settings > Tasks. "
                        "Tell them that now.")
            when = f" It starts {('in ' + c['due_in']) if c['due_in'] else 'as soon as the model is free'}"
            rep = f", repeating {c['repeat']}" if c["repeat"] else ""
            return (head + when + rep + f". Budget: {c['budget']['seconds'] // 60} min, {c['budget']['tool_calls']} tool calls. "
                    "It runs in the background; the result is posted here.")
        if action == "list":
            cards = [c for c in runner.list_cards() if not c["system"] and c["status"] not in ("done", "failed", "cancelled")]
            return "\n".join(_line(runner, c) for c in cards) if cards else "No tasks are active."
        if args.get("id") in (None, ""):
            return f"Error: {action} needs the task number (use action=list to see them)."
        if action == "status":
            c = runner.card(runner._need(args["id"]), detail=True)
            out = [_line(runner, c), f"Goal: {c['goal']}"]
            if c["parked"]:
                out.append(f"Waiting for the user's approval: {c['parked']['summary']}")
            if c["last_error"]:
                out.append(f"Last error: {c['last_error']}")
            if c["last_result"]:
                out.append(f"Last result: {c['last_result'][:400]}")
            out += ["- " + p["text"] for p in c["progress"][-4:]]
            return "\n".join(out)
        if action == "cancel":
            job = runner.cancel(args["id"])
            return f"Cancelled task #{job['id']}: {job['title']}."
        if action == "resume":
            job = runner._need(args["id"])
            if job["status"] == "parked":
                return f"Error: task #{job['id']} is waiting for the USER to approve a step in Settings > Tasks; you cannot do that for them."
            if job["status"] == "waiting_ok":
                return f"Error: task #{job['id']} needs the USER to press OK in Settings > Tasks before it can start."
            job = runner.extend(job["id"])
            return f"Task #{job['id']} continues with more budget."
        return "Error: action must be add, list, status, cancel or resume."
    except UserError as e:
        return f"Error: {e}"
    except (TypeError, ValueError) as e:
        return f"Error: {e}"
