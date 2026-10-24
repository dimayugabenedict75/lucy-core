"""LLMManager: idle setting, sleep/wake, VRAM planning (fit / fall back to the smaller model / refuse), learned usage."""
import asyncio
import os
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))

from lucy.server import llama_check, llm_manager  # noqa: E402
from lucy.server.llm_manager import LLMManager, llama_exe_override  # noqa: E402


def sparse(path: pathlib.Path, mib: int) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        f.truncate(mib * 1048576)
    return path.as_posix()


def mk(tmp_path, big=5000, small=2000) -> LLMManager:
    rt = tmp_path / "runtime"
    (rt / "data").mkdir(parents=True)
    import json
    cfg = {"llama_server": sparse(tmp_path / "llama-server.exe", 1), "models": [
        {"id": "big", "label": "Big", "port": 18080, "temperature": 0.7, "gguf": sparse(tmp_path / "big.gguf", big), "mmproj": ""},
        {"id": "small", "label": "Small", "port": 18081, "temperature": 0.5, "gguf": sparse(tmp_path / "small.gguf", small), "mmproj": ""},
    ]}
    (rt / "data" / "llm_models.json").write_text(json.dumps(cfg), encoding="utf-8")
    (rt / "data" / "llm_state.json").write_text(json.dumps({"active": "big"}), encoding="utf-8")
    return LLMManager(runtime=rt)


class Patch:
    """Tiny monkeypatch that works with or without pytest."""
    def __init__(self):
        self.undo = []

    def set(self, obj, name, val):
        self.undo.append((obj, name, getattr(obj, name)))
        setattr(obj, name, val)

    def close(self):
        for o, n, v in reversed(self.undo):
            setattr(o, n, v)


def test_idle_setting_validated_saved_and_default(tmp_path):
    m = mk(tmp_path)
    assert m.state["idle_unload_min"] == 15
    assert m.update({"idle_unload_min": 30}) == [] and m.state["idle_unload_min"] == 30
    assert LLMManager(runtime=m.runtime).state["idle_unload_min"] == 30                # persisted
    for bad in (-1, 5000, "abc"):
        try:
            m.update({"idle_unload_min": bad})
            assert False, bad
        except ValueError:
            pass


def test_sleep_unloads_everything_and_marks_state(tmp_path):
    m = mk(tmp_path)
    p = Patch()
    stopped = []
    p.set(llm_manager, "stop_port", lambda port: stopped.append(port) or [])
    try:
        m._owned.add("big")
        assert asyncio.run(m.sleep_now("test")) is True
        assert sorted(stopped) == [18080, 18081] and m.sleeping and m.phase == "sleeping" and not m._owned
        st = asyncio.run(m.status())
        assert st["sleeping"] is True and st["phase"] == "sleeping"
    finally:
        p.close()


def test_sleep_refused_while_loading(tmp_path):
    m = mk(tmp_path)
    m.phase = "loading"
    assert asyncio.run(m.sleep_now()) is False


def test_wake_starts_load_when_sleeping_and_not_twice(tmp_path):
    m = mk(tmp_path)
    started = []

    async def fake_ensure(restart=False):
        started.append(1)
        m.phase = "idle"
    m.ensure_active = fake_ensure

    async def go():
        m.sleeping, m.phase = True, "sleeping"
        await m.wake()
        assert m.phase == "loading"
        await m.wake()                                   # already loading: no second load
        await asyncio.sleep(0.05)
    asyncio.run(go())
    assert started == [1]


def test_wake_does_nothing_when_model_is_up(tmp_path):
    m = mk(tmp_path)

    async def up(port):
        return True
    m._healthy = up
    started = []
    m.ensure_active = lambda restart=False: started.append(1)
    asyncio.run(m.wake())
    assert started == [] and m.phase == "idle"


def _plan(m, free):
    p = Patch()
    p.set(llama_check, "free_vram_mib", lambda exe, age=0: free)
    try:
        return asyncio.run(m._fit_target(m.models["big"]))
    finally:
        p.close()


def test_fits_loads_chosen_model(tmp_path):
    m = mk(tmp_path)
    t, free = _plan(m, 9000)
    assert t["id"] == "big" and free == 9000 and m.fallback_note == "" and m.state["active"] == "big"


