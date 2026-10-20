"""Item 6 - checkpoints / rewind. One rewind point per user turn.

A checkpoint remembers (a) which user message started the turn and (b) how many entries the safety layer's undo index
had at that moment. Rewinding restores every file the agent changed in this chat since then (the earliest snapshot of each
file wins = the file as it was before the turn) and/or removes the chat from that message on. Only changes made through
the file tools are covered (they snapshot first); a shell command that rewrites files is not. Restores go through
`Backups.restore` as the USER, so they are audited and themselves undoable.
"""

from __future__ import annotations

import time

from . import convo

KEEP = 200                                             # checkpoints kept per chat


class Checkpoints:
    def __init__(self, conn, lock, backups_fn=lambda: None, now_fn=None):
        self.conn, self.lock, self.backups_fn = conn, lock, backups_fn
        self._now = now_fn or (lambda: time.strftime("%Y-%m-%d %H:%M:%S"))

    def _mark(self) -> int:
        bk = self.backups_fn()
        try:
            return len(bk.entries()) if bk else 0
        except Exception:                              # noqa: BLE001
            return 0

    def begin(self, sid: str, user_rowid: int) -> int:
        with self.lock:
            cur = self.conn.execute("INSERT INTO checkpoints (session_id, user_rowid, backup_mark, created_at) VALUES (?, ?, ?, ?)",
                                    (sid, int(user_rowid), self._mark(), self._now()))
            cid = cur.lastrowid
            extra = self.conn.execute("SELECT id FROM checkpoints WHERE session_id = ? ORDER BY id DESC LIMIT -1 OFFSET ?",
                                      (sid, KEEP)).fetchall()
            for r in extra:
                self.conn.execute("DELETE FROM checkpoints WHERE id = ?", (r[0],))
            self.conn.commit()
        return cid

    def _rows(self, sid: str) -> list:
        with self.lock:
            return self.conn.execute("SELECT id, user_rowid, backup_mark, created_at FROM checkpoints WHERE session_id = ? "
                                     "ORDER BY id", (sid,)).fetchall()

    def _changes(self, sid: str, lo: int, hi: int | None = None) -> list[dict]:
        bk = self.backups_fn()
        if not bk:
            return []
        es = bk.entries()[lo:hi]
        return [e for e in es if e.get("session") == sid and e.get("kind") in ("modified", "created", "too_big", "failed")]

    def list(self, sid: str) -> list[dict]:
        rows = self._rows(sid)
        texts = {m["id"]: m["content"] for m in convo.messages(self.conn, self.lock, sid) if m["role"] == "user"}
        out = []
        for i, r in enumerate(rows):
            hi = rows[i + 1]["backup_mark"] if i + 1 < len(rows) else None
            files = sorted({e["path"] for e in self._changes(sid, r["backup_mark"], hi)})
            text = " ".join((texts.get(r["user_rowid"]) or "").split())
            out.append({"id": r["id"], "message_id": r["user_rowid"], "created_at": r["created_at"],
                        "preview": text[:70] + ("…" if len(text) > 70 else ""), "files": files})
        return list(reversed(out))                      # newest first

    def rewind(self, sid: str, cp_id: int, *, files: bool = True, conversation: bool = True) -> dict:
        row = next((r for r in self._rows(sid) if r["id"] == int(cp_id)), None)
        if not row:
            raise convo.ConvoError("That rewind point no longer exists.")
        res: dict = {"restored": [], "failed": [], "removed": 0, "text": "", "attachments": []}
        if files:
            bk = self.backups_fn()
            first: dict[str, dict] = {}
            for e in self._changes(sid, row["backup_mark"]):
                first.setdefault(e["path"], e)
            for path, e in first.items():
                if e["kind"] not in ("modified", "created") or bk is None:
                    res["failed"].append(f"{path}: no backup was kept ({e['kind']})")
                    continue
                msg = bk.restore(e["id"], session=None, by="user (rewind)")
                (res["failed"] if msg.startswith(("Error", "Refused", "Nothing")) else res["restored"]).append(
                    path if not msg.startswith(("Error", "Refused", "Nothing")) else f"{path}: {msg}")
        if conversation:
            user_msg = next((m for m in convo.messages(self.conn, self.lock, sid) if m["id"] == row["user_rowid"]), None)
            if user_msg:
                res["text"] = user_msg["content"]
                res["attachments"] = [{"sha": a["sha"], "name": a.get("name")} for a in user_msg["attachments"]
                                      if a.get("sha") and not a.get("sticker")]
            res["removed"] = convo.truncate_from(self.conn, self.lock, sid, row["user_rowid"])
        return res
