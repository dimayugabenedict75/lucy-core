"""Stella embed server lifecycle (port 8092), separate from llm_manager's chat models on purpose:
ensure_active() stops every model it knows about, so the embedder must never be in `llm.models`.
Fail-open: ensure_up() never blocks; callers fall back to TF-IDF until the server is ready.
Config: DEFAULT_EMBED_CFG below, optionally overridden by runtime/data/embed.json."""
from __future__ import annotations

import atexit
import json
import logging
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx

from lucy.paths import ROOT, RUNTIME
from lucy.server import llama_check
from lucy.server.llm_manager import _bind_to_lucy_core, _kill_tree, llama_exe_override, stop_port

log = logging.getLogger("lucy.core.embed")

DEFAULT_EMBED_CFG = {
    "enabled": True,
    "llama_server": "C:/Users/dimay/AppData/Local/Microsoft/WinGet/Packages/ggml.llamacpp_Microsoft.Winget.Source_8wekyb3d8bbwe/llama-server.exe",
    "host": "127.0.0.1", "port": 8092,                      # 8091 is Lucy Audio
    "model": (ROOT / "models/rag/llm/Stella-Max-Q8_0.snq").as_posix(),
    "mmproj": (ROOT / "models/rag/sensor/Stella-Max-mmproj-Q8_0.snq").as_posix(),
    "use_mmproj": False,        # flip to True only after tools/embed_blind_check.py passes
    "ctx": 2048, "media_ctx": 4096, "image_tokens": 0,
    "gpu_layers": 99,           # 0 = CPU
    "extra_args": [],
    "ready_timeout_s": 120,
    "warm_start": True,         # load it right after the chat model at startup (it unloads itself when idle)
    "vram_plan": True,          # not enough free GPU memory for it -> run it on the CPU for this launch
    "retry_after_s": 300,       # after a failed start, do not try again for this long (no crash loop)
}


