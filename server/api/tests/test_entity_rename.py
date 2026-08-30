"""Phase 4 API surface: PATCH /tags/{tag}/entities/{slug}.

Contract: label required (non-empty after trim, ≤200), optional type;
404 for unknown tag (no done recordings) or slug not in the timeline's
aggregated entities; 202 + workflow id on success; 503 when Temporal is
unreachable; 409 when the graph backend is off (same shape as digest).
Temporal mocked at the temporal_client level (rename test pattern).
"""

from __future__ import annotations

import importlib
import json
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from app import temporal_client


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("TRANSCRIPTER_TOKEN", "sekrit")
    monkeypatch.setattr(
        temporal_client,
        "start_rename_entity",
        AsyncMock(return_value="wf-rename-abc"),
    )
    from app import main

    main = importlib.reload(main)
    c = TestClient(main.app)
    c.headers.update({"authorization": "Bearer sekrit"})
    return c


def _enable_graph(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """Flip graph.uri in place (test_digest pattern): ``enabled`` is a
    derived property, so the assignment is enough."""
    cfg = client.app.state.config
    monkeypatch.setattr(cfg.graph, "uri", "bolt://n:7687")
    assert cfg.graph.enabled is True


def _force_state(rid: str, state: str) -> None:
    from app.db import Recording, RecordingState, get_session

    gen = get_session()
    session = next(gen)
    try:
        rec = session.get(Recording, rid)
        assert rec is not None
        rec.state = RecordingState[state]
        session.commit()
    finally:
        gen.close()


def _write_events(rid: str, doc: dict) -> None:
    from app.main import app

    root = app.state.config.recordings_root / rid / "meta"
    root.mkdir(parents=True, exist_ok=True)
    (root / "events.json").write_text(json.dumps(doc), encoding="utf-8")


def _events_doc(entities: list[dict]) -> dict:
    return {
        "recording_id": "x",
        "recording_date": "2026-08-01T00:00:00",
        "recording_title": "t",
        "profile_id": "default",
        "namespaces": ["tag"],
        "events": [],
        "entities": entities,
        "relations": [],
    }


def _seed_tag_with_entity(client: TestClient, *, tag: str = "daily blob", slug: str = "vova") -> str:
    rid = client.post("/recordings", json={"tags": [tag]}).json()["id"]
    _force_state(rid, "done")
    _write_events(rid, _events_doc([{"slug": slug, "label": "Валя", "type": "person"}]))
    return rid


# ---------- 202 happy path ----------


def test_patch_entity_returns_202(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_graph(client, monkeypatch)
    _seed_tag_with_entity(client)
    r = client.patch("/tags/daily blob/entities/vova", json={"label": "Валли"})
    assert r.status_code == 202
    body = r.json()
    assert body["workflow_id"] == "wf-rename-abc"
    assert body["tag"] == "daily blob"
    assert body["slug"] == "vova"
    assert body["label"] == "Валли"


def test_patch_entity_starts_workflow_with_trimmed_label(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_graph(client, monkeypatch)
    _seed_tag_with_entity(client)
    r = client.patch("/tags/daily blob/entities/vova", json={"label": "  Валли  "})
    assert r.status_code == 202
    assert r.json()["label"] == "Валли"
    temporal_client.start_rename_entity.assert_awaited_once_with(
        "daily blob", "vova", "Валли", None
    )


def test_patch_entity_with_type_passes_type(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_graph(client, monkeypatch)
    _seed_tag_with_entity(client)
    r = client.patch(
        "/tags/daily blob/entities/vova", json={"label": "Валли", "type": "person"}
    )
    assert r.status_code == 202
    temporal_client.start_rename_entity.assert_awaited_once_with(
        "daily blob", "vova", "Валли", "person"
    )


# ---------- 404 existence ----------


def test_patch_entity_unknown_tag_404(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_graph(client, monkeypatch)
    r = client.patch("/tags/ghost/entities/vova", json={"label": "X"})
    assert r.status_code == 404
    assert "no recordings" in r.json()["detail"]


def test_patch_entity_unknown_slug_404(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_graph(client, monkeypatch)
    _seed_tag_with_entity(client)
    r = client.patch("/tags/daily blob/entities/ghost", json={"label": "X"})
    assert r.status_code == 404
    assert "not found" in r.json()["detail"]


def test_patch_entity_tag_with_sessions_but_no_entities_404(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A tag whose recordings never extracted entities has no renamable
    rows — the slug check still 404s (not a 500 on empty aggregation)."""
    _enable_graph(client, monkeypatch)
    rid = client.post("/recordings", json={"tags": ["empty"]}).json()["id"]
    _force_state(rid, "done")
    r = client.patch("/tags/empty/entities/anyone", json={"label": "X"})
    assert r.status_code == 404


# ---------- 400 / 422 validation ----------


def test_patch_entity_blank_label_400(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_graph(client, monkeypatch)
    _seed_tag_with_entity(client)
    # Pydantic min_length rejects "" pre-trim; whitespace-only passes
    # the model and is caught by the strip check in the handler.
    r = client.patch("/tags/daily blob/entities/vova", json={"label": "   "})
    assert r.status_code == 400
    assert "empty" in r.json()["detail"]


def test_patch_entity_long_label_422(client: TestClient) -> None:
    r = client.patch(
        "/tags/daily blob/entities/vova", json={"label": "x" * 201}
    )
    assert r.status_code == 422


def test_patch_entity_missing_label_422(client: TestClient) -> None:
    r = client.patch("/tags/daily blob/entities/vova", json={})
    assert r.status_code == 422



# ---------- 409 / 503 ----------
def test_patch_entity_graph_disabled_409(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Default conftest config has graph.uri='' — the same 409 shape as
    POST digest: a concrete operator-facing error, not a Temporal 500."""
    _seed_tag_with_entity(client)
    r = client.patch("/tags/daily blob/entities/vova", json={"label": "X"})
    assert r.status_code == 409

def test_patch_entity_bad_tag_400(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_graph(client, monkeypatch)
    # %24 = '$' — disallowed by _TAG_RE (test_digest pattern).
    r = client.patch("/tags/bad%24chars/entities/vova", json={"label": "X"})
    assert r.status_code == 400

def test_patch_entity_temporal_down_503(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_graph(client, monkeypatch)
    _seed_tag_with_entity(client)
    monkeypatch.setattr(
        temporal_client,
        "start_rename_entity",
        AsyncMock(side_effect=ConnectionError("temporal down")),
    )
    r = client.patch("/tags/daily blob/entities/vova", json={"label": "X"})
    assert r.status_code == 503
