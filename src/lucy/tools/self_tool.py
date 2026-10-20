"""update_self: Lucy's way to change her own SELF persona file (traits, how the user likes to be spoken to, ...).

No approval card (it writes one fixed file that is not a constitution file), but everything is enforced in code by
persona.SelfStore: fixed sections, size caps, a content guard (no secrets, no rule/permission overrides), a daily
edit limit, and a changelog the user can revert from Settings > Persona. There is no tool to revert: only the user can.
"""
from lucy import persona

SCHEMA = {
    "type": "function",
    "function": {
        "name": "update_self",
        "description": ("Add, revise or remove one short line of your own SELF notes (what you learned about yourself or how "
                        "the user likes you). Always give a reason; it cannot change your rules."),
        "parameters": {
            "type": "object",
            "properties": {
                "section": {"type": "string",
                                "enum": ["traits", "speaking", "preferences", "in_jokes", "mistakes", "opinions"],
                                "description": "traits | speaking (how the user likes to be spoken to) | preferences | in_jokes | mistakes | opinions"},
                "op": {"type": "string", "enum": ["add", "revise", "remove"], "description": "add, revise or remove"},
                "text": {"type": "string", "description": "The line (one short sentence). For remove: the line's number or words."},
                "reason": {"type": "string", "description": "Why, in a few words (what happened that taught you this)."},
                "old_text": {"type": "string", "description": "For revise: the number or words of the line being replaced."},
            },
            "required": ["section", "op", "text", "reason"],
        },
    },
}
TIER = "write"
LABEL = "remember"


def handler(args, ctx):
    store = persona.get_store()
    try:
        e = store.edit(args.get("section", ""), args.get("op", ""), args.get("text", ""), args.get("reason", ""),
                       old_text=args.get("old_text", ""), actor="agent", session=ctx.session_id)
    except persona.SelfError as err:
        return f"Error: {err}"
    verb = {"add": "Added to", "revise": "Revised in", "remove": "Removed from"}[e["op"]]
    return f"{verb} SELF ({e['section']}): {e['new'] or e['old']} (change #{e['id']}; the user can revert it)."
