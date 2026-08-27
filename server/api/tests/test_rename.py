"""Rename endpoint contract tests (Temporal mocked)."""

from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("TRANSCRIPTER_TOKEN", "sekrit")

    from app import temporal_client

    monkeypatch.setattr(temporal_client, "start_export", AsyncMock(return_value="wf-export"))

    import importlib

    from app import main

    main = importlib.reload(main)
    c = TestClient(main.app)
    c.headers.update({"authorization": "Bearer sekrit"})
    return c


def _make_recording(client: TestClient) -> str:
    r = client.post("/recordings", json={"title": "orig"})
    return r.json()["id"]


def test_rename_persists_trimmed_title(client: TestClient) -> None:
    rid = _make_recording(client)
    r = client.patch(f"/recordings/{rid}", json={"title": "  morning standup  "})
    assert r.status_code == 200
    assert r.json()["title"] == "morning standup"
    assert client.get(f"/recordings/{rid}").json()["title"] == "morning standup"


def test_rename_blank_title_persists_empty(client: TestClient) -> None:
    rid = _make_recording(client)
    r = client.patch(f"/recordings/{rid}", json={"title": "   "})
    assert r.status_code == 200
    assert r.json()["title"] == ""
    assert client.get(f"/recordings/{rid}").json()["title"] == ""


def test_rename_unknown_id_404(client: TestClient) -> None:
    rid = "00000000-0000-0000-0000-000000000000"
    r = client.patch(f"/recordings/{rid}", json={"title": "x"})
    assert r.status_code == 404


def test_rename_triggers_export(client: TestClient) -> None:
    from app import temporal_client

    rid = _make_recording(client)
    r = client.patch(f"/recordings/{rid}", json={"title": "x"})
    assert r.status_code == 200
    # Rename path is rename-only: the folder is renamed, files are NOT
    # rewritten (Obsidian edits survive).
    temporal_client.start_export.assert_awaited_once_with(rid, rename_only=True)


def test_rename_succeeds_when_export_trigger_fails(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app import temporal_client

    monkeypatch.setattr(
        temporal_client,
        "start_export",
        AsyncMock(side_effect=ConnectionError("temporal down")),
    )
    rid = _make_recording(client)
    r = client.patch(f"/recordings/{rid}", json={"title": "still saved"})
    assert r.status_code == 200
    assert r.json()["title"] == "still saved"
    assert client.get(f"/recordings/{rid}").json()["title"] == "still saved"
