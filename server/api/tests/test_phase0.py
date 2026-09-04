"""Phase 0 (type/freehand-tag split) API contract tests.

Covers plan §0.1/§0.4:
- direct upload with optional `type` + `recorded_at` (import backdate)
- GET /tags — distinct tags with counts (suggestions source)
- PATCH type/recorded_at — Temporal regenerate semantics (mocked client)
- serialize_recording exposes type + recorded_at
"""

from __future__ import annotations

import importlib
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


# ---------- serialize_recording ----------


def test_serialize_exposes_type_and_recorded_at(client: TestClient) -> None:
    r = client.post("/recordings", json={"title": "t"})
    rid = r.json()["id"]
    body = client.get(f"/recordings/{rid}").json()
    assert body["type"] is None
    assert body["recorded_at"] is None


# ---------- POST /recordings/direct: type + recorded_at ----------


def _direct(client: TestClient, *, type_: str = "", recorded_at: str = "") -> dict:
    """Minimal valid FLAC (magic + STREAMINFO + frame) multipart upload."""
    streaminfo = bytes([0x80, 0x00, 0x00, 0x22]) + bytes(34)
    data = b"fLaC" + streaminfo + b"\xff\xf8\x00\x00\x00\x00frame"
    form: dict[str, str] = {"title": "import", "tags": "[]"}
    if type_:
        form["type"] = type_
    if recorded_at:
        form["recorded_at"] = recorded_at
    r = client.post(
        "/recordings/direct",
        data=form,
        files=[("file", ("audio.flac", data, "audio/flac"))],
    )
    assert r.status_code == 201, r.text
    return client.get(f"/recordings/{r.json()['id']}").json()


def test_direct_recorded_at_backdate_naive_utc(client: TestClient) -> None:
    """A naive timestamp is stored as UTC — the column never mixes
    aware/naive values."""
    body = _direct(client, recorded_at="2026-08-20T18:30:00")
    assert body["recorded_at"].startswith("2026-08-20T18:30:00")


def test_direct_no_type_or_recorded_at_nulls(client: TestClient) -> None:
    body = _direct(client)
    assert body["type"] is None
    assert body["recorded_at"] is None


def test_direct_unknown_type_stored_as_is(client: TestClient) -> None:
    """Unknown types are stored (pipeline matches no profile); only a
    garbage SHAPE is a 400."""
    body = _direct(client, type_="lecture")
    assert body["type"] == "lecture"


@pytest.mark.parametrize("bad", ["Meeting", "-meet", "m" * 33, "bad type"])
def test_direct_garbage_type_400(client: TestClient, bad: str) -> None:
    streaminfo = bytes([0x80, 0x00, 0x00, 0x22]) + bytes(34)
    data = b"fLaC" + streaminfo + b"\xff\xf8\x00\x00\x00\x00frame"
    r = client.post(
        "/recordings/direct",
        data={"title": "x", "tags": "[]", "type": bad},
        files=[("file", ("audio.flac", data, "audio/flac"))],
    )
    assert r.status_code == 400, r.text
    assert "type" in r.json()["detail"].lower()


def test_direct_type_and_recorded_at_persisted(client: TestClient) -> None:
    body = _direct(client, type_="meeting", recorded_at="2026-08-20T18:30:00+00:00")
    assert body["type"] == "meeting"
    # Offset-bearing input is normalized to naive UTC.
    assert body["recorded_at"] == "2026-08-20T18:30:00"

def test_direct_garbage_recorded_at_400(client: TestClient) -> None:
    streaminfo = bytes([0x80, 0x00, 0x00, 0x22]) + bytes(34)
    data = b"fLaC" + streaminfo + b"\xff\xf8\x00\x00\x00\x00frame"
    r = client.post(
        "/recordings/direct",
        data={"title": "x", "tags": "[]", "recorded_at": "not-a-date"},
        files=[("file", ("audio.flac", data, "audio/flac"))],
    )
    assert r.status_code == 400, r.text
    assert "iso-8601" in r.json()["detail"].lower()


# ---------- PATCH type / recorded_at ----------


def _make(client: TestClient, *, type_: str | None = None, tags: list | None = None) -> str:
    rid = client.post("/recordings", json={"title": "t", "tags": tags or []}).json()["id"]
    if type_ is not None:
        # POST /recordings has no type field; set it via PATCH (uploading
        # state → no regenerate side effect).
        client.patch(f"/recordings/{rid}", json={"type": type_})
    # The setup PATCH fires start_export (recorded_at/title-less path):
    # reset the mocks so tests assert only their own call.
    from app import temporal_client

    temporal_client.start_export.reset_mock()
    temporal_client.regenerate_stage.reset_mock()
    return rid


