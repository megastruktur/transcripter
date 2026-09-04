"""Tag registry contract tests (tag_defs: create-before-recording +
per-tag hot-word vocabulary).

Covers: POST/GET/PATCH/DELETE /tags registry CRUD, auto-registration on
recording create/direct/PATCH, and the registry union in GET /tags."""

import importlib
import io
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("TRANSCRIPTER_TOKEN", "sekrit")

    from app import temporal_client

    temporal_client.start_pipeline = AsyncMock(return_value="wf-test")  # type: ignore[method-assign]
    temporal_client.regenerate_stage = AsyncMock(return_value="wf-test")  # type: ignore[method-assign]
    temporal_client.start_export = AsyncMock(return_value="wf-export")  # type: ignore[method-assign]

    from app import main

    main = importlib.reload(main)
    c = TestClient(main.app)
    c.headers.update({"authorization": "Bearer sekrit"})
    return c


def _direct(client: TestClient, tags: list[str], title: str = "direct") -> str:
    streaminfo = bytes([0x80, 0x00, 0x00, 0x22]) + bytes(34)
    flac = b"fLaC" + streaminfo + b"\xff\xf8\x00\x00\x00\x00frame"
    r = client.post(
        "/recordings/direct",
        files={"file": ("a.flac", io.BytesIO(flac), "audio/flac")},
        data={"title": title, "tags": __import__("json").dumps(tags)},
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]



# ---------- POST /tags (registry create) ----------


def test_create_tag_without_recording(client: TestClient) -> None:
    r = client.post("/tags", json={"name": "pathfinder", "vocabulary": ["Абсалом", "Bytchez"]})
    assert r.status_code == 201
    body = r.json()
    assert body["name"] == "pathfinder"
    assert body["vocabulary"] == ["Абсалом", "Bytchez"]
    assert body["recordings"] == 0


def test_create_tag_normalizes_name(client: TestClient) -> None:
    r = client.post("/tags", json={"name": "  Dark Castle  "})
    assert r.status_code == 201
    assert r.json()["name"] == "dark castle"


def test_create_tag_conflict_409(client: TestClient) -> None:
    assert client.post("/tags", json={"name": "pf"}).status_code == 201
    r = client.post("/tags", json={"name": "PF"})
    assert r.status_code == 409




def test_create_tag_invalid_400(client: TestClient) -> None:
    # leading space survives trim? No: name is trimmed by _normalize_tag,
    # so a space-FIRST char must come after a word char — "  x" trims to
    # "x" (valid). Invalid shapes: empty, >64 chars, no word chars.
    assert client.post("/tags", json={"name": "x" * 65}).status_code == 400
    assert client.post("/tags", json={"name": ""}).status_code == 400
    assert client.post("/tags", json={"name": "///"}).status_code == 400


def test_vocabulary_normalized_dedup_case_preserved(client: TestClient) -> None:
    r = client.post(
        "/tags",
        json={"name": "t1", "vocabulary": [" Foo ", "foo", "  ", "Bar", "BAR"]},
    )
    assert r.status_code == 201
    # casefold-dedup, first spelling wins; blanks dropped
    assert r.json()["vocabulary"] == ["Foo", "Bar"]

# ---------- GET /tags/{tag} ----------


def test_get_tag_404_when_unregistered(client: TestClient) -> None:
    assert client.get("/tags/ghost").status_code == 404


def test_get_tag_returns_row_and_count(client: TestClient) -> None:
    client.post("/tags", json={"name": "pf", "vocabulary": ["Абсалом"]})
    client.post("/recordings", json={"title": "t", "tags": ["pf"]})
    body = client.get("/tags/pf").json()
    assert body["vocabulary"] == ["Абсалом"]
    assert body["recordings"] == 1


# ---------- PATCH /tags/{tag} (upsert vocabulary) ----------


