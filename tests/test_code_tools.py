"""P2 coding tools: code_search, project_map, apply_patch, run_tests, git, python, format_and_lint (real files, real git,
real subprocesses; the test program for run_tests is a fake that prints canned pytest output)."""
import asyncio, os, pathlib, shutil, subprocess, sys, tempfile, time
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from lucy import paths as _paths  # noqa: E402
from lucy.tools import registry as R, run_tests as RT, code_search as CS, project_map as PM, _code, lint as LINT  # noqa: E402
from _safety_helpers import Env  # noqa: E402

reg = R.Registry(); reg.discover("lucy.tools")


class proj:
    """SAFE_ROOT = ROOT = a temp folder."""
    def __enter__(self):
        self.saved = (_paths.SAFE_ROOT, _paths.ROOT)
        self.tmp = pathlib.Path(tempfile.mkdtemp()).resolve()
        _paths.SAFE_ROOT = _paths.ROOT = self.tmp
        PM.clear_cache()
        return self.tmp
    def __exit__(self, *a):
        _paths.SAFE_ROOT, _paths.ROOT = self.saved
        shutil.rmtree(self.tmp, ignore_errors=True)


def call(name, **args):
    return asyncio.run(reg.run(name, args))


def test_all_p2_tools_are_registered_with_small_model_shapes():
    for name, tier in [("code_search", "read"), ("project_map", "read"), ("apply_patch", "write"), ("run_tests", "exec"),
                       ("git", "write"), ("python", "exec"), ("format_and_lint", "exec")]:
        s = reg.get(name)
        assert s is not None and s.tier == tier, name
        fn = s.schema["function"]
        assert len(fn["description"].split(". ")) <= 2 and len(fn["description"]) < 300, name
        assert len(fn["parameters"]["properties"]) <= 8, name


# ---------------------------------------------------------------------------------------------- code_search
def _tree(tmp):
    (tmp / "src").mkdir(); (tmp / "node_modules/x").mkdir(parents=True); (tmp / "secrets").mkdir()
    (tmp / "src/a.py").write_text("def foo():\n    return 1\n\nclass Bar:\n    def foo(self):\n        pass\n")
    (tmp / "src/b.ts").write_text("export function foo() {}\n")
    (tmp / "node_modules/x/c.js").write_text("function foo(){}")
    (tmp / "bin.dat").write_bytes(b"foo\x00\x01")


def test_code_search_finds_matches_and_skips_junk():
    with proj() as t:
        _tree(t)
        out = call("code_search", query="foo")
        assert "3 match(es) in 2 file(s)" in out and "src/a.py" in out and "src/b.ts" in out
        assert "node_modules" not in out and "bin.dat" not in out
        out = call("code_search", query="foo", glob="*.py", context=1)
        assert "src/b.ts" not in out and "2- " in out and "4- class Bar" in out
        assert call("code_search", query="def \\w+", regex=True, files_only=True).strip().endswith("src/a.py (2)")
        assert call("code_search", query="FOO", case_sensitive=True).startswith("No matches")
        assert call("code_search", query="(", regex=True).startswith("Error: invalid regex")
        assert call("code_search", query="").startswith("Error")
        assert call("code_search", query="x", path=str(t / "nope")).startswith("Error")


def test_code_search_never_walks_into_blocked_locations():
    env = Env("ask_for_risky")
    try:
        (env.root / "secrets" / "k.txt").write_text("TOPSECRET foo")
        (env.root / "work" / "ok.txt").write_text("foo here")
        out = asyncio.run(reg.run("code_search", {"query": "foo", "path": str(env.root)}))
        assert "ok.txt" in out and "TOPSECRET" not in out and "secrets" not in out
    finally:
        env.close()


def test_code_search_uses_ripgrep_json_when_present(monkeypatch):
    with proj() as t:
        (t / "a.py").write_text("x = 1\n")
        fake = t / "fake_rg.py"
        p = (t / "a.py").as_posix()
        fake.write_text("import json\nfor e in [{'type':'match','data':{'path':{'text':%r},'lines':{'text':'x = 1\\n'},'line_number':1}}]:\n    print(json.dumps(e))\n" % p)
        monkeypatch.setattr(CS, "find_rg", lambda: [sys.executable, str(fake)])
        out = call("code_search", query="x")
        assert "1 match(es) in 1 file(s)" in out and "1: x = 1" in out


