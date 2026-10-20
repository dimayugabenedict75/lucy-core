"""P5 PC admin tools: services, processes, apps (winget), scheduled tasks, registry, system info.

Reading runs in Lucy's own process. Changes that need administrator rights go to the elevated admin broker
(lucy.broker), which Windows starts with a UAC prompt the first time. Every call still passes Lucy's approval cards
first (tiers + hard actions in safety/policy.py). Every tool accepts dry_run=true (says what would happen, changes nothing).
"""
from __future__ import annotations

from . import _pc

S = {"type": "string"}


def _h_service(args, ctx):
    a = _pc.action_of(args, "list")
    op = {"list": "service.list", "status": "service.status", "start": "service.start", "stop": "service.stop",
          "restart": "service.restart"}.get(a)
    if not op:
        return "Failed: action must be list, status, start, stop or restart"
    return _pc.execute(op, args, tool="service", dry_run=args.get("dry_run") is True)


def _h_process(args, ctx):
    a = _pc.action_of(args, "list")
    if a == "list":
        return _pc.execute("process.list", args, tool="process")
    if a == "kill":
        return _pc.execute("process.kill", args, tool="process", dry_run=args.get("dry_run") is True)
    return "Failed: action must be list or kill"


def _h_winget(args, ctx):
    a = _pc.action_of(args, "search")
    if a == "poll":
        return _pc.poll(args.get("job_id"), "winget")
    if a not in ("search", "list", "install", "uninstall", "upgrade"):
        return "Failed: action must be search, list, install, uninstall, upgrade or poll"
    return _pc.execute("winget." + a, args, tool="winget", dry_run=args.get("dry_run") is True)


def _h_task(args, ctx):
    a = _pc.action_of(args, "list")
    if a not in ("list", "info", "run", "enable", "disable"):
        return "Failed: action must be list, info, run, enable or disable"
    return _pc.execute("task." + a, args, tool="scheduled_task", dry_run=args.get("dry_run") is True)


def _h_registry(args, ctx):
    a = _pc.action_of(args, "read")
    if a not in ("read", "write"):
        return "Failed: action must be read or write"
    return _pc.execute("registry." + a, args, tool="registry", dry_run=args.get("dry_run") is True)


def _h_system_info(args, ctx):
    a = _pc.action_of(args, "disk")
    op = {"disk": "info.disk", "firewall": "info.firewall", "events": "info.events", "updates": "info.updates"}.get(a)
    if not op:
        return "Failed: action must be disk, firewall, events or updates"
    return _pc.execute(op, args, tool="system_info")


def describe_call(name: str, args: dict) -> str:
    """One plain line for the approval card (safety.layer shows the raw arguments under it too)."""
    a = _pc.action_of(args)
    dry = " (dry run: nothing changes)" if args.get("dry_run") is True else ""
    if name == "service":
        return f"Windows service: {a} {args.get('name', '')}{dry}  (needs admin: Windows may show a UAC prompt)"
    if name == "process":
        who = f"process {args.get('pid')}" if args.get("pid") not in (None, "") else f"every {args.get('name')}"
        return f"Force-close {who}{dry}"
    if name == "winget":
        return f"App: winget {a} {args.get('id', '')}{dry}  (runs as admin: Windows may show a UAC prompt)"
    if name == "scheduled_task":
        return f"Scheduled task: {a} {args.get('name', '')}{dry}  (needs admin)"
    if name == "registry":
        return (f"Registry {a}: {args.get('key', '')} -> {args.get('value') or '(Default)'} = {str(args.get('data'))[:80]!s}"
                f" ({args.get('type') or 'REG_SZ'}){dry}")
    return ""


TOOLS = [
    {"schema": _pc.schema("service", "Windows services: list, status, start, stop or restart one by name. "
                          "Changes need admin (UAC prompt).", ["list", "status", "start", "stop", "restart"],
                          {"name": {"type": "string", "description": "service name, e.g. Spooler"},
                           "filter": {"type": "string", "description": "list: part of a name"}}),
     "handler": _h_service, "tier": "system", "label": "services"},
    {"schema": _pc.schema("process", "Running programs: list them (biggest memory first) or force-close one. "
                          "Prefer pid from list over name.", ["list", "kill"],
                          {"pid": {"type": "integer"}, "name": {"type": "string", "description": "e.g. notepad"},
                           "filter": {"type": "string"}}),
     "handler": _h_process, "tier": "system", "label": "processes"},
    {"schema": _pc.schema("winget", "Install, update or remove apps with winget: search first, then use the Id. "
                          "Long installs return a job_id; check it with action=poll.",
                          ["search", "list", "install", "uninstall", "upgrade", "poll"],
                          {"query": {"type": "string", "description": "search/list: app name"},
                           "id": {"type": "string", "description": "package Id, e.g. 7zip.7zip ('all' for upgrade)"},
                           "job_id": {"type": "string"}}),
     "handler": _h_winget, "tier": "system", "label": "apps"},
    {"schema": _pc.schema("scheduled_task", "Windows Task Scheduler: list, info, run, enable or disable a task. "
                          "Changes need admin.", ["list", "info", "run", "enable", "disable"],
                          {"name": {"type": "string"}, "path": {"type": "string", "description": "task folder, e.g. \\"},
                           "filter": {"type": "string"}}),
     "handler": _h_task, "tier": "system", "label": "tasks"},
    {"schema": _pc.schema("registry", "Read a registry key/value, or write one value (always asks the user). "
                          "Keys like HKCU\\Software\\App.", ["read", "write"],
                          {"key": S, "value": {"type": "string", "description": "value name ('' = default)"},
                           "data": {"description": "new data (write)"},
                           "type": {"type": "string", "enum": ["REG_SZ", "REG_EXPAND_SZ", "REG_DWORD", "REG_QWORD",
                                                               "REG_MULTI_SZ"]}}, ["key"]),
     "handler": _h_registry, "tier": "system", "label": "registry"},
    {"schema": _pc.schema("system_info", "PC health: disk space (or biggest folders in a path), firewall rules, "
                          "Windows event log errors, Windows Update status.", ["disk", "firewall", "events", "updates"],
                          {"path": {"type": "string", "description": "disk: folder to measure"},
                           "filter": {"type": "string", "description": "firewall: part of a rule name"},
                           "log": {"type": "string", "enum": ["System", "Application", "Setup"]},
                           "level": {"type": "string", "enum": ["error", "warning", "all"]},
                           "hours": {"type": "integer"}}, dry_run=False),
     "handler": _h_system_info, "tier": "read", "label": "system info"},
]
