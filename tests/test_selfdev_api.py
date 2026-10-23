"""P7 wiring in api.py (needs FastAPI): user tools reach the catalog (not the always-on prompt), and the user-only routes
(approve / reject / rollback, tier / enable / disable). Lane B checks are injected (no real test suite / canary here)."""
import asyncio, pathlib, sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import pytest  # noqa: E402
from _safety_helpers import Env  # noqa: E402
from test_selfdev_lane_b import make_repo, edit  # noqa: E402
from lucy import selfdev  # noqa: E402
from lucy.selfdev import lane_b as LB, user_tools as UT, trace_mining as TM  # noqa: E402
from lucy.server import api  # noqa: E402
from lucy.tools import registry as R  # noqa: E402


class Req:
    def __init__(self, body):
        self._b = body

    async def json(self):
        return self._b


@pytest.fixture
def world(tmp_path, monkeypatch):
    env = Env("ask_for_risky", monkeypatch)
    ut = UT.UserTools(tools_dir=tmp_path / "tu", state_dir=tmp_path / "sd", registry=R.default)
    root = make_repo(tmp_path / "live")
    lb = LB.LaneB(root=root, state_dir=tmp_path / "sd", workspaces=tmp_path / "ws",
                  run_tests=lambda ws, log: (True, "ok"), run_canary=lambda ws, log: (True, "ok"))
    mn = TM.TraceMiner(trace_path=tmp_path / "none.jsonl", state_dir=tmp_path / "sd")
    selfdev.set_instances(ut, lb)
    selfdev.set_miner(mn)
    yield env, ut, lb
    for n in [t["name"] for t in ut.list()]:
        R.default.unregister(n)
    selfdev.set_instances(None, None)
    selfdev.set_miner(None)
    env.close()


def test_user_tools_are_in_the_catalog_not_the_prompt(world):
    env, ut, lb = world
    ut.create(name="word_count", description="Count words.", code="def run(args):\n    return '1'\n",
              tests="assert run({}) == '1'", reason="test")
    shown, cat = api._tools_for_turn([{"role": "user", "content": "hello"}], session_id="p7-api-1")
    assert "word_count" not in [t["function"]["name"] for t in shown]
    assert cat.get("word_count") is not None and cat.search("count words")[0]["function"]["name"] == "word_count"
    shown, _ = api._tools_for_turn([{"role": "user", "content": "make yourself a tool for this"}], session_id="p7-api-2")
    assert "user_tool" in [t["function"]["name"] for t in shown]


def test_status_and_the_user_only_routes(world):
    env, ut, lb = world
    ut.create(name="word_count", description="Count words.", code="def run(args):\n    return '1'\n",
              tests="assert run({}) == '1'", reason="test")
    st = asyncio.run(api.selfdev_status())
    assert st["root"] and st["tools"][0]["name"] == "word_count" and st["proposals"] == []
    t = asyncio.run(api.selfdev_tool("word_count", "tier", Req({"tier": "read"})))
    assert t["tier"] == "read" and R.default.get("word_count").tier == "read"
    asyncio.run(api.selfdev_tool("word_count", "disable", Req({})))
    assert R.default.get("word_count") is None
    with pytest.raises(api.HTTPException) as e:
        asyncio.run(api.selfdev_tool("nope", "enable", Req({})))
    assert e.value.status_code == 409
    pid = lb.start("t", "r")["id"]
    edit(lb, pid, "src/lucy/tools/web.py", "TIMEOUT = 20\n")
    lb.submit(pid, background=False)
    assert "+TIMEOUT = 20" in asyncio.run(api.selfdev_proposal(pid))["diff"]
    rec = asyncio.run(api.selfdev_decide(pid, "approve"))
    assert rec["status"] == "applied" and (lb.root / "src/lucy/tools/web.py").read_text() == "TIMEOUT = 20\n"
    with pytest.raises(api.HTTPException):
        asyncio.run(api.selfdev_decide(pid, "approve"))
    m = asyncio.run(api.selfdev_rollback(Req({})))
    assert m["status"] == "rolled_back" and (lb.root / "src/lucy/tools/web.py").read_text() == "TIMEOUT = 10\n"
    assert asyncio.run(api.selfdev_mine())["ideas"] == []
