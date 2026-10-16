"""Job runner: real subprocesses (the Python interpreter running the tests), so it works on Windows and Linux."""
import os, pathlib, sys, tempfile, time
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
from lucy.tools import jobs as J  # noqa: E402

PY = sys.executable


def start(script, **kw):
    return J.JobManager().start([PY, "-u", "-c", script], **kw)


def test_quick_command_returns_output_and_exit_code():
    j = start("print('hello')")
    assert j.wait(20) and j.returncode == 0 and j.status() == "exit code 0"
    text, skipped = j.read_new()
    assert text.strip() == "hello" and skipped == 0
    bad = start("import sys; sys.exit(3)")
    assert bad.wait(20) and bad.returncode == 3 and bad.status() == "exit code 3"


def test_output_is_incremental_between_polls():
    j = start("import time\nprint('a', flush=True)\ntime.sleep(2)\nprint('b', flush=True)")
    time.sleep(0.8)
    first, _ = j.read_new()
    assert "a" in first and "b" not in first and j.running and j.status().startswith("running")
    assert j.wait(20)
    rest, _ = j.read_new()
    assert rest.strip() == "b"                                         # only what is new, not "a" again
    assert j.read_new() == ("", 0)


def test_timeout_kills_the_job():
    t0 = time.time()
    j = start("import time; time.sleep(60)", max_runtime=1)
    assert j.wait(15) and time.time() - t0 < 12
    assert j.timed_out and "timed out after 1s" in j.status()


def test_kill_stops_a_background_job():
    j = start("import time; time.sleep(60)")
    time.sleep(0.3)
    assert j.running
    j.kill()
    assert j.wait(15) and j.status() == "killed" and not j.running


def test_long_output_keeps_the_tail():
    j = start("print('x' * 50000 + 'END')")
    assert j.wait(20)
    text, skipped = j.read_new(max_chars=1000)
    assert len(text) == 1000 and text.rstrip().endswith("END")
    assert skipped >= 49000


def test_cwd_and_env_are_applied():
    tmp = pathlib.Path(tempfile.mkdtemp()).resolve()
    j = start("import os; print(os.getcwd()); print(os.environ['LUCY_TEST_VAR'])", cwd=str(tmp), env={"LUCY_TEST_VAR": "42"})
    assert j.wait(20)
    out = j.read_new()[0].split()
    assert pathlib.Path(out[0]).resolve() == tmp and out[1] == "42"


def test_a_command_that_asks_for_input_cannot_hang():
    t0 = time.time()
    j = start("input('say something: ')")
    assert j.wait(15) and time.time() - t0 < 12 and j.returncode != 0     # stdin is closed: EOF, not a hang


def test_missing_program_is_a_clean_error():
    try:
        J.JobManager().start(["definitely-not-a-real-program-xyz"])
        assert False, "should have raised"
    except J.JobError:
        pass


def test_manager_lookup_list_and_prune():
    m = J.JobManager()
    j1 = m.start([PY, "-c", "print(1)"]); j2 = m.start([PY, "-c", "print(2)"])
    assert m.get(j1.id) is j1 and m.get(str(j2.id)) is j2 and m.get("nope") is None and m.get(None) is None
    assert [j.id for j in m.all()] == [j1.id, j2.id]
    j1.wait(20); j2.wait(20)
    old = J.KEEP_FINISHED
    J.KEEP_FINISHED = 1
    try:
        m.prune()
    finally:
        J.KEEP_FINISHED = old
    assert [j.id for j in m.all()] == [j2.id]                           # oldest finished job dropped
