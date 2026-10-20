"""Item 1 - Projects. A project = a working folder + standing instructions + attached files + its own notes.

A chat belongs to at most one project. When it does, `prompt_block()` is added to the system prompt (after the static
blocks, so the KV cache of the persona is not disturbed) and the agent's `remember` calls go to the project's notes
instead of the global memory (`add_note`). Attached files are kept in the Library cache (`library_keep`), so they
survive cleanup; their text is inlined in the prompt when small, otherwise the path is listed for read_local_file.
"""

from __future__ import annotations

import re
import time
import uuid
from pathlib import Path

NAME_MAX = 80
INSTR_MAX = 4000
NOTE_MAX = 300
NOTES_MAX = 60                      # per project; the oldest is dropped past this
INLINE_FILE_MAX = 4000              # chars of one attached text file shown in the prompt
INLINE_TOTAL_MAX = 10000            # chars of all attached files together
_TEXT_EXT = {".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".yaml", ".yml", ".py", ".js", ".ts", ".html", ".css",
             ".ini", ".cfg", ".toml", ".log", ".sql", ".xml", ".bat", ".ps1", ".sh"}


class ProjectError(ValueError):
    pass


def _clean(text, limit: int) -> str:
    return str(text or "").replace("\r\n", "\n").strip()[:limit]


