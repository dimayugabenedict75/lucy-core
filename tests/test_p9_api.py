"""P9 wiring (needs FastAPI + the full app): project block in the prompt, remember -> project notes, the desk routes."""
import json
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import pytest  # noqa: E402
from test_p9_desk import Env  # noqa: E402

fastapi = pytest.importorskip("fastapi")
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from lucy.desk import web  # noqa: E402
from lucy.server import api  # noqa: E402


@pytest.fixture
def world(tmp_path, monkeypatch):
    env = Env(pathlib.Path(tmp_path))
    monkeypatch.setattr(api, "_desk_ctx", env.desk)
    yield env
    for sid in ("p9-api-1", "p9-api-2", "p9-api-3"):      # _build_messages creates these in the REAL sessions db; an empty
        api._delete_session_db(sid)                       # newest chat would steal proactive messages in other tests


def test_prompt_has_the_project_block_only_inside_a_project(world):
    p = world.desk.projects.create("Garden", "", "Talk like a gardener.")
    world.session("p9-api-1")
    world.desk.projects.assign("p9-api-1", p["id"])
    msgs = api._build_messages("p9-api-1", "hi")
    block = [m["content"] for m in msgs if m["role"] == "system" and m["content"].startswith("PROJECT:")]
    assert block and "Talk like a gardener." in block[0]
    world.session("p9-api-2")
    assert not any(m["content"].startswith("PROJECT:") for m in api._build_messages("p9-api-2", "hi") if m["role"] == "system")


def test_remember_inside_a_project_goes_to_its_notes_not_global_memory(world, monkeypatch):
    p = world.desk.projects.create("Garden")
    world.session("p9-api-3")
    world.desk.projects.assign("p9-api-3", p["id"])

    def boom(*a, **k):
        raise AssertionError("global memory must not be touched")
    monkeypatch.setattr(api._memory_manager, "save_memory", boom)
    token = api._current_session.set("p9-api-3")
    try:
        out = json.loads(api._run_memory_tool("remember", {"content": "Tomatoes go on the south wall."}))
    finally:
        api._current_session.reset(token)
    assert out["status"] == "saved" and "Garden" in out["scope"]
    assert [n["text"] for n in world.desk.projects.notes(p["id"])] == ["Tomatoes go on the south wall."]


def test_desk_routes_round_trip(world):
    app = FastAPI()
    app.include_router(web.build_router(lambda: world.desk))
    c = TestClient(app)
    pid = c.post("/api/projects", json={"name": "Site", "instructions": "Be brief."}).json()["id"]
    assert c.post(f"/api/projects/{pid}/files", files={"file": ("n.txt", b"hello")}).status_code == 200
    one = c.get(f"/api/projects/{pid}").json()
    assert one["name"] == "Site" and one["files"][0]["name"] == "n.txt"
    world.session("s1", "Export me")
    u = world.msg("user", "hi there")
    world.msg("assistant", "hello!")
    c.post("/api/projects/assign", json={"session_id": "s1", "project_id": pid})
    assert c.get("/api/projects").json()["sessions"] == {"s1": pid}
    r = c.get("/api/desk/sessions/s1/export?format=md")
    assert r.status_code == 200 and "attachment" in r.headers["content-disposition"] and "### You" in r.text
    assert c.get("/api/desk/sessions/s1/export?format=json").json()["messages"][0]["content"] == "hi there"
    b = c.post("/api/desk/sessions/s1/branch", json={"message_id": u}).json()
    assert b["copied"] == 1 and b["session_id"] != "s1"
    e = c.post("/api/desk/sessions/s1/edit", json={"message_id": u}).json()
    assert e["text"] == "hi there" and e["removed"] == 2
    assert c.post("/api/desk/sessions/s1/regenerate").status_code == 400          # nothing left to regenerate
    assert c.get("/api/desk/usage?session_id=s1").json()["level"] == "ok"
    assert [x["name"] for x in c.get("/api/desk/commands").json()["commands"]][:2] == ["plan", "research"]
    assert c.delete(f"/api/projects/{pid}").status_code == 200
