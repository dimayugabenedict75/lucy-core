"""P6 wiring in api.py (needs FastAPI): persona in the prompt (hot reload), the user routes, SELF never in front of CORE."""
import asyncio
import pathlib
import sys
import time

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import pytest  # noqa: E402
from lucy import persona  # noqa: E402
from lucy.persona.loader import PersonaLoader  # noqa: E402
from lucy.persona.self_store import SelfStore  # noqa: E402
from lucy.server import api  # noqa: E402


@pytest.fixture
def world(tmp_path, monkeypatch):
    core = pathlib.Path(tmp_path) / "lucy.md"
    core.write_text("CORE-TEXT", encoding="utf-8")
    store = SelfStore(pathlib.Path(tmp_path) / "lucy_self.md")
    persona.set_store(store)
    monkeypatch.setattr(api, "_persona", PersonaLoader(core, store))
    monkeypatch.setattr(api, "_reflector", None)
    yield store
    persona.set_store(None)


class Req:
    def __init__(self, body):
        self._b = body

    async def json(self):
        return self._b


def test_persona_message_is_core_then_self_and_changes_without_a_restart(world):
    first = api._build_messages("p6-session-1", "hello")[0]["content"]
    assert first == "CORE-TEXT"
    world.edit("speaking", "add", "Lead with the answer", "he asked")
    second = api._build_messages("p6-session-1", "hello")[0]["content"]
    assert second.startswith("CORE-TEXT") and "Lead with the answer" in second
    assert api._build_messages("p6-session-1", "hello again")[0]["content"] == second     # stable = KV cache friendly


def test_memory_rules_tell_lucy_about_update_self(world):
    msgs = api._build_messages("p6-session-2", "hello")
    assert any("update_self" in m["content"] for m in msgs if m["role"] == "system")


def test_status_changelog_revert_routes(world):
    e = world.edit("traits", "add", "Dry humor lands", "he laughed")
    st = asyncio.run(api.persona_status())
    assert st["chars"] > 0 and st["sections"][0]["items"] == ["Dry humor lands"] and "reflection" in st
    log = asyncio.run(api.persona_changelog())
    assert log["entries"][0]["id"] == e["id"]
    out = asyncio.run(api.persona_revert(Req({"id": e["id"]})))
    assert out["status"] == "reverted" and world.read()["traits"] == []
    with pytest.raises(api.HTTPException) as err:
        asyncio.run(api.persona_revert(Req({"id": e["id"]})))
    assert err.value.status_code == 409
    with pytest.raises(api.HTTPException) as err:
        asyncio.run(api.persona_revert(Req({"id": "abc"})))
    assert err.value.status_code == 400


def test_reflect_route_without_a_reflector_is_a_clear_503(world):
    with pytest.raises(api.HTTPException) as err:
        asyncio.run(api.persona_reflect())
    assert err.value.status_code == 503


def test_recall_marks_conflicting_memories(world, monkeypatch):
    doc = {"id": 1, "text": "Benny likes tea", "category": "fact", "importance": 3, "flag": "may conflict with #2"}
    monkeypatch.setattr(api._memory_manager, "recall", lambda q, n: [doc])
    out = api._run_memory_tool("recall_memory", {"query": "tea"})
    assert "may conflict with #2" in out