def _force_state(rid: str, state: str) -> None:
    from app.db import Recording, RecordingState, get_session

    gen = get_session()
    session = next(gen)
    try:
        rec = session.get(Recording, rid)
        rec.state = RecordingState[state]
        session.commit()
    finally:
        gen.close()


def test_patch_type_persists_and_serializes(client: TestClient) -> None:
    rid = _make(client)
    r = client.patch(f"/recordings/{rid}", json={"type": "ttrpg"})
    assert r.status_code == 200
    assert r.json()["type"] == "ttrpg"
    assert client.get(f"/recordings/{rid}").json()["type"] == "ttrpg"


def test_patch_garbage_type_400_and_no_side_effect(client: TestClient) -> None:
    from app import temporal_client

    rid = _make(client)
    _force_state(rid, "done")
    r = client.patch(f"/recordings/{rid}", json={"type": "Bad Type"})
    assert r.status_code == 400
    temporal_client.start_export.assert_not_awaited()
    temporal_client.regenerate_stage.assert_not_awaited()
    # Row untouched.
    assert client.get(f"/recordings/{rid}").json()["type"] is None


def test_patch_garbage_recorded_at_400(client: TestClient) -> None:
    rid = _make(client)
    r = client.patch(f"/recordings/{rid}", json={"recorded_at": "yesterday"})
    assert r.status_code == 400
    assert "iso-8601" in r.json()["detail"].lower()


def test_patch_recorded_at_persists(client: TestClient) -> None:
    rid = _make(client)
    r = client.patch(f"/recordings/{rid}", json={"recorded_at": "2026-08-01T10:00:00+00:00"})
    assert r.status_code == 200
    assert r.json()["recorded_at"] == "2026-08-01T10:00:00"


def test_patch_type_on_done_regenerates_summarize(client: TestClient) -> None:
    """Type change on a DONE recording → regenerate (summarize start →
    the workflow cascades enrich + export). No separate export call."""
    from app import temporal_client

    rid = _make(client, type_="meeting")
    _force_state(rid, "done")
    r = client.patch(f"/recordings/{rid}", json={"type": "ttrpg"})
    assert r.status_code == 200
    temporal_client.regenerate_stage.assert_awaited_once_with(rid, "summarize", None)
    temporal_client.start_export.assert_not_awaited()


def test_patch_same_type_no_regenerate(client: TestClient) -> None:
    """Setting type to its current value is not a change → plain export."""
    from app import temporal_client

    rid = _make(client, type_="meeting")
    _force_state(rid, "done")
    client.patch(f"/recordings/{rid}", json={"type": "meeting"})
    temporal_client.regenerate_stage.assert_not_awaited()
    temporal_client.start_export.assert_awaited_once_with(rid, rename_only=False)


def test_patch_type_on_uploading_no_regenerate(client: TestClient) -> None:
    """Regenerate only fires on DONE recordings — uploading ones are
    still going through the pipeline."""
    from app import temporal_client

    rid = _make(client)
    client.patch(f"/recordings/{rid}", json={"type": "meeting"})
    temporal_client.regenerate_stage.assert_not_awaited()


def test_patch_tags_on_done_regenerates_enrich(client: TestClient) -> None:
    """Tags change → enrich regenerate (graph namespaces = tags), no
    summarize re-run."""
    from app import temporal_client

    rid = _make(client)
    _force_state(rid, "done")
    r = client.patch(f"/recordings/{rid}", json={"tags": ["dnd", "dark castle"]})
    assert r.status_code == 200
    temporal_client.regenerate_stage.assert_awaited_once_with(rid, "enrich", None)
    temporal_client.start_export.assert_not_awaited()


def test_patch_same_tags_no_regenerate(client: TestClient) -> None:
    from app import temporal_client

    rid = _make(client, tags=["dnd"])
    _force_state(rid, "done")
    client.patch(f"/recordings/{rid}", json={"tags": ["dnd"]})
    temporal_client.regenerate_stage.assert_not_awaited()
    temporal_client.start_export.assert_awaited_once_with(rid, rename_only=False)


def test_patch_recorded_at_done_export_only(client: TestClient) -> None:
    """recorded_at feeds the frontmatter only → start_export, no
    pipeline regeneration."""
    from app import temporal_client

    rid = _make(client)
    _force_state(rid, "done")
    r = client.patch(
        f"/recordings/{rid}", json={"recorded_at": "2026-08-01T10:00:00+00:00"}
    )
    assert r.status_code == 200
    temporal_client.regenerate_stage.assert_not_awaited()
    temporal_client.start_export.assert_awaited_once_with(rid, rename_only=False)


