"""P7 Lane A: Lucy's own tools - lifecycle (create -> probation -> active), tests gate, improve keeps the old version on
failure, auto-disable, tamper detection, user-only tier/enable, registry hot reload, and the safety layer around it."""
import asyncio, json, pathlib, sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from _safety_helpers import Env, safety  # noqa: E402
from lucy import selfdev  # noqa: E402
from lucy.selfdev import user_tools as UT  # noqa: E402
from lucy.safety import constitution as C  # noqa: E402
from lucy.tools import registry as R  # noqa: E402
import pytest  # noqa: E402

CODE = "def run(args):\n    return str(len(str(args.get('text', '')).split()))\n"
TESTS = "assert run({'text': 'a b c'}) == '3'\nassert run({}) == '0'\n"
FLAKY = "def run(args):\n    if args.get('boom'):\n        raise ValueError('boom')\n    return 'ok'\n"


class Clock:
    def __init__(self):
        self.t = 1_760_000_000.0

    def __call__(self):
        return self.t


@pytest.fixture
def world(tmp_path, monkeypatch):
    env = Env("ask_for_risky", monkeypatch)
    reg = R.Registry()
    clock = Clock()
    m = UT.UserTools(tools_dir=tmp_path / "tools_user", state_dir=env.root / "runtime" / "selfdev", registry=reg,
                     clock=clock, resolve=lambda p: p)
    monkeypatch.setattr(selfdev, "_tools", m)
    yield env, m, reg, clock
    env.close()


def make(m, name="word_count", code=CODE, tests=TESTS, **kw):
    return m.create(name=name, description="Count the words in a text. Returns the number.", code=code, tests=tests,
                    parameters={"text": "string: the text"}, reason="user asks for word counts often", session="s1", **kw)


def test_create_writes_the_files_registers_the_tool_and_records_provenance(world):
    env, m, reg, _ = world
    t = make(m)
    assert t["status"] == "probation" and t["tier"] == "exec" and t["version"] == 1
    mod, test = m.files("word_count")
    assert mod.exists() and test.exists() and "def run(args)" in mod.read_text()
    spec = reg.get("word_count")
    assert spec is not None and spec.tier == "exec" and spec.module == "lucy.tools_user.word_count"
    assert spec.schema["function"]["parameters"]["required"] == ["text"]
    h = t["history"][-1]
    assert h["session"] == "s1" and "word counts" in h["reason"]
    assert [e["event"] for e in m.log()] == ["created"]
    assert asyncio.run(spec.run({"text": "one two"}, R.ToolContext(session_id="s1"))).startswith("2")


def test_failing_tests_or_bad_code_save_nothing(world):
    env, m, reg, _ = world
    with pytest.raises(UT.ToolError, match="tests failed"):
        make(m, tests="assert run({'text': 'a'}) == '9'")
    with pytest.raises(UT.ToolError, match="import socket"):
        make(m, code="import socket\n" + CODE)
    with pytest.raises(UT.ToolError, match="tests are required"):
        make(m, tests="")
    assert reg.get("word_count") is None and not m.files("word_count")[0].exists() and m.list() == []


def test_names_cannot_shadow_builtins_and_limits_hold(world, monkeypatch):
    env, m, reg, _ = world
    reg.register(R.ToolSpec("read_local_file", {"type": "function", "function": {"name": "read_local_file",
                            "description": "x", "parameters": {}}}, lambda a, c: "", "read", "read"))
    for bad in ("read_local_file", "user_tool", "Bad-Name", "x"):
        with pytest.raises(UT.ToolError):
            make(m, name=bad)
    monkeypatch.setattr(UT, "DAILY_LIMIT", 1)
    make(m)
    with pytest.raises(UT.ToolError, match="daily limit"):
        make(m, name="other_tool")


def test_probation_ends_after_enough_good_runs(world, monkeypatch):
    env, m, reg, _ = world
    monkeypatch.setattr(UT, "PROBATION_USES", 2)
    make(m)
    first = m.call("word_count", {"text": "a"})
    assert first.startswith("1") and "probation: 1 more" in first
    m.call("word_count", {"text": "a"})
    assert m.list()[0]["status"] == "active"
    assert m.call("word_count", {"text": "a b"}) == "2"


def test_repeated_failures_switch_it_off_and_improve_brings_it_back(world):
    env, m, reg, _ = world
    make(m, name="flaky", code=FLAKY, tests="assert run({}) == 'ok'")
    for _ in range(UT.FAILS_TO_DISABLE):
        out = m.call("flaky", {"boom": True})
        assert out.startswith("Error: flaky failed")
    assert "switched OFF" in out and reg.get("flaky") is None
    t = m.list()[0]
    assert not t["enabled"] and t["disabled_by"] == "auto"
    fixed = "def run(args):\n    return 'ok'\n"
    t = m.create(name="flaky", description="", code=fixed, tests="", reason="crashed on boom", improve=True)
    assert t["enabled"] and t["version"] == 2 and reg.get("flaky") is not None
    assert m.call("flaky", {"boom": True}).startswith("ok")


def test_a_failing_improve_keeps_the_working_version(world):
    env, m, reg, _ = world
    make(m)
    with pytest.raises(UT.ToolError, match="working version 1 stays active"):
        m.create(name="word_count", description="", code="def run(args):\n    return 'nope'\n", tests="", reason="x",
                 improve=True)
    assert m.list()[0]["version"] == 1 and m.call("word_count", {"text": "a b"}).startswith("2")


