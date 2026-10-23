"""Settings > Safety 'Seal changes' button: GET/POST /api/safety/seal (needs FastAPI + the full app)."""
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import pytest  # noqa: E402
from _safety_helpers import Env  # noqa: E402

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402
from lucy.server import api  # noqa: E402


@pytest.fixture
def world(monkeypatch):
    env = Env(monkeypatch=monkeypatch, enforce=True)       # sealed on first start, constitution enforced
    client = TestClient(api.app)
    yield env, client, {"X-API-Key": api.DEV_API_KEY}
    env.close()


def _tamper(env):
    (env.root / ".core" / "permissions.yaml").write_text("mode: auto\n", encoding="utf-8")


def test_nothing_pending_when_sealed(world):
    env, client, h = world
    r = client.get("/api/safety/seal", headers=h)
    assert r.status_code == 200 and r.json()["pending"] == []


def test_changed_file_is_listed_then_sealed_with_confirm(world):
    env, client, h = world
    _tamper(env)
    pend = client.get("/api/safety/seal", headers=h).json()
    assert any("permissions.yaml" in p for p in pend["pending"]) and pend["needs_restart"] is False
    assert client.get("/api/safety/status", headers=h).json()["halted"]          # agent is halted until sealed
    r = client.post("/api/safety/seal", headers=h, json={"confirm": "SEAL"})
    assert r.status_code == 200 and r.json()["sealed"] is True
    assert client.get("/api/safety/seal", headers=h).json()["pending"] == []
    assert not client.get("/api/safety/status", headers=h).json()["halted"]


def test_wrong_or_missing_confirm_does_not_seal(world):
    env, client, h = world
    _tamper(env)
    for body in ({}, {"confirm": "yes"}, {"confirm": "seal"}):
        assert client.post("/api/safety/seal", headers=h, json=body).status_code == 400
    assert client.get("/api/safety/seal", headers=h).json()["pending"]


def test_needs_api_key(world):
    env, client, h = world
    assert client.get("/api/safety/seal").status_code == 401
    assert client.post("/api/safety/seal", json={"confirm": "SEAL"}).status_code == 401


def test_sealing_nothing_is_a_noop_and_audit_records_a_seal(world):
    env, client, h = world
    assert client.post("/api/safety/seal", headers=h, json={"confirm": "SEAL"}).json()["sealed"] is False
    _tamper(env)
    client.post("/api/safety/seal", headers=h, json={"confirm": "SEAL"})
    log = (env.root / "runtime" / "logs" / "audit.jsonl")
    assert log.exists() and '"event": "seal"' in log.read_text(encoding="utf-8")
