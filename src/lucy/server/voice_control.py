"""
Voice control endpoints for the Lucy Audio Server (8091).

These back the Voice toggle in the chat UI (chat.html). The browser can't
launch a Windows .exe itself, so Lucy Core's own process does it here,
using subprocess in place of what run_lucy_core.bat used to do.

WIRING INSTRUCTIONS
|--------------------
In lucy/server/api.py:

    from lucy.server.voice_control import router as voice_router
    app.include_router(voice_router)

(Adjust the import path to wherever you drop this file inside your
lucy.server package, e.g. lucy/server/voice_control.py.)

If your api.py already has its own API-key auth dependency, apply it to
this router the same way you do for your other /api/* routes, e.g.:

    app.include_router(voice_router, dependencies=[Depends(verify_api_key)])

or add `dependencies=[Depends(verify_api_key)]` directly to the
APIRouter(...) call below.
"""

import os
import subprocess
import threading
import time
from pathlib import Path

import httpx
from fastapi import APIRouter

router = APIRouter(prefix="/api/voice", tags=["voice"])

# lucy_audio is baked into Lucy_Core's own folder, so its location is
# derived from this file's path — no more external path dependencies.
# Override with environment variables if needed:
#   LUCY_AUDIO_DIR     folder containing build/ and server.json
#   LUCY_AUDIO_EXE     full path to lucy_audio_server.exe
#   LUCY_AUDIO_CONFIG  full path to server.json
_LUCY_CORE = Path(__file__).resolve().parents[3]  # src/lucy/server/voice_control.py -> Lucy_Core/
LUCY_AUDIO_DIR = Path(os.environ.get("LUCY_AUDIO_DIR", _LUCY_CORE / "lucy_audio"))
LUCY_AUDIO_EXE = os.environ.get("LUCY_AUDIO_EXE") or str(
    LUCY_AUDIO_DIR / "build" / "windows-vulkan-release" / "bin" / "lucy_audio_server.exe"
)
LUCY_AUDIO_CONFIG = os.environ.get("LUCY_AUDIO_CONFIG") or str(LUCY_AUDIO_DIR / "server.json")
LUCY_AUDIO_HOST = "127.0.0.1"
LUCY_AUDIO_PORT = 8091
LUCY_AUDIO_HEALTH_URL = f"http://{LUCY_AUDIO_HOST}:{LUCY_AUDIO_PORT}/health"

LUCY_AUDIO_ARGS = [
    LUCY_AUDIO_EXE,
    "--config", LUCY_AUDIO_CONFIG,
    "--port", str(LUCY_AUDIO_PORT),
    "--host", LUCY_AUDIO_HOST,
    "--backend", "vulkan",
    "--device", "0",
    "--threads", "8",
    "--idle-unload-ms", "300000",
    "--no-ui",
]

# Guards access to _process from concurrent /start /stop /status calls.
_lock = threading.Lock()
_process: subprocess.Popen | None = None


def _is_alive() -> bool:
    return _process is not None and _process.poll() is None


def _wait_healthy(timeout_s: float = 30.0) -> bool:
    """Poll /health until it responds or times out."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if not _is_alive():
            return False  # process died before becoming healthy
        try:
            r = httpx.get(LUCY_AUDIO_HEALTH_URL, timeout=1.0)
            if r.status_code < 500:
                return True
        except httpx.RequestError:
            pass
        time.sleep(1.0)
    return False


@router.post("/start")
def start_voice():
    global _process
    with _lock:
        if _is_alive():
            return {"running": True, "already_running": True}

        # Fail with a readable message instead of an opaque 500 if a path is wrong.
        for label, p in (("executable", LUCY_AUDIO_EXE), ("config", LUCY_AUDIO_CONFIG)):
            if not Path(p).exists():
                return {
                    "running": False,
                    "error": f"Lucy Audio {label} not found: {p} "
                             f"(set LUCY_AUDIO_DIR / LUCY_AUDIO_EXE / LUCY_AUDIO_CONFIG)",
                }

        _process = subprocess.Popen(
            LUCY_AUDIO_ARGS,
            cwd=str(LUCY_AUDIO_DIR),  # ensures server.json relative paths resolve
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW,  # Windows-only flag
        )

    healthy = _wait_healthy()
    if not healthy:
        # Didn't come up in time / crashed — clean up and report failure.
        stop_voice()
        return {"running": False, "error": "lucy_audio_server did not become healthy"}
    return {"running": True}


@router.post("/stop")
def stop_voice():
    global _process
    with _lock:
        if not _is_alive():
            _process = None
            return {"running": False}

        _process.terminate()
        try:
            _process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            _process.kill()
            _process.wait(timeout=5)
        _process = None

    return {"running": False}


@router.get("/status")
def voice_status():
    return {"running": _is_alive()}


LUCY_AUDIO_IMAGE_NAME = "lucy_audio_server.exe"


def _external_lucy_audio_running() -> bool:
    """True if a lucy_audio_server.exe process exists on this machine,
    regardless of who launched it (e.g. an old run_lucy_core.bat)."""
    try:
        out = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {LUCY_AUDIO_IMAGE_NAME}", "/NH"],
            capture_output=True, text=True, timeout=10,
            creationflags=subprocess.CREATE_NO_WINDOW,
        ).stdout
        return LUCY_AUDIO_IMAGE_NAME.lower() in out.lower()
    except Exception:
        return False


def stop_all() -> bool:
    """Stop the Lucy Audio Server ONLY if it is running.

    1. If Lucy Core spawned it (the Voice toggle), terminate that process.
    2. If a copy is still running that Lucy Core did not spawn, stop it by its
       dedicated image name. This name is specific to Lucy Audio, so unlike
       `taskkill /IM python.exe` it cannot hit unrelated programs.

    Returns True if something was running and got stopped.
    """
    was_running = _is_alive()
    if was_running:
        stop_voice()
    if _external_lucy_audio_running():
        was_running = True
        subprocess.run(
            ["taskkill", "/F", "/IM", LUCY_AUDIO_IMAGE_NAME],
            capture_output=True, timeout=15,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    return was_running
