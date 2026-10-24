"""Health snapshot for the health panel (/health-panel): which servers answer, GPU memory, the llama.cpp build,
the startup self-check, the supervisor, memory/index status and the latest warnings from the log.

Pure helpers + one `snapshot()`; api.py passes in the live objects, so this file never imports api.py.
"""
from __future__ import annotations

import asyncio
import collections
import logging
import time
from pathlib import Path

import httpx

from lucy import clock
from lucy.server import llama_check

LAST_CHECK: dict = {"ok": None, "problems": [], "checked_at": None, "running": False}


class _Ring(logging.Handler):
    """Keeps the last warnings / errors from Lucy's own loggers for the panel."""
    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.items: collections.deque = collections.deque(maxlen=60)

    def emit(self, record: logging.LogRecord) -> None:
        if record.name.startswith("lucy") or record.name.startswith("uvicorn"):
            try:
                self.items.append({"t": clock.now_str(record.created), "level": record.levelname,
                                   "name": record.name, "msg": record.getMessage()[:400]})
            except Exception:
                pass


_RING = _Ring()


def install_log_capture() -> None:
    root = logging.getLogger()
    if _RING not in root.handlers:
        root.addHandler(_RING)


def recent_problems(n: int = 25) -> list[dict]:
    return list(_RING.items)[-n:][::-1]


def run_startup_check(llm, embed) -> dict:
    """Does the llama.cpp we launch still accept every flag we pass? Blocking (runs the exe): call in a thread."""
    LAST_CHECK["running"] = True
    try:
        argvs: dict = {}
        for m in llm.models.values():
            try:
                argvs[m["label"]] = llm._build_argv(m)
            except Exception as e:
                argvs[m["label"]] = e
        try:
            argvs["Embed (Stella)"] = embed.build_argv()
        except Exception as e:
            argvs["Embed (Stella)"] = e
        res = llama_check.check(llm.cfg["llama_server"], argvs)
        if not Path(embed.cfg["model"]).is_file():
            res["problems"].append(f"embed model file missing: {embed.cfg['model']}")
            res["ok"] = False
        res["checked_at"] = clock.now_str()
        res["running"] = False
        LAST_CHECK.clear()
        LAST_CHECK.update(res)
        log = logging.getLogger("lucy.core.health")
        (log.info if res.get("ok") else log.warning)(
            "llama.cpp self-check: " + ("OK " + str(res.get("version")) if res.get("ok") else "; ".join(res["problems"])))
        return res
    finally:
        LAST_CHECK["running"] = False


async def _probe(port: int, paths=("/health", "/")) -> dict:
    async with httpx.AsyncClient(timeout=1.0) as c:
        for p in paths:
            try:
                r = await c.get(f"http://127.0.0.1:{port}{p}")
                return {"up": r.status_code < 500, "status": r.status_code}
            except Exception:
                continue
    return {"up": False, "status": None}


async def snapshot(*, llm, embed, supervisor, memory, semantic, started_at: float, audio_port: int = 8091,
                   db: dict | None = None, self_port: int = 8090) -> dict:
    exe = llm.cfg["llama_server"]
    probes = await asyncio.gather(*(_probe(m["port"], ("/health",)) for m in llm.models.values()),
                                  _probe(embed.cfg["port"], ("/health",)), _probe(audio_port))
    n = len(llm.models)
    servers = [{"name": "Lucy Core (this app)", "port": self_port, "up": True, "detail": "answering"}]
    for m, p in zip(llm.models.values(), probes[:n]):
        detail = "loaded" if p["up"] else ("sleeping (idle)" if llm.sleeping else "not running")
        if m["id"] == llm.state["active"] and llm.phase in ("loading", "stopping"):
            detail = llm.message or llm.phase
        servers.append({"name": m["label"] + (" (active)" if m["id"] == llm.state["active"] else ""),
                        "port": m["port"], "up": p["up"], "detail": detail})
    e_up = probes[n]["up"]
    if e_up:
        e_detail = "loaded" + (f" ({embed.cpu_note})" if embed.cpu_note else "")
    else:
        e_detail = "starts on first memory use" if embed.cfg.get("enabled", True) else "disabled"
    servers.append({"name": "Embed server (Stella)", "port": embed.cfg["port"], "up": e_up, "detail": e_detail})
    servers.append({"name": "Lucy Audio", "port": audio_port, "up": probes[n + 1]["up"],
                    "detail": "answering" if probes[n + 1]["up"] else "not running"})
    devs = await asyncio.to_thread(llama_check.devices, exe, 10.0)
    snap = {
        "now": clock.now_str(), "timezone": clock.tz_label(),
        "uptime_s": int(time.time() - started_at),
        "servers": servers,
        "llm": await llm.status(),
        "vram": devs,
        "llama": {"exe": exe, "source": llm.exe_source, "version": LAST_CHECK.get("version"),
                  "check_ok": LAST_CHECK.get("ok"), "problems": LAST_CHECK.get("problems", []),
                  "checked_at": LAST_CHECK.get("checked_at"), "check_running": LAST_CHECK.get("running", False)},
        "supervisor": supervisor.status() if supervisor else None,
        "embed": {"enabled": embed.cfg.get("enabled", True), "use_mmproj": embed.cfg.get("use_mmproj", False),
                  "model": Path(embed.cfg["model"]).name, "cpu_note": embed.cpu_note},
        "memory": memory.conductor.vector_status() if memory else None,
        "semantic": semantic.counts() if semantic else None,
        "db": db or {},
        "problems": recent_problems(),
    }
    return snap