def test_a_file_edited_behind_the_harness_back_is_switched_off_not_run(world):
    env, m, reg, _ = world
    make(m)
    mod = m.files("word_count")[0]
    mod.write_text(mod.read_text() + "\nimport os\n", encoding="utf-8")
    out = m.call("word_count", {"text": "a"})
    assert "changed outside user_tool" in out and reg.get("word_count") is None


def test_only_the_user_controls_tier_and_switching_back_on(world):
    env, m, reg, _ = world
    make(m)
    m.disable("word_count", "user does not want it", by="user")
    with pytest.raises(UT.ToolError, match="only the user"):
        m.create(name="word_count", description="", code=CODE, tests="", reason="again", improve=True)
    m.enable("word_count")
    m.set_tier("word_count", "read")
    assert reg.get("word_count").tier == "read"
    make(m, name="saver", code="def run(args):\n    return 'ok'\n", tests="assert run({}) == 'ok'", needs=["files"])
    with pytest.raises(UT.ToolError, match="cannot be lower than write"):
        m.set_tier("saver", "read")
    # the TIER line in the tool file is informational: editing it changes nothing (and is caught as tampering)
    assert "TIER = 'exec'" in m.files("saver")[0].read_text()


def test_revert_goes_back_one_version(world):
    env, m, reg, _ = world
    make(m)
    m.create(name="word_count", description="", code=CODE.replace("str(len(", "'n=' + str(len("), reason="prefix",
             tests="assert run({'text': 'a'}) == 'n=1'", improve=True)
    assert m.call("word_count", {"text": "a"}).startswith("n=1")
    m.revert("word_count")
    assert m.list()[0]["version"] == 1 and m.call("word_count", {"text": "a"}).startswith("1")


def test_parameters_are_built_forgivingly():
    p = UT.build_parameters({"text": "string: the text", "limit": "optional integer: at most this many", "flags": "list"})
    assert p["properties"]["text"] == {"type": "string", "description": "the text"}
    assert p["properties"]["limit"]["type"] == "integer" and p["required"] == ["text", "flags"]
    assert UT.build_parameters('["a", "b"]')["required"] == ["a", "b"]
    js = {"type": "object", "properties": {"x": {"type": "number"}}, "required": []}
    assert UT.build_parameters(js)["required"] == []


def test_the_user_tool_tool_runs_through_the_safety_layer_without_a_card(world):
    env, m, reg, _ = world
    from lucy.tools import user_tool
    d = env.layer.decide("user_tool", user_tool.TIER, {"action": "create"}, "s1")
    assert d.action == "auto"                                         # autonomous lane: no card in the default mode
    assert env.layer.decide("user_tool", user_tool.TIER, {"action": "list"}, "s1").tier == "read"
    d = env.layer.decide("word_count", "exec", {"text": "x"}, "s1")
    assert d.action == "ask"                                          # a new user tool asks until the user promotes it
    out = user_tool.handler({"action": "create", "name": "word_count", "description": "Count words.", "code": CODE,
                             "tests": TESTS, "parameters": {"text": "string"}, "reason": "test"}, R.ToolContext("s1"))
    assert out.startswith("Created word_count v1")
    assert "word_count v1 [probation, tier exec" in user_tool.handler({"action": "list"}, R.ToolContext("s1"))
    assert user_tool.handler({"action": "create", "name": "word_count", "code": CODE, "tests": TESTS, "reason": "x",
                              "description": "d"}, R.ToolContext("s1")).startswith("Error: word_count already exists")


def test_the_records_and_the_selfdev_code_are_constitution_protected(world):
    env, m, reg, _ = world
    rec = (env.root / "runtime" / "selfdev" / "user_tools.json").as_posix()
    d = env.layer.decide("write_local_file", "write", {"file_path": rec, "content": "{}"}, "s1")
    assert d.action == "deny" and d.rule == "constitution"            # the agent cannot promote its own tools
    code = [p.name for p in C.code_files() if p.parent.name == "selfdev"]
    assert {"sandbox.py", "_runner.py", "user_tools.py", "lane_b.py", "smoke.py"} <= set(code)
    new_mod = (C.SELFDEV_DIR / "sneaky.py").as_posix()
    cons = C.Constitution(env.root)
    assert cons.is_protected(C.norm_path(new_mod))                     # no new modules sneaking into the package


def test_tools_can_be_tested_and_run_with_the_real_deny_list(tmp_path, monkeypatch):
    """Regression (found on the user's PC): the staging folder sat inside a denied folder, so every create failed with
    'blocked: reading .../staging/<name>.py'. Uses the same deny list as production (selfdev._deny_read)."""
    env = Env("ask_for_risky", monkeypatch)
    try:
        sd = env.root / "runtime" / "selfdev"
        m = UT.UserTools(tools_dir=tmp_path / "tu", state_dir=sd, registry=R.Registry(),
                         deny_read=lambda: selfdev._deny_read() + [str(sd)])       # even if selfdev were denied
        make(m)
        assert m.call("word_count", {"text": "a b"}).startswith("2")
        secret = env.root / "secrets" / "key.txt"
        secret.write_text("hunter2")
        m.create(name="reader", description="Read a file.", code="def run(args):\n    return open(args['f']).read()\n",
                 tests="assert callable(run)", reason="test")
        assert "hunter2" not in m.call("reader", {"f": str(secret)})               # denied_paths still hold
    finally:
        env.close()
