"""P7: the sandbox that runs Lucy's own tools (static check + restricted child process). Includes escape attempts that
skip the static check on purpose, to prove the runtime hook holds on its own."""
import pathlib, sys, textwrap

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
from lucy.selfdev import sandbox as SB  # noqa: E402
import pytest  # noqa: E402


def tool_file(tmp_path, body):
    p = tmp_path / "t.py"
    p.write_text(textwrap.dedent(body), encoding="utf-8")
    return p


def test_a_plain_tool_runs_and_returns_text(tmp_path):
    p = tool_file(tmp_path, "def run(args):\n    return args['text'][::-1]\n")
    o = SB.run_job(p, args={"text": "abc"})
    assert o.ok and o.text == "cba"


def test_non_string_results_become_json_and_prints_do_not_leak_into_the_result(tmp_path):
    p = tool_file(tmp_path, "def run(args):\n    print('noise')\n    return {'n': 2}\n")
    o = SB.run_job(p)
    assert o.ok and o.text == '{"n": 2}' and "noise" in o.stdout


def test_tests_pass_and_fail(tmp_path):
    p = tool_file(tmp_path, "def run(args):\n    return str(len(args.get('text', '').split()))\n")
    assert SB.run_job(p, mode="test", tests="assert run({'text': 'a b'}) == '2'\ndef test_empty():\n    assert run({}) == '0'\n").ok
    bad = SB.run_job(p, mode="test", tests="assert run({'text': 'a b'}) == '3'")
    assert not bad.ok and "AssertionError" in bad.error
    bad = SB.run_job(p, mode="test", tests="def test_x():\n    assert run({}) == 'x'\n")
    assert not bad.ok and "test_x" in bad.error


ESCAPES = {
    "write outside temp": "import os\ndef run(a):\n    open(os.path.join(a['dir'], 'evil.txt'), 'w').write('x')\n    return 'wrote'\n",
    "os.open write flags": "import os\ndef run(a):\n    os.open(os.path.join(a['dir'], 'evil.txt'), os.O_WRONLY | os.O_CREAT)\n    return 'wrote'\n",
    "rename into place": "import os\ndef run(a):\n    open('x', 'w').write('1')\n    os.replace('x', os.path.join(a['dir'], 'evil.txt'))\n    return 'moved'\n",
    "network": "import socket\ndef run(a):\n    socket.create_connection(('127.0.0.1', 9), timeout=1)\n    return 'net'\n",
    "start a program": "import subprocess, sys\ndef run(a):\n    return subprocess.run([sys.executable, '-c', 'print(1)'], capture_output=True, text=True).stdout\n",
    "os.system": "import os\ndef run(a):\n    os.system('echo hi')\n    return 'ran'\n",
    "ctypes": "import ctypes\ndef run(a):\n    return 'native'\n",
    "importlib trick": "import importlib\ndef run(a):\n    return str(importlib.import_module('ctypes'))\n",
    "remove the hook": "import sys\ndef run(a):\n    sys.settrace(None)\n    return 'traced'\n",
}


@pytest.mark.parametrize("kind", sorted(ESCAPES))
def test_escape_attempts_are_blocked_at_runtime_even_without_the_static_check(tmp_path, kind):
    outside = tmp_path / "outside"
    outside.mkdir()
    p = tool_file(tmp_path, ESCAPES[kind])
    o = SB.run_job(p, args={"dir": str(outside)}, timeout=20)
    assert not o.ok, (kind, o.text)
    assert "blocked" in o.error, (kind, o.error)
    assert not (outside / "evil.txt").exists()


def test_the_static_check_also_catches_them_with_fixable_messages():
    for kind, code in ESCAPES.items():
        if kind in ("write outside temp", "os.open write flags", "rename into place"):
            continue                                          # plain file writes are only stopped at runtime
        assert SB.static_check(code), kind
    probs = SB.static_check("def run(a):\n    return ().__class__.__bases__[0].__subclasses__()\n")
    assert any("__subclasses__" in p for p in probs)
    assert any("eval" in p for p in SB.static_check("def run(a):\n    return eval('1')\n"))
    assert SB.static_check("x = 1\n") == ["define a top-level function  def run(args):  that returns a string"]
    assert "exactly one argument" in SB.static_check("def run(a, b):\n    return ''\n")[0]
    assert "at least one assert" in SB.static_check("run({})\n", is_tests=True)[0]
    assert "syntax error" in SB.static_check("def run(a)\n    return 1\n")[0]


def test_declared_needs_open_exactly_what_they_say(tmp_path):
    target = tmp_path / "out" / "result.txt"
    target.parent.mkdir()
    p = tool_file(tmp_path, "def run(a):\n    open(a['path'], 'w').write('ok')\n    return 'saved'\n")
    assert not SB.run_job(p, args={"path": str(target)}).ok                              # no 'files' need
    assert SB.run_job(p, args={"path": str(target)}, caps=["files"], write_paths=[str(target)]).ok
    assert target.read_text() == "ok"
    other = tmp_path / "out" / "other.txt"
    o = SB.run_job(p, args={"path": str(other)}, caps=["files"], write_paths=[str(target)])
    assert not o.ok and not other.exists()                  # only the paths the call named (and the policy vetted)
    prog = tool_file(tmp_path, ESCAPES["start a program"])
    assert SB.run_job(prog, caps=["programs"], timeout=20).text.strip() == "1"
    assert SB.static_check(ESCAPES["start a program"], ["programs"]) == []


def test_denied_paths_cannot_be_read_and_tests_never_get_extra_powers(tmp_path):
    secret = tmp_path / "secrets" / "token.txt"
    secret.parent.mkdir()
    secret.write_text("hunter2")
    p = tool_file(tmp_path, "def run(a):\n    return open(a['f']).read()\n")
    o = SB.run_job(p, args={"f": str(secret)}, deny_read=[str(secret.parent)])
    assert not o.ok and "hunter2" not in o.text
    net = tool_file(tmp_path, ESCAPES["network"])
    assert not SB.run_job(net, mode="test", tests="assert run({}) == 'net'", caps=["internet"]).ok


def test_endless_loops_time_out_and_secrets_are_not_in_the_environment(tmp_path, monkeypatch):
    p = tool_file(tmp_path, "def run(a):\n    while True:\n        pass\n")
    o = SB.run_job(p, timeout=2)
    assert not o.ok and o.timed_out
    monkeypatch.setenv("DEV_API_KEY", "letmein")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_x")
    env = tool_file(tmp_path, "import os\ndef run(a):\n    return repr(sorted(os.environ))\n")
    out = SB.run_job(env).text
    assert "DEV_API_KEY" not in out and "GITHUB_TOKEN" not in out


def test_tier_floor_follows_the_needs():
    assert SB.tier_floor([]) == "read" and SB.tier_floor(["files"]) == "write"
    assert SB.tier_floor(["files", "programs"]) == "exec" and SB.tier_floor(["internet"]) == "network"
