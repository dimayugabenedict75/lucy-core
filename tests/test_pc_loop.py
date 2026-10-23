"""P5 PC tools inside the agent loop against the scripted fake llama-server: a system-tier PC action pauses for an
approval card, runs through _pc after approval, and a denial is a result the model works around (same P1 behaviour).
"""
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from _safety_helpers import Env  # noqa: E402
from test_agent_loop import start_server, tool, CALL, ANS  # noqa: E402
from test_safety_loop import drive  # noqa: E402  (reuses the card-answering driver)
from lucy.tools import registry as R  # noqa: E402
from lucy.tools import _pc  # noqa: E402
from lucy.broker import ops  # noqa: E402

if R.default.get("service") is None:
    R.default.discover()

PC_TOOLS = [tool("service", {"action": {"type": "string"}, "name": {"type": "string"}}, ["action"]),
            tool("system_info", {"action": {"type": "string"}}, ["action"]),
            tool("process", {"action": {"type": "string"}, "pid": {"type": "integer"}}, ["action"])]


def _fake_local(result):
    _pc.set_runner(lambda argv, t, env: (0, result, ""))


def test_a_read_action_runs_without_a_card(monkeypatch):
    e = Env(monkeypatch=monkeypatch)
    _fake_local("50000000000 free of 100000000000")
    try:
        ran, cards, tm, text, _ = drive_pc(e, [CALL("system_info", {"action": "disk"}), ANS("Plenty of space.")])
        assert cards == [] and text == "Plenty of space."
    finally:
        _pc.set_runner(None)
    e.close()


def test_a_control_action_asks_and_runs_when_approved(monkeypatch):
    e = Env(monkeypatch=monkeypatch)

    class FakeClient:
        def __init__(self):
            self.ran = []

        def call(self, op, args, dry_run=False, wait=100):
            self.ran.append(op)
            return {"done": True, "result": f"Done: {op}"}
    fc = FakeClient()
    _pc.set_client(fc)
    try:
        ran, cards, tm, text, _ = drive_pc(e, [CALL("service", {"action": "stop", "name": "Spooler"}),
                                               ANS("Stopped the Spooler.")], answer=lambda c: "approve")
        assert len(cards) == 1 and "Spooler" in cards[0]["summary"]
        assert fc.ran == ["service.stop"] and text == "Stopped the Spooler."
    finally:
        _pc.set_client(None)
    e.close()


def test_a_denied_pc_action_becomes_a_result_the_model_works_around(monkeypatch):
    e = Env(monkeypatch=monkeypatch)

    class FakeClient:
        def call(self, *a, **k):
            raise AssertionError("broker must not be called after a denial")
    _pc.set_client(FakeClient())
    try:
        ran, cards, tm, text, _ = drive_pc(e, [CALL("service", {"action": "stop", "name": "Spooler"}),
                                               ANS("OK, I left it running.")], answer=lambda c: "deny")
        assert "DENIED" in tm[0]["content"] and text == "OK, I left it running."
    finally:
        _pc.set_client(None)
    e.close()


def drive_pc(env, script, answer=None):
    """Like test_safety_loop.drive, but the tools actually run through the registry (so _pc / the safety layer run)."""
    import asyncio
    from lucy.server.agent_loop import AgentLoop, LoopConfig
    from test_agent_loop import Brain
    srv = start_server()
    srv.script = list(script)
    events = []

    async def execute(name, args):
        return await R.default.run(name, args, R.ToolContext(session_id="s1"))
    loop = AgentLoop(brain=Brain(srv.server_address[1]), tools=PC_TOOLS, execute=execute,
                     activity_for=lambda n, a: n, count_tokens=lambda t: max(1, len(t) // 4), temperature=0.5,
                     cfg=LoopConfig(), gate=env.layer.gate_for("s1"), session_id="s1")

    async def go():
        agen = loop.run([{"role": "user", "content": "do it"}])
        async for ev in agen:
            events.append(ev)
            if ev.kind == "approval_request":
                d = answer(ev.data) if answer else None
                if d:
                    env.layer.broker.resolve(ev.data["id"], d, "s1")
    asyncio.run(go())
    srv.shutdown()
    tm = [m for m in srv.requests[-1]["messages"] if m["role"] == "tool"] if srv.requests else []
    cards = [ev.data for ev in events if ev.kind == "approval_request"]
    text = "".join(ev.data for ev in events if ev.kind == "content")
    return None, cards, tm, text, events
