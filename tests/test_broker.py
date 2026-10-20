"""P5 admin broker + the op menu. Runs on any OS: the real elevated launch is replaced by a thread that runs the
broker in-process (same code, no UAC), and PowerShell/program runs are faked. Windows-only ops are exercised through
the fake runner (their argv/PowerShell is checked), registry uses the real winreg only when present (skipped elsewhere).
"""
import json
import os
import pathlib
import sys
import threading
import time

HERE = pathlib.Path(__file__).resolve().parent
SRC = HERE.parent / "src"
for p in (str(SRC), str(HERE)):
    if p not in sys.path:
        sys.path.insert(0, p)

from lucy.broker import ops, server as broker_server          # noqa: E402
from lucy.broker.client import BrokerClient                   # noqa: E402


# ------------------------------------------------------------------------------------------- the op menu directly
def fake_runner(script_map):
    """Return a runner that answers PowerShell/program calls from a dict of (substring -> (rc, out, err))."""
    calls = []

    def run(argv, timeout, env):
        calls.append((argv, env))
        joined = " ".join(argv)
        for needle, resp in script_map.items():
            if needle in joined or any(needle in str(v) for v in env.values()):
                return resp
        return 0, "", ""
    run.calls = calls
    return run


def ctx(runner=None, dry_run=False, guard=None):
    return ops.Ctx(runner=runner or fake_runner({}), guard=guard, dry_run=dry_run)


def test_service_list_parses_json_table():
    r = fake_runner({"Get-Service": (0, json.dumps([{"Name": "Spooler", "DisplayName": "Print Spooler",
                                                     "Status": "Running", "Start": "Automatic"}]), "")})
    out = ops.run("service.list", {}, ctx(r))
    assert "Spooler" in out and "Print Spooler" in out and "Running" in out


def test_service_stop_refuses_core_service_without_running_anything():
    r = fake_runner({})
    out = ops.run("service.stop", {"name": "RpcSs"}, ctx(r))
    assert out.startswith("Failed") and "core Windows" in out and r.calls == []


def test_dry_run_changes_nothing():
    r = fake_runner({})
    out = ops.run("service.restart", {"name": "Spooler", "dry_run": True}, ctx(r, dry_run=True))
    assert out.startswith("DRY RUN") and r.calls == []


def test_process_kill_protects_lucy_and_core_processes():
    me = os.getpid()
    assert ops.run("process.kill", {"pid": me}, ops.Ctx()).startswith("Failed")
    assert ops.run("process.kill", {"name": "lsass"}, ops.Ctx()).startswith("Failed")
    assert "Lucy herself" in ops.run("process.kill", {"name": "python"}, ops.Ctx())
    r = fake_runner({"taskkill": (0, "SUCCESS", "")})
    out = ops.run("process.kill", {"pid": 99999}, ctx(r))
    assert out.startswith("Done") and r.calls[0][0][:2] == ["taskkill", "/PID"]


def test_winget_install_builds_a_safe_argv():
    r = fake_runner({"winget": (0, "Successfully installed", "")})
    out = ops.run("winget.install", {"id": "7zip.7zip"}, ctx(r))
    argv = r.calls[0][0]
    assert argv[:3] == ["winget", "install", "--id"] and "7zip.7zip" in argv and "--silent" in out.lower() or out.startswith("Done")
    assert ops.run("winget.install", {"id": "bad id; rm -rf"}, ctx(r)).startswith("Failed")


def test_registry_key_parsing_and_denied_keys():
    assert ops.reg_key("HKLM\\Software\\X") == ("HKEY_LOCAL_MACHINE", "Software\\X")
    assert ops.reg_key("HKCU:/Software/App") == ("HKEY_CURRENT_USER", "Software/App".replace("/", "\\"))
    try:
        ops.reg_key("NOPE\\x")
        assert False
    except ops.OpError:
        pass
    out = ops.run("registry.write", {"key": "HKLM\\SAM\\x", "value": "v", "data": "1"}, ops.Ctx())
    assert out.startswith("Failed") and "security" in out.lower()


def test_disk_usage_lists_free_space(tmp_path):
    out = ops.run("info.disk", {}, ops.Ctx())
    assert "free of" in out
    (tmp_path / "big.bin").write_bytes(b"x" * 5000)
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "a.bin").write_bytes(b"y" * 3000)
    out2 = ops.run("info.disk", {"path": str(tmp_path)}, ops.Ctx())
    assert "sub/" in out2


def test_events_level_and_log_validation():
    assert ops.run("info.events", {"log": "Security"}, ops.Ctx()).startswith("Failed")   # Security is off the menu
    assert ops.run("info.events", {"log": "System", "level": "nope"}, ops.Ctx()).startswith("Failed")


# ------------------------------------------------------------------------------------------- guard (protected paths)
def test_guard_blocks_lucy_core_drive_roots_and_system(tmp_path):
    lucy = tmp_path / "Lucy_Core"
    lucy.mkdir()
    g = ops.Guard(str(lucy), extra_denied=[str(tmp_path / "secrets")])
    for bad in (str(lucy / "src" / "x.py"), str(tmp_path / "secrets" / "k")):
        try:
            g.check(bad)
            assert False, bad
        except ops.OpError:
            pass
    assert g.check(str(tmp_path / "ok" / "a.txt")).endswith("/ok/a.txt")


def test_guard_blocks_drive_roots_with_windows_path_rules(monkeypatch):
    import ntpath
    monkeypatch.setattr(ops.os, "path", ntpath)
    monkeypatch.setattr(ops.os, "name", "nt")
    g = ops.Guard("C:/Users/me/Lucy_Core", extra_denied=[])
    for bad in ("C:\\", "C:", "D:/", "/"):
        try:
            g.check(bad)
            assert False, bad
        except ops.OpError:
            pass


