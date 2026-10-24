"""Supervisor: idle unload, never while busy, crash restart with a limit, embed idle stop. Fake clock + fake servers."""
import asyncio
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))

from lucy.server.supervisor import Supervisor, MAX_RESTARTS, HUNG_TICKS  # noqa: E402


class Proc:
    def __init__(self, alive=True):
        self.alive = alive

    def poll(self):
        return None if self.alive else 1


class FakeLLM:
    def __init__(self, idle_min=10):
        self.phase, self.sleeping, self.last_used = "idle", False, 0.0
        self.state = {"idle_unload_min": idle_min}
        self.active = {"id": "m", "label": "Lucy 12B", "port": 8080}
        self._owned, self._procs = {"m"}, {"m": Proc()}
        self.inflight, self.healthy, self.message = 0, True, ""
        self.slept, self.restarted = [], 0

    def touch(self):
        self.last_used = CLOCK.t

    async def _healthy(self, port):
        return self.healthy

    async def sleep_now(self, reason="x"):
        self.slept.append(reason)
        self.sleeping, self.phase = True, "sleeping"
        self._owned.clear()
        return True

    async def ensure_active(self, restart=False):
        self.restarted += 1
        self._procs["m"] = Proc()
        self.healthy = True


class FakeEmbed:
    def __init__(self):
        self.stopped = 0

    def idle_stop(self, limit, now):
        if limit > 0 and now() - 0.0 >= limit and self.stopped == 0:
            self.stopped += 1
            return True
        return False


class Clock:
    t = 0.0

    def __call__(self):
        return self.t


CLOCK = Clock()


def run(coro):
    return asyncio.run(coro)


def test_unloads_after_idle_minutes_but_not_before():
    llm = FakeLLM(10)
    s = Supervisor(llm, FakeEmbed(), lambda: False, clock=CLOCK)
    CLOCK.t = 9 * 60
    run(s.tick())
    assert llm.slept == []
    CLOCK.t = 10 * 60 + 1
    run(s.tick())
    assert len(llm.slept) == 1 and llm.sleeping


def test_never_unloads_while_busy_and_idle_counts_from_last_busy():
    llm, busy = FakeLLM(10), {"b": True}
    s = Supervisor(llm, FakeEmbed(), lambda: busy["b"], clock=CLOCK)
    CLOCK.t = 3600
    run(s.tick())                                     # busy: touched at t=3600, not unloaded
    assert llm.slept == [] and llm.last_used == 3600
    busy["b"] = False
    CLOCK.t = 3600 + 5 * 60
    run(s.tick())
    assert llm.slept == []                            # only 5 min since it was last busy
    CLOCK.t = 3600 + 11 * 60
    run(s.tick())
    assert len(llm.slept) == 1


def test_zero_minutes_means_never():
    llm = FakeLLM(0)
    s = Supervisor(llm, FakeEmbed(), lambda: False, clock=CLOCK)
    CLOCK.t = 10 ** 6
    run(s.tick())
    assert llm.slept == []


def test_embed_server_idle_stop_is_independent():
    llm, emb = FakeLLM(10), FakeEmbed()
    s = Supervisor(llm, emb, lambda: False, clock=CLOCK)
    CLOCK.t = 11 * 60
    run(s.tick())
    assert emb.stopped == 1


def test_restarts_a_crashed_model_then_gives_up_after_the_limit():
    llm = FakeLLM(0)
    s = Supervisor(llm, FakeEmbed(), lambda: False, clock=CLOCK)
    for i in range(MAX_RESTARTS):
        llm._procs["m"] = Proc(alive=False)
        CLOCK.t += 30
        run(s.tick())
    assert llm.restarted == MAX_RESTARTS
    llm._procs["m"] = Proc(alive=False)
    CLOCK.t += 30
    run(s.tick())
    assert llm.restarted == MAX_RESTARTS and llm.phase == "error" and "keeps crashing" in llm.message and s.gave_up


def test_restart_window_expires():
    llm = FakeLLM(0)
    s = Supervisor(llm, FakeEmbed(), lambda: False, clock=CLOCK)
    for _ in range(MAX_RESTARTS):
        llm._procs["m"] = Proc(alive=False)
        CLOCK.t += 30
        run(s.tick())
    CLOCK.t += 3600
    llm.phase = "idle"
    llm._procs["m"] = Proc(alive=False)
    run(s.tick())
    assert llm.restarted == MAX_RESTARTS + 1


def test_hung_server_needs_several_failed_checks_and_busy_blocks_it():
    llm, busy = FakeLLM(0), {"b": False}
    s = Supervisor(llm, FakeEmbed(), lambda: busy["b"], clock=CLOCK)
    llm.healthy = False
    for _ in range(HUNG_TICKS - 1):
        run(s.tick())
    assert llm.restarted == 0
    busy["b"] = True
    run(s.tick())
    assert llm.restarted == 0                         # a long generation must not look like a hang
    busy["b"] = False
    for _ in range(HUNG_TICKS):
        run(s.tick())
    assert llm.restarted == 1


def test_not_launched_by_us_or_sleeping_is_left_alone():
    llm = FakeLLM(0)
    s = Supervisor(llm, FakeEmbed(), lambda: False, clock=CLOCK)
    llm._owned.clear()
    llm._procs["m"] = Proc(alive=False)
    run(s.tick())
    assert llm.restarted == 0
    llm._owned.add("m")
    llm.sleeping = True
    run(s.tick())
    assert llm.restarted == 0