class Projects:
    def __init__(self, conn, lock, library=None, now_fn=None):
        self.conn, self.lock, self.library = conn, lock, library
        self._now = now_fn or (lambda: time.strftime("%Y-%m-%d %H:%M:%S"))

    # ------------------------------------------------------------------ projects
    def create(self, name: str, folder: str = "", instructions: str = "") -> dict:
        name = _clean(name, NAME_MAX)
        if not name:
            raise ProjectError("A project needs a name.")
        folder = self._check_folder(folder)
        pid = uuid.uuid4().hex[:10]
        now = self._now()
        with self.lock:
            self.conn.execute("INSERT INTO projects (id, name, folder, instructions, created_at, updated_at) "
                              "VALUES (?, ?, ?, ?, ?, ?)", (pid, name, folder, _clean(instructions, INSTR_MAX), now, now))
            self.conn.commit()
        return self.get(pid)

    def update(self, pid: str, **fields) -> dict:
        cur = self.get(pid)
        if not cur:
            raise ProjectError("No such project.")
        name = _clean(fields["name"], NAME_MAX) if "name" in fields else cur["name"]
        if not name:
            raise ProjectError("A project needs a name.")
        folder = self._check_folder(fields["folder"]) if "folder" in fields else cur["folder"]
        instr = _clean(fields["instructions"], INSTR_MAX) if "instructions" in fields else cur["instructions"]
        with self.lock:
            self.conn.execute("UPDATE projects SET name = ?, folder = ?, instructions = ?, updated_at = ? WHERE id = ?",
                              (name, folder, instr, self._now(), pid))
            self.conn.commit()
        return self.get(pid)

    @staticmethod
    def _check_folder(folder) -> str:
        folder = str(folder or "").strip().strip('"')
        if not folder:
            return ""
        p = Path(folder).expanduser()
        if not p.is_dir():
            raise ProjectError(f"The folder {folder!r} does not exist.")
        return p.resolve().as_posix()

    def get(self, pid: str) -> dict | None:
        with self.lock:
            r = self.conn.execute("SELECT * FROM projects WHERE id = ?", (pid,)).fetchone()
        return dict(r) if r else None

    def list(self) -> list[dict]:
        with self.lock:
            rows = self.conn.execute(
                "SELECT p.*, (SELECT COUNT(*) FROM sessions s WHERE s.project_id = p.id) AS chats, "
                "(SELECT COUNT(*) FROM project_files f WHERE f.project_id = p.id) AS files, "
                "(SELECT COUNT(*) FROM project_notes n WHERE n.project_id = p.id) AS notes "
                "FROM projects p ORDER BY p.name COLLATE NOCASE").fetchall()
        return [dict(r) for r in rows]

    def delete(self, pid: str, delete_chats: bool = False) -> bool:
        if not self.get(pid):
            return False
        shas = [f["sha"] for f in self.files(pid)]
        with self.lock:
            if delete_chats:
                ids = [r[0] for r in self.conn.execute("SELECT session_id FROM sessions WHERE project_id = ?", (pid,))]
                for sid in ids:
                    self.conn.execute("DELETE FROM messages WHERE session_id = ?", (sid,))
                    self.conn.execute("DELETE FROM sessions WHERE session_id = ?", (sid,))
                    self.conn.execute("DELETE FROM checkpoints WHERE session_id = ?", (sid,))
            else:
                self.conn.execute("UPDATE sessions SET project_id = NULL WHERE project_id = ?", (pid,))
            self.conn.execute("DELETE FROM project_files WHERE project_id = ?", (pid,))
            self.conn.execute("DELETE FROM project_notes WHERE project_id = ?", (pid,))
            self.conn.execute("DELETE FROM projects WHERE id = ?", (pid,))
            self.conn.commit()
        if self.library:
            for sha in shas:
                self.library.unkeep(sha, f"project:{pid}")
        return True

    # ------------------------------------------------------------------ sessions
    def assign(self, session_id: str, pid: str | None) -> None:
        if pid and not self.get(pid):
            raise ProjectError("No such project.")
        with self.lock:
            self.conn.execute("INSERT OR IGNORE INTO sessions (session_id, title) VALUES (?, 'New Session')", (session_id,))
            self.conn.execute("UPDATE sessions SET project_id = ? WHERE session_id = ?", (pid or None, session_id))
            self.conn.commit()

    def project_of(self, session_id: str | None) -> dict | None:
        if not session_id:
            return None
        with self.lock:
            r = self.conn.execute("SELECT p.* FROM projects p JOIN sessions s ON s.project_id = p.id "
                                  "WHERE s.session_id = ?", (session_id,)).fetchone()
        return dict(r) if r else None

    def session_map(self) -> dict:
        with self.lock:
            rows = self.conn.execute("SELECT session_id, project_id FROM sessions WHERE project_id IS NOT NULL").fetchall()
        return {r[0]: r[1] for r in rows}

    # ------------------------------------------------------------------ files
    def add_file(self, pid: str, data: bytes, name: str) -> dict:
        if not self.get(pid):
            raise ProjectError("No such project.")
        if not self.library:
            raise ProjectError("The Library is not available.")
        att = self.library.ingest_bytes(data, name, "user")
        if not att:
            raise ProjectError("That file could not be stored.")
        self.library.keep(att["sha"], f"project:{pid}")
        with self.lock:
            self.conn.execute("INSERT OR REPLACE INTO project_files (project_id, sha, name, size, created_at) "
                              "VALUES (?, ?, ?, ?, ?)", (pid, att["sha"], att["name"], len(data), self._now()))
            self.conn.commit()
        return {"sha": att["sha"], "name": att["name"], "size": len(data)}

    def files(self, pid: str) -> list[dict]:
        with self.lock:
            rows = self.conn.execute("SELECT sha, name, size FROM project_files WHERE project_id = ? ORDER BY created_at, name",
                                     (pid,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            item = self.library.get(d["sha"]) if self.library else None
            d["path"] = item["path"].as_posix() if item else None
            d["missing"] = item is None
            out.append(d)
        return out

    def remove_file(self, pid: str, sha: str) -> bool:
        with self.lock:
            cur = self.conn.execute("DELETE FROM project_files WHERE project_id = ? AND sha = ?", (pid, sha))
            self.conn.commit()
        if cur.rowcount and self.library:
            self.library.unkeep(sha, f"project:{pid}")
        return bool(cur.rowcount)

    # ------------------------------------------------------------------ project notes (the project's own memory)
    def add_note(self, pid: str, text: str) -> dict:
        text = _clean(text, NOTE_MAX)
        if not text:
            raise ProjectError("The note is empty.")
        with self.lock:
            dup = self.conn.execute("SELECT id FROM project_notes WHERE project_id = ? AND lower(text) = lower(?)",
                                    (pid, text)).fetchone()
            if dup:
                return {"id": dup[0], "text": text, "status": "already known"}
            cur = self.conn.execute("INSERT INTO project_notes (project_id, text, created_at) VALUES (?, ?, ?)",
                                    (pid, text, self._now()))
            nid = cur.lastrowid
            extra = self.conn.execute("SELECT id FROM project_notes WHERE project_id = ? ORDER BY id DESC LIMIT -1 OFFSET ?",
                                      (pid, NOTES_MAX)).fetchall()
            for r in extra:
                self.conn.execute("DELETE FROM project_notes WHERE id = ?", (r[0],))
            self.conn.commit()
        return {"id": nid, "text": text, "status": "saved"}

    def notes(self, pid: str) -> list[dict]:
        with self.lock:
            rows = self.conn.execute("SELECT id, text, created_at FROM project_notes WHERE project_id = ? ORDER BY id",
                                     (pid,)).fetchall()
        return [dict(r) for r in rows]

    def delete_note(self, pid: str, nid: int) -> bool:
        with self.lock:
            cur = self.conn.execute("DELETE FROM project_notes WHERE project_id = ? AND id = ?", (pid, nid))
            self.conn.commit()
        return bool(cur.rowcount)

    # ------------------------------------------------------------------ prompt
    def prompt_block(self, pid: str | None) -> str:
        """The system-prompt text for a chat inside project `pid` ('' when there is none)."""
        p = self.get(pid) if pid else None
        if not p:
            return ""
        lines = [f"PROJECT: \"{p['name']}\". This chat belongs to a project; stay within its context."]
        if p["folder"]:
            lines.append(f"Project folder (work here by default; relative paths mean this folder): {p['folder']}")
        if p["instructions"]:
            lines.append("Project instructions (follow them):\n" + p["instructions"])
        used = 0
        listed = []
        for f in self.files(pid):
            if f["missing"]:
                continue
            body = ""
            if Path(f["name"]).suffix.lower() in _TEXT_EXT and used < INLINE_TOTAL_MAX:
                try:
                    raw = Path(f["path"]).read_text(encoding="utf-8", errors="replace")
                except OSError:
                    raw = ""
                if raw and len(raw) <= INLINE_FILE_MAX and used + len(raw) <= INLINE_TOTAL_MAX:
                    body = "\n" + raw
                    used += len(raw)
            listed.append(f"- {f['name']} ({f['path']}){':' if body else ''}{body}")
        if listed:
            lines.append("Project files (read the ones not shown with read_local_file):\n" + "\n".join(listed))
        notes = self.notes(pid)
        if notes:
            lines.append("Project notes (what you remembered in this project):\n" + "\n".join(f"- {n['text']}" for n in notes))
        lines.append("When you remember something in this project, `remember` saves it to the project notes.")
        return "\n".join(lines)


def slug(text: str, default: str = "item") -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "-", str(text or "")).strip("-").lower()
    return s[:48] or default
