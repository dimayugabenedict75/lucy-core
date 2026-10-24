r"""Pin a known-good llama.cpp and roll back to it in one command (winget updates replace the files in place).

    python tools\llama_pin.py status          what Lucy uses now, what is pinned
    python tools\llama_pin.py pin             copy the CURRENT llama.cpp folder to runtime\llama_pinned\ (do this while it works)
    python tools\llama_pin.py use-pinned      make Lucy use the pinned copy (writes runtime\data\llama_exe.json)
    python tools\llama_pin.py use-current     go back to the path in llm_manager.py (deletes llama_exe.json)
Restart Lucy Core after use-pinned / use-current. Run with Lucy Core's venv from the Lucy_Core folder."""
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
PIN_DIR = ROOT / "runtime" / "llama_pinned"
PIN_JSON = ROOT / "runtime" / "data" / "llama_pin.json"
OVERRIDE = ROOT / "runtime" / "data" / "llama_exe.json"


def _version(exe: Path) -> str:
    try:
        p = subprocess.run([str(exe), "--version"], capture_output=True, text=True, timeout=60, cwd=str(exe.parent))
        for line in (p.stdout + p.stderr).splitlines():
            if line.lower().startswith("version"):
                return line.strip()
    except Exception as e:
        return f"unknown ({e})"
    return "unknown"


def _configured_exe() -> Path:
    from lucy.server.llm_manager import DEFAULT_MODELS_CFG
    cfg = ROOT / "runtime" / "data" / "llm_models.json"
    try:
        p = json.loads(cfg.read_text(encoding="utf-8")).get("llama_server") if cfg.exists() else None
    except Exception:
        p = None
    return Path(p or DEFAULT_MODELS_CFG["llama_server"])


def main(argv: list[str]) -> int:
    cmd = argv[0] if argv else "status"
    cur = _configured_exe()
    pin = json.loads(PIN_JSON.read_text(encoding="utf-8")) if PIN_JSON.exists() else None
    if cmd == "status":
        print("configured exe :", cur, "(exists)" if cur.is_file() else "(MISSING)")
        print("  version      :", _version(cur) if cur.is_file() else "-")
        print("override active:", json.loads(OVERRIDE.read_text("utf-8")).get("llama_server") if OVERRIDE.exists() else "no")
        print("pinned copy    :", f"{pin['exe']}  [{pin['version']}]" if pin else "none (run: pin)")
        return 0
    if cmd == "pin":
        if not cur.is_file():
            print("current llama-server.exe not found:", cur); return 1
        ver = _version(cur)
        tag = "".join(c if c.isalnum() or c in "._-" else "_" for c in ver)[:60] or "pinned"
        dest = PIN_DIR / tag
        if dest.exists():
            shutil.rmtree(dest)
        print(f"copying {cur.parent} -> {dest} ...")
        shutil.copytree(cur.parent, dest)
        PIN_JSON.parent.mkdir(parents=True, exist_ok=True)
        PIN_JSON.write_text(json.dumps({"exe": str(dest / cur.name), "version": ver}, indent=2), encoding="utf-8")
        print("pinned:", ver)
        return 0
    if cmd == "use-pinned":
        if not pin or not Path(pin["exe"]).is_file():
            print("nothing pinned (or the pinned copy is gone). Run: pin"); return 1
        OVERRIDE.parent.mkdir(parents=True, exist_ok=True)
        OVERRIDE.write_text(json.dumps({"llama_server": pin["exe"]}, indent=2), encoding="utf-8")
        print("Lucy will use", pin["exe"], "after a restart.")
        return 0
    if cmd == "use-current":
        OVERRIDE.unlink(missing_ok=True)
        print("override removed: Lucy uses", cur, "after a restart.")
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
