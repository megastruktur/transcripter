"""Phase A API surface: graph editing endpoints.

Contract under test (all in the Phase 4 rename style):
- 409 graph off; 404 unknown tag / event_key / slug; 400 bad payloads.
- 202 + {workflow_id, edit_id} on success; the graph_edits row lands
  with the right target/op/anchor BEFORE the workflow start.
- GET /edits lists rows newest-first; POST retire flips status.
- GET /graph aggregates entities+relations from events.json only.
- GET /digest/status: queued while the newest edit predates the digest
  note (or no note), fresh otherwise.
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
    monkeypatch.setattr(temporal_client, "start_pipeline", AsyncMock(return_value="wf-test"))
    monkeypatch.setattr(temporal_client, "regenerate_stage", AsyncMock(return_value="wf-regen"))
    monkeypatch.setattr(temporal_client, "start_export", AsyncMock(return_value="wf-export"))
    monkeypatch.setattr(
        temporal_client,
        "start_apply_graph_edit",
        AsyncMock(return_value="wf-edit-abc"),
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


def _write_events(rid: str, doc: dict) -> None:
    """Write meta/events.json the same shape worker enrich emits."""
    from app.main import app

    root = app.state.config.recordings_root / rid / "meta"
    root.mkdir(parents=True, exist_ok=True)
    (root / "events.json").write_text(json.dumps(doc), encoding="utf-8")


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


def _seed_tag(client: TestClient) -> str:
    rid = client.post("/recordings", json={"tags": ["quest"]}).json()["id"]
    _force_state(rid, "done")
    return rid


# ---------- event PATCH / DELETE ----------


def test_patch_event_202_and_row_lands(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_graph(client, monkeypatch)
    rid = _seed_tag(client)
    _write_events(
        rid,
        {
            "entities": [],
            "events": [
                {"ts": "t1", "kind": "note", "summary": "Glennis built the network", "mentions": []}
            ],
        },
    )
    body = client.get("/tags/quest/timeline").json()
    assert body["sessions"], f"no sessions: {body}"
    assert body["sessions"][0]["events"], "no events served"
    key = body["sessions"][0]["events"][0]["event_key"]

    r = client.patch(
        f"/tags/quest/events/{key}",
        json={"summary": "The operator built the network", "feedback_text": "network was built by the operator"},
    )
    assert r.status_code == 202
    payload = r.json()
    assert payload["workflow_id"] == "wf-edit-abc"
    assert isinstance(payload["edit_id"], int)

    edits = client.get("/tags/quest/edits").json()["items"]
    assert edits and edits[0]["target"] == "event"
    assert edits[0]["op"] == "update"
    assert edits[0]["obj_key"] == key
    assert edits[0]["anchor"]["origin_recording_id"] == rid
    assert edits[0]["feedback_text"] == "network was built by the operator"


def test_patch_event_404_unknown_key(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_graph(client, monkeypatch)
    _seed_tag(client)
    r = client.patch(
        "/tags/quest/events/deadbeef00000000", json={"summary": "x"}
    )
    assert r.status_code == 404


def test_patch_event_400_nothing_to_update(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_graph(client, monkeypatch)
    rid = _seed_tag(client)
    _write_events(
        rid, {"entities": [], "events": [{"ts": "t", "kind": "k", "summary": "s", "mentions": []}]}
    )
    body = client.get("/tags/quest/timeline").json()
    key = body["sessions"][0]["events"][0]["event_key"]
    r = client.patch(f"/tags/quest/events/{key}", json={})
    assert r.status_code == 400


def test_delete_event_202(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_graph(client, monkeypatch)
    rid = _seed_tag(client)
    _write_events(
        rid, {"entities": [], "events": [{"ts": "t", "kind": "k", "summary": "s", "mentions": []}]}
    )
    key = client.get("/tags/quest/timeline").json()["sessions"][0]["events"][0]["event_key"]
    r = client.delete(f"/tags/quest/events/{key}")
    assert r.status_code == 202
    edits = client.get("/tags/quest/edits").json()["items"]
    assert edits[0]["op"] == "delete" and edits[0]["target"] == "event"


def test_edit_endpoints_409_graph_off(client: TestClient) -> None:
    _seed_tag(client)
    r = client.patch("/tags/quest/events/x", json={"summary": "y"})
    assert r.status_code == 409
    r = client.delete("/tags/quest/events/x")
    assert r.status_code == 409
    r = client.post("/tags/quest/relations", json={"from_slug": "a", "to_slug": "b", "type": "owns"})
    assert r.status_code == 409


# ---------- relations ----------


def test_create_relation_202_and_unknown_slug_404(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_graph(client, monkeypatch)
    rid = _seed_tag(client)
    _write_events(
        rid,
        {
            "entities": [{"slug": "grim", "label": "Grim", "type": "character"}],
            "events": [],
            "relations": [],
        },
    )
    r = client.post(
        "/tags/quest/relations",
        json={"from_slug": "grim", "to_slug": "nope", "type": "owns"},
    )
    assert r.status_code == 404
    r = client.post(
        "/tags/quest/relations",
        json={"from_slug": "grim", "to_slug": "grim", "type": "owns"},
    )
    assert r.status_code == 202
    edits = client.get("/tags/quest/edits").json()["items"]
    assert edits[0]["target"] == "relation" and edits[0]["op"] == "create"
    assert edits[0]["after"] == {"from": "grim", "to": "grim", "type": "owns"}


def test_delete_relation_202(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_graph(client, monkeypatch)
    _seed_tag(client)
    r = client.request(
        "DELETE",
        "/tags/quest/relations",
        json={"from_slug": "a", "to_slug": "b", "type": "owns"},
    )
    assert r.status_code == 202
    edits = client.get("/tags/quest/edits").json()["items"]
    assert edits[0]["op"] == "delete" and edits[0]["target"] == "relation"


# ---------- entity delete / merge ----------


def test_delete_entity_202_and_404(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_graph(client, monkeypatch)
    rid = _seed_tag(client)
    _write_events(
        rid, {"entities": [{"slug": "x", "label": "X", "type": "t"}], "events": []}
    )
    assert client.delete("/tags/quest/entities/nope").status_code == 404
    assert client.delete("/tags/quest/entities/x").status_code == 202
    edits = client.get("/tags/quest/edits").json()["items"]
    assert edits[0]["target"] == "entity" and edits[0]["op"] == "delete"


def test_merge_entities_202_and_selfmerge_400(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_graph(client, monkeypatch)
    rid = _seed_tag(client)
    _write_events(
        rid,
        {
            "entities": [
                {"slug": "a", "label": "A", "type": "t"},
                {"slug": "b", "label": "B", "type": "t"},
            ],
            "events": [],
        },
    )
    r = client.post(
        "/tags/quest/entities/merge",
        json={"source_slug": "a", "target_slug": "a"},
    )
    assert r.status_code == 400
    r = client.post(
        "/tags/quest/entities/merge",
        json={"source_slug": "a", "target_slug": "b"},
    )
    assert r.status_code == 202
    edits = client.get("/tags/quest/edits").json()["items"]
    assert edits[0]["op"] == "merge"
    assert edits[0]["before"] == {"source": "a", "target": "b"}


# ---------- audit + retire ----------


def test_retire_edit_flips_status(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_graph(client, monkeypatch)
    rid = _seed_tag(client)
    _write_events(
        rid, {"entities": [{"slug": "x", "label": "X", "type": "t"}], "events": []}
    )
    client.delete("/tags/quest/entities/x")
    edit_id = client.get("/tags/quest/edits").json()["items"][0]["id"]
    r = client.post(f"/tags/quest/edits/{edit_id}/retire")
    assert r.status_code == 202
    assert r.json()["status"] == "retired"
    assert client.get("/tags/quest/edits").json()["items"][0]["status"] == "retired"
    # Retiring an edit of ANOTHER tag is a 404, not a cross-tag write.
    assert client.post(f"/tags/quest/edits/{edit_id + 999}/retire").status_code == 404


# ---------- GET /graph ----------


def test_graph_aggregates_entities_and_relations(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_graph(client, monkeypatch)
    rid = _seed_tag(client)
    _write_events(
        rid,
        {
            "entities": [
                {"slug": "grim", "label": "Grim", "type": "character"},
                {"slug": "castle", "label": "Castle", "type": "location"},
            ],
            "events": [],
            "relations": [
                {"from": "grim", "to": "castle", "type": "located_in"},
                {"from": "grim", "to": "castle", "type": "located_in"},
            ],
        },
    )
    g = client.get("/tags/quest/graph").json()
    slugs = {e["slug"] for e in g["entities"]}
    assert slugs == {"grim", "castle"}
    assert len(g["relations"]) == 1  # deduped by (from, to, type)
    assert g["relations"][0]["type"] == "located_in"


# ---------- GET /digest/status ----------


def test_digest_status_queued_then_fresh(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_graph(client, monkeypatch)
    rid = _seed_tag(client)
    _write_events(
        rid, {"entities": [{"slug": "x", "label": "X", "type": "t"}], "events": []}
    )
    # No edits → fresh.
    assert client.get("/tags/quest/digest/status").json()["state"] == "fresh"
    client.delete("/tags/quest/entities/x")
    # Edit newer than (missing) digest → queued.
    st = client.get("/tags/quest/digest/status").json()
    assert st["state"] == "queued" and st["last_edit_at"] is not None
    # Write a digest note NEWER than the edit → fresh again. The vault
    # path is the container-fixed /transcripts in the example config —
    # rebind it to the test tmp dir first.
    import os
    from datetime import UTC, datetime, timedelta

    from app.main import app

    monkeypatch.setattr(
        app.state.config.vault, "path", app.state.config.storage.path / "vault"
    )
    digests = app.state.config.vault.path / "digests"
    digests.mkdir(parents=True, exist_ok=True)
    note = digests / "quest.md"
    note.write_text("---\ntag: quest\n---\nbody", encoding="utf-8")
    future = (datetime.now(UTC) + timedelta(minutes=1)).timestamp()
    os.utime(note, (future, future))
    assert client.get("/tags/quest/digest/status").json()["state"] == "fresh"
