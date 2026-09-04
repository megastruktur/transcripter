"""Tag-memory purge/rebuild contract tests.

DELETE /tags/{tag}/memory   → 202 (purge-only workflow)
POST  /tags/{tag}/rebuild   → 202 (purge + sequential enrich rebuild)
GET   /tags/{tag}/memory/{workflow_id} → poll

Guards under test: 409 graph-off (before Temporal), 404 no-done-recordings,
409 while a recording of the tag is processing, 409 on an already-running
workflow, 400 on a non-memory workflow id.
"""

import importlib
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("TRANSCRIPTER_TOKEN", "sekrit")

    from app import temporal_client

    monkeypatch.setattr(temporal_client, "start_pipeline", AsyncMock(return_value="wf-test"))
    monkeypatch.setattr(temporal_client, "regenerate_stage", AsyncMock(return_value="wf-test"))
    monkeypatch.setattr(temporal_client, "start_export", AsyncMock(return_value="wf-export"))
    monkeypatch.setattr(
        temporal_client,
        "start_rebuild_tag_memory",
        AsyncMock(return_value="rebuild-tag-memory-quest"),
    )

    from app import main

    main = importlib.reload(main)
    c = TestClient(main.app)
    c.headers.update({"authorization": "Bearer sekrit"})
    return c


def _enable_graph(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = client.app.state.config
    monkeypatch.setattr(cfg.graph, "uri", "bolt://n:7687")


def _seed_done(client: TestClient, tag: str = "quest", state: str = "done") -> str:
    from app.db import get_session

    rid = client.post("/recordings", json={"title": "s", "tags": [tag]}).json()["id"]
    gen = get_session()
    try:
        s = next(gen)
        s.execute(
            __import__("sqlalchemy").text(
                "UPDATE recordings SET state = :st WHERE id = :id"
            ),
            {"st": state, "id": rid},
        )
        s.commit()
    finally:
        gen.close()
    return rid


# ---------- guards ----------


def test_purge_graph_disabled_409(client: TestClient) -> None:
    _seed_done(client)
    r = client.delete("/tags/quest/memory")
    assert r.status_code == 409
    assert "graph backend not configured" in r.json()["detail"]


def test_purge_unknown_tag_404(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_graph(client, monkeypatch)
    r = client.delete("/tags/ghost/memory")
    assert r.status_code == 404
    assert "no done recordings" in r.json()["detail"]


def test_purge_tag_with_only_uploading_404(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """_tag_recordings is done-only: an uploading recording is not a
    purge target either — the tag has no memory to wipe."""
    _enable_graph(client, monkeypatch)
    _seed_done(client, state="uploading")
    r = client.delete("/tags/quest/memory")
    assert r.status_code == 404


def test_purge_processing_recording_409(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_graph(client, monkeypatch)
    _seed_done(client, state="processing")
    r = client.delete("/tags/quest/memory")
    assert r.status_code == 409
    assert "still processing" in r.json()["detail"]


def test_rebuild_processing_recording_409(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_graph(client, monkeypatch)
    _seed_done(client, state="processing")
    r = client.post("/tags/quest/rebuild")
    assert r.status_code == 409


def test_bad_tag_400(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_graph(client, monkeypatch)
    r = client.delete("/tags/%20%20/memory")
    assert r.status_code == 400


# ---------- 202 happy paths ----------


def test_purge_202_starts_workflow(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    from app import temporal_client

    _enable_graph(client, monkeypatch)
    _seed_done(client)
    temporal_client.start_rebuild_tag_memory.reset_mock()
    r = client.delete("/tags/quest/memory")
    assert r.status_code == 202
    body = r.json()
    assert body["workflow_id"] == "rebuild-tag-memory-quest"
    assert body["done_recordings"] == 1
    temporal_client.start_rebuild_tag_memory.assert_awaited_once_with("quest", False)


def test_rebuild_202_starts_workflow(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    from app import temporal_client

    _enable_graph(client, monkeypatch)
    _seed_done(client)
    temporal_client.start_rebuild_tag_memory.reset_mock()
    r = client.post("/tags/quest/rebuild")
    assert r.status_code == 202
    temporal_client.start_rebuild_tag_memory.assert_awaited_once_with("quest", True)


def test_already_running_409(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_graph(client, monkeypatch)
    _seed_done(client)

    from app import temporal_client

    temporal_client.start_rebuild_tag_memory = AsyncMock(
        side_effect=RuntimeError("workflow already started")
    )
    r = client.delete("/tags/quest/memory")
    assert r.status_code == 409
    assert "already running" in r.json()["detail"]


def test_temporal_down_503(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_graph(client, monkeypatch)
    _seed_done(client)

    from app import temporal_client

    temporal_client.start_rebuild_tag_memory = AsyncMock(
        side_effect=ConnectionError("refused")
    )
    r = client.post("/tags/quest/rebuild")
    assert r.status_code == 503


# ---------- status poll ----------


def test_status_rejects_foreign_workflow_id(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_graph(client, monkeypatch)
    r = client.get("/tags/quest/memory/graph-fix-apply-123")
    assert r.status_code == 400
