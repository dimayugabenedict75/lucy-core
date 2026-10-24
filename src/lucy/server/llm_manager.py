"""LLM manager: Lucy 12B (port 8080) / Lucy 4B (port 8081) llama-server lifecycle + settings.

Responsibilities
  * keep ONE model loaded: choosing a model stops the other llama-server, then starts the chosen one
  * per-model temperature preset (12B = 0.7, 4B = 0.5); the value stays editable afterwards
  * reasoning ON/OFF   -> launch flags (--reasoning off --reasoning-budget 0 / --reasoning on ...),
                          exactly like start-llm.bat, so the active model is restarted
  * KV cache Q4 / Q8   -> launch flags --cache-type-k/v, so the active model is restarted
  * api.py reads the live endpoint/temperature from the `llm` singleton at the bottom of this file

Lifetime: the llama-server is tied to this Lucy Core process (Windows Job Object, KILL_ON_JOB_CLOSE),
so whichever model is loaded unloads whenever Lucy Core stops, even after a crash or a kill.

Sleep / wake: after `idle_unload_min` minutes without use (Settings value, 0 = never) server/supervisor.py asks
sleep_now() to unload the model so the GPU is free for games; wake() (called when a chat starts) loads it again.
Before every load a VRAM check (llama.cpp --list-devices) compares free memory with what this model used last time;
if it would not fit (a game is running) the other, smaller model is loaded instead and the chosen one is retried
at the next wake.

Pinned llama.cpp: runtime/data/llama_exe.json {"llama_server": "<path>"} overrides the exe path (tools/llama_pin.py
writes it), so a bad llama.cpp update can be rolled back without touching this file.

Config: DEFAULT_MODELS_CFG below (edit it here). If runtime/data/llm_models.json exists it OVERRIDES
it, so delete that file if you only edit this module.
State (runtime/data/llm_state.json): what the settings tab saved: active model, temperature,
reasoning, kv cache.
Logs of each llama-server: runtime/logs/llama_<model-id>.log
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from lucy.server import llama_check

logger = logging.getLogger("lucy.core.llm")

KV_CHOICES = {"q4": "q4_0", "q4_0": "q4_0", "q8": "q8_0", "q8_0": "q8_0"}

DEFAULT_MODELS_CFG: dict[str, Any] = {
    "_help": ("Mirrors start-llm.bat. Use forward slashes in paths (C:/Users/...): a single backslash "
              "in a Python string is an escape sequence. Launch command built from this: "
              "llama_server -m <gguf> --mmproj <mmproj> -ngl <gpu_layers> -c <context_length> "
              "<flash_attn_args> --cache-type-k/v <Settings KV> <reasoning flags from Settings> "
              "<extra_args> --host <host> --port <model port>"),
    "llama_server": "C:/Users/dimay/AppData/Local/Microsoft/WinGet/Packages/ggml.llamacpp_Microsoft.Winget.Source_8wekyb3d8bbwe/llama-server.exe",   # winget b11829 (was D:/llama.cpp/bin)
    "host": "127.0.0.1",
    "context_length": 100000,
    "gpu_layers": 999,
    "flash_attn_args": ["--flash-attn", "on"],
    "extra_args": ["--jinja", "--parallel", "1"],
    "ready_timeout_s": 300,
    "vram_plan": True,         # check free GPU memory before loading (needs llama-server --list-devices)
    "vram_margin_mib": 700,    # keep this much free after loading
    "auto_fallback": True,     # model does not fit -> load the other (smaller) one instead of failing
    "autostart": True,      # load the Settings > Model selection when Lucy Core (8090) starts
    "models": [
        {"id": "lucy-12b", "label": "Lucy 12B", "port": 8080, "temperature": 0.7,
         "gguf": "C:/Users/dimay/Lucy/Lucy_Core/models/LLM/Lucy-12b/Lux-Plus-M12B.i1-QAT_Q4_K.gguf",
         "mmproj": "C:/Users/dimay/Lucy/Lucy_Core/models/LLM/Lucy-12b/Lux-Plus-M12B.mmproj-bf16.gguf",
         "extra_args": []},
        {"id": "lucy-4b", "label": "Lucy 4B", "port": 8081, "temperature": 0.5,
         "gguf": "C:/Users/dimay/Lucy/Lucy_Core/models/LLM/Lucy-4b/Lucy-4B-it-qat-q4_0.i1-Q4_K_M.gguf",
         "mmproj": "C:/Users/dimay/Lucy/Lucy_Core/models/LLM/Lucy-4b/Lucy-4B-it-qat-q4_0.mmproj-Q8_0.gguf",
         "extra_args": ["--reasoning", "off"]},
    ],
}

DEFAULT_STATE: dict[str, Any] = {"active": "lucy-12b", "temperature": 0.7, "reasoning": False,
                                 "kv_cache": "q8_0", "idle_unload_min": 15, "vram_learned": {}}


def _runtime_dir() -> Path:
    # <Lucy_Core>/src/lucy/server/llm_manager.py -> <Lucy_Core>/runtime
    return Path(__file__).resolve().parents[3] / "runtime"


def llama_exe_override(runtime: Path) -> str | None:
    """Path from runtime/data/llama_exe.json (pinned / rolled-back llama.cpp), if that file exists and points at an exe."""
    try:
        p = json.loads((runtime / "data" / "llama_exe.json").read_text(encoding="utf-8")).get("llama_server")
        return p if p and Path(p).is_file() else None
    except Exception:
        return None


def _mib(path: str | None) -> int:
    try:
        return int(Path(path).stat().st_size / 1048576) if path else 0
    except OSError:
        return 0


def _read_json(path: Path, default: dict) -> dict:
    try:
        return {**default, **json.loads(path.read_text(encoding="utf-8"))}
    except FileNotFoundError:
        return dict(default)
    except Exception as e:                                # corrupt file: keep running with defaults
        logger.warning(f"{path.name}: {e}; using defaults")
        return dict(default)


# ---------------------------------------------------------------- process helpers
def _listening_pids(port: int) -> set[int]:
    pids: set[int] = set()
    try:
        import psutil
        for c in psutil.net_connections(kind="tcp"):
            if c.status == psutil.CONN_LISTEN and c.laddr and c.laddr.port == port and c.pid:
                pids.add(c.pid)
        return pids
    except Exception:                                     # no psutil / access denied: fall back
        pass
    if sys.platform == "win32":
        try:
            out = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True, text=True,
                                 creationflags=0x08000000, timeout=10).stdout
            for line in out.splitlines():
                parts = line.split()
                if len(parts) >= 5 and parts[3] == "LISTENING" and parts[1].rsplit(":", 1)[-1] == str(port):
                    pids.add(int(parts[4]))
        except Exception as e:
            logger.warning(f"netstat failed: {e}")
    return pids


def _process_name(pid: int) -> str:
    try:
        import psutil
        return psutil.Process(pid).name().lower()
    except Exception:
        pass
    if sys.platform == "win32":
        try:
            out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                                 capture_output=True, text=True, creationflags=0x08000000, timeout=10).stdout
            return out.strip().strip('"').split('","')[0].lower()
        except Exception:
            pass
    return ""


def _kill_tree(pid: int) -> None:
    try:
        import psutil
        proc = psutil.Process(pid)
        kids = proc.children(recursive=True)
        for p in [*kids, proc]:
            with _suppress():
                p.terminate()
        _, alive = psutil.wait_procs([*kids, proc], timeout=8)
        for p in alive:
            with _suppress():
                p.kill()
        return
    except ImportError:
        pass
    except Exception as e:
        logger.warning(f"psutil kill failed for {pid}: {e}")
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True,
                       creationflags=0x08000000, timeout=15)
    else:
        os.kill(pid, 15)


class _suppress:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return True


_JOB_HANDLE = None          # kept for the life of the process; closing it (= process exit) kills the models


def _bind_to_lucy_core(pid: int) -> bool:
    """Windows: put `pid` in a Job Object with KILL_ON_JOB_CLOSE so the llama-server is terminated
    whenever Lucy Core's process ends (normal exit, crash, Task Manager kill, console closed).
    Other platforms: handled by the atexit hook only."""
    global _JOB_HANDLE
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        from ctypes import wintypes as wt

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [(n, ctypes.c_ulonglong) for n in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

        class BASIC(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                        ("LimitFlags", wt.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wt.DWORD),
                        ("Affinity", ctypes.c_size_t), ("PriorityClass", wt.DWORD), ("SchedulingClass", wt.DWORD)]

        class EXTENDED(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", BASIC), ("IoInfo", IO_COUNTERS),
                        ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateJobObjectW.restype = wt.HANDLE
        k32.CreateJobObjectW.argtypes = [wt.LPVOID, wt.LPCWSTR]
        k32.OpenProcess.restype = wt.HANDLE
        k32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
        k32.AssignProcessToJobObject.argtypes = [wt.HANDLE, wt.HANDLE]
        k32.SetInformationJobObject.argtypes = [wt.HANDLE, ctypes.c_int, wt.LPVOID, wt.DWORD]
        k32.CloseHandle.argtypes = [wt.HANDLE]

        if _JOB_HANDLE is None:
            job = k32.CreateJobObjectW(None, None)
            if not job:
                raise ctypes.WinError(ctypes.get_last_error())
            info = EXTENDED()
            info.BasicLimitInformation.LimitFlags = 0x2000          # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not k32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):
                raise ctypes.WinError(ctypes.get_last_error())
            _JOB_HANDLE = job
        proc = k32.OpenProcess(0x0100 | 0x0001, False, pid)         # PROCESS_SET_QUOTA | PROCESS_TERMINATE
        if not proc:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            if not k32.AssignProcessToJobObject(_JOB_HANDLE, proc):
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            k32.CloseHandle(proc)
        return True
    except Exception as e:
        logger.warning(f"Could not bind llama-server (pid {pid}) to Lucy Core's lifetime: {e}")
        return False


def stop_port(port: int) -> list[int]:
    """Stop the llama-server listening on `port`. Refuses to kill anything that isn't llama-server."""
    killed = []
    for pid in _listening_pids(port):
        name = _process_name(pid)
        if "llama" not in name:
            raise RuntimeError(f"port {port} is held by '{name or pid}', not llama-server; not killing it")
        logger.info(f"Stopping llama-server pid={pid} on port {port}")
        _kill_tree(pid)
        killed.append(pid)
    deadline = time.time() + 15
    while time.time() < deadline and _listening_pids(port):
        time.sleep(0.25)
    if _listening_pids(port):
        raise RuntimeError(f"port {port} is still in use after stopping llama-server")
    return killed


