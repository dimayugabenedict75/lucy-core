"""Facts about the llama.cpp build Lucy launches: version, GPU memory, and whether it still accepts every flag we pass.

Used by the startup self-check, the health panel and tools/check_llama.py. Each call runs the exe (a second or two),
so results are cached: version/flags by (exe size + modified time), GPU memory for a few seconds.
"""
from __future__ import annotations

import re
import subprocess
import sys
import time
from pathlib import Path

_FLAGS_CACHE: dict = {}
_DEV_CACHE: dict = {"t": 0.0, "exe": None, "val": None}
_NOWIN = 0x08000000 if sys.platform == "win32" else 0


def _run(exe: str, *args: str, timeout: float = 30.0) -> str:
    try:
        p = subprocess.run([exe, *args], capture_output=True, text=True, timeout=timeout, creationflags=_NOWIN,
                           cwd=str(Path(exe).parent) if Path(exe).is_absolute() else None)
        return (p.stdout or "") + (p.stderr or "")
    except Exception as e:                                      # missing exe, timeout, access denied...
        return f"ERROR: {e}"


def _sig(exe: str):
    try:
        st = Path(exe).stat()
        return (str(exe), st.st_size, int(st.st_mtime))
    except OSError:
        return (str(exe), 0, 0)


def version(exe: str) -> str:
    """First useful line of `llama-server --version`, e.g. 'version: 0.7.0-dev (build 11829, commit 184f65360)'."""
    out = _run(exe, "--version")
    for line in out.splitlines():
        if line.strip().lower().startswith("version"):
            return line.strip()
    return (out.strip().splitlines() or ["unknown"])[0][:200]


_DEV = re.compile(r"^\s*(\S+):\s*(.+?)\s*\((\d+)\s*MiB,\s*(\d+)\s*MiB free\)", re.M)


def parse_devices(text: str) -> list[dict]:
    """'Vulkan0: AMD Radeon RX 9060 XT (16304 MiB, 15427 MiB free)' -> [{id, name, total_mib, free_mib}]"""
    return [{"id": m[0], "name": m[1], "total_mib": int(m[2]), "free_mib": int(m[3])} for m in _DEV.findall(text or "")]


def devices(exe: str, max_age: float = 15.0) -> list[dict]:
    """GPU list with total/free memory right now (cached `max_age` seconds). [] if it can't be read."""
    now = time.time()
    if _DEV_CACHE["exe"] == exe and now - _DEV_CACHE["t"] < max_age and _DEV_CACHE["val"] is not None:
        return _DEV_CACHE["val"]
    val = parse_devices(_run(exe, "--list-devices", timeout=40.0))
    _DEV_CACHE.update(t=now, exe=exe, val=val)
    return val


def free_vram_mib(exe: str, max_age: float = 3.0) -> int | None:
    """Free memory on the biggest GPU, or None when unknown (callers then skip memory planning)."""
    d = devices(exe, max_age)
    return max(d, key=lambda x: x["total_mib"])["free_mib"] if d else None


def _help(exe: str) -> str:
    key = _sig(exe)
    if key not in _FLAGS_CACHE:
        _FLAGS_CACHE.clear()
        _FLAGS_CACHE[key] = _run(exe, "--help", timeout=60.0)
    return _FLAGS_CACHE[key]


def flags_in(argv: list[str]) -> list[str]:
    return sorted({a for a in argv[1:] if a.startswith("-") and not re.fullmatch(r"-\d+(\.\d+)?", a)})


def unknown_flags(exe: str, argv: list[str]) -> list[str]:
    """Flags in `argv` that this build's --help doesn't list (an update renamed or removed them)."""
    h = _help(exe)
    return [f for f in flags_in(argv) if not re.search(rf"(?<![\w-]){re.escape(f)}(?![\w-])", h)]


def check(exe: str, argvs: dict[str, list[str] | Exception]) -> dict:
    """Full self-check. `argvs` = {label: argv or the exception raised while building it}."""
    res = {"exe": exe, "exists": Path(exe).is_file(), "version": None, "problems": [], "per_label": {}, "devices": []}
    if not res["exists"]:
        res["problems"].append(f"llama-server not found: {exe}")
        return res
    res["version"] = version(exe)
    res["devices"] = devices(exe, 0)
    for label, argv in argvs.items():
        if isinstance(argv, Exception):
            res["per_label"][label] = {"error": str(argv)}
            res["problems"].append(f"{label}: {argv}")
            continue
        bad = unknown_flags(exe, argv)
        res["per_label"][label] = {"unknown_flags": bad}
        if bad:
            res["problems"].append(f"{label}: this llama.cpp build does not list {', '.join(bad)}")
    res["ok"] = not res["problems"]
    return res
