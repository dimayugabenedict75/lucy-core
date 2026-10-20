"""user_tool: Lucy writes, improves, lists or switches off her OWN small tools (P7 Lane A, lucy.selfdev).

Autonomous but fenced in by code: tests must pass in the sandbox before a tool exists, every call runs in a separate
restricted process, new tools are on probation and switch off after repeated failures, and they run at tier "exec"
(asks first in the default mode) until the user promotes them in Settings > Evolve.
"""
from lucy import selfdev
from lucy.selfdev.user_tools import ToolError

SCHEMA = {
    "type": "function",
    "function": {
        "name": "user_tool",
        "description": ("Create, improve, list or disable your own small Python tools (code + tests, run in a sandbox). "
                        "Use it when no existing tool can do a pure calculation / text / file job you need again."),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["create", "improve", "list", "disable"],
                           "description": "create | improve | list | disable"},
                "name": {"type": "string", "description": "Tool name: lowercase_with_underscores, e.g. word_count."},
                "description": {"type": "string", "description": "One or two short sentences: what the tool does."},
                "code": {"type": "string", "description": "Python that defines  def run(args):  and returns a string."},
                "tests": {"type": "string", "description": "Plain asserts calling run, e.g.  assert run({'text': 'a b'}) == '2'"},
                "parameters": {"type": "object", "description": "Arguments of the tool: {\"text\": \"string: the text\"}."},
                "needs": {"type": "array", "items": {"type": "string", "enum": ["files", "programs", "internet", "system"]},
                          "description": "Leave empty unless the tool must write files, start programs, use the internet."},
                "reason": {"type": "string", "description": "Why (create / improve / disable), in a few words."},
            },
            "required": ["action"],
        },
    },
}
TIER = "write"
LABEL = "run"


def handler(args, ctx):
    action = str(args.get("action") or "").strip().lower()
    mgr = selfdev.get_tools()
    try:
        if action == "list":
            items = mgr.list()
            if not items:
                return "You have no user tools yet. Create one with action=create (name, description, code, tests, reason)."
            return "\n".join(f"- {t['name']} v{t['version']} [{t['status']}, tier {t['tier']}, used {t['uses_total']}x, "
                             f"{t['fails_total']} failures]: {t['description']}"
                             + (f" (off: {t['disabled_reason']})" if not t['enabled'] else "") for t in items)
        if action in ("create", "improve"):
            t = mgr.create(name=args.get("name", ""), description=args.get("description", ""), code=args.get("code", ""),
                           tests=args.get("tests", ""), parameters=args.get("parameters"), needs=args.get("needs"),
                           reason=args.get("reason", ""), session=ctx.session_id, improve=action == "improve")
            return (f"{'Improved' if action == 'improve' else 'Created'} {t['name']} v{t['version']}: tests passed. "
                    f"It is available now (call it by name; tier {t['tier']}, on probation for {t['probation_left']} runs).")
        if action == "disable":
            t = mgr.disable(args.get("name", ""), args.get("reason", ""), by="agent", session=ctx.session_id)
            return f"Switched off {t['name']}. The user can switch it back on in Settings > Evolve."
        return "Error: action must be create, improve, list or disable."
    except ToolError as e:
        return f"Error: {e}"