class EmbedServer:
    def __init__(self, runtime: Path | None = None):
        self.runtime = runtime or RUNTIME
        p = self.runtime / "data" / "embed.json"
        try:
            over = json.loads(p.read_text("utf-8")) if p.exists() else {}
        except Exception as e:
            log.warning(f"embed.json: {e}; using defaults"); over = {}
        base = dict(DEFAULT_EMBED_CFG)
        ov = llama_exe_override(self.runtime)                # pinned / rolled-back llama.cpp wins over the built-in path
        if ov:
            base["llama_server"] = ov
        elif runtime is None:                                # production: same llama.cpp as the chat models
            try:
                from lucy.server.llm_manager import llm
                base["llama_server"] = llm.cfg["llama_server"]
            except Exception:
                pass
        self.cfg = {**base, **over}
        self.last_used = time.monotonic()
        self.cpu_note = ""
        self.proc = None
        self._starting = threading.Lock()
        self._retry_at = 0.0

    def build_argv(self, gpu_layers: int | None = None) -> list[str]:
        c = self.cfg
        ngl = c["gpu_layers"] if gpu_layers is None else gpu_layers
        media = c["use_mmproj"]
        ctx = max(c["ctx"], c["media_ctx"]) if media else c["ctx"]
        argv = [c["llama_server"], "-m", c["model"], "--host", c["host"], "--port", str(c["port"]),
                "--embeddings", "--pooling", "mean", "-c", str(ctx), "-b", str(ctx), "-ub", str(ctx),
                "-np", "1", "-fa", "off", "-ngl", str(ngl), *c["extra_args"]]
        if media:
            argv += ["--mmproj", c["mmproj"]]
            if c["image_tokens"]:
                argv += ["--image-min-tokens", str(c["image_tokens"]), "--image-max-tokens", str(c["image_tokens"])]
        return argv

    def is_ready(self) -> bool:
        try:
            return httpx.get(f"http://127.0.0.1:{self.cfg['port']}/health", timeout=1.0).status_code == 200
        except Exception:
            return False

    def ensure_up(self) -> bool:
        """True if ready now. Otherwise starts it in the background and returns False (caller uses TF-IDF)."""
        if not self.cfg.get("enabled", True):
            return False
        if self.is_ready():
            self.last_used = time.monotonic()
            return True
        if time.time() < self._retry_at:
            return False
        if self._starting.acquire(blocking=False):
            threading.Thread(target=self._start, daemon=True).start()
        return False

    def _start(self) -> None:
        try:
            if not (Path(self.cfg["model"]).is_file() and Path(self.cfg["llama_server"]).is_file()):
                log.warning("embed server: model or llama-server.exe missing")
                self._retry_at = time.time() + self.cfg["retry_after_s"]
                return
            (self.runtime / "logs").mkdir(parents=True, exist_ok=True)
            logf = open(self.runtime / "logs" / "embed_stella.log", "ab")
            argv = self.build_argv(self._pick_gpu_layers())
            logf.write(f"\n==== {time.strftime('%F %T')} {' '.join(argv)}\n".encode()); logf.flush()
            flags = 0x08000000 | 0x00000200 if sys.platform == "win32" else 0
            self.proc = subprocess.Popen(argv, stdout=logf, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                         creationflags=flags)
            _bind_to_lucy_core(self.proc.pid)
            deadline = time.time() + self.cfg["ready_timeout_s"]
            while time.time() < deadline and self.proc.poll() is None and not self.is_ready():
                time.sleep(1.0)
            if not self.is_ready():
                if self.proc.poll() is not None:
                    log.warning(f"embed server exited with code {self.proc.returncode}; see runtime/logs/embed_stella.log")
                else:
                    log.warning("embed server did not become ready in time; see runtime/logs/embed_stella.log")
                self._retry_at = time.time() + self.cfg["retry_after_s"]
            else:
                self._retry_at = 0.0
        except Exception as e:
            log.warning(f"embed server start failed: {e}")
            self._retry_at = time.time() + self.cfg["retry_after_s"]
        finally:
            self._starting.release()

    def _pick_gpu_layers(self) -> int | None:
        """None = use the config. 0 = CPU for this launch because the GPU is too full (a game is running)."""
        self.cpu_note = ""
        if not self.cfg.get("vram_plan", True) or int(self.cfg["gpu_layers"]) == 0:
            return None
        try:
            free = llama_check.free_vram_mib(self.cfg["llama_server"], 0.0)
            size = int(Path(self.cfg["model"]).stat().st_size / 1048576)
            if self.cfg["use_mmproj"] and Path(self.cfg["mmproj"]).is_file():
                size += int(Path(self.cfg["mmproj"]).stat().st_size / 1048576)
        except Exception:
            return None
        if free is not None and free < int(size * 1.3) + 400:
            self.cpu_note = f"only {free} MiB of GPU memory free: embed server runs on the CPU"
            log.warning(self.cpu_note)
            return 0
        return None

    def idle_stop(self, limit_s: float, now=time.monotonic) -> bool:
        """Stop the server if nothing has used it for `limit_s` seconds. True if it was stopped."""
        if limit_s <= 0 or now() - self.last_used < limit_s or not self.is_ready():
            return False
        self.stop()
        return True

    def restart(self) -> None:
        """Health panel button: stop it, forget any failed-start cooldown, start again in the background."""
        try:
            self.stop()
        except Exception as e:
            log.warning(f"embed stop failed: {e}")
        self._retry_at = 0.0
        self.last_used = time.monotonic()
        self.ensure_up()

    def stop(self) -> None:
        stop_port(self.cfg["port"])


embed_server = EmbedServer()


def _stop_on_exit() -> None:
    p = embed_server.proc
    if p is not None and p.poll() is None:
        try:
            _kill_tree(p.pid)
        except Exception:
            pass


atexit.register(_stop_on_exit)
