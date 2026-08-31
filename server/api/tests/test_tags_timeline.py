"""Phase 3 API surface: GET /tags/{tag}/timeline.

Payload contract: sessions (the tag's DONE recordings, newest first by
coalesce(recorded_at, created_at)) each carrying its meta/events.json
events, plus entities aggregated across those files (entities[] OR any
event's mentions), plus digest_generated. 404 when the tag has no done
recordings; 400 for tags outside _TAG_RE.
"""

from __future__ import annotations

import importlib
import json
from datetime import datetime
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("TRANSCRIPTER_TOKEN", "sekrit")
    from app import temporal_client

    monkeypatch.setattr(temporal_client, "start_pipeline", AsyncMock(return_value="wf-test"))
    monkeypatch.setattr(temporal_client, "regenerate_stage", AsyncMock(return_value="wf-regen"))
    monkeypatch.setattr(temporal_client, "start_export", AsyncMock(return_value="wf-export"))

    from app import main

    main = importlib.reload(main)
    c = TestClient(main.app)
    c.headers.update({"authorization": "Bearer sekrit"})
    return c



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


def _backdate(rid: str, iso: str) -> None:
    from app.db import Recording, get_session

    gen = get_session()
    session = next(gen)
    try:
        rec = session.get(Recording, rid)
        assert rec is not None
        rec.recorded_at = datetime.fromisoformat(iso)
        session.commit()
    finally:
        gen.close()


def _write_events(rid: str, doc: dict) -> None:
    """Write meta/events.json the same shape worker enrich emits."""
    from app.main import app

    root = app.state.config.recordings_root / rid / "meta"
    root.mkdir(parents=True, exist_ok=True)
    (root / "events.json").write_text(json.dumps(doc), encoding="utf-8")


def _events_doc(
    *, entities: list[dict] | None = None, events: list[dict] | None = None
) -> dict:
    return {
        "recording_id": "x",
        "recording_date": "2026-08-01T00:00:00",
        "recording_title": "t",
        "profile_id": "default",
        "namespaces": ["tag"],
        "events": events or [],
        "entities": entities or [],
        "relations": [],
    }


# ---------- populated timeline ----------


def test_timeline_sessions_newest_first_by_recorded_at(client: TestClient) -> None:
    old = client.post("/recordings", json={"tags": ["quest"]}).json()["id"]
    new = client.post("/recordings", json={"tags": ["quest"]}).json()["id"]
    for rid in (old, new):
        _force_state(rid, "done")
    _backdate(old, "2026-01-01T10:00:00")
    _backdate(new, "2026-08-01T10:00:00")

    body = client.get("/tags/quest/timeline").json()
    assert [s["recording_id"] for s in body["sessions"]] == [new, old]
    # ISO-8601 UTC dates echo the coalesce(recorded_at, created_at) value.
    assert body["sessions"][0]["date"] == "2026-08-01T10:00:00"
    assert body["tag"] == "quest"


def test_timeline_session_shape_and_events(client: TestClient) -> None:
    rid = client.post(
        "/recordings", json={"title": "Boss fight", "tags": ["quest"]}
    ).json()["id"]
    _force_state(rid, "done")
    _write_events(
        rid,
        _events_doc(
            entities=[{"slug": "strahd", "label": "Strahd", "type": "person"}],
            events=[
                {
                    "ts": "2026-08-01T10:05:00",
                    "kind": "state_change",
                    "summary": "Strahd retreats",
                    "mentions": ["strahd"],
                }
            ],
        ),
    )

    body = client.get("/tags/quest/timeline").json()
    (session,) = body["sessions"]
    assert session["recording_id"] == rid
    assert session["title"] == "Boss fight"
    assert session["type"] is None
    assert session["duration_sec"] is None
    assert session["entity_count"] == 1
    assert session["events"] == [
        {
            "ts": "2026-08-01T10:05:00",
            "kind": "state_change",
            "summary": "Strahd retreats",
            "mentions": ["strahd"],
        }
    ]


def test_timeline_entities_aggregate_across_files(client: TestClient) -> None:
    """A slug counts per recording when it appears in entities[] OR in any
    event's mentions; last_seen is the newest recording date among them."""
    r1 = client.post("/recordings", json={"tags": ["quest"]}).json()["id"]
    r2 = client.post("/recordings", json={"tags": ["quest"]}).json()["id"]
    other = client.post("/recordings", json={"tags": ["other"]}).json()["id"]
    for rid in (r1, r2, other):
        _force_state(rid, "done")
    _backdate(r1, "2026-01-05T00:00:00")
    _backdate(r2, "2026-02-05T00:00:00")
    _backdate(other, "2026-03-05T00:00:00")
    _write_events(
        r1,
        _events_doc(
            entities=[{"slug": "strahd", "label": "Strahd", "type": "person"}],
            events=[{"ts": "t1", "kind": "note", "summary": "s", "mentions": []}],
        ),
    )
    _write_events(
        r2,
        _events_doc(
            # r2 has NO entity entry for strahd — the mention alone counts.
            events=[
                {"ts": "t2", "kind": "note", "summary": "s", "mentions": ["strahd"]},
                {"ts": "t2", "kind": "note", "summary": "s", "mentions": ["ireena"]},
            ],
        ),
    )
    _write_events(other, _events_doc(entities=[{"slug": "strahd", "label": "x", "type": "person"}]))

    body = client.get("/tags/quest/timeline").json()
    ents = {e["slug"]: e for e in body["entities"]}
    assert ents["strahd"]["sessions"] == 2
    assert ents["strahd"]["last_seen"] == "2026-02-05T00:00:00"
    assert ents["strahd"]["label"] == "Strahd"  # entity entry from the older file
    # Mention-only slug: label falls back to the slug, type empty.
    assert ents["ireena"]["sessions"] == 1
    assert ents["ireena"]["label"] == "ireena"
    assert ents["ireena"]["type"] == ""
    # The other tag's file must not leak into the quest aggregation.
    assert set(ents) == {"strahd", "ireena"}


