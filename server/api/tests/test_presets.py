"""Capture-preset sync store contract tests: flat feed (tombstones
included), PUT upsert/validation/resurrect, DELETE idempotence, auth."""

import importlib
from datetime import datetime
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("TRANSCRIPTER_TOKEN", "sekrit")

    from app import temporal_client

    temporal_client.start_pipeline = AsyncMock(return_value="wf-test")  # type: ignore[method-assign]
    temporal_client.regenerate_stage = AsyncMock(return_value="wf-test")  # type: ignore[method-assign]

    from app import main

    main = importlib.reload(main)
    c = TestClient(main.app)
    c.headers.update({"authorization": "Bearer sekrit"})
    return c


# ---------- GET /presets ----------


def test_empty_list(client: TestClient) -> None:
    assert client.get("/presets").json() == []


def test_list_sorted_by_id(client: TestClient) -> None:
    client.put("/presets/preset-b", json={"name": "b"})
    client.put("/presets/preset-a", json={"name": "a"})
    ids = [p["id"] for p in client.get("/presets").json()]
    assert ids == ["preset-a", "preset-b"]


# ---------- PUT /presets/{id} ----------


def test_put_roundtrip_normalizes_tags(client: TestClient) -> None:
    r = client.put(
        "/presets/preset-vet-x7k2",
        json={
            "name": "  Vet visit  ",
            "rec_type": "vet",
            "tags": [" Vet ", "vet", "CLINIC"],
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["name"] == "Vet visit"  # trimmed, case preserved
    assert body["rec_type"] == "vet"
    assert body["tags"] == ["vet", "clinic"]
    assert body["deleted"] is False
    datetime.fromisoformat(body["updated_at"])  # parseable ISO-8601

    listed = client.get("/presets").json()
    assert len(listed) == 1 and listed[0]["id"] == "preset-vet-x7k2"


def test_put_defaults_empty_rec_type_and_tags(client: TestClient) -> None:
    r = client.put("/presets/preset-plain", json={"name": "Plain"})
    assert r.status_code == 200
    assert r.json()["rec_type"] == ""
    assert r.json()["tags"] == []


def test_put_update_overwrites_and_bumps_updated_at(client: TestClient) -> None:
    first = client.put("/presets/p1", json={"name": "one", "tags": ["a"]}).json()
    second = client.put("/presets/p1", json={"name": "two", "tags": ["b", "a"]}).json()
    assert second["name"] == "two"
    assert second["tags"] == ["b", "a"]
    assert datetime.fromisoformat(second["updated_at"]) >= datetime.fromisoformat(
        first["updated_at"]
    )
    assert len(client.get("/presets").json()) == 1  # upsert, not append


# ---------- PUT validation (400/422 before any mutation) ----------


def test_put_bad_id_400(client: TestClient) -> None:
    for bad in ("Preset-1", "preset_vet", "-preset", "p" * 65):
        assert client.put(f"/presets/{bad}", json={"name": "x"}).status_code == 400
    assert client.get("/presets").json() == []  # nothing mutated


def test_put_bad_name_400(client: TestClient) -> None:
    assert client.put("/presets/p1", json={"name": ""}).status_code == 400
    assert client.put("/presets/p1", json={"name": "   "}).status_code == 400
    assert client.put("/presets/p1", json={"name": "x" * 101}).status_code == 400
    # boundary: exactly 100 chars after trim is fine
    assert client.put("/presets/p1", json={"name": "x" * 100}).status_code == 200


def test_put_bad_rec_type_400(client: TestClient) -> None:
    for bad in ("vet_consultation", "VET"):  # underscore + uppercase
        r = client.put("/presets/p1", json={"name": "x", "rec_type": bad})
        assert r.status_code == 400


def test_put_non_list_tags_422(client: TestClient) -> None:
    assert client.put("/presets/p1", json={"name": "x", "tags": "vet"}).status_code == 422


def test_put_extra_body_key_422(client: TestClient) -> None:
    assert client.put("/presets/p1", json={"name": "x", "bogus": 1}).status_code == 422


# ---------- DELETE /presets/{id} (tombstone) ----------


def test_delete_tombstone_flow(client: TestClient) -> None:
    client.put("/presets/p1", json={"name": "x"})
    r = client.delete("/presets/p1")
    assert r.status_code == 200
    assert r.json()["deleted"] is True

    listed = client.get("/presets").json()
    assert len(listed) == 1 and listed[0]["deleted"] is True  # row kept

    assert client.delete("/presets/p1").status_code == 200  # idempotent re-delete
    assert client.delete("/presets/ghost").status_code == 404

    # PUT resurrects the tombstone
    r = client.put("/presets/p1", json={"name": "back"})
    assert r.status_code == 200
    assert r.json()["deleted"] is False
    assert client.get("/presets").json()[0]["deleted"] is False


# ---------- auth (global bearer middleware) ----------


def test_presets_require_token(client: TestClient) -> None:
    unauth = TestClient(client.app)
    assert unauth.get("/presets").status_code == 401
