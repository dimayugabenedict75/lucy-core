r"""Checks that the llama-server Lucy Core launches accepts every flag it passes, and that the GPU shows up.
Run from Lucy_Core:   .\.venv\Scripts\python.exe tools\check_llama.py
Exit 0 = all flags known. Same check Lucy runs at startup (health panel shows the result)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from lucy.server import health
from lucy.server.embed_manager import embed_server
from lucy.server.llm_manager import llm


def main() -> int:
    res = health.run_startup_check(llm, embed_server)
    print("exe     :", res["exe"], "(exists)" if res["exists"] else "(MISSING)", "| source:", llm.exe_source)
    print("version :", res.get("version"))
    for label, r in res["per_label"].items():
        if "error" in r:
            print(f"[{label}] cannot build launch command: {r['error']}")
        else:
            print(f"[{label}] unknown flags: {', '.join(r['unknown_flags']) or 'none'}")
    for d in res["devices"]:
        print(f"gpu     : {d['name']}  {d['free_mib']} MiB free of {d['total_mib']}")
    print("PASS" if res.get("ok") else "FAIL: " + "; ".join(res["problems"]))
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
