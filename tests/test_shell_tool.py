"""run_shell_command (run/start/poll/kill/list) and find_file. A fake 'shell' runs Python code with the interpreter that
runs the tests, so everything works the same on Windows and Linux."""
import asyncio, os, pathlib, sys, tempfile, time, types
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
from lucy import paths as _paths  # noqa: E402
from lucy.tools import jobs, registry as R, shell  # noqa: E402

reg = R.Registry(); reg.discover("lucy.tools")


class fake_shell:
    """Context manager: `command` is Python code run by this interpreter; SAFE_ROOT points at a temp folder."""
    def __enter__(self):
        self.saved = (shell.build_argv, _paths.SAFE_ROOT, _paths.ROOT)
        shell.build_argv = lambda sh, command: [sys.executable, "-u", "-c", command]
        self.tmp = pathlib.Path(tempfile.mkdtemp()).resolve()
        _paths.SAFE_ROOT = _paths.ROOT = self.tmp
        return self.tmp
    def __exit__(self, *a):
        shell.build_argv, _paths.SAFE_ROOT, _paths.ROOT = self.saved


def sh(**args):
    return asyncio.run(reg.run("run_shell_command", args))


def test_schema_is_small_model_friendly():
    s = reg.get("run_shell_command")
    fn = s.schema["function"]
    assert (s.tier, s.label) == ("exec", "run") and fn["name"] == "run_shell_command"
    assert fn["description"].count(". ") <= 1 and len(fn["description"]) < 220
    assert fn["parameters"]["properties"]["action"]["enum"] == ["run", "start", "poll", "kill", "list"]
    assert fn["parameters"]["required"] == []                      # poll/kill/list need no command; the handler explains what is missing


def test_build_argv_for_each_shell():
    ps = shell.build_argv("powershell", "Get-Date")
    assert "-NoProfile" in ps and "-NonInteractive" in ps and ps[-2] == "-Command" and ps[-1].endswith("Get-Date") and "UTF8" in ps[-1]
    assert shell.build_argv("cmd", "dir /b") == 'cmd.exe /d /s /c "chcp 65001>nul & dir /b"'
    assert shell.build_argv("bash", "ls -la") == ["bash", "-c", "ls -la"]


def test_default_shell_follows_the_os_and_env():
    old = os.environ.pop("LUCY_SHELL", None)
    try:
        assert shell.default_shell() == ("powershell" if os.name == "nt" else "bash")
        os.environ["LUCY_SHELL"] = "cmd"; assert shell.default_shell() == "cmd"
        os.environ["LUCY_SHELL"] = "nonsense"; assert shell.default_shell() in ("powershell", "bash")
    finally:
        os.environ.pop("LUCY_SHELL", None)
        if old: os.environ["LUCY_SHELL"] = old


def test_run_returns_output_and_exit_code():
    with fake_shell():
        out = sh(command="print('hi there')")
        assert "hi there" in out and out.rstrip().endswith("[exit code 0]") and not out.startswith("Command failed")
        assert sh(command="pass").startswith("[no output]")


def test_failures_start_with_command_failed_so_the_loop_counts_them():
    with fake_shell():
        out = sh(command="import sys; print('boom'); sys.exit(2)")
        assert out.startswith("Command failed (exit code 2).") and "boom" in out and "[exit code 2]" in out


def test_timeout_kills_and_says_so():
    with fake_shell():
        t0 = time.time()
        out = sh(command="import time; time.sleep(60)", timeout=1)
        assert time.time() - t0 < 12 and out.startswith("Command failed: timed out after 1s") and "action 'start'" in out


def test_foreground_runs_are_capped_below_the_loop_timeout():
    old = os.environ.get("LUCY_TOOL_TIMEOUT")
    os.environ["LUCY_TOOL_TIMEOUT"] = "12"                         # cap = max(5, 12 - 10) = 5 s
    try:
        with fake_shell():
            t0 = time.time()
            out = sh(command="import time; time.sleep(60)", timeout=3600)
            assert time.time() - t0 < 15 and "timed out after 5s" in out and "limited to 5s" in out
    finally:
        if old is None: os.environ.pop("LUCY_TOOL_TIMEOUT", None)
        else: os.environ["LUCY_TOOL_TIMEOUT"] = old


def _jid(text):
    return int(text.split("#")[1].split(" ")[0].split(":")[0])


def test_background_job_start_poll_list_kill():
    with fake_shell():
        started = sh(action="start", command="import time\nprint('TI' + 'CK', flush=True)\ntime.sleep(60)")
        assert started.startswith("Started job #")
        jid = _jid(started)
        time.sleep(1.5)
        polled = sh(action="poll", job_id=jid)
        assert f"Job #{jid} [running" in polled
        assert "TICK" in started + polled                                # shown by 'start' or by the first poll
        assert f"#{jid} [running" in sh(action="list")
        assert sh(action="kill", job_id=jid).startswith(f"Job #{jid}: killed")
        assert "killed" in sh(action="poll", job_id=jid)


