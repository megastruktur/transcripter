"""Phase 3 API surface: GET /vault.

One item per distinct free tag: sessions (recordings in ANY state — a
tag on an uploading capture is real user intent), entities (aggregated
from the tag's done recordings' events.json files), last_activity
(max coalesce(recorded_at, created_at)), and digest state:
ready (note exists, mtime >= newest recording date) / stale (mtime
older) / none (no note). Untagged recordings are not listed.
"""

from __future__ import annotations

import importlib
import json
import os
from datetime import UTC, datetime, timedelta
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
    from app.main import app

    root = app.state.config.recordings_root / rid / "meta"
    root.mkdir(parents=True, exist_ok=True)
    (root / "events.json").write_text(json.dumps(doc), encoding="utf-8")


def _touch_digest(client: TestClient, tmp_path, slug: str, tag: str, *, age_s: float = 0) -> None:
    """Write a digest note for ``tag``; ``age_s`` backdates the mtime."""
    digests = tmp_path / "transcripts" / "digests"
    digests.mkdir(parents=True, exist_ok=True)
    md = digests / f"{slug}.md"
    md.write_text(f'---\ntag: "{tag}"\n---\n\nbody', encoding="utf-8")
    if age_s:
        past = (datetime.now(UTC) - timedelta(seconds=age_s)).timestamp()
        os.utime(md, (past, past))


def _vault(client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path) -> list[dict]:
    monkeypatch.setattr(
        client.app.state.config.transcripts, "path", tmp_path / "transcripts"
    )
    return client.get("/vault").json()["items"]


# ---------- shape & aggregation ----------


def test_vault_empty(client: TestClient) -> None:
    assert client.get("/vault").json() == {"items": []}


def test_vault_untagged_skipped(client: TestClient) -> None:
    client.post("/recordings", json={"title": "no tags"})
    assert client.get("/vault").json() == {"items": []}


def test_vault_item_shape_and_counts(client: TestClient) -> None:
    a = client.post("/recordings", json={"tags": ["quest"]}).json()["id"]
    b = client.post("/recordings", json={"tags": ["quest", "dnd"]}).json()["id"]
    for rid in (a, b):
        _force_state(rid, "done")
    _write_events(
        a,
        {
            "entities": [
                {"slug": "strahd", "label": "Strahd", "type": "person"},
                {"slug": "ireena", "label": "Ireena", "type": "person"},
            ],
            "events": [],
        },
    )
    _write_events(
        b,
        {"entities": [], "events": [{"ts": "t", "kind": "note", "summary": "s", "mentions": ["strahd"]}]},
    )

    r = client.get("/vault")
    assert r.status_code == 200
    items = {it["tag"]: it for it in r.json()["items"]}
    assert items["quest"]["sessions"] == 2
    assert items["quest"]["entities"] == 2  # strahd + ireena
    assert items["quest"]["digest"] == "none"
    assert items["dnd"]["sessions"] == 1
    assert items["dnd"]["entities"] == 1  # mention-only strahd
    assert set(items["quest"]) == {"tag", "sessions", "entities", "last_activity", "digest"}


def test_vault_uploading_state_counts(client: TestClient) -> None:
    """Vault counts recordings in ANY state (GET /tags precedent)."""
    client.post("/recordings", json={"tags": ["wip"]})
    (items,) = client.get("/vault").json()["items"]
    assert items["tag"] == "wip"
    assert items["sessions"] == 1


def test_vault_last_activity_uses_recorded_at(client: TestClient) -> None:
    a = client.post("/recordings", json={"tags": ["quest"]}).json()["id"]
    b = client.post("/recordings", json={"tags": ["quest"]}).json()["id"]
    _force_state(a, "done")
    _force_state(b, "done")
    _backdate(a, "2026-01-01T10:00:00")
    _backdate(b, "2026-08-01T10:00:00")
    (item,) = client.get("/vault").json()["items"]
    assert item["last_activity"] == "2026-08-01T10:00:00"


def test_vault_last_activity_falls_back_to_created_at(client: TestClient) -> None:
    client.post("/recordings", json={"tags": ["quest"]})
    (item,) = client.get("/vault").json()["items"]
    assert item["last_activity"]  # created_at is always set


def test_vault_ordering_newest_activity_first(client: TestClient) -> None:
    old = client.post("/recordings", json={"tags": ["older"]}).json()["id"]
    new = client.post("/recordings", json={"tags": ["newer"]}).json()["id"]
    _force_state(old, "done")
    _force_state(new, "done")
    _backdate(old, "2026-01-01T00:00:00")
    _backdate(new, "2026-08-01T00:00:00")
    items = client.get("/vault").json()["items"]
    assert [it["tag"] for it in items] == ["newer", "older"]


# ---------- digest ready / stale / none ----------


def test_vault_digest_ready(client: TestClient, monkeypatch, tmp_path) -> None:
    rid = client.post("/recordings", json={"tags": ["quest"]}).json()["id"]
    _force_state(rid, "done")
    _backdate(rid, "2026-01-01T00:00:00")
    _touch_digest(client, tmp_path, "quest", "quest")
    (item,) = _vault(client, monkeypatch, tmp_path)
    assert item["digest"] == "ready"


def test_vault_digest_stale_when_note_older_than_newest_recording(
    client: TestClient, monkeypatch, tmp_path
) -> None:
    """Note generated BEFORE the newest recording (mtime < recorded_at)
    → the note no longer covers the newest session → stale."""
    rid = client.post("/recordings", json={"tags": ["quest"]}).json()["id"]
    _force_state(rid, "done")
    # Both sides relative: recording at now-30d, note mtime at now-90d.
    # mtime (90d) < recorded_at (30d) is always true → stale.
    _backdate(rid, (datetime.now(UTC) - timedelta(days=30)).isoformat())
    _touch_digest(client, tmp_path, "quest", "quest", age_s=90 * 24 * 3600)
    (item,) = _vault(client, monkeypatch, tmp_path)
    assert item["digest"] == "stale"


def test_vault_digest_none_without_file(client: TestClient, monkeypatch, tmp_path) -> None:
    rid = client.post("/recordings", json={"tags": ["quest"]}).json()["id"]
    _force_state(rid, "done")
    (item,) = _vault(client, monkeypatch, tmp_path)
    assert item["digest"] == "none"


def test_vault_digest_ready_requires_frontmatter_match(
    client: TestClient, monkeypatch, tmp_path
) -> None:
    """A note for another tag must not mark this tag ready."""
    rid = client.post("/recordings", json={"tags": ["quest"]}).json()["id"]
    _force_state(rid, "done")
    _touch_digest(client, tmp_path, "other", "other")
    (item,) = _vault(client, monkeypatch, tmp_path)
    assert item["digest"] == "none"