def test_patch_title_and_recorded_at_full_export(client: TestClient) -> None:
    """Title alone is rename-only, but a recorded_at change must rewrite
    the frontmatter → full (non-rename) export, no regenerate."""
    from app import temporal_client

    rid = _make(client)
    _force_state(rid, "done")
    client.patch(
        f"/recordings/{rid}",
        json={"title": "renamed", "recorded_at": "2026-08-01T10:00:00+00:00"},
    )
    temporal_client.regenerate_stage.assert_not_awaited()
    temporal_client.start_export.assert_awaited_once_with(rid, rename_only=False)


def test_patch_type_and_tags_regenerates_summarize_once(client: TestClient) -> None:
    """Combined change fires ONE regenerate starting at summarize (which
    cascades enrich) — never two workflow starts."""
    from app import temporal_client

    rid = _make(client)
    _force_state(rid, "done")
    r = client.patch(f"/recordings/{rid}", json={"type": "meeting", "tags": ["dnd"]})
    assert r.status_code == 200
    temporal_client.regenerate_stage.assert_awaited_once_with(rid, "summarize", None)


def test_patch_all_null_400(client: TestClient) -> None:
    rid = _make(client)
    assert client.patch(f"/recordings/{rid}", json={}).status_code == 400


# ---------- GET /tags ----------


def test_get_tags_counts_and_order(client: TestClient) -> None:
    client.post("/recordings", json={"title": "a", "tags": ["dnd", "meeting"]})
    client.post("/recordings", json={"title": "b", "tags": ["dnd"]})
    client.post("/recordings", json={"title": "c", "tags": ["Meeting"]})

    body = client.get("/tags")
    assert body.status_code == 200
    items = body.json()["items"]
    assert items == [
        {"tag": "dnd", "count": 2, "registered": True, "vocabulary_count": 0},
        {"tag": "meeting", "count": 2, "registered": True, "vocabulary_count": 0},
    ]


def test_get_tags_count_desc_then_tag_asc(client: TestClient) -> None:
    client.post("/recordings", json={"tags": ["zeta"]})
    client.post("/recordings", json={"tags": ["alpha"]})
    client.post("/recordings", json={"tags": ["zeta"]})

    items = client.get("/tags").json()["items"]
    assert [i["tag"] for i in items] == ["zeta", "alpha"]


def test_get_tags_spaces_preserved(client: TestClient) -> None:
    client.post("/recordings", json={"tags": ["dark castle"]})
    items = client.get("/tags").json()["items"]
    assert items == [{"tag": "dark castle", "count": 1, "registered": True, "vocabulary_count": 0}]


def test_get_tags_empty_catalog(client: TestClient) -> None:
    assert client.get("/tags").json() == {"items": []}


# ---------- migration (sqlite path: no-op but must not raise) ----------


def test_migrate_type_columns_idempotent() -> None:
    from app import main

    main._migrate_type_columns()
    main._migrate_type_columns()


def test_backfill_tag_defs_registers_existing_tags(client: TestClient) -> None:
    """The startup superset backfill: tags already on recordings (created
    before the registry existed, restored backups, manual INSERTs) gain
    tag_defs rows. The client fixture's recordings carry tags, but
    auto-registration (v0.24.0) already covers THAT path — simulate the
    pre-registry state by deleting the rows first, then prove the
    backfill re-creates them."""
    from app import main
    from app.db import TagDef, get_session

    # A recording with tags — but NOT via the registry-aware routes: the
    # point is tags that predate/evade auto-registration.
    client.post("/recordings", json={"title": "pre-registry", "tags": ["legacy tag"]})
    gen = get_session()
    try:
        s = next(gen)
        s.query(TagDef).delete()
        s.commit()
    finally:
        gen.close()

    main._backfill_tag_defs()

    gen = get_session()
    try:
        s = next(gen)
        names = {d.name for d in s.query(TagDef).all()}
        assert len(names) > 0  # the fixture created at least one recording
        # every tag of every recording is registered
        from app.db import Recording

        rec_tags = {
            t for rec in s.query(Recording).all() for t in (rec.tags or []) if t
        }
        assert rec_tags <= names
    finally:
        gen.close()


def test_backfill_tag_defs_preserves_vocabulary_and_idempotent() -> None:
    from app import main
    from app.db import TagDef, get_session

    # A pre-existing registry row with a vocabulary must survive the
    # backfill untouched (ON CONFLICT DO NOTHING).
    gen = get_session()
    try:
        s = next(gen)
        s.add(TagDef(name="dnd", vocabulary=["Абсалом"]))
        s.commit()
    finally:
        gen.close()

    main._backfill_tag_defs()
    main._backfill_tag_defs()  # re-run: still no-op

    gen = get_session()
    try:
        s = next(gen)
        dnd = s.get(TagDef, "dnd")
        assert dnd is not None and dnd.vocabulary == ["Абсалом"]
    finally:
        gen.close()
