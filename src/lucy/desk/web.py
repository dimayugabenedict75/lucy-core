"""HTTP surface for the P9 features. api.py includes this router with the API-key dependency.

  /api/projects ...                  projects, their files and notes, assigning a chat
  /api/desk/commands, /files, /usage slash-command list, @file autocomplete, context meter
  /api/desk/sessions/{sid}/edit | regenerate | branch | checkpoints | rewind | export
  /api/desk/overview                 Settings summary (connectors, permission mode, tasks)
These are the USER's actions (UI buttons); the agent has no tool that calls them.
"""

import asyncio
import uuid

from . import commands, convo, usage


def build_router(get_desk):
    from fastapi import APIRouter, File, HTTPException, UploadFile
    from fastapi.responses import Response

    from .projects import ProjectError

    router = APIRouter(tags=["desk"])

    def desk():
        d = get_desk()
        if d is None:
            raise HTTPException(503, "the desk features are not running")
        return d

    def _guard(fn, *a, **k):
        try:
            return fn(*a, **k)
        except (ProjectError, convo.ConvoError) as e:
            raise HTTPException(400, str(e))

    # ------------------------------------------------------------------ projects
    @router.get("/api/projects")
    def list_projects():
        d = desk()
        return {"projects": d.projects.list(), "sessions": d.projects.session_map()}

    @router.post("/api/projects")
    def create_project(body: dict):
        return _guard(desk().projects.create, body.get("name"), body.get("folder", ""), body.get("instructions", ""))

    @router.post("/api/projects/assign")
    def assign(body: dict):
        sid = str(body.get("session_id") or "").strip()
        if not sid:
            raise HTTPException(400, "session_id is required")
        _guard(desk().projects.assign, sid, body.get("project_id") or None)
        return {"session_id": sid, "project_id": body.get("project_id") or None}

    @router.get("/api/projects/{pid}")
    def one_project(pid: str):
        d = desk()
        p = d.projects.get(pid)
        if not p:
            raise HTTPException(404, "No such project")
        return {**p, "files": d.projects.files(pid), "notes": d.projects.notes(pid)}

    @router.patch("/api/projects/{pid}")
    def update_project(pid: str, body: dict):
        fields = {k: body[k] for k in ("name", "folder", "instructions") if k in body}
        return _guard(desk().projects.update, pid, **fields)

    @router.delete("/api/projects/{pid}")
    def delete_project(pid: str, delete_chats: int = 0):
        if not desk().projects.delete(pid, bool(delete_chats)):
            raise HTTPException(404, "No such project")
        return {"status": "deleted"}

    @router.post("/api/projects/{pid}/files")
    async def add_project_file(pid: str, file: UploadFile = File(...)):
        data = await file.read()
        if len(data) > 20 * 1024 * 1024:
            raise HTTPException(413, "Project files can be at most 20 MB.")
        return await asyncio.to_thread(_guard, desk().projects.add_file, pid, data, file.filename or "file")

    @router.delete("/api/projects/{pid}/files/{sha}")
    def remove_project_file(pid: str, sha: str):
        if not desk().projects.remove_file(pid, sha):
            raise HTTPException(404, "No such file in this project")
        return {"status": "removed"}

    @router.delete("/api/projects/{pid}/notes/{nid}")
    def remove_project_note(pid: str, nid: int):
        if not desk().projects.delete_note(pid, nid):
            raise HTTPException(404, "No such note")
        return {"status": "removed"}

    # ------------------------------------------------------------------ commands, @files, usage
    @router.get("/api/desk/commands")
    def list_commands():
        return {"commands": commands.COMMANDS}

    @router.get("/api/desk/files")
    async def find_files(q: str = "", session_id: str | None = None, limit: int = 12):
        d = desk()
        roots = d.roots_for(session_id)
        return {"files": await asyncio.to_thread(commands.find_files, q, roots, limit)}

    @router.get("/api/desk/usage")
    async def context_usage(session_id: str | None = None):
        d = desk()
        info = d.hook("usage", session_id, default=None)
        if asyncio.iscoroutine(info):
            info = await info
        if info is None:
            info = {"tokens": 0, "limit": 100000}
        m = usage.meter(info.get("tokens", 0), info.get("limit", 100000))
        m.update({k: v for k, v in info.items() if k not in m})
        return m

    # ------------------------------------------------------------------ edit / regenerate / branch
    @router.post("/api/desk/sessions/{sid}/edit")
    def edit_message(sid: str, body: dict):
        try:
            mid = int(body.get("message_id"))
        except (TypeError, ValueError):
            raise HTTPException(400, "message_id is required")
        d = desk()
        res = _guard(convo.prepare_edit, d.conn, d.lock, sid, mid)
        d.library.gc_orphans()
        return res

    @router.post("/api/desk/sessions/{sid}/regenerate")
    def regenerate(sid: str):
        d = desk()
        res = _guard(convo.prepare_regenerate, d.conn, d.lock, sid)
        d.library.gc_orphans()
        return res

    @router.post("/api/desk/sessions/{sid}/branch")
    def branch(sid: str, body: dict):
        try:
            mid = int(body.get("message_id"))
        except (TypeError, ValueError):
            raise HTTPException(400, "message_id is required")
        d = desk()
        return _guard(convo.branch, d.conn, d.lock, sid, mid, str(uuid.uuid4()), d.hook("now", default=None))

    # ------------------------------------------------------------------ checkpoints
    @router.get("/api/desk/sessions/{sid}/checkpoints")
    def checkpoints(sid: str):
        return {"checkpoints": desk().checkpoints.list(sid)}

    @router.post("/api/desk/sessions/{sid}/rewind")
    async def rewind(sid: str, body: dict):
        try:
            cid = int(body.get("checkpoint_id"))
        except (TypeError, ValueError):
            raise HTTPException(400, "checkpoint_id is required")
        d = desk()
        files, conv = bool(body.get("files", True)), bool(body.get("conversation", True))
        res = await asyncio.to_thread(_guard, d.checkpoints.rewind, sid, cid, files=files, conversation=conv)
        d.hook("audit", "rewind", session=sid, checkpoint=cid, files=len(res["restored"]), removed=res["removed"])
        d.library.gc_orphans()
        return res

    # ------------------------------------------------------------------ export
    @router.get("/api/desk/sessions/{sid}/export")
    def export(sid: str, format: str = "md"):
        d = desk()
        title = d.hook("title_of", sid, default="") or "Conversation"
        proj = d.projects.project_of(sid)
        name = proj["name"] if proj else ""
        if format == "json":
            body, mime = convo.export_json(d.conn, d.lock, sid, title, name), "application/json"
        else:
            format, body, mime = "md", convo.export_markdown(d.conn, d.lock, sid, title, name), "text/markdown; charset=utf-8"
        fname = convo.export_filename(title, format)
        return Response(content=body, media_type=mime, headers={"Content-Disposition": f'attachment; filename="{fname}"'})

    # ------------------------------------------------------------------ settings overview (item 10)
    @router.get("/api/desk/overview")
    async def overview():
        d = desk()
        res = d.hook("overview", default=None)
        if asyncio.iscoroutine(res):
            res = await res
        return res or {}

    return router
