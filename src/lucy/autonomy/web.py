"""HTTP surface of the task queue (Settings > Tasks). These routes are the USER's decisions: the agent has no tool that
calls them (the `task` tool can add / list / cancel, but never approve, OK-start or extend).

  GET    /api/jobs                       cards + queue status
  GET    /api/jobs/{id}                  one card with progress lines and run history
  POST   /api/jobs                       create {goal, title?, when?, repeat?, every_minutes?, cron?, deadline?, access?, minutes?, idle?, notify?}
  POST   /api/jobs/{id}/decision         {decision: approve|deny|always, approval_id?}   answers a parked approval
  POST   /api/jobs/{id}/ok               start a task the model made with full tools
  POST   /api/jobs/{id}/run              run now (ignores quiet hours / idle rules / daily caps)
  POST   /api/jobs/{id}/extend           {minutes?}  continue a task that ran out of budget
  POST   /api/jobs/{id}/cancel
  DELETE /api/jobs/{id}                  remove a finished task from the list
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from lucy.proactive.engine import UserError

router = APIRouter(prefix="/api/jobs", tags=["jobs"])


def _runner():
    from . import get_runner
    r = get_runner()
    if r is None:
        raise HTTPException(503, "the task system is not running")
    return r


async def _body(request: Request) -> dict:
    try:
        b = await request.json()
    except Exception:                                   # noqa: BLE001
        return {}
    return b if isinstance(b, dict) else {}


def _guard(fn, *a, **k):
    try:
        return fn(*a, **k)
    except UserError as e:
        raise HTTPException(409 if "not " in str(e) or "already" in str(e) else 400, str(e))


@router.get("")
async def list_jobs():
    r = _runner()
    return {"jobs": r.list_cards(), "status": r.status()}


@router.get("/{jid}")
async def one_job(jid: int):
    r = _runner()
    job = _guard(r._need, jid)
    return r.card(job, detail=True)


@router.post("")
async def create_job(request: Request):
    r = _runner()
    b = await _body(request)
    card = _guard(r.add_job, goal=b.get("goal", ""), title=b.get("title"), when=b.get("when"), repeat=b.get("repeat"),
                  every_minutes=b.get("every_minutes"), cron=b.get("cron"), deadline=b.get("deadline"),
                  access=b.get("access") or "read", minutes=b.get("minutes"), idle=bool(b.get("idle")),
                  notify=b.get("notify") or "always", source="user")
    return card


@router.post("/{jid}/decision")
async def decide(jid: int, request: Request):
    r = _runner()
    b = await _body(request)
    job = _guard(r.decide, jid, b.get("decision", ""), b.get("approval_id"))
    return r.card(job)


@router.post("/{jid}/ok")
async def ok_job(jid: int):
    r = _runner()
    return r.card(_guard(r.ok_to_start, jid))


@router.post("/{jid}/run")
async def run_job(jid: int):
    r = _runner()
    return r.card(_guard(r.run_now, jid))


@router.post("/{jid}/extend")
async def extend_job(jid: int, request: Request):
    r = _runner()
    b = await _body(request)
    return r.card(_guard(r.extend, jid, b.get("minutes")))


@router.post("/{jid}/cancel")
async def cancel_job(jid: int):
    r = _runner()
    _guard(r.cancel, jid)
    return r.card(r._need(jid))


@router.delete("/{jid}")
async def delete_job(jid: int):
    r = _runner()
    _guard(r.delete, jid)
    return {"ok": True}
