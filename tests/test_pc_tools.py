"""P5 PC tools: how the safety policy tiers them (read actions auto, control actions ask, hard actions always ask,
dry runs are read), that _pc routes admin vs non-admin correctly, and that routing shows the right small tool set.
"""
import os
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
SRC = HERE.parent / "src"
for p in (str(SRC), str(HERE)):
    if p not in sys.path:
        sys.path.insert(0, p)

from _safety_helpers import Env  # noqa: E402
from lucy.safety.policy import AUTO, ASK, ASK_HARD, DENY  # noqa: E402
from lucy.tools import registry as R, routing  # noqa: E402

if R.default.get("service") is None:
    R.default.discover()


def lvl(e, name, args):
    spec = R.default.get(name)
    return e.layer.decide(name, spec.tier, args, "s1").level


# ------------------------------------------------------------------------------------------- tiers / asks
def test_read_actions_run_but_control_actions_ask(monkeypatch):
    e = Env(monkeypatch=monkeypatch)
    assert lvl(e, "service", {"action": "list"}) == AUTO
    assert lvl(e, "process", {"action": "list"}) == AUTO
    assert lvl(e, "system_info", {"action": "disk"}) == AUTO
    assert lvl(e, "window", {"action": "list"}) == AUTO
    assert lvl(e, "desktop", {"action": "clipboard_get"}) == AUTO
    assert lvl(e, "winget", {"action": "search", "query": "7zip"}) == AUTO
    assert lvl(e, "registry", {"action": "read", "key": "HKCU\\Software\\X"}) == AUTO
    # control / change actions ask in the default "ask_for_risky"
    assert lvl(e, "service", {"action": "start", "name": "Spooler"}) == ASK      # system tier -> asks (start isn't "hard")
    assert lvl(e, "window", {"action": "focus", "pid": 10}) == ASK
    assert lvl(e, "desktop", {"action": "open", "target": "notepad"}) == ASK
    e.close()


def test_hard_actions_always_ask_even_in_full_auto(monkeypatch):
    e = Env(mode="full_auto", monkeypatch=monkeypatch)
    for name, args in [("process", {"action": "kill", "pid": 1234}),
                       ("service", {"action": "stop", "name": "Spooler"}),
                       ("winget", {"action": "uninstall", "id": "7zip.7zip"}),
                       ("scheduled_task", {"action": "disable", "name": "T"}),
                       ("registry", {"action": "write", "key": "HKLM\\Software\\X", "value": "v", "data": "1"})]:
        assert lvl(e, name, args) == ASK_HARD, (name, args)
    # a plain control action is allowed to run in full auto
    assert lvl(e, "window", {"action": "focus", "pid": 10}) == AUTO
    e.close()


def test_dry_run_is_always_just_read(monkeypatch):
    e = Env(mode="ask_every_time", monkeypatch=monkeypatch)
    assert lvl(e, "service", {"action": "stop", "name": "Spooler", "dry_run": True}) == AUTO
    assert lvl(e, "process", {"action": "kill", "pid": 10, "dry_run": True}) == AUTO
    assert lvl(e, "winget", {"action": "install", "id": "7zip.7zip", "dry_run": True}) == AUTO
    e.close()


def test_registry_write_to_hkcu_vs_hklm_is_still_hard(monkeypatch):
    e = Env(monkeypatch=monkeypatch)
    # HKCU write does not need admin, but a registry write is a HARD action regardless.
    assert lvl(e, "registry", {"action": "write", "key": "HKCU\\Software\\App", "value": "v", "data": "1"}) == ASK_HARD
    e.close()


# ------------------------------------------------------------------------------------------- admin routing (_pc)
def test_pc_routes_admin_ops_to_the_broker_and_reads_to_local(monkeypatch):
    from lucy.tools import _pc
    from lucy.broker import ops

    class FakeClient:
        def __init__(self):
            self.calls = []

        def call(self, op, args, dry_run=False, wait=100):
            self.calls.append((op, dry_run))
            return {"done": True, "result": f"broker ran {op}"}

        def poll(self, job, wait=100):
            return {"done": True, "result": "polled"}
    fc = FakeClient()
    _pc.set_client(fc)
    _pc.set_runner(lambda argv, t, env: (0, "local out", ""))
    try:
        # service.list is read -> local (no broker call)
        from lucy.tools.pc_admin import _h_service, _h_process
        out = _h_service({"action": "list"}, None)
        assert fc.calls == [] and ("local out" in out or out.startswith("Failed") is False)
        # service.start is admin -> broker
        _h_service({"action": "start", "name": "Spooler"}, None)
        assert fc.calls[-1][0] == "service.start"
        # process.kill tries local first, then broker on "needs administrator rights"
        _pc.set_runner(lambda argv, t, env: (1, "", "Access is denied"))
        _h_process({"action": "kill", "pid": 99999}, None)
        assert fc.calls[-1][0] == "process.kill"
    finally:
        _pc.set_client(None)
        _pc.set_runner(None)


# ------------------------------------------------------------------------------------------- routing
def test_routing_keeps_the_pc_set_small():
    cases = {
        "install 7zip please": "winget",
        "the print spooler service is stuck": "service",
        "chrome is frozen, kill it": "process",
        "how much free space on D": "system_info",
        "back up my thesis folder to E": "file_ops",
        "take a screenshot": "screenshot",
        "click the Save button": "ui_click",
        "what windows are open": "window",
        "set the volume to 20": "desktop",
        "disable the startup task for OneDrive": "scheduled_task",
    }
    for text, want in cases.items():
        sel = routing.select_tools(text)
        assert want in sel, (text, want, sel)
        assert len(sel) <= 12, (text, len(sel), sel)           # still a small prompt


def test_describe_call_gives_a_plain_summary():
    from lucy.tools import pc_admin, pc_desktop, file_ops
    assert "UAC" in pc_admin.describe_call("service", {"action": "stop", "name": "Spooler"})
    assert "Delete" in file_ops.describe_call("file_ops", {"action": "delete", "path": "D:/x"})
    assert "Recycle Bin" in file_ops.describe_call("file_ops", {"action": "delete", "path": "D:/x"})
    assert "Open" in pc_desktop.describe_call("desktop", {"action": "open", "target": "notepad"})


def test_layer_uses_tool_describe_call_in_the_card(monkeypatch):
    e = Env(monkeypatch=monkeypatch)
    d = e.layer.decide("service", "system", {"action": "stop", "name": "Spooler"}, "s1")
    card = e.layer.describe("service", {"action": "stop", "name": "Spooler"}, d)
    assert "Spooler" in card["summary"] and "stop" in card["summary"].lower()
    assert card["details"]                                      # raw args still shown underneath
    e.close()