# ---------------------------------------------------------------------------------------------- project_map
def test_project_map_outline_focus_and_cache():
    with proj() as t:
        _tree(t)
        out = call("project_map")
        assert "src/" in out and "a.py" in out and "L1 def foo()" in out and "L4 class Bar" in out and "L5   def foo(self)" in out
        assert "export" not in out and "function foo()" in out        # ts symbol shown
        assert "node_modules" not in out
        one = call("project_map", focus="src/a.py")
        assert one.startswith("src/a.py (7 lines)") and "L4 class Bar" in one
        assert call("project_map", focus="../outside").startswith("Error: focus must be inside")
        assert call("project_map", focus="nope.py").startswith("Error: focus not found")
        (t / "src/a.py").write_text("def changed():\n    pass\n")      # cache must notice the edit
        assert "def changed()" in call("project_map", focus="src/a.py")


def test_project_map_shrinks_detail_to_fit():
    with proj() as t:
        for d in range(12):
            (t / f"pkg{d}").mkdir()
            for f in range(25):
                (t / f"pkg{d}/mod{f}.py").write_text("".join(f"def fn_{i}(a, b):\n    pass\n" for i in range(40)))
        out = call("project_map")
        assert len(out) < PM.BUDGET + 400 and "zoom in" in out and "pkg0/ (25 files)" in out


# ---------------------------------------------------------------------------------------------- apply_patch
SRC = "import os\n\ndef a(x):\n    return x + 1\n\n\ndef b(y):\n    return y * 2\n\n\nprint(a(1))\n"


def patch(f, **kw):
    return call("apply_patch", file_path=str(f), **kw)


def test_apply_patch_edits_are_all_or_nothing_and_keep_line_endings():
    with proj() as t:
        f = t / "m.py"; f.write_bytes(SRC.replace("\n", "\r\n").encode())
        out = patch(f, edits=[{"old": "x + 1", "new": "x + 10"}, {"old": "y * 2", "new": "y * 3"}])
        assert out.startswith("Patched") and "2 edits applied" in out and "+    return x + 10" in out
        raw = f.read_bytes()
        assert b"x + 10" in raw and b"y * 3" in raw and raw.count(b"\r\n") == raw.count(b"\n")
        before = f.read_bytes()
        out = patch(f, edits=[{"old": "x + 10", "new": "x"}, {"old": "NOPE", "new": "z"}])
        assert out.startswith("Error: edit #2 of 2 failed") and "NOTHING was changed" in out and f.read_bytes() == before


def test_apply_patch_refuses_to_break_python_and_reports_no_change():
    with proj() as t:
        f = t / "m.py"; f.write_text(SRC)
        assert patch(f, edits=[{"old": "return x + 1", "new": "return (x +"}]).startswith("Refused") and f.read_text() == SRC
        assert patch(f, edits=[{"old": "x + 1", "new": "x + 1"}]).startswith("No change")


def test_apply_patch_unified_diff_with_wrong_line_numbers():
    with proj() as t:
        f = t / "m.py"; f.write_text(SRC)
        d = "--- a/m.py\n+++ b/m.py\n@@ -90,3 +90,3 @@\n def b(y):\n-    return y * 2\n+    return y * 5\n \n"
        out = patch(f, diff=d)
        assert out.startswith("Patched") and "1 hunk applied" in out and "y * 5" in f.read_text()
        bad = patch(f, diff="@@ -1,2 +1,2 @@\n-def nothing_like_this():\n+def x():\n")
        assert "does not match the file" in bad and "NOTHING was changed" in bad
        multi = patch(f, diff="--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b\n--- a/y\n+++ b/y\n@@ -1 +1 @@\n-a\n+b\n")
        assert "more than one file" in multi


def test_apply_patch_ambiguous_hunk_and_argument_slips():
    with proj() as t:
        f = t / "m.py"; f.write_text("a = 1\nb = 2\na = 1\n")
        out = patch(f, diff="@@\n-a = 1\n+a = 9\n")
        assert "matches several places" in out and f.read_text() == "a = 1\nb = 2\na = 1\n"
        assert patch(f, edits='[{"old": "b = 2", "new": "b = 3"}]').startswith("Patched")        # edits sent as a JSON string
        assert patch(f, old_string="b = 3", new_string="b = 4").startswith("Patched")             # edit_local_file style names
        assert patch(f).startswith("Error: give either")
        assert patch(f, edits=[{"old": "a", "new": "b"}], diff="@@\n-a\n+b").startswith("Error: give 'edits' OR 'diff'")
        assert patch(t / "zzz.py", edits=[{"old": "a", "new": "b"}]).startswith("Error: file not found")


# ---------------------------------------------------------------------------------------------- run_tests
PYTEST_FAIL = """.F.                                                                      [100%]
================================== FAILURES ===================================
________________________________ test_add_bad _________________________________
tests/test_calc.py:8: in test_add_bad
    assert add(1, 2) == 4
E   assert 3 == 4
=========================== short test summary info ===========================
FAILED tests/test_calc.py::test_add_bad - assert 3 == 4
1 failed, 2 passed in 0.12s
"""


