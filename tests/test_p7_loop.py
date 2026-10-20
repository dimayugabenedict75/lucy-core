"""P7 in the agent loop (scripted fake llama-server, temporary safety layer): Lucy writes a tool with user_tool (no card),
then calls it in the SAME turn (live catalog); the new tool runs at tier exec, so it asks; a denial is a normal result."""
import asyncio, pathlib, sys, types

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from _safety_helpers import Env  # noqa: E402
from test_agent_loop import Brain, start_server, tool, CALL, ANS, S  # noqa: E402
from lucy import selfdev  # noqa: E402
from lucy.selfdev import user_tools as UT  # noqa: E402
from lucy.server.agent_loop import AgentLoop, LoopConfig  # noqa: E402
from lucy.tools import registry as R, routing as RT  # noqa: E402
import pytest  # noqa: E402

CREATE = {"action": "create", "name": "word_count", "description": "Count the words in a text.",
          "code": "def run(args):\n    return str(len(args.get('text', '').split()))\n",
          "tests": "assert run({'text': 'a b'}) == '2'", "parameters": {"text": "string: the text"},
          "reason": "the user counts words a lot"}


@pytest.fixture
def world(tmp_path, monkeypatch):
    env = Env("ask_for_risky", monkeypatch)
    R.default.discover()
    m = UT.UserTools(tools_dir=tmp_path / "tools_user", state_dir=env.root / "runtime" / "selfdev", registry=R.default)
    monkeypatch.setattr(selfdev, "_tools", m)
    yield env, m
    for n in [t["name"] for t in m.list()]:
        R.default.unregister(n)
    env.close()


def drive(world, script, answer=None, session="s1"):
    env, m = world
    srv = start_server(); srv.script = list(script)
    events, ran = [], []

    async def execute(name, args):
        ran.append(name)
        spec = R.default.get(name)
        return await spec.run(args, R.ToolContext(session_id=session)) if spec else f"Unknown tool: {name}"
    base = [R.default.get("user_tool").schema, tool("read_local_file", {"file_path": S}, ["file_path"])]
    shown = base + [RT.tool_search_schema()]
    cat = RT.ToolCatalog(base + m.schemas(), keywords=m.keywords(), live=m.schemas)
    loop = AgentLoop(brain=Brain(srv.server_address[1]), tools=shown, execute=execute, activity_for=lambda n, a: n,
                     count_tokens=lambda t: max(1, len(t) // 4), temperature=0.5, cfg=LoopConfig(),
                     gate=env.layer.gate_for(session), session_id=session, tool_catalog=cat)

    async def go():
        async for ev in loop.run([{"role": "user", "content": "make yourself a tool that counts words, then count 'a b c'"}]):
            events.append(ev)
            if ev.kind == "approval_request" and answer:
                assert env.layer.broker.resolve(ev.data["id"], answer(ev.data), session)
    asyncio.run(go()); srv.shutdown()
    return types.SimpleNamespace(ran=ran, requests=srv.requests,
                                 tool_msgs=lambda i=-1: [x["content"] for x in srv.requests[i]["messages"] if x["role"] == "tool"],
                                 cards=[e.data for e in events if e.kind == "approval_request"],
                                 text="".join(e.data for e in events if e.kind == "content"))


def test_lucy_writes_a_tool_and_uses_it_in_the_same_turn(world):
    r = drive(world, [CALL("user_tool", CREATE), CALL("word_count", {"text": "a b c"}), ANS("It has 3 words.")],
              answer=lambda c: "approve")
    assert r.ran == ["user_tool", "word_count"]
    assert r.tool_msgs(1)[0].startswith("Created word_count v1: tests passed")
    assert len(r.cards) == 1 and r.cards[0]["tool"] == "word_count"               # user_tool itself: no card
    assert r.tool_msgs(2)[-1].startswith("3") and "probation" in r.tool_msgs(2)[-1]
    assert "word_count" in [t["function"]["name"] for t in r.requests[2]["tools"]]
    assert r.text.endswith("It has 3 words.")


def test_a_denied_user_tool_is_a_result_and_failing_tests_are_reported(world):
    bad = dict(CREATE, tests="assert run({'text': 'a b'}) == '5'")
    r = drive(world, [CALL("user_tool", bad), CALL("user_tool", CREATE), CALL("word_count", {"text": "x"}),
                      ANS("You did not allow it.")], answer=lambda c: "deny")
    first = r.tool_msgs(1)[0]
    assert first.startswith("Error: tests failed, nothing changed") and "AssertionError" in first
    assert "DENIED" in r.tool_msgs(3)[-1] and r.text.endswith("You did not allow it.")
    assert world[1].list()[0]["uses_total"] == 0                                  # it never ran
