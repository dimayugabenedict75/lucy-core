"""P9 - Claude-Desktop-style features (projects, edit/branch, slash commands + @files, usage meter, plan mode, rewind,
citations, deep research, Library pins/cap, export, quick entry). Logic lives in these modules (importable and tested
without the web app); `web.build_router` is the HTTP surface and api.py only wires it in.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import commands, convo, db, plan, research, sources, usage            # noqa: F401  (re-exported)
from .checkpoints import Checkpoints
from .projects import ProjectError, Projects


@dataclass
class Desk:
    conn: Any
    lock: Any
    library: Any
    projects: Projects
    checkpoints: Checkpoints
    roots_fn: Callable[[], list] = lambda: []
    hooks: dict = field(default_factory=dict)

    def roots_for(self, session_id: str | None) -> list[Path]:
        """Folders @mentions may point into: the project folder (if the chat is in a project) + the configured roots."""
        roots = [Path(r) for r in self.roots_fn() if r]
        p = self.projects.project_of(session_id)
        if p and p.get("folder"):
            roots.insert(0, Path(p["folder"]))
        seen, out = set(), []
        for r in roots:
            k = r.as_posix().lower()
            if k not in seen:
                seen.add(k)
                out.append(r)
        return out

    def hook(self, name: str, *args, default=None, **kw):
        fn = self.hooks.get(name)
        return fn(*args, **kw) if fn else default


def init(conn, lock, library, *, backups_fn=lambda: None, roots_fn=lambda: [], now_fn=None, hooks: dict | None = None) -> Desk:
    db.ensure_schema(conn, lock)
    return Desk(conn=conn, lock=lock, library=library, projects=Projects(conn, lock, library, now_fn),
                checkpoints=Checkpoints(conn, lock, backups_fn, now_fn), roots_fn=roots_fn, hooks=hooks or {})


__all__ = ["Desk", "init", "ProjectError", "Projects", "Checkpoints"]