def test_pytest_output_is_condensed():
    passed, text = RT.condense("pytest", PYTEST_FAIL, 1)
    assert not passed and text.startswith("Tests FAILED (pytest, exit code 1)\n1 failed, 2 passed in 0.12s")
    assert "FAILED tests/test_calc.py::test_add_bad" in text and "assert 3 == 4" in text and "(at tests/test_calc.py:8)" in text
    assert RT.condense("pytest", "...\n3 passed in 0.03s\n", 0) == (True, "Tests PASSED (pytest)\n3 passed in 0.03s")
    assert "garbage" in RT.condense("pytest", "garbage\n", 1)[1]
    jest = "FAIL src/a.test.js\n  ● sum › adds\n\n    Expected: 4\n\nTests:       1 failed, 2 passed, 3 total\n"
    assert "Tests:       1 failed" in RT.condense("npm", jest, 1)[1] and "sum › adds" in RT.condense("npm", jest, 1)[1]


def test_run_tests_detects_runs_and_refuses_options(monkeypatch):
    with proj() as t:
        (t / "tests").mkdir(); (t / "tests/test_a.py").write_text("def test_a(): pass\n")
        assert _code.detect_test_kind(t) == "pytest"
        monkeypatch.setattr(RT, "build_command", lambda kind, cwd, target, flt: [sys.executable, "-c", f"import sys; print({PYTEST_FAIL!r}); sys.exit(1)"])
        out = call("run_tests")
        assert out.startswith("Tests FAILED (pytest") and "assert 3 == 4" in out
        assert "may not start with '-'" in call("run_tests", target="--rootdir=/")
        assert "may not start with '-'" in call("run_tests", filter="-p evil")
        assert call("run_tests", cwd=str(t / "nope")).startswith("Error")
        empty = t / "empty"; empty.mkdir()
        assert call("run_tests", cwd=str(empty)).startswith("Error: no tests found")


def test_run_tests_command_per_framework():
    t = pathlib.Path(".")
    assert RT.build_command("pytest", t, "tests/x.py", "foo")[-3:] == ["-k", "foo", "tests/x.py"] or "tests/x.py" in RT.build_command("pytest", t, "tests/x.py", "foo")
    assert RT.build_command("npm", t, None, None)[1:] == ["test"] and RT.build_command("npm", t, "a", "b")[1:] == ["test", "--", "-t", "b", "a"]
    assert RT.build_command("dotnet", t, None, "X")[-2:] == ["--filter", "X"]


def test_long_test_run_continues_as_a_job_and_stop_kills_it(monkeypatch):
    with proj() as t:
        (t / "tests").mkdir(); (t / "tests/test_a.py").write_text("def test_a(): pass\n")
        monkeypatch.setenv("LUCY_TOOL_TIMEOUT", "15")             # cap = 5 s
        monkeypatch.setattr(RT, "build_command", lambda *a: [sys.executable, "-c", "import time; time.sleep(30)"])
        t0 = time.time()
        out = call("run_tests")
        assert out.startswith("Tests are still running") and "run_shell_command" in out and time.time() - t0 < 12
        from lucy.tools import jobs
        for j in jobs.default.all():
            j.kill()


# ---------------------------------------------------------------------------------------------- git
def git(**a):
    return call("git", **a)


def test_git_round_trip_and_no_option_injection():
    if shutil.which("git") is None:
        return
    with proj() as t:
        assert "not a git repository" in git(action="status")
        assert git(action="init").startswith("Initialized")
        subprocess.run(["git", "config", "user.email", "a@b.c"], cwd=t); subprocess.run(["git", "config", "user.name", "T"], cwd=t)
        (t / "a.txt").write_text("one\n")
        assert "No commits yet" in git(action="log")
        assert git(action="commit", message="x").startswith("Nothing to commit")
        assert "A  a.txt" in git(action="add", files=["a.txt"])
        assert git(action="commit", message="first").startswith("Committed")
        (t / "a.txt").write_text("one\ntwo\n")
        assert "+two" in git(action="diff") and "1 file changed" in git(action="diff")
        git(action="add", files=["a.txt"]); git(action="commit", message="second")
        assert git(action="log").count("\n") == 1
        assert git(action="branch", ref="feature").startswith("Created branch")
        assert git(action="checkout", ref="feature").startswith("Now on feature")
        (t / "a.txt").write_text("scribble\n")
        assert git(action="checkout", files=["a.txt"]).startswith("Restored") and (t / "a.txt").read_text() == "one\ntwo\n"
        (t / "a.txt").write_text("wip\n")
        assert "wip" in git(action="stash", message="wip") and "wip" in git(action="stash_list")
        git(action="stash_pop")
        for bad in (dict(action="checkout", ref="--force"), dict(action="add", files=["--all"]), dict(action="push"),
                    dict(action="log", ref="-p")):
            assert git(**bad).startswith("Error"), bad


