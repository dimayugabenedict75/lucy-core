"""restore_from_backup: undo a file change Lucy made in THIS chat (the user can undo anything from Settings > Safety)."""
from lucy import safety

SCHEMA = {
    "type": "function",
    "function": {
        "name": "restore_from_backup",
        "description": "Undo a file change you made in this chat. action 'list' shows your changes with ids; 'restore' puts one back.",
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["list", "restore"], "description": "list (default) or restore"},
                "backup_id": {"type": "string", "description": "Id from 'list' (needed for restore)."},
            },
            "required": [],
        },
    },
}
TIER = "write"
LABEL = "edit"


def handler(args, ctx):
    layer = safety.get()
    session = ctx.session_id or "_none_"          # no session -> sees nothing (never "everything")
    action = str(args.get("action") or "list").strip().lower()
    if action == "list":
        items = layer.backups.list(session=session, limit=20)
        if not items:
            return "No file changes recorded in this chat."
        return "\n".join(f"{e['id']}  {e['ts']}  {e['tool']}  {e['path']}"
                         + ("  (new file: undo moves it to the Recycle Bin)" if e["kind"] == "created" else "")
                         for e in items)
    if action != "restore":
        return "Error: action must be 'list' or 'restore'."
    bid = str(args.get("backup_id") or "").strip()
    if not bid:
        return "Error: give backup_id (see action 'list')."
    e = layer.backups.get(bid)
    if e is None or e.get("session") != session:
        return "Error: no change with that id in this chat. Use action 'list'."
    d = layer.decide("write_local_file", "write", {"file_path": e["path"]}, ctx.session_id)
    if d.level != safety.AUTO:
        return (f"Refused: restoring {e['path']} needs the user ({d.reason}). Ask them to restore id {bid} "
                "from Settings > Safety.")
    return layer.backups.restore(bid, session=session)