# ---------------------------------------------------------------- manager
class LLMManager:
    def __init__(self, runtime: Path | None = None) -> None:
        self.runtime = runtime or _runtime_dir()
        self.data_dir = self.runtime / "data"
        self.models_path = self.data_dir / "llm_models.json"
        self.state_path = self.data_dir / "llm_state.json"
        self._lock = threading.Lock()                     # guards state/phase fields
        self._busy = asyncio.Lock()                       # serialises model transitions
        self._task: asyncio.Task | None = None
        self._procs: dict[str, subprocess.Popen] = {}
        self._owned: set[str] = set()                     # models launched by THIS Lucy Core process
        self.phase = "idle"                               # idle | stopping | loading | sleeping | error
        self.message = ""
        self.sleeping = False
        self.last_used = time.monotonic()                 # last time a chat used the model (idle unload counts from here)
        self.inflight = 0
        self.fallback_note = ""
        self.exe_source = "config"
        self.load()

    # -- config ---------------------------------------------------------------
    def load(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        if self.models_path.exists():
            logger.warning(f"{self.models_path} exists and overrides DEFAULT_MODELS_CFG in llm_manager.py")
        self.cfg = _read_json(self.models_path, DEFAULT_MODELS_CFG)
        ov = llama_exe_override(self.runtime)
        if ov:
            self.cfg["llama_server"] = ov
            self.exe_source = "pinned override (runtime/data/llama_exe.json)"
        self.models: dict[str, dict] = {m["id"]: m for m in self.cfg["models"]}
        st = _read_json(self.state_path, DEFAULT_STATE)
        if st["active"] not in self.models:
            st["active"] = next(iter(self.models))
            st["temperature"] = self.models[st["active"]]["temperature"]
        st["kv_cache"] = KV_CHOICES.get(str(st["kv_cache"]).lower(), "q8_0")
        try:
            st["idle_unload_min"] = max(0, min(1440, int(st.get("idle_unload_min", 15))))
        except (TypeError, ValueError):
            st["idle_unload_min"] = 15
        if not isinstance(st.get("vram_learned"), dict):
            st["vram_learned"] = {}
        self.state = st
        self.preferred = st["active"]                      # what the user chose; state["active"] may differ while a fallback runs
        if not self.state_path.exists():
            self.state["temperature"] = self.models[st["active"]]["temperature"]
            self._save()

    def _save(self) -> None:
        try:
            self.state_path.write_text(json.dumps({**self.state, "active": self.preferred}, indent=2), encoding="utf-8")
        except OSError as e:
            logger.warning(f"could not save llm_state.json: {e}")

    # -- live values used by api.py ----------------------------------------
    @property
    def active(self) -> dict:
        return self.models[self.state["active"]]

    @property
    def api_url(self) -> str:
        return f"http://127.0.0.1:{self.active['port']}/v1"

    @property
    def model_name(self) -> str:
        return self.active["gguf"]

    @property
    def temperature(self) -> float:
        return float(self.state["temperature"])

    @property
    def reasoning(self) -> bool:
        return bool(self.state["reasoning"])

    def request_extras(self) -> dict:
        """Extra fields for chat/completions payloads. Reasoning is a launch flag now. cache_prompt makes llama-server
        reuse the KV cache of the unchanged start of the prompt (persona, rules, history) instead of re-reading it."""
        return {"cache_prompt": True}

    # -- health -----------------------------------------------------------------
    async def _healthy(self, port: int) -> bool:
        import httpx
        try:
            async with httpx.AsyncClient(timeout=2.0) as c:
                return (await c.get(f"http://127.0.0.1:{port}/health")).status_code == 200
        except Exception:
            return False

    async def status(self) -> dict:
        health = await asyncio.gather(*(self._healthy(m["port"]) for m in self.models.values()))
        return {
            "active": self.state["active"],
            "temperature": self.temperature,
            "reasoning": self.reasoning,
            "kv_cache": self.state["kv_cache"],
            "phase": self.phase,
            "message": self.message,
            "sleeping": self.sleeping,
            "preferred": self.preferred,
            "fallback_note": self.fallback_note,
            "idle_unload_min": self.state["idle_unload_min"],
            "idle_for_s": int(time.monotonic() - self.last_used),
            "exe_source": self.exe_source,
            "models": [{"id": m["id"], "label": m["label"], "port": m["port"],
                        "temperature_preset": m["temperature"], "ready": ok}
                       for m, ok in zip(self.models.values(), health)],
        }

    # -- settings changes ----------------------------------------------------------
    def update(self, body: dict) -> list[str]:
        """Apply a partial settings change. Returns what needs (re)loading: [] | ['switch'] | ['restart']."""
        todo: list[str] = []
        with self._lock:
            if "model" in body and body["model"] != self.state["active"]:
                if body["model"] not in self.models:
                    raise ValueError(f"unknown model '{body['model']}'")
                self.state["active"] = self.preferred = body["model"]
                self.state["temperature"] = self.models[body["model"]]["temperature"]   # preset
                todo.append("switch")
            if "temperature" in body and "switch" not in todo:
                t = float(body["temperature"])
                if not 0.0 <= t <= 2.0:
                    raise ValueError("temperature must be between 0 and 2")
                self.state["temperature"] = round(t, 2)
            if "reasoning" in body:
                v = body["reasoning"]
                new = v if isinstance(v, bool) else str(v).lower() in ("on", "true", "1", "yes")
                if new != self.state["reasoning"]:
                    self.state["reasoning"] = new
                    if "switch" not in todo and "restart" not in todo:
                        todo.append("restart")
            if "idle_unload_min" in body:
                try:
                    m = int(body["idle_unload_min"])
                except (TypeError, ValueError):
                    raise ValueError("idle_unload_min must be a whole number of minutes (0 = never)")
                if not 0 <= m <= 1440:
                    raise ValueError("idle_unload_min must be between 0 and 1440")
                self.state["idle_unload_min"] = m
                self.touch()
            if "kv_cache" in body:
                kv = KV_CHOICES.get(str(body["kv_cache"]).lower())
                if kv is None:
                    raise ValueError("kv_cache must be q4 or q8")
                if kv != self.state["kv_cache"]:
                    self.state["kv_cache"] = kv
                    if "switch" not in todo:
                        todo.append("restart")
            self._save()
        return todo

    def schedule(self, todo: list[str]) -> None:
        if not todo:
            return
        restart = "restart" in todo and "switch" not in todo
        self._task = asyncio.get_running_loop().create_task(self.ensure_active(restart=restart))

    # -- lifecycle -------------------------------------------------------------------
    async def ensure_active(self, restart: bool = False) -> None:
        """Make the active model the only one loaded (restart=True: reload it, e.g. new KV cache)."""
        async with self._busy:
            self.sleeping = False
            self.state["active"] = self.preferred                      # always try the chosen model first
            target = self.active
            free_before = None
            try:
                self.phase, self.message = "stopping", "Unloading other model..."
                # A server we did not launch (e.g. started by hand / by an older Lucy Core) is not tied
                # to this process and may have different flags: reload it so Settings is what's loaded.
                restart = restart or target["id"] not in self._owned
                for m in self.models.values():
                    if m["id"] != target["id"] or restart:
                        await asyncio.to_thread(stop_port, m["port"])
                        self._owned.discard(m["id"])
                if await self._healthy(target["port"]):
                    self.phase, self.message = "idle", f"{target['label']} ready"
                    self.touch()
                    return
                target, free_before = await self._fit_target(target)
                self.phase, self.message = "loading", f"Loading {target['label']}..."
                proc = await asyncio.to_thread(self._launch, target)
                self._owned.add(target["id"])
                await self._wait_ready(target, proc)
                self.phase, self.message = "idle", f"{target['label']} ready" + (f" ({self.fallback_note})" if self.fallback_note else "")
                self.touch()
                await self._learn_vram(target, free_before)
                logger.info(f"{target['label']} ready on port {target['port']} (kv={self.state['kv_cache']})")
            except Exception as e:
                self.phase, self.message = "error", str(e)
                logger.error(f"LLM switch failed: {e}")

    # -- sleep / wake / memory planning ---------------------------------------------------
    def touch(self) -> None:
        """A chat used (or is about to use) the model: restart the idle clock."""
        self.last_used = time.monotonic()

    async def wake(self) -> None:
        """A chat is starting: if the model is unloaded (idle sleep, crash, never started) begin loading it NOW.
        Non-blocking: the agent loop already waits for the model and shows 'loading' in the UI."""
        self.touch()
        if self.phase in ("loading", "stopping"):
            return
        if self.sleeping or self.phase == "sleeping" or not await self._healthy(self.active["port"]):
            self.phase, self.message = "loading", f"Loading {self.models[self.preferred]['label']}..."
            self._task = asyncio.get_running_loop().create_task(self.ensure_active())

    async def sleep_now(self, reason: str = "manual") -> bool:
        """Unload every llama-server so the GPU is free. Returns False if a load/unload is already in progress."""
        if self.phase in ("loading", "stopping"):
            return False
        async with self._busy:
            self.phase, self.message = "stopping", "Unloading..."
            try:
                for m in self.models.values():
                    await asyncio.to_thread(stop_port, m["port"])
            except Exception as e:
                self.phase, self.message = "error", f"Could not unload: {e}"
                logger.error(self.message)
                return False
            self._owned.clear()
            self.sleeping = True
            self.phase, self.message = "sleeping", f"Unloaded ({reason}). It loads again when you send a message."
            logger.info(self.message)
            return True

    def _need_mib(self, m: dict) -> tuple[int, bool]:
        """(MiB the model needs on the GPU, measured?). Unmeasured = just the file sizes: a floor, never an overestimate."""
        key = f"{m['id']}:{self.cfg['context_length']}:{self.state['kv_cache']}"
        learned = self.state["vram_learned"].get(key)
        if learned:
            return int(learned), True
        return _mib(m["gguf"]) + _mib(m.get("mmproj")), False

    async def _fit_target(self, target: dict) -> tuple[dict, int | None]:
        """Check free GPU memory before loading. Returns (model to load, free MiB before loading or None)."""
        self.fallback_note = ""
        if not self.cfg.get("vram_plan", True):
            return target, None
        free = await asyncio.to_thread(llama_check.free_vram_mib, self.cfg["llama_server"], 0.0)
        if free is None:
            return target, None
        margin = int(self.cfg.get("vram_margin_mib", 700))
        need, known = self._need_mib(target)
        if free >= need + margin:
            return target, free
        if self.cfg.get("auto_fallback", True):
            for m in sorted((x for x in self.models.values() if x["id"] != target["id"]),
                            key=lambda x: self._need_mib(x)[0]):
                n2, _ = self._need_mib(m)
                if n2 < need and free >= n2 + margin:
                    self.state["active"] = m["id"]
                    self.fallback_note = (f"{target['label']} needs about {need} MiB but only {free} MiB are free, "
                                          f"so {m['label']} was loaded instead; {target['label']} is retried at the next wake")
                    logger.warning(self.fallback_note)
                    return m, free
        raise RuntimeError(f"Not enough free GPU memory for {target['label']}: {free} MiB free, needs about {need}"
                           f"{'' if known else '+'} MiB. Close whatever is using the GPU (a game?) and send a message again.")

    async def _learn_vram(self, m: dict, free_before: int | None) -> None:
        """Remember how much GPU memory this model really took (free before - free after), for the next planning."""
        if free_before is None or not self.cfg.get("vram_plan", True):
            return
        await asyncio.sleep(2.0)                                   # let the allocations settle
        after = await asyncio.to_thread(llama_check.free_vram_mib, self.cfg["llama_server"], 0.0)
        if after is None:
            return
        used = free_before - after
        floor = max(300, int(_mib(m["gguf"]) * 0.5))
        if used < floor:                                           # implausible: something else freed memory meanwhile
            return
        key = f"{m['id']}:{self.cfg['context_length']}:{self.state['kv_cache']}"
        self.state["vram_learned"][key] = int(used)
        self._save()
        logger.info(f"{m['label']} uses about {used} MiB of GPU memory (saved for load planning)")

    def _build_argv(self, m: dict) -> list[str]:
        exe = self.cfg["llama_server"]
        resolved = shutil.which(exe) or (exe if Path(exe).is_file() else None)
        if not resolved:
            raise RuntimeError(f"llama-server not found ('{exe}'). Set llama_server in {self.models_path}")
        if not Path(m["gguf"]).is_file():
            raise RuntimeError(f"{m['label']}: GGUF not found: {m['gguf']}. Edit {self.models_path}")
        kv = self.state["kv_cache"]
        argv = [resolved, "-m", m["gguf"], "--host", self.cfg["host"], "--port", str(m["port"]),
                "-c", str(self.cfg["context_length"]), "-ngl", str(self.cfg["gpu_layers"]),
                *self.cfg["flash_attn_args"], "--cache-type-k", kv, "--cache-type-v", kv,
                *(["--reasoning", "on", "--reasoning-budget", "-1"] if self.reasoning
                  else ["--reasoning", "off", "--reasoning-budget", "0"]),
                *self.cfg["extra_args"], *m.get("extra_args", [])]
        if m.get("mmproj"):
            argv += ["--mmproj", m["mmproj"]]
        return argv

    def _launch(self, m: dict) -> subprocess.Popen:
        argv = self._build_argv(m)
        log_dir = self.runtime / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log = open(log_dir / f"llama_{m['id']}.log", "ab")
        log.write(f"\n==== {time.strftime('%F %T')} {' '.join(argv)}\n".encode())
        log.flush()
        flags = 0
        if sys.platform == "win32":
            flags = 0x08000000 | 0x00000200            # CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP
        logger.info("Launching: " + " ".join(argv))
        proc = subprocess.Popen(argv, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                cwd=str(Path(argv[0]).parent) if Path(argv[0]).is_absolute() else None,
                                creationflags=flags)
        if _bind_to_lucy_core(proc.pid):
            logger.info(f"{m['label']} (pid {proc.pid}) is tied to Lucy Core: it unloads when Lucy Core stops")
        self._procs[m["id"]] = proc
        return proc

    async def _wait_ready(self, m: dict, proc: subprocess.Popen) -> None:
        deadline = time.time() + float(self.cfg["ready_timeout_s"])
        while time.time() < deadline:
            if proc.poll() is not None:
                raise RuntimeError(f"{m['label']} exited with code {proc.returncode}; "
                                   f"see runtime/logs/llama_{m['id']}.log")
            if await self._healthy(m["port"]):
                return
            await asyncio.sleep(1.0)
        raise RuntimeError(f"{m['label']} did not become ready in {self.cfg['ready_timeout_s']}s")


llm = LLMManager()


def _unload_on_exit() -> None:
    for mid, proc in list(llm._procs.items()):
        if proc.poll() is None:
            logger.info(f"Lucy Core stopping: unloading {llm.models[mid]['label']}")
            with _suppress():
                _kill_tree(proc.pid)


import atexit as _atexit
_atexit.register(_unload_on_exit)