def test_patch_vocabulary_replace(client: TestClient) -> None:
    client.post("/tags", json={"name": "t1", "vocabulary": ["a"]})
    r = client.patch("/tags/t1", json={"vocabulary": ["b", "c"]})
    assert r.status_code == 200
    assert r.json()["vocabulary"] == ["b", "c"]


def test_patch_upserts_legacy_tag(client: TestClient) -> None:
    """Tag existing only on recordings (pre-registry) gains a row on
    first vocabulary edit."""
    client.post("/recordings", json={"title": "t", "tags": ["legacy"]})
    r = client.patch("/tags/legacy", json={"vocabulary": ["word"]})
    assert r.status_code == 200
    assert r.json()["recordings"] == 1
    assert client.get("/tags/legacy").status_code == 200


# ---------- DELETE /tags/{tag} ----------


def test_delete_tag_no_recordings_204(client: TestClient) -> None:
    client.post("/tags", json={"name": "t1"})
    assert client.delete("/tags/t1").status_code == 204
    assert client.get("/tags/t1").status_code == 404


def test_delete_tag_with_recordings_409(client: TestClient) -> None:
    client.post("/tags", json={"name": "t1"})
    client.post("/recordings", json={"title": "t", "tags": ["t1"]})
    assert client.delete("/tags/t1").status_code == 409


def test_delete_tag_unregistered_404(client: TestClient) -> None:
    assert client.delete("/tags/ghost").status_code == 404


# ---------- auto-registration ----------


def test_recording_create_autoregisters_tags(client: TestClient) -> None:
    client.post("/recordings", json={"title": "t", "tags": ["alpha", "beta"]})
    assert client.get("/tags/alpha").json()["name"] == "alpha"
    assert client.get("/tags/beta").json()["recordings"] == 1


def test_direct_upload_autoregisters_tags(client: TestClient) -> None:
    _direct(client, ["directtag"])
    assert client.get("/tags/directtag").json()["recordings"] == 1


def test_patch_recording_tags_autoregisters(client: TestClient) -> None:
    rid = client.post("/recordings", json={"title": "t", "tags": []}).json()["id"]
    r = client.patch(f"/recordings/{rid}", json={"tags": ["patched"]})
    assert r.status_code == 200
    assert client.get("/tags/patched").json()["name"] == "patched"


def test_autoregistration_keeps_manual_vocabulary(client: TestClient) -> None:
    """A tag registered manually (with vocabulary) then attached to a
    recording keeps its vocabulary (ON CONFLICT DO NOTHING)."""
    client.post("/tags", json={"name": "pf", "vocabulary": ["Абсалом"]})
    client.post("/recordings", json={"title": "t", "tags": ["pf"]})
    assert client.get("/tags/pf").json()["vocabulary"] == ["Абсалом"]


# ---------- GET /tags union ----------


def test_list_tags_includes_registered_zero_count(client: TestClient) -> None:
    client.post("/tags", json={"name": "future", "vocabulary": ["w1", "w2"]})
    items = client.get("/tags").json()["items"]
    row = next(i for i in items if i["tag"] == "future")
    assert row["count"] == 0
    assert row["registered"] is True
    assert row["vocabulary_count"] == 2


    client.post("/recordings", json={"title": "a", "tags": ["dnd"]})
    # dnd auto-registered on the recording create; vocabulary comes via
    # PATCH (POST would 409 on the auto-registered row)
    client.patch("/tags/dnd", json={"vocabulary": ["x"]})
    client.post("/tags", json={"name": "solo"})
    items = client.get("/tags").json()["items"]
    dnd = next(i for i in items if i["tag"] == "dnd")
    assert dnd["count"] == 1 and dnd["registered"] is True and dnd["vocabulary_count"] == 1
    solo = next(i for i in items if i["tag"] == "solo")
    assert solo["count"] == 0 and solo["registered"] is True
    # Every recording-derived tag is auto-registered → registered=True everywhere
    assert all(i["registered"] for i in items)