# ---------------------------------------------------------------------------------------------- python
def test_python_repl_keeps_state_reports_errors_and_times_out(monkeypatch):
    env = Env("ask_for_risky", monkeypatch)
    other = tempfile.mkdtemp(prefix="lucy_pytmp_")
    monkeypatch.setenv("TMPDIR", other); monkeypatch.setenv("TEMP", other); monkeypatch.setenv("TMP", other)
    ctx = R.ToolContext(session_id="py-test")
    py = lambda code, **a: asyncio.run(reg.run("python", {"code": code, **a}, ctx))
    try:
        assert py("x = 21 * 2\nx") == "[new Python session]\n42"
        assert py("x + 1") == "43"
        out = py("print('hi')\n1/0")
        assert out.startswith("hi\nTraceback (most recent call last):\n  File \"<python>\", line 2") and "ZeroDivisionError" in out
        assert py("input()").count("EOFError") == 1
        t0 = time.time()
        out = py("import time\ntime.sleep(30)", timeout=2)
        assert out.startswith("Command failed: the Python code ran longer than 2s") and time.time() - t0 < 10
        assert "NameError" in py("x")                                  # the timeout started a fresh session
        assert py("y = 5", reset=True).startswith("[new Python session")
    finally:
        from lucy.tools import python_repl
        python_repl.kill_session("py-test"); env.close(); shutil.rmtree(other, ignore_errors=True)


def test_python_repl_is_confined_by_the_users_permissions(monkeypatch):
    env = Env("ask_for_risky", monkeypatch)
    other = tempfile.mkdtemp(prefix="lucy_pytmp_")
    monkeypatch.setenv("TMPDIR", other); monkeypatch.setenv("TEMP", other); monkeypatch.setenv("TMP", other)
    ctx = R.ToolContext(session_id="py-conf")
    py = lambda code: asyncio.run(reg.run("python", {"code": code}, ctx))
    root = env.root.as_posix()
    try:
        assert py(f"open(r'{root}/work/ok.txt', 'w').write('x')").endswith("1")                     # inside folders.write
        assert "writing outside the allowed folders" in py(f"open(r'{root}/outside.txt', 'w').write('x')")
        assert "blocked location" in py(f"open(r'{root}/secrets/k.txt').read()")
        assert "may not change this location" in py(f"open(r'{root}/src/x.py', 'w').write('x')")
        assert "starting programs" in py("import subprocess\nsubprocess.run(['echo', 'hi'])")
        assert "starting programs" in py("import os\nos.system('echo hi')")
        assert "own network addresses" in py("import socket\nsocket.socket().connect(('127.0.0.1', 8090))")
        assert "no such file" not in py("import tempfile\nf = tempfile.NamedTemporaryFile(delete=False); f.close(); 'tmp ok'")
        assert py("import os\n(os.environ.get('LUCY_PY_RULES'), os.environ.get('DEV_API_KEY'))").endswith("(None, None)")
    finally:
        from lucy.tools import python_repl
        python_repl.kill_session("py-conf"); env.close(); shutil.rmtree(other, ignore_errors=True)


# ---------------------------------------------------------------------------------------------- format_and_lint
def test_lint_reports_missing_tools_and_fixes_one_file_only(monkeypatch):
    with proj() as t:
        (t / "a.py").write_text("x=1\n")
        monkeypatch.setattr(LINT, "pick_tools", lambda target, project: [])
        assert call("format_and_lint", path=str(t / "a.py")).startswith("No linter/formatter is installed")
        assert call("format_and_lint", path=str(t), action="fix").startswith("Error: 'fix' works on ONE file")
        assert call("format_and_lint", path=str(t / "nope.py")).startswith("Error: not found")
        fake = t / "fakefmt.py"
        fake.write_text("import sys, pathlib\npathlib.Path(sys.argv[-1]).write_text('x = 1\\n')\n")
        monkeypatch.setattr(LINT, "pick_tools", lambda target, project: [("black", [sys.executable, str(fake)])])
        monkeypatch.setattr(LINT, "_argv", lambda tool, prefix, target, fix: prefix + [str(target)])
        out = call("format_and_lint", path=str(t / "a.py"), action="fix")
        assert out.startswith("black rewrote") and (t / "a.py").read_text() == "x = 1\n"
