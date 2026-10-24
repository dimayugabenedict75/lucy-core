r"""One command to run after updating llama.cpp, Python packages or Lucy's code. Stops at nothing: runs every check and
prints a summary. Exit 0 = everything passed.

    .\.venv\Scripts\python.exe tools\after_update.py            # all checks
    .\.venv\Scripts\python.exe tools\after_update.py --quick    # skip the slow test suite

Checks: 1) every .py file compiles  2) chat.js / panel pages have valid JavaScript (needs Node, skipped if missing)
3) llama.cpp accepts every flag Lucy passes + GPU visible  4) the test suite (pytest)  5) safety seal status."""
import py_compile
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RESULTS: list[tuple[str, str, str]] = []          # (name, PASS|FAIL|SKIP, detail)


def add(name: str, status: str, detail: str = "") -> None:
    RESULTS.append((name, status, detail))
    print(f"[{status}] {name}" + (f" - {detail}" if detail else ""), flush=True)


def run(cmd: list[str], timeout: int = 1800, cwd: Path = ROOT) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=str(cwd))
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except Exception as e:
        return 99, str(e)


def main() -> int:
    quick = "--quick" in sys.argv
    bad = []
    for d in ("src", "tests", "tools"):
        for f in (ROOT / d).rglob("*.py"):
            if "__pycache__" in f.parts or ".venv" in f.parts:
                continue
            try:
                py_compile.compile(str(f), doraise=True)
            except py_compile.PyCompileError as e:
                bad.append(f"{f.relative_to(ROOT)}: {e.msg.splitlines()[-1]}")
    add("python files compile", "FAIL" if bad else "PASS", "; ".join(bad[:3]))

    node = shutil.which("node")
    if not node:
        add("javascript syntax", "SKIP", "Node.js not installed")
    else:
        problems = []
        tmp = ROOT / "runtime" / "_js_check.js"
        for page in [ROOT / "ui" / "chat.js"] + sorted((ROOT / "ui").glob("*.html")):
            if page.suffix == ".js":
                code, out = run([node, "--check", str(page)])
            else:
                m = re.findall(r"<script>(.*?)</script>", page.read_text(encoding="utf-8"), re.S)
                if not m:
                    continue
                tmp.parent.mkdir(parents=True, exist_ok=True)
                tmp.write_text("\n".join(m), encoding="utf-8")
                code, out = run([node, "--check", str(tmp)])
            if code:
                problems.append(page.name)
        tmp.unlink(missing_ok=True)
        add("javascript syntax", "FAIL" if problems else "PASS", ", ".join(problems))

    code, out = run([sys.executable, str(ROOT / "tools" / "check_llama.py")], timeout=300)
    add("llama.cpp flags + GPU", "PASS" if code == 0 else "FAIL", "" if code == 0 else out.strip().splitlines()[-1][:300])

    if quick:
        add("test suite", "SKIP", "--quick")
    else:
        code, out = run([sys.executable, "-m", "pytest", "-q", "-x", "--no-header", "-p", "no:cacheprovider"])
        tail = (out.strip().splitlines() or [""])[-1]
        add("test suite", "PASS" if code == 0 else "FAIL", tail[:200])

    code, out = run([sys.executable, "-m", "lucy.safety", "status"], cwd=ROOT / "src")
    add("safety seal", "PASS" if code == 0 and "CHANGED" not in out else "FAIL",
        " ".join(out.split())[:200])

    fails = [r for r in RESULTS if r[1] == "FAIL"]
    print("\n" + ("ALL GOOD" if not fails else f"{len(fails)} CHECK(S) FAILED: " + ", ".join(r[0] for r in fails)))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
