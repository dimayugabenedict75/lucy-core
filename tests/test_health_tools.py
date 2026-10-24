"""llama.cpp facts (version / GPU memory / flag check), health snapshot helpers, embed server idle + CPU fallback."""
import asyncio
import logging
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))

from lucy.server import health, llama_check  # noqa: E402
from lucy.server.embed_manager import EmbedServer  # noqa: E402

HELP = """
-m,    --model FNAME                    model path
-c,    --ctx-size N                     context size
-ngl,  --gpu-layers N                   layers on GPU
-fa,   --flash-attn [on|off|auto]       flash attention
       --port PORT                      port
       --reasoning-budget N             budget
       --embeddings                     embedding mode
"""


class Patch:
    def __init__(self):
        self.undo = []

    def set(self, obj, name, val):
        self.undo.append((obj, name, getattr(obj, name)))
        setattr(obj, name, val)

    def close(self):
        for o, n, v in reversed(self.undo):
            setattr(o, n, v)


def test_parse_devices_and_free_vram():
    txt = "Available devices:\n  Vulkan0: AMD Radeon RX 9060 XT (16304 MiB, 15427 MiB free)\n  Vulkan1: iGPU (2048 MiB, 100 MiB free)\n"
    d = llama_check.parse_devices(txt)
    assert [x["free_mib"] for x in d] == [15427, 100] and d[0]["name"] == "AMD Radeon RX 9060 XT"
    assert llama_check.parse_devices("garbage") == []
    p = Patch()
    p.set(llama_check, "_run", lambda exe, *a, **k: txt)
    llama_check._DEV_CACHE.update(t=0.0, exe=None, val=None)
    try:
        assert llama_check.free_vram_mib("x.exe", 0) == 15427                        # the biggest GPU
    finally:
        p.close()
        llama_check._DEV_CACHE.update(t=0.0, exe=None, val=None)


def test_flags_in_ignores_values_including_negative_numbers():
    assert llama_check.flags_in(["x", "-m", "a.gguf", "--reasoning-budget", "-1", "--port", "8080", "-c", "100000"]) == \
        ["--port", "--reasoning-budget", "-c", "-m"]


def test_unknown_flags_found_after_an_update_renames_one(tmp_path):
    exe = tmp_path / "llama-server.exe"
    exe.write_bytes(b"x")
    p = Patch()
    p.set(llama_check, "_run", lambda e, *a, **k: HELP if a == ("--help",) else "version: 1 (build 2)")
    llama_check._FLAGS_CACHE.clear()
    try:
        argv = [str(exe), "-m", "a", "--port", "1", "-c", "2", "--flash-attn", "on", "--reasoning", "off"]
        assert llama_check.unknown_flags(str(exe), argv) == ["--reasoning"]            # '--reasoning-budget' must not satisfy '--reasoning'
        res = llama_check.check(str(exe), {"Lucy 12B": argv, "Bad": RuntimeError("GGUF not found")})
        assert not res["ok"] and any("--reasoning" in x for x in res["problems"]) and any("GGUF" in x for x in res["problems"])
        assert res["version"].startswith("version: 1")
    finally:
        p.close()
        llama_check._FLAGS_CACHE.clear()


def test_missing_exe_is_a_clear_problem(tmp_path):
    r = llama_check.check(str(tmp_path / "nope.exe"), {})
    assert r["exists"] is False and "not found" in r["problems"][0]


def test_log_capture_keeps_lucy_warnings_only():
    health.install_log_capture()
    health.install_log_capture()                                                       # twice = still one handler
    assert logging.getLogger().handlers.count(health._RING) == 1
    logging.getLogger("lucy.core.test").warning("gpu is hot")
    logging.getLogger("lucy.core.test").info("just info")
    logging.getLogger("someone.else").warning("not ours")
    msgs = [x["msg"] for x in health.recent_problems()]
    assert "gpu is hot" in msgs and "just info" not in msgs and "not ours" not in msgs


class FakeLLM:
    sleeping = False
    phase = "idle"
    message = ""
    exe_source = "config"
    state = {"active": "m"}
    models = {"m": {"id": "m", "label": "Lucy 12B", "port": 1}}
    cfg = {"llama_server": "x.exe"}

    async def status(self):
        return {"phase": "idle"}


def test_embed_idle_stop_and_cooldown_logic(tmp_path):
    s = EmbedServer(runtime=tmp_path)
    stopped = []
    s.stop = lambda: stopped.append(1)
    s.is_ready = lambda: True
    s.last_used = 100.0
    assert s.idle_stop(600, now=lambda: 400.0) is False                                # used 5 min ago, limit 10
    assert s.idle_stop(600, now=lambda: 800.0) is True and stopped == [1]
    assert s.idle_stop(0, now=lambda: 10 ** 9) is False                                # 0 = never
    s.is_ready = lambda: False
    assert s.idle_stop(600, now=lambda: 10 ** 9) is False                              # nothing running: nothing to stop


def test_embed_runs_on_cpu_when_the_gpu_is_full(tmp_path):
    model = tmp_path / "stella.snq"
    with open(model, "wb") as f:
        f.truncate(900 * 1048576)
    s = EmbedServer(runtime=tmp_path)
    s.cfg.update(model=str(model), llama_server=str(tmp_path / "llama-server.exe"), gpu_layers=99)
    p = Patch()
    try:
        p.set(llama_check, "free_vram_mib", lambda exe, age=0: 700)                     # a game ate the GPU
        assert s._pick_gpu_layers() == 0 and "CPU" in s.cpu_note
        assert s.build_argv(0)[s.build_argv(0).index("-ngl") + 1] == "0"
        p.set(llama_check, "free_vram_mib", lambda exe, age=0: 8000)
        assert s._pick_gpu_layers() is None and s.cpu_note == ""
        p.set(llama_check, "free_vram_mib", lambda exe, age=0: None)                    # unknown: don't second-guess
        assert s._pick_gpu_layers() is None
        s.cfg["vram_plan"] = False
        p.set(llama_check, "free_vram_mib", lambda exe, age=0: 10)
        assert s._pick_gpu_layers() is None
    finally:
        p.close()


def test_embed_default_exe_follows_the_pinned_override(tmp_path):
    (tmp_path / "data").mkdir()
    exe = tmp_path / "pinned" / "llama-server.exe"
    exe.parent.mkdir()
    exe.write_bytes(b"x")
    (tmp_path / "data" / "llama_exe.json").write_text('{"llama_server": "%s"}' % exe.as_posix(), encoding="utf-8")
    assert EmbedServer(runtime=tmp_path).cfg["llama_server"] == exe.as_posix()


def test_api_wiring_is_in_place():
    src = (HERE.parent / "src" / "lucy" / "server" / "api.py").read_text(encoding="utf-8")
    for needle in ('await llm.wake()', '"/api/health/full"', '"/health-panel"', '"/memory-panel"', "/api/memory/explain",
                   "/api/search/semantic", "Supervisor(llm, embed_server", "_db_migrate.run(", "_semantic.search("):
        assert needle in src, needle
    assert src.count("await llm.wake()") >= 2                                          # chat + CLI endpoints
    ui = HERE.parent / "ui"
    assert "__API_KEY_PLACEHOLDER__" in (ui / "health.html").read_text(encoding="utf-8")
    assert "__API_KEY_PLACEHOLDER__" in (ui / "memory.html").read_text(encoding="utf-8")
