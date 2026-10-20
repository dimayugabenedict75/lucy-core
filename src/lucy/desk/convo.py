"""Item 2 (edit / regenerate / branch) and item 11 (export) - pure functions over the sessions database.

Edit and regenerate are the same move: rewind the chat to just BEFORE a user message and hand back its text and
attachments, so the normal chat endpoint can answer it again (the UI re-sends it, optionally with changed text). Nothing
the agent did to files is undone by this - that is what checkpoints (item 6) are for.
"""

from __future__ import annotations

import json
import re
import time

from .projects import slug


class ConvoError(ValueError):
    pass


def _atts(raw) -> list:
    if not raw:
        return []
    try:
        v = json.loads(raw)
    except (ValueError, TypeError):
        return []
    return v if isinstance(v, list) else []


def messages(conn, lock, sid: str) -> list[dict]:
    with lock:
        rows = conn.execute("SELECT rowid AS rid, role, content, created_at, attachments FROM messages "
                            "WHERE session_id = ? ORDER BY rowid", (sid,)).fetchall()
    return [{"id": r["rid"], "role": r["role"], "content": r["content"], "timestamp": r["created_at"],
             "attachments": _atts(r["attachments"])} for r in rows]


def truncate_from(conn, lock, sid: str, rowid: int) -> int:
    """Delete the message `rowid` and everything after it (plus their Library references and rewind points)."""
    with lock:
        n = conn.execute("SELECT COUNT(*) FROM messages WHERE session_id = ? AND rowid >= ?", (sid, rowid)).fetchone()[0]
        conn.execute("DELETE FROM messages WHERE session_id = ? AND rowid >= ?", (sid, rowid))
        try:
            conn.execute("DELETE FROM library_refs WHERE session_id = ? AND message_rowid >= ?", (sid, rowid))
        except Exception:                                # noqa: BLE001  (no library tables in a bare test db)
            pass
        try:
            conn.execute("DELETE FROM checkpoints WHERE session_id = ? AND user_rowid >= ?", (sid, rowid))
        except Exception:                                # noqa: BLE001
            pass
        conn.commit()
    return n


def _user_message(conn, lock, sid: str, rowid: int) -> dict:
    for m in messages(conn, lock, sid):
        if m["id"] == rowid:
            if m["role"] != "user":
                raise ConvoError("Only your own messages can be edited.")
            return m
    raise ConvoError("That message is not in this chat any more.")


def _reply(m: dict, removed: int) -> dict:
    keep = [{"sha": a["sha"], "name": a.get("name")} for a in m["attachments"] if a.get("sha") and not a.get("sticker")]
    return {"text": m["content"], "attachments": keep, "removed": removed}


def prepare_edit(conn, lock, sid: str, rowid: int) -> dict:
    """Rewind to before user message `rowid`. The caller re-sends the (edited) text."""
    m = _user_message(conn, lock, sid, int(rowid))
    return _reply(m, truncate_from(conn, lock, sid, m["id"]))


def prepare_regenerate(conn, lock, sid: str) -> dict:
    """Rewind to before the LAST user message so it can be answered again."""
    last = next((m for m in reversed(messages(conn, lock, sid)) if m["role"] == "user"), None)
    if not last:
        raise ConvoError("There is nothing to regenerate yet.")
    return _reply(last, truncate_from(conn, lock, sid, last["id"]))


def branch(conn, lock, sid: str, rowid: int, new_sid: str, now: str | None = None, title: str | None = None) -> dict:
    """Copy the chat up to and including message `rowid` into a new session (same project)."""
    msgs = [m for m in messages(conn, lock, sid) if m["id"] <= int(rowid)]
    if not msgs or msgs[-1]["id"] != int(rowid):
        raise ConvoError("That message is not in this chat any more.")
    now = now or time.strftime("%Y-%m-%d %H:%M:%S")
    with lock:
        src = conn.execute("SELECT title, project_id FROM sessions WHERE session_id = ?", (sid,)).fetchone()
        base = (src["title"] if src else None) or "Chat"
        new_title = title or f"{base} (branch)"
        conn.execute("INSERT INTO sessions (session_id, title, project_id, updated_at) VALUES (?, ?, ?, ?)",
                     (new_sid, new_title[:120], src["project_id"] if src else None, now))
        for m in msgs:
            cur = conn.execute("INSERT INTO messages (session_id, role, content, created_at, attachments) VALUES (?, ?, ?, ?, ?)",
                               (new_sid, m["role"], m["content"], m["timestamp"] or now,
                                json.dumps(m["attachments"], ensure_ascii=False) if m["attachments"] else None))
            try:
                conn.execute("INSERT OR IGNORE INTO library_refs (sha, session_id, message_rowid, origin, name, created_at) "
                             "SELECT sha, ?, ?, origin, name, created_at FROM library_refs "
                             "WHERE session_id = ? AND message_rowid = ?", (new_sid, cur.lastrowid, sid, m["id"]))
            except Exception:                            # noqa: BLE001
                pass
        conn.commit()
    return {"session_id": new_sid, "title": new_title, "copied": len(msgs)}


# ---------------------------------------------------------------------------------------------------- export
_MEDIA = re.compile(r"\n?MEDIA:[^\s]+")


def _clean(text: str) -> str:
    return _MEDIA.sub("", text or "").strip()


def export_markdown(conn, lock, sid: str, title: str = "", project: str = "", user_name: str = "You", bot_name: str = "Lucy") -> str:
    out = [f"# {title or 'Conversation'}", ""]
    meta = [f"Exported {time.strftime('%Y-%m-%d %H:%M')}"]
    if project:
        meta.append(f"Project: {project}")
    out += ["_" + " · ".join(meta) + "_", ""]
    for m in messages(conn, lock, sid):
        who = user_name if m["role"] == "user" else bot_name
        out.append(f"### {who}" + (f" — {m['timestamp']}" if m["timestamp"] else ""))
        out.append("")
        body = _clean(m["content"])
        if body:
            out += [body, ""]
        for a in m["attachments"]:
            if a.get("name"):
                out.append(f"📎 {a['name']}")
        if m["attachments"]:
            out.append("")
    return "\n".join(out).rstrip() + "\n"


def export_json(conn, lock, sid: str, title: str = "", project: str = "") -> str:
    data = {"session_id": sid, "title": title, "project": project or None,
            "exported_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "messages": [{"role": m["role"], "content": _clean(m["content"]), "timestamp": m["timestamp"],
                          "attachments": [{"name": a.get("name"), "sha": a.get("sha")} for a in m["attachments"]]}
                         for m in messages(conn, lock, sid)]}
    return json.dumps(data, ensure_ascii=False, indent=2)


def export_filename(title: str, fmt: str) -> str:
    return f"{slug(title, 'conversation')}-{time.strftime('%Y%m%d')}.{'json' if fmt == 'json' else 'md'}"