def test_denied_paths_read_from_yaml_without_pyyaml(tmp_path):
    (tmp_path / ".core").mkdir()
    (tmp_path / ".core" / "permissions.yaml").write_text(
        "denied_paths:\n  - \"{LUCY_ROOT}/.env\"\n  - \"{HOME}/.ssh\"\nreadonly_paths:\n  - \"C:/Windows\"\n")
    got = ops.denied_paths_from_yaml(str(tmp_path))
    assert any(".env" in x for x in got) and any(".ssh" in x for x in got) and not any("Windows" in x for x in got)


# ------------------------------------------------------------------------------------------- file ops
def test_fs_copy_move_delete_mkdir(tmp_path):
    trashed = []
    c = ops.Ctx(guard=ops.Guard(None), trash=lambda p: (trashed.append(p), "the Recycle Bin")[1])
    (tmp_path / "a.txt").write_text("hi")
    assert ops.run("fs.copy", {"source": str(tmp_path / "a.txt"), "destination": str(tmp_path / "b.txt")}, c).startswith("Done")
    assert (tmp_path / "b.txt").read_text() == "hi"
    assert ops.run("fs.copy", {"source": str(tmp_path / "a.txt"), "destination": str(tmp_path / "b.txt")}, c).startswith("Failed")
    assert ops.run("fs.mkdir", {"path": str(tmp_path / "d")}, c).startswith("Done") and (tmp_path / "d").is_dir()
    assert ops.run("fs.move", {"source": str(tmp_path / "b.txt"), "destination": str(tmp_path / "d")}, c).startswith("Done")
    assert (tmp_path / "d" / "b.txt").exists()
    assert ops.run("fs.delete", {"path": str(tmp_path / "a.txt")}, c).startswith("Done") and trashed


def test_fs_dry_run(tmp_path):
    c = ops.Ctx(guard=ops.Guard(None), dry_run=True)
    (tmp_path / "a.txt").write_text("hi")
    out = ops.run("fs.delete", {"path": str(tmp_path / "a.txt")}, c)
    assert out.startswith("DRY RUN") and (tmp_path / "a.txt").exists()


# ------------------------------------------------------------------------------------------- broker over loopback
class InProcBroker:
    """The real Broker, served in a background thread (no UAC). Client talks to it over 127.0.0.1 with the HMAC secret."""

    def __init__(self, tmp_path, runner=None):
        self.dir = pathlib.Path(tmp_path) / "broker"
        self.dir.mkdir(parents=True)
        self.root = pathlib.Path(tmp_path)
        self.runner = runner or fake_runner({"taskkill": (0, "SUCCESS", ""), "Get-Service": (0, "[]", "")})
        self.broker = None

    def launcher(self, python, args, cwd):
        secret = bytes.fromhex((self.dir / "secret").read_text())
        (self.dir / "secret").unlink()
        self.broker = ops_broker = broker_server.Broker(secret, root=str(self.root), parent_pid=os.getpid(),
                                                         idle_s=30, runner=self.runner, guard=ops.Guard(None))
        with open(self.dir / "port.json", "w") as f:
            json.dump({"port": ops_broker.port, "pid": os.getpid()}, f)
        threading.Thread(target=ops_broker.serve_forever, daemon=True).start()
        for _ in range(50):
            if ops_broker.listener.address[1]:
                break
            time.sleep(0.01)
        return None


def _client(tmp_path, runner=None):
    ip = InProcBroker(tmp_path, runner)
    return BrokerClient(ip.dir, ip.root, launcher=ip.launcher, python="python", start_timeout=5), ip


def test_broker_starts_and_runs_an_admin_op(tmp_path):
    cl, ip = _client(tmp_path, fake_runner({"taskkill": (0, "SUCCESS", "")}))
    r = cl.call("process.kill", {"pid": 99999}, wait=5)
    assert r.get("done") and str(r["result"]).startswith("Done")
    assert cl.running()
    assert cl.stop()


def test_broker_rejects_a_wrong_secret(tmp_path):
    cl, ip = _client(tmp_path)
    assert cl.ensure() is None
    from multiprocessing.connection import Client as MC
    try:
        MC(("127.0.0.1", ip.broker.port), authkey=b"x" * 32).close()
        bad = False
    except Exception:
        bad = True
    assert bad                                                 # authentication fails for the wrong key
    cl.stop()


def test_broker_only_serves_the_menu(tmp_path):
    cl, ip = _client(tmp_path)
    cl.ensure()
    r = cl._request({"op": "evil.delete_everything", "args": {}}, 5)
    assert not r["ok"] and "menu" in r["error"]
    cl.stop()


def test_broker_refuses_to_start_when_unsealed(tmp_path, monkeypatch):
    # verify_self must fail if a broker file is not in the lock.
    (tmp_path / ".core").mkdir()
    (tmp_path / ".core" / "constitution.lock").write_text(json.dumps({"files": {}}))
    why = broker_server.verify_self(str(tmp_path))
    assert why and "sealed" in why


def test_broker_job_poll_for_a_long_op(tmp_path):
    def slow(argv, timeout, env):
        time.sleep(0.3)
        return 0, "installed", ""
    cl, ip = _client(tmp_path, slow)
    r = cl.call("winget.install", {"id": "7zip.7zip"}, wait=0.05)       # returns before it finishes
    assert not r.get("done") and r.get("job")
    r2 = cl.poll(r["job"], wait=5)
    assert r2.get("done") and "installed" in str(r2["result"])
    cl.stop()
