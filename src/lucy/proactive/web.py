"""HTTP surface of the proactive system.

  GET  /api/proactive/status            state, next reminder, counters
  GET  /api/proactive/stream            SSE: messages Lucy sends first (EventSource: pass ?api_key=)
  GET  /api/proactive/pending?since=ID  polling fallback for the same events
  POST /api/proactive/presence          {"visible": bool}  window focus (used for notifications)
  POST /api/proactive/feedback          {"id", "action": open|dismiss|snooze|mute}
  GET/POST/DELETE /api/proactive/reminders[/id]
  POST /api/proactive/control           {"action": pause|resume|mute|unmute|quiet_hours, ...}
  POST /api/proactive/test              deliver a test message now
  GET  /proactive.js                    the small client that shows the messages (no auth: it is only code)
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse

from .engine import UserError
from .runtime import get_engine
from .schedule import describe_rule

router = APIRouter(prefix="/api/proactive", tags=["proactive"])
public_router = APIRouter(tags=["proactive"])
CLIENT_JS = Path(__file__).with_name("client.js")


def _eng():
    e = get_engine()
    if e is None:
        raise HTTPException(503, "proactive system not running")
    return e


def _sse(ev: dict) -> str:
    return f"id: {ev['id']}\ndata: {json.dumps(ev, ensure_ascii=False)}\n\n"


def _event_dict(row: dict) -> dict:
    return {"id": row["id"], "ts": row["ts"], "kind": row["kind"],
            "title": {"reminder": "Reminder", "task": "Follow-up", "alert": "Heads up"}.get(row["kind"], "Lucy"),
            "text": row["text"], "media": row["media"], "session_id": row["session_id"],
            "mutable": row["kind"] in ("greeting", "checkin", "alert")}


@router.get("/status")
async def status():
    return _eng().status()


@router.get("/stream")
async def stream(request: Request, since: int = 0):
    eng = _eng()
    q = eng.bus.subscribe()
    eng.client_connected()

    async def gen():
        try:
            yield "retry: 4000\n\n"
            rows = {r["id"]: r for r in eng.store.unseen_events(eng.clock() - 24 * 3600)}
            if since:
                rows.update({r["id"]: r for r in eng.store.events_after(since)})
            seen = set()
            for rid in sorted(rows):                     # missed while no window was open, or newer than the client's last id
                if rid <= since:
                    continue
                seen.add(rid)
                eng.store.mark_live(rid)
                yield _sse(_event_dict(rows[rid]))
            while not eng.bus.closed:
                if await request.is_disconnected():
                    break
                try:
                    ev = await asyncio.wait_for(q.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
                    continue
                if ev is None:
                    break
                if ev["id"] in seen:
                    continue
                yield _sse(ev)
        finally:
            eng.bus.unsubscribe(q)
            eng.client_disconnected()

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.get("/pending")
async def pending(since: int = 0):
    eng = _eng()
    return {"events": [_event_dict(r) for r in eng.store.events_after(since)]}


@router.post("/presence")
async def presence(request: Request):
    body = await request.json()
    _eng().set_presence(bool(body.get("visible", True)))
    return {"ok": True}


@router.post("/feedback")
async def feedback(request: Request):
    body = await request.json()
    try:
        return _eng().feedback(int(body.get("id", 0)), str(body.get("action", "dismiss")))
    except UserError as e:
        raise HTTPException(404, str(e))


@router.get("/reminders")
async def reminders(all: bool = False):
    eng = _eng()
    now = eng.clock()
    return {"reminders": [dict(r, due=eng.now_local(r["due_ts"]).isoformat(), repeat=describe_rule(r["rule"]),
                               in_seconds=round(r["due_ts"] - now)) for r in eng.list_reminders(all)]}


@router.post("/reminders")
async def add_reminder(request: Request):
    body = await request.json()
    try:
        res = _eng().add_reminder(text=body.get("text", ""), when=body.get("when", ""),
                                  kind="task" if body.get("kind") == "task" else "reminder",
                                  repeat=body.get("repeat"), every_minutes=body.get("every_minutes"))
    except UserError as e:
        raise HTTPException(400, str(e))
    return {"id": res["id"], "due": res["due"].isoformat(), "due_text": res["due_text"], "repeat": res["repeat_text"]}


@router.delete("/reminders/{rid}")
async def delete_reminder(rid: int):
    try:
        _eng().cancel_reminder(rid)
    except UserError as e:
        raise HTTPException(404, str(e))
    return {"ok": True}


@router.post("/control")
async def control(request: Request):
    from .tools import run_tool
    body = await request.json()
    out = await run_tool("proactive_control", body)
    if out.startswith("Error:"):
        raise HTTPException(400, out)
    return {"ok": True, "message": out, "status": _eng().status()}


@router.post("/test")
async def test(request: Request):
    eng = _eng()
    try:
        text = (await request.json()).get("text")
    except Exception:                                  # noqa: BLE001
        text = None
    eid = await eng.send_test(text)
    return {"ok": True, "event_id": eid, "ui_clients": eng.ui_clients()}


@public_router.get("/proactive.js")
async def client_js():
    return HTMLResponse(CLIENT_JS.read_text(encoding="utf-8"), media_type="application/javascript",
                        headers={"Cache-Control": "no-cache"})


def inject_client(html: str, api_key: str) -> str:
    """Add the client script to the chat page (called by the GET / route in api.py). Idempotent."""
    if "proactive.js" in html:
        return html
    try:
        v = int(CLIENT_JS.stat().st_mtime)
    except OSError:
        v = 0
    key = json.dumps(api_key).replace("</", "<\\/")
    tag = f'<script>window.LUCY_PROACTIVE_KEY={key};</script><script src="/proactive.js?v={v}" defer></script>'
    i = html.lower().rfind("</body>")
    return html[:i] + tag + html[i:] if i != -1 else html + tag
