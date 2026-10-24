"""Background watchdog for the model servers: idle unload, crash restart, embed server idle stop.

* Idle unload  - Settings value `idle_unload_min` (0 = never). When nobody has used the model for that long and
                 nothing is generating, the chat model AND the embed server are stopped so the GPU is free for games.
                 The next message wakes them again (api.py calls llm.wake() when a chat starts; the agent loop also
                 reloads a missing model by itself). Proactive messages never wake a sleeping model.
* Crash restart - a model Lucy launched that died (or stopped answering for ~1 min) is restarted, at most
                 MAX_RESTARTS times per RESTART_WINDOW_S; after that it stays down with a clear error instead of
                 looping, and the next message you send tries again.

The class has no FastAPI dependency and takes `clock`, so tests drive it with a fake clock and fake servers.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Callable

log = logging.getLogger("lucy.core.supervisor")

MAX_RESTARTS = 3
RESTART_WINDOW_S = 600.0
HUNG_TICKS = 3                      # consecutive failed health checks (of a live process) before a restart


class Supervisor:
    def __init__(self, llm, embed, busy: Callable[[], bool], interval: float = 20.0,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.llm, self.embed, self.busy, self.interval, self.clock = llm, embed, busy, interval, clock
        self.restarts: list[float] = []
        self.hung = 0
        self.gave_up = False
        self.last_action = ""
        self._task: asyncio.Task | None = None

    # -- loop -----------------------------------------------------------------------
    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.get_running_loop().create_task(self._loop())

    def stop(self) -> None:
        if self._task:
            self._task.cancel()

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self.interval)
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as e:                                   # the watchdog itself must never die
                log.warning(f"supervisor tick failed: {e}")

    # -- one pass -------------------------------------------------------------------
    async def tick(self) -> None:
        llm = self.llm
        busy = bool(self.busy())
        if busy:
            llm.touch()                                              # idle time counts from when it was last busy
        if llm.phase in ("loading", "stopping"):
            return
        await self._crash_check(busy)
        await self._idle_check(busy)

    async def _crash_check(self, busy: bool) -> None:
        llm = self.llm
        if llm.phase != "idle" or llm.sleeping:
            self.hung = 0
            return
        mid = llm.active["id"]
        if mid not in llm._owned:                                    # not launched by us (or already unloaded)
            self.hung = 0
            return
        proc = llm._procs.get(mid)
        dead = proc is not None and proc.poll() is not None
        if not dead:
            if await llm._healthy(llm.active["port"]):
                self.hung = 0
                self.gave_up = False
                return
            if busy:
                return
            self.hung += 1
            if self.hung < HUNG_TICKS:
                return
        now = self.clock()
        self.restarts = [t for t in self.restarts if now - t < RESTART_WINDOW_S]
        if len(self.restarts) >= MAX_RESTARTS:
            if not self.gave_up:
                self.gave_up = True
                llm.phase = "error"
                llm.message = (f"{llm.active['label']} keeps crashing ({MAX_RESTARTS} restarts in "
                               f"{int(RESTART_WINDOW_S / 60)} min). See runtime/logs/llama_{mid}.log. "
                               f"Sending a message tries again.")
                log.error(llm.message)
            return
        self.restarts.append(now)
        self.hung = 0
        self.last_action = f"restarted {llm.active['label']} ({'crashed' if dead else 'stopped answering'})"
        log.warning(self.last_action)
        await llm.ensure_active(restart=True)

    async def _idle_check(self, busy: bool) -> None:
        llm = self.llm
        minutes = float(llm.state.get("idle_unload_min", 0) or 0)
        if minutes <= 0 or busy:
            return
        limit = minutes * 60.0
        now = self.clock()
        if llm.phase == "idle" and not llm.sleeping and llm.active["id"] in llm._owned \
                and now - llm.last_used >= limit and getattr(llm, "inflight", 0) == 0:
            if await llm.sleep_now(f"idle for {int(minutes)} min"):
                self.last_action = f"unloaded {llm.active['label']} after {int(minutes)} min idle"
                log.info(self.last_action)
        # the embed server follows its own last use, so a Stella-only workload (indexing) can't keep it alive
        try:
            if await asyncio.to_thread(self.embed.idle_stop, limit, self.clock):
                self.last_action = "stopped the embed server (idle)"
                log.info(self.last_action)
        except Exception as e:
            log.warning(f"embed idle stop failed: {e}")

    def status(self) -> dict:
        return {"restarts_recent": len(self.restarts), "gave_up": self.gave_up, "last_action": self.last_action,
                "interval_s": self.interval}