def test_timeline_entities_sorted_last_seen_desc_then_slug(client: TestClient) -> None:
    a = client.post("/recordings", json={"tags": ["quest"]}).json()["id"]
    b = client.post("/recordings", json={"tags": ["quest"]}).json()["id"]
    for rid in (a, b):
        _force_state(rid, "done")
    _backdate(a, "2026-01-01T00:00:00")
    _backdate(b, "2026-02-01T00:00:00")
    _write_events(
        a,
        _events_doc(
            entities=[
                {"slug": "beta", "label": "Beta", "type": "t"},
                {"slug": "old", "label": "Old", "type": "t"},
            ]
        ),
    )
    _write_events(
        b, _events_doc(entities=[{"slug": "alpha", "label": "Alpha", "type": "t"}])
    )

    body = client.get("/tags/quest/timeline").json()
    slugs = [e["slug"] for e in body["entities"]]
    # Newest last_seen first; alpha and beta share the newest window
    # (both live in b, 2026-02) → slug ASC tiebreak.
    assert slugs == ["alpha", "beta", "old"]


def test_timeline_missing_and_garbage_events_files_skipped(client: TestClient) -> None:
    rid = client.post("/recordings", json={"tags": ["quest"]}).json()["id"]
    _force_state(rid, "done")
    _write_events(rid, {"events": "not-a-list", "entities": 42})

    body = client.get("/tags/quest/timeline").json()
    (session,) = body["sessions"]
    assert session["events"] == []
    assert session["entity_count"] == 0
    assert body["entities"] == []


def test_timeline_digest_generated_flag(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    rid = client.post("/recordings", json={"tags": ["quest"]}).json()["id"]
    _force_state(rid, "done")
    digests = tmp_path / "transcripts" / "digests"
    digests.mkdir(parents=True, exist_ok=True)
    (digests / "quest.md").write_text(
        '---\ntag: "quest"\n---\n\nbody', encoding="utf-8"
    )
    monkeypatch.setattr(
        client.app.state.config.vault, "path", tmp_path / "transcripts"
    )

    body = client.get("/tags/quest/timeline").json()
    assert body["digest_generated"] is True


# ---------- 404 / 400 ----------


def test_timeline_unknown_tag_404(client: TestClient) -> None:
    r = client.get("/tags/no-such-tag/timeline")
    assert r.status_code == 404
    assert "no recordings for tag no-such-tag" in r.json()["detail"]


def test_timeline_done_only(client: TestClient) -> None:
    """Uploading recordings carrying the tag do not count as sessions."""
    client.post("/recordings", json={"tags": ["wip"]})
    r = client.get("/tags/wip/timeline")
    assert r.status_code == 404


def test_timeline_bad_tag_400(client: TestClient) -> None:
    assert client.get("/tags/bad%21tag/timeline").status_code == 400
    assert client.get("/tags/%20%20/timeline").status_code == 400
    assert client.get(f"/tags/{'a' * 65}/timeline").status_code == 400


def test_timeline_tag_normalized(client: TestClient) -> None:
    rid = client.post("/recordings", json={"tags": ["quest"]}).json()["id"]
    _force_state(rid, "done")
    r = client.get("/tags/Quest/timeline")
    assert r.status_code == 200
    assert r.json()["tag"] == "quest"


# ---------- serialize_recording stage details ----------


def test_serialize_stage_details_defaults_empty(client: TestClient) -> None:
    rid = client.post("/recordings", json={"title": "t"}).json()["id"]
    body = client.get(f"/recordings/{rid}").json()
    for stage in body["stages"]:
        assert stage["details"] == {}


def test_serialize_stage_details_roundtrip(client: TestClient) -> None:
    """Worker summarize writes recap info into details (contract:
    {"recap": {...}}); serialization must echo whatever is stored."""
    from app.db import Stage, get_session

    rid = client.post("/recordings", json={"title": "t"}).json()["id"]
    gen = get_session()
    session = next(gen)
    try:
        stage = (
            session.query(Stage)
            .filter(Stage.recording_id == rid, Stage.kind == "summarize")
            .one()
        )
        stage.details = {"recap": {"used": True, "sessions": 3, "chars": 1200}}
        session.commit()
    finally:
        gen.close()

    body = client.get(f"/recordings/{rid}").json()
    (summarize,) = [s for s in body["stages"] if s["kind"] == "summarize"]
    assert summarize["details"] == {"recap": {"used": True, "sessions": 3, "chars": 1200}}


# ---------- time source of digest staleness is vault-side; here we only
# guard that a fresh mtime does not flip the flag (ready vs stale logic
# lives in the /vault tests) ----------


def test_timeline_rescans_on_every_call(client: TestClient) -> None:
    """No caching of per-recording artifacts between calls."""
    rid = client.post("/recordings", json={"tags": ["quest"]}).json()["id"]
    _force_state(rid, "done")
    _write_events(rid, _events_doc(entities=[{"slug": "a", "label": "A", "type": "t"}]))
    first = client.get("/tags/quest/timeline").json()
    _write_events(rid, _events_doc(entities=[{"slug": "b", "label": "B", "type": "t"}]))
    second = client.get("/tags/quest/timeline").json()
    assert first["entities"][0]["slug"] == "a"
    assert second["entities"][0]["slug"] == "b"


