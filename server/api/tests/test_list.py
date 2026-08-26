"""Paginated recording list contract tests."""

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("TRANSCRIPTER_TOKEN", "sekrit")
    import importlib

    from app import main

    main = importlib.reload(main)
    c = TestClient(main.app)
    c.headers.update({"authorization": "Bearer sekrit"})
    return c


def _make(client: TestClient, title: str) -> str:
    r = client.post("/recordings", json={"title": title})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_list_returns_envelope(client: TestClient) -> None:
    ids = {_make(client, f"rec-{n}") for n in range(3)}
    r = client.get("/recordings")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 3
    assert body["limit"] == 50
    assert body["offset"] == 0
    assert {item["id"] for item in body["items"]} == ids
    # Items keep the full per-recording shape the client renders.
    assert {"id", "title", "state", "stages", "created_at"} <= set(body["items"][0])


def test_limit_and_offset_page_through(client: TestClient) -> None:
    for n in range(5):
        _make(client, f"rec-{n}")
    first = client.get("/recordings", params={"limit": 2, "offset": 0}).json()
    assert len(first["items"]) == 2
    assert first["total"] == 5
    last = client.get("/recordings", params={"limit": 2, "offset": 4}).json()
    assert len(last["items"]) == 1
    assert last["total"] == 5
    # Pages are disjoint slices of one ordered list.
    middle = client.get("/recordings", params={"limit": 2, "offset": 2}).json()
    seen = [i["id"] for i in first["items"] + middle["items"] + last["items"]]
    assert len(set(seen)) == 5


def test_query_filters_by_title_case_insensitive(client: TestClient) -> None:
    _make(client, "Morning standup")
    _make(client, "Evening sync")
    r = client.get("/recordings", params={"q": "morning"})
    body = r.json()
    assert body["total"] == 1
    assert body["items"][0]["title"] == "Morning standup"


def test_query_matches_id(client: TestClient) -> None:
    rid = _make(client, "unrelated title")
    _make(client, "another")
    body = client.get("/recordings", params={"q": rid[:12]}).json()
    assert body["total"] == 1
    assert body["items"][0]["id"] == rid


def test_query_wildcards_are_literal(client: TestClient) -> None:
    _make(client, "100% done")
    _make(client, "1000 done")
    body = client.get("/recordings", params={"q": "100%"}).json()
    assert body["total"] == 1
    assert body["items"][0]["title"] == "100% done"


def test_state_filter(client: TestClient) -> None:
    _make(client, "fresh upload")  # state=uploading after create
    done = client.get("/recordings", params={"state": "done"}).json()
    assert done["total"] == 0
    assert done["items"] == []
    uploading = client.get("/recordings", params={"state": "uploading"}).json()
    assert uploading["total"] == 1


def test_invalid_state_rejected(client: TestClient) -> None:
    assert client.get("/recordings", params={"state": "bogus"}).status_code == 422


def test_invalid_limit_rejected(client: TestClient) -> None:
    assert client.get("/recordings", params={"limit": 0}).status_code == 422
    assert client.get("/recordings", params={"offset": -1}).status_code == 422
