"""Tools the model uses to schedule things and to control how often she reaches out.

  set_reminder      "remind me at 5pm" / "I'll remind you tomorrow"      -> delivered as text at that time
  schedule_task     "I'll check that again tomorrow"                      -> Lucy runs it later with read-only tools
  manage_reminders  list / cancel
  proactive_control pause, mute, quiet hours, status

Result strings: success text starts normally; failures start with "Error:" so the agent loop counts them as failed calls.
"""

from __future__ import annotations

from .engine import UserError
from .runtime import get_engine
from .schedule import REPEAT_CHOICES, describe_rule
from .timeparse import describe, humanize_delta

TOOL_DEFS = [
    {"type": "function", "function": {
        "name": "set_reminder",
        "description": (
            "Schedule a reminder: at the given time the harness posts it in the chat by itself (it is shown "
            "when the chat is next open if it is closed). Call this whenever the user asks to be reminded of "
            "something, or you say you will remind them - never just promise. `message` is the reminder itself "
            "('Stretch', 'Call mom'). `when` examples: 'in 20 minutes', 'tomorrow 9am', 'friday 5pm', "
            "'oct 20 8:30', '2026-10-14 09:00'. Only say it is set after this tool confirms."),
        "parameters": {"type": "object", "properties": {
            "message": {"type": "string", "description": "What to remind about."},
            "when": {"type": "string", "description": "When to fire (first time)."},
            "repeat": {"type": "string", "enum": list(REPEAT_CHOICES), "description": "Repeat rule (default none)."},
            "every_minutes": {"type": "integer", "description": "For 'every N minutes' (min 10). Instead of repeat."},
            "replaces_id": {"type": "integer", "description": "Id of an existing reminder to change instead of adding."}},
            "required": ["message", "when"]}}},
    {"type": "function", "function": {
        "name": "schedule_task",
        "description": (
            "Schedule a follow-up for YOURSELF: at the given time you will run `task` in the background with "
            "read-only tools (web search, reading files, memory) and message the user the result. Use it when "
            "something should be checked or researched later ('check the price again tomorrow'). For a plain "
            "reminder use set_reminder. `task` must be a complete instruction to your future self with every "
            "detail (names, URLs, what counts as news) - no 'this' or 'that'."),
        "parameters": {"type": "object", "properties": {
            "task": {"type": "string", "description": "Complete, self-contained instruction."},
            "when": {"type": "string", "description": "When to run it (first time)."},
            "repeat": {"type": "string", "enum": list(REPEAT_CHOICES)},
            "every_minutes": {"type": "integer"}},
            "required": ["task", "when"]}}},
    {"type": "function", "function": {
        "name": "manage_reminders",
        "description": "List the scheduled reminders/tasks (each has an id), cancel one by id, or cancel all.",
        "parameters": {"type": "object", "properties": {
            "action": {"type": "string", "enum": ["list", "cancel", "cancel_all"]},
            "id": {"type": "integer", "description": "Reminder id (for cancel)."}},
            "required": ["action"]}}},
    {"type": "function", "function": {
        "name": "proactive_control",
        "description": (
            "Control how often you reach out first (greetings, check-ins, alerts). Call it when the user says "
            "to be quieter, stop pinging, leave them alone, do not disturb, change quiet hours, or asks about "
            "your proactive settings. pause: minutes (and scope 'all' to also hold reminders). mute/unmute: "
            "kind greeting|checkin|alert. quiet_hours: start/end as HH:MM, enabled true/false."),
        "parameters": {"type": "object", "properties": {
            "action": {"type": "string", "enum": ["status", "pause", "resume", "mute", "unmute", "quiet_hours"]},
            "minutes": {"type": "integer"},
            "scope": {"type": "string", "enum": ["initiative", "all"]},
            "kind": {"type": "string", "enum": ["greeting", "checkin", "alert"]},
            "start": {"type": "string"}, "end": {"type": "string"}, "enabled": {"type": "boolean"}},
            "required": ["action"]}}},
]
TOOL_NAMES = tuple(t["function"]["name"] for t in TOOL_DEFS)


def _line(eng, r: dict, now: float) -> str:
    when = describe(eng.now_local(r["due_ts"]), eng.now_local(now))
    rep = describe_rule(r["rule"])
    tag = "task" if r["kind"] == "task" else "reminder"
    return (f"#{r['id']} [{tag}] {when} (in {humanize_delta(r['due_ts'] - now)})"
            f"{' ' + rep if rep else ''}: {r['text'][:100]}")


async def run_tool(name: str, args: dict, session: str | None = None) -> str:
    eng = get_engine()
    if eng is None:
        return "Error: the proactive system is not running."
    try:
        if name in ("set_reminder", "schedule_task"):
            task = name == "schedule_task"
            res = eng.add_reminder(
                text=args.get("task" if task else "message", ""), when=args.get("when", ""),
                kind="task" if task else "reminder", repeat=args.get("repeat"),
                every_minutes=args.get("every_minutes"), session=session,
                replaces_id=args.get("replaces_id") if not task else None)
            rep = f", repeating {res['repeat_text']}" if res["repeat_text"] else ""
            verb = "Updated" if res["replaced"] else "Scheduled"
            what = "follow-up task" if task else "reminder"
            return (f"{verb} {what} #{res['id']} for {res['due_text']}{rep}: {res['text'][:100]}"
                    + (" It runs in the background and the result is posted here." if task else ""))
        if name == "manage_reminders":
            action = str(args.get("action", "list")).lower()
            now = eng.clock()
            if action == "list":
                rows = eng.list_reminders()
                return "\n".join(_line(eng, r, now) for r in rows) if rows else "Nothing is scheduled."
            if action == "cancel":
                if args.get("id") in (None, ""):
                    return "Error: cancel needs the reminder id (use action=list to see ids)."
                r = eng.cancel_reminder(int(args["id"]))
                return f"Cancelled #{r['id']}: {r['text'][:100]}"
            if action == "cancel_all":
                return f"Cancelled {eng.cancel_all()} scheduled item(s)."
            return "Error: action must be list, cancel or cancel_all."
        if name == "proactive_control":
            action = str(args.get("action", "status")).lower()
            if action == "status":
                return eng.status_text()
            if action == "pause":
                scope = args.get("scope") or "initiative"
                until = eng.pause(args.get("minutes") or 60, scope)
                mins = round((until - eng.clock()) / 60)
                return (f"Paused for {mins} min. I won't greet, check in or raise alerts"
                        f"{' and reminders are held too' if scope == 'all' else '; reminders still fire'}.")
            if action == "resume":
                eng.resume()
                return "Resumed. I may reach out again."
            if action in ("mute", "unmute"):
                if not args.get("kind"):
                    return "Error: give kind = greeting, checkin or alert."
                muted = eng.mute(args["kind"]) if action == "mute" else eng.unmute(args["kind"])
                return f"{action.capitalize()}d {args['kind']}. Muted now: {', '.join(muted) or 'nothing'}."
            if action == "quiet_hours":
                if args.get("start") is None and args.get("end") is None and args.get("enabled") is None:
                    return "Error: give start/end (HH:MM) and/or enabled."
                q = eng.set_quiet_hours(args.get("start"), args.get("end"), args.get("enabled"))
                return f"Quiet hours {q['start']}-{q['end']} are {'on' if q['enabled'] else 'off'}."
            return "Error: unknown action."
        return f"Error: unknown proactive tool {name}."
    except UserError as e:
        return f"Error: {e}"
    except (TypeError, ValueError) as e:
        return f"Error: {e}"
