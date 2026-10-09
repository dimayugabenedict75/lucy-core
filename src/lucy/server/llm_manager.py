"""LLM manager: Lucy 12B (port 8080) / Lucy 4B (port 8081) llama-server lifecycle + settings.

Responsibilities
  * keep ONE model loaded: choosing a model stops the other llama-server, then starts the chosen one
  * per-model temperature preset (12B = 0.7, 4B = 0.5); the value stays editable afterwards
  * reasoning ON/OFF   -> launch flags (--reasoning off --reasoning-budget 0 / --reasoning on ...),
                          exactly like start-llm.bat, so the active model is restarted
  * KV cache Q4 / Q8   -> launch flags --cache-type-k/v, so the active model is restarted
  * api.py reads the live endpoint/temperature from the `llm` singleton at the bottom of this file

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

logger = logging.getLogger("lucy.core.llm")

KV_CHOICES = {"q4": "q4_0", "q4_0": "q4_0", "q8": "q8_0", "q8_0": "q8_0"}

DEFAULT_MODELS_CFG: dict[str, Any] = {
    "_help": ("Mirrors start-llm.bat. Use forward slashes in paths (C:/Users/...): a single backslash "
              "in a Python string is an escape sequence. Launch command built from this: "
              "llama_server -m <gguf> --mmproj <mmproj> -ngl <gpu_layers> -c <context_length> "
              "<flash_attn_args> --cache-type-k/v <Settings KV> <reasoning flags from Settings> "
              "<extra_args> --host <host> --port <model port>"),
    "llama_server": "D:/llama.cpp/bin/llama-server.exe",
    "host": "127.0.0.1",
    "context_length": 100000,
    "gpu_layers": 999,
    "flash_attn_args": ["--flash-attn", "on"],
    "extra_args": ["--jinja", "--parallel", "1"],
    "ready_timeout_s": 300,
    "autostart": False,
    "models": [
        {"id": "lucy-12b", "label": "Lucy 12B", "port": 8080, "temperature": 0.7,
         "gguf": "C:/Users/dimay/Lucy/Lucy_Core/models/Lucy-12b/Lux-Plus-M12B.i1-QAT_Q4_K.gguf",
         "mmproj": "C:/Users/dimay/Lucy/Lucy_Core/models/Lucy-12b/Lux-Plus-M12B.mmproj-bf16.gguf",
         "extra_args": []},
        {"id": "lucy-4b", "label": "Lucy 4B", "port": 8081, "temperature": 0.5,
         "gguf": "C:/Users/dimay/Lucy/Lucy_Core/models/Lucy-4b/Lucy-4B-it-qat-q4_0.i1-Q4_K_M.gguf",
         "mmproj": "C:/Users/dimay/Lucy/Lucy_Core/models/Lucy-4b/Lucy-4B-it-qat-q4_0.mmproj-Q8_0.gguf",
         "extra_args": []},
    ],
}

DEFAULT_STATE: dict[str, Any] = {"active": "lucy-12b", "temperature": 0.7, "reasoning": False,
                                 "kv_cache": "q8_0"}


def _runtime_dir() -> Path:
    # <Lucy_Core>/src/lucy/server/llm_manager.py -> <Lucy_Core>/runtime
    return Path(__file__).resolve().parents[3] / "runtime"


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
        self.phase = "idle"                               # idle | stopping | loading | error
        self.message = ""
        self.load()

    # -- config ---------------------------------------------------------------
    def load(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        if self.models_path.exists():
            logger.warning(f"{self.models_path} exists and overrides DEFAULT_MODELS_CFG in llm_manager.py")
        self.cfg = _read_json(self.models_path, DEFAULT_MODELS_CFG)
        self.models: dict[str, dict] = {m["id"]: m for m in self.cfg["models"]}
        st = _read_json(self.state_path, DEFAULT_STATE)
        if st["active"] not in self.models:
            st["active"] = next(iter(self.models))
            st["temperature"] = self.models[st["active"]]["temperature"]
        st["kv_cache"] = KV_CHOICES.get(str(st["kv_cache"]).lower(), "q8_0")
        self.state = st
        if not self.state_path.exists():
            self.state["temperature"] = self.models[st["active"]]["temperature"]
            self._save()

    def _save(self) -> None:
        try:
            self.state_path.write_text(json.dumps(self.state, indent=2), encoding="utf-8")
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
        """Extra fields for chat/completions payloads. Reasoning is a launch flag now, so none."""
        return {}

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
                self.state["active"] = body["model"]
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
            target = self.active
            try:
                self.phase, self.message = "stopping", "Unloading other model..."
                for m in self.models.values():
                    if m["id"] != target["id"] or restart:
                        await asyncio.to_thread(stop_port, m["port"])
                if await self._healthy(target["port"]):
                    self.phase, self.message = "idle", f"{target['label']} ready"
                    return
                self.phase, self.message = "loading", f"Loading {target['label']}..."
                proc = await asyncio.to_thread(self._launch, target)
                await self._wait_ready(target, proc)
                self.phase, self.message = "idle", f"{target['label']} ready"
                logger.info(f"{target['label']} ready on port {target['port']} (kv={self.state['kv_cache']})")
            except Exception as e:
                self.phase, self.message = "error", str(e)
                logger.error(f"LLM switch failed: {e}")

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
        return subprocess.Popen(argv, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                cwd=str(Path(argv[0]).parent) if Path(argv[0]).is_absolute() else None,
                                creationflags=flags)

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