def test_game_running_falls_back_to_smaller_model_and_remembers_preference(tmp_path):
    m = mk(tmp_path)
    t, _ = _plan(m, 3000)                                 # big needs >= 5000+700; small needs 2000+700
    assert t["id"] == "small" and m.state["active"] == "small" and m.preferred == "big"
    assert "Big" in m.fallback_note and "Small" in m.fallback_note
    assert llm_manager.json.loads(m.state_path.read_text("utf-8")).get("active") in (None, "big")   # saved choice untouched


def test_nothing_fits_gives_clear_error(tmp_path):
    m = mk(tmp_path)
    try:
        _plan(m, 1000)
        assert False, "should refuse"
    except RuntimeError as e:
        assert "Not enough free GPU memory" in str(e) and "1000" in str(e)


def test_unknown_free_memory_never_blocks_loading(tmp_path):
    m = mk(tmp_path)
    t, free = _plan(m, None)
    assert t["id"] == "big" and free is None


def test_fallback_can_be_turned_off(tmp_path):
    m = mk(tmp_path)
    m.cfg["auto_fallback"] = False
    try:
        _plan(m, 3000)
        assert False
    except RuntimeError:
        pass


def test_learned_usage_beats_the_file_size_floor(tmp_path):
    m = mk(tmp_path)
    key = f"big:{m.cfg['context_length']}:{m.state['kv_cache']}"
    m.state["vram_learned"][key] = 14000                  # measured: the 5 GB file really takes 14 GB with its context
    assert m._need_mib(m.models["big"]) == (14000, True)
    t, _ = _plan(m, 12000)
    assert t["id"] == "small"


def test_learn_vram_records_plausible_numbers_only(tmp_path):
    m = mk(tmp_path)
    p = Patch()
    p.set(asyncio, "sleep", _instant_sleep())
    try:
        p.set(llama_check, "free_vram_mib", lambda exe, age=0: 5000)
        asyncio.run(m._learn_vram(m.models["big"], 15000))             # used 10000
        key = f"big:{m.cfg['context_length']}:{m.state['kv_cache']}"
        assert m.state["vram_learned"][key] == 10000
        asyncio.run(m._learn_vram(m.models["small"], 5000))            # used 0: implausible -> ignored
        assert not [k for k in m.state["vram_learned"] if k.startswith("small")]
    finally:
        p.close()


def _instant_sleep():
    real = asyncio.sleep

    async def fast(delay, *a, **k):
        return await real(0)
    return fast


def test_pinned_exe_override(tmp_path):
    m = mk(tmp_path)
    assert llama_exe_override(m.runtime) is None
    exe = sparse(tmp_path / "pinned" / "llama-server.exe", 1)
    (m.runtime / "data" / "llama_exe.json").write_text('{"llama_server": "%s"}' % exe, encoding="utf-8")
    assert llama_exe_override(m.runtime) == exe
    m2 = LLMManager(runtime=m.runtime)
    assert m2.cfg["llama_server"] == exe and "pinned" in m2.exe_source
    (m.runtime / "data" / "llama_exe.json").write_text('{"llama_server": "%s"}' % (tmp_path / "gone.exe").as_posix(), encoding="utf-8")
    assert llama_exe_override(m.runtime) is None                       # pointing at nothing is ignored


def test_ensure_active_end_to_end_with_fallback_then_back_after_sleep(tmp_path):
    """Game running -> wake loads the small model; after sleep + free GPU the next wake loads the chosen one again."""
    m = mk(tmp_path)
    p = Patch()
    launched = []
    free = {"v": 3000}

    async def healthy(port):
        return False

    async def no_wait(mm, proc):
        return None

    async def no_learn(mm, fb):
        return None
    m._healthy, m._wait_ready, m._learn_vram = healthy, no_wait, no_learn
    m._launch = lambda mm: launched.append(mm["id"]) or object()
    p.set(llm_manager, "stop_port", lambda port: [])
    p.set(llama_check, "free_vram_mib", lambda exe, age=0: free["v"])
    try:
        m.sleeping, m.phase = True, "sleeping"
        asyncio.run(m.ensure_active())
        assert launched == ["small"] and m.phase == "idle" and not m.sleeping
        assert m.state["active"] == "small" and m.preferred == "big" and "Small" in m.message
        free["v"] = 12000
        asyncio.run(m.sleep_now())
        asyncio.run(m.ensure_active())
        assert launched == ["small", "big"] and m.state["active"] == "big" and m.fallback_note == ""
    finally:
        p.close()
