"""P4 wiring in api.py (needs FastAPI): MCP tools reach the model only through tool_search / by name, the user routes work."""
import asyncio, json, pathlib, sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import pytest  # noqa: E402
from _safety_helpers import Env  # noqa: E402
from test_mcp import make  # noqa: E402
from lucy import mcp as MCP  # noqa: E402
from lucy.server import api  # noqa: E402


@pytest.fixture
def world(monkeypatch):
    env = Env("ask_for_risky", monkeypatch)
    m, reg = make(env, use_default=True)
    m.refresh("fake")
    MCP.set_default(m)
    yield env, m, reg
    MCP.set_default(None)                      # shuts the fake server down
    env.close()


def msgs(text):
    return [{"role": "user", "content": text}]


def test_turn_tools_never_include_mcp_tools_but_the_catalog_and_search_description_do(world):
    shown, catalog = api._tools_for_turn(msgs("hello there"), None, "mcp-api-1")
    shown_names = [t["function"]["name"] for t in shown]
    assert not [n for n in shown_names if n.startswith("fake__")] and shown_names[-1] == "tool_search"
    assert "Also connected: fake things" in shown[-1]["function"]["description"]
    assert catalog.get("fake__echo") is not None and [t["function"]["name"] for t in catalog.search("fake things")][0].startswith("fake__")


def test_a_tool_the_model_pulled_in_stays_for_the_next_turns_after_the_builtin_tools(world):
    sid = "mcp-api-2"
    api._tools_for_turn(msgs("hi"), None, sid)
    api._note_added_tools(sid, ["fake__echo"])
    shown, _ = api._tools_for_turn(msgs("and now?"), None, sid)
    n = [t["function"]["name"] for t in shown]
    assert "fake__echo" in n and n.index("fake__echo") == len(n) - 2 and n[-1] == "tool_search"


def test_routing_off_means_no_catalog_so_no_mcp(world, monkeypatch):
    monkeypatch.setenv("LUCY_TOOL_ROUTING", "0")
    shown, catalog = api._tools_for_turn(msgs("hi"), None, "mcp-api-3")
    assert catalog is None and not [t for t in shown if t["function"]["name"].startswith("fake__")]


def test_a_broken_manager_never_breaks_a_chat_turn(world, monkeypatch):
    monkeypatch.setattr(world[1], "catalog_schemas", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    shown, catalog = api._tools_for_turn(msgs("hi"), None, "mcp-api-4")
    assert shown[-1]["function"]["name"] == "tool_search" and catalog is not None


def test_user_routes_status_toggle_refresh_stop(world):
    st = asyncio.run(api.mcp_status())
    assert st["servers"][0]["key"] == "fake" and st["servers"][0]["tools"] == 15
    class Req:
        async def json(self): return {"enabled": False}
    st = asyncio.run(api.mcp_enabled("fake", Req()))
    assert st["servers"][0]["enabled"] is False and st["servers"][0]["tools"] == 0
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as e:
        asyncio.run(api.mcp_enabled("nope", Req()))
    assert e.value.status_code == 404
    class On:
        async def json(self): return {"enabled": True}
    asyncio.run(api.mcp_enabled("fake", On()))
    st = asyncio.run(api.mcp_refresh("fake"))
    assert st["servers"][0]["tools"] == 15 and st["servers"][0]["running"] is True
    st = asyncio.run(api.mcp_stop("fake"))
    assert st["servers"][0]["running"] is False