def test_start_shows_instant_output_and_instant_failure():
    with fake_shell():
        out = sh(action="start", command="print('QUI' + 'CK')")
        if "It already finished" not in out:                              # very slow machine: wait for it instead
            out += sh(action="poll", job_id=_jid(out), wait=15)
        assert "QUICK" in out and "[exit code 0]" in out
        out = sh(action="start", command="import sys; sys.exit(5)")
        if "Command failed" not in out:
            out += sh(action="poll", job_id=_jid(out), wait=15)
        assert "[exit code 5]" in out


def test_poll_is_incremental():
    with fake_shell():
        started = sh(action="start", command="import time\nprint('ON' + 'E_', flush=True)\ntime.sleep(4)\nprint('TW' + 'O_', flush=True)")
        jid = _jid(started)
        time.sleep(1.5)
        seen = started + sh(action="poll", job_id=jid)
        assert "ONE_" in seen and "TWO_" not in seen
        out = sh(action="poll", job_id=jid, wait=20)
        assert "TWO_" in out and "ONE_" not in out and "[exit code 0]" in out          # only what is new
        assert "[no new output]" in sh(action="poll", job_id=jid)


def test_helpful_errors():
    with fake_shell() as tmp:
        assert "'command' is required" in sh()
        assert "'command' is required for action 'start'" in sh(action="start")
        assert "no job 999" in sh(action="poll", job_id=999) and "Use action 'list'" in sh(action="kill", job_id=999)
        assert "unknown action" in sh(action="explode", command="x")
        assert "unknown shell" in sh(command="x", shell="zsh")
        assert "must be an object" in sh(command="x", env="A=1")
        assert sh(command="x", cwd="") != "" and "folder not found" in sh(command="print(1)", cwd="no_such_folder_xyz")


def test_cwd_and_env_reach_the_command():
    with fake_shell() as tmp:
        sub = tmp / "work"; sub.mkdir()
        out = sh(command="import os; print(os.getcwd()); print(os.environ['LUCY_X'])", cwd=sub.as_posix(), env={"LUCY_X": "7"})
        lines = out.splitlines()
        assert pathlib.Path(lines[0]).resolve() == sub and lines[1] == "7"
        assert pathlib.Path(sh(command="import os; print(os.getcwd())").splitlines()[0]).resolve() == tmp      # default cwd = file-tool root


def test_long_output_is_tail_truncated():
    with fake_shell():
        out = sh(command="print('a' * 30000); print('THE-END')")
        assert "earlier characters skipped" in out and "THE-END" in out and len(out) < 9000


def test_the_event_loop_keeps_running_during_a_command():
    with fake_shell():
        async def go():
            ticks = 0
            async def ticker():
                nonlocal ticks
                while True:
                    await asyncio.sleep(0.05); ticks += 1
            t = asyncio.create_task(ticker())
            out = await reg.run("run_shell_command", {"command": "import time; time.sleep(1.5); print('done')"})
            t.cancel()
            return ticks, out
        ticks, out = asyncio.run(go())
        assert "done" in out and ticks >= 15                           # a blocked loop would have counted ~0


def test_stop_button_kills_the_running_command():
    with fake_shell():
        async def go():
            try:
                await asyncio.wait_for(reg.run("run_shell_command", {"command": "import time; time.sleep(60)"}), 1.0)
            except asyncio.TimeoutError:
                pass
        asyncio.run(go())
        job = jobs.default.all()[-1]
        assert job.wait(10) and not job.running and job.killed


def test_a_missing_shell_is_reported_not_raised():
    old = shell.build_argv
    shell.build_argv = lambda sh_, command: ["definitely-not-a-real-shell-xyz", command]
    try:
        assert asyncio.run(reg.run("run_shell_command", {"command": "x"})).startswith("Error: could not start the command")
    finally:
        shell.build_argv = old


# ---------------------------------------------------------------- find_file (pure Python now, no bash needed)
def find(**args):
    return asyncio.run(reg.run("find_file", args))


def test_find_file_works_without_bash():
    with fake_shell() as tmp:
        for d in ["a/b/c", "a/b/c/d", "x/.git", "x/node_modules/q", "docs"]:
            (tmp / d).mkdir(parents=True)
        for f in ["a/b/c/Target.txt", "a/b/c/d/too_deep.txt", "x/.git/target.cfg", "x/node_modules/q/target.js", "docs/notes_target.md", "top.txt"]:
            (tmp / f).write_text("x")
        rel = lambda out: sorted(pathlib.Path(l).relative_to(tmp).as_posix() for l in out.splitlines() if l and not l.startswith("["))
        assert rel(find(file_name="target*", dir_path=tmp.as_posix())) == ["a/b/c/Target.txt"]            # case-insensitive, junk folders skipped
        assert rel(find(file_name="*target*", dir_path=tmp.as_posix())) == ["a/b/c/Target.txt", "docs/notes_target.md"]
        assert find(file_name="too_deep.txt", dir_path=tmp.as_posix()).startswith("No files matching")       # deeper than 4 levels
        assert rel(find(file_name="top.txt")) == ["top.txt"]                                                  # default folder = file-tool root
        assert "invalid characters" in find(file_name="a/b", dir_path=tmp.as_posix()) and "invalid characters" in find(file_name="")
        assert find(file_name="x", dir_path=(tmp / "nope").as_posix()).startswith("Directory not found")
