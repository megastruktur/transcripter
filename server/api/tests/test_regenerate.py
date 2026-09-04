"""Regenerate + artifacts contract tests (Temporal mocked)."""

import hashlib
import os
from unittest.mock import AsyncMock, patch

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


def _make_recording(client: TestClient) -> str:
    r = client.post("/recordings", json={"title": "regen"})
    return r.json()["id"]


def test_regenerate_unknown_stage_400(client: TestClient) -> None:
    rid = _make_recording(client)
    r = client.post(f"/recordings/{rid}/regenerate", json={"stage": "nope"})
    assert r.status_code == 400


def test_regenerate_uploading_409(client: TestClient) -> None:
    rid = _make_recording(client)
    r = client.post(f"/recordings/{rid}/regenerate", json={"stage": "transcribe"})
    assert r.status_code == 409


def _force_state(rid: str, state: str) -> None:
    from app.db import Recording, get_session

    gen = get_session()
    s = next(gen)
    try:
        rec = s.get(Recording, rid)
        assert rec is not None
        rec.state = state
        s.commit()
    finally:
        gen.close()


def test_regenerate_backfills_missing_stage_rows(client: TestClient) -> None:
    """Recordings created before a stage kind existed (e.g. `chunk`) have no
    # stage row for it; regenerate must backfill so the worker's
    # set_stage(.one()) cannot fail."""
    rid = _make_recording(client)
    _force_state(rid, "done")
    # Simulate a pre-chunk recording: drop its chunk stage row.
    from app.db import Stage, get_session

    gen = get_session()
    s = next(gen)
    try:
        s.query(Stage).filter_by(recording_id=rid, kind="chunk").delete()
        s.commit()
        assert s.query(Stage).filter_by(recording_id=rid).count() == 6
    finally:
        gen.close()

    with patch("app.temporal_client.regenerate_stage", new_callable=AsyncMock) as m:
        m.return_value = "wf-chunk"
        r = client.post(f"/recordings/{rid}/regenerate", json={"stage": "chunk"})
    assert r.status_code == 200

    gen = get_session()
    s = next(gen)
    try:
        kinds = {st.kind for st in s.query(Stage).filter_by(recording_id=rid)}
        assert kinds == {"chunk", "separate", "transcribe", "diarize", "merge_speakers", "summarize", "enrich"}
    finally:
        gen.close()


def test_regenerate_starts_workflow(client: TestClient) -> None:
    rid = _make_recording(client)
    _force_state(rid, "done")

    with patch("app.temporal_client.regenerate_stage", new_callable=AsyncMock) as m:
        m.return_value = "wf-123"
        r = client.post(f"/recordings/{rid}/regenerate", json={"stage": "diarize"})
    assert r.status_code == 200
    assert r.json()["workflow_id"] == "wf-123"
    m.assert_awaited_once()


def test_regenerate_processing_409(client: TestClient) -> None:
    rid = _make_recording(client)
    _force_state(rid, "processing")
    r = client.post(f"/recordings/{rid}/regenerate", json={"stage": "transcribe"})
    assert r.status_code == 409
    assert "already processing" in r.json()["detail"]


def test_regenerate_temporal_down_503(client: TestClient) -> None:
    rid = _make_recording(client)
    _force_state(rid, "done")

    with patch(
        "app.temporal_client.regenerate_stage",
        new_callable=AsyncMock,
        side_effect=ConnectionError("temporal down"),
    ):
        r = client.post(f"/recordings/{rid}/regenerate", json={"stage": "summarize"})
    assert r.status_code == 503


def test_artifacts_unknown_stage_404(client: TestClient) -> None:
    rid = _make_recording(client)
    assert client.get(f"/recordings/{rid}/artifacts/bogus").status_code == 404


def test_artifact_not_generated_404(client: TestClient) -> None:
    rid = _make_recording(client)
    r = client.get(f"/recordings/{rid}/artifacts/transcribe")
    assert r.status_code == 404
    assert "not generated" in r.json()["detail"]


def test_artifact_served_when_present(client: TestClient) -> None:
    rid = _make_recording(client)
    from pathlib import Path as P

    storage = os.environ["TRANSCRIPTER_STORAGE"]
    meta = P(storage) / "recordings" / rid / "meta"
    meta.mkdir(parents=True, exist_ok=True)
    (meta / "transcript.md").write_text("# t", encoding="utf-8")
    r = client.get(f"/recordings/{rid}/artifacts/transcribe")
    assert r.status_code == 200
    assert r.text == "# t"


def _sha(data: bytes) -> str:

    return hashlib.sha256(data).hexdigest()

def _upload_and_finalize(client: TestClient, data: bytes) -> str:
    rid = _make_recording(client)
    client.put(
        f"/recordings/{rid}/audio",
        params={"offset": 0},
        content=data,
        headers={"content-length": str(len(data))},
    )
    client.post(f"/recordings/{rid}/finalize", json={"sha256": _sha(data)})
    return rid


def test_audio_uploading_409(client: TestClient) -> None:
    rid = _make_recording(client)
    r = client.get(f"/recordings/{rid}/audio")
    assert r.status_code == 409


def test_audio_served_after_upload(client: TestClient) -> None:
    """Full GET without a Range header → 200 with the whole file."""
    data = b"a" * 128
    rid = _upload_and_finalize(client, data)
    r = client.get(f"/recordings/{rid}/audio")
    assert r.status_code == 200
    assert r.content == data


def test_audio_range_partial_content(client: TestClient) -> None:
    """Range regression: starlette FileResponse must serve exact byte ranges
    (the in-card player's seek depends on it)."""
    data = bytes(range(256)) * 4  # 1024 bytes, non-uniform so offsets matter
    rid = _upload_and_finalize(client, data)
    r = client.get(f"/recordings/{rid}/audio", headers={"range": "bytes=0-99"})
    assert r.status_code == 206
    assert len(r.content) == 100
    assert r.content == data[:100]
    assert r.headers["content-range"] == f"bytes 0-99/{len(data)}"


def test_audio_range_unsatisfiable_416(client: TestClient) -> None:
    data = b"a" * 128
    rid = _upload_and_finalize(client, data)
    r = client.get(
        f"/recordings/{rid}/audio",
        headers={"range": f"bytes={len(data) + 100}-"},
    )
    assert r.status_code == 416

def test_audio_head_200_empty_body(client: TestClient) -> None:
    """HEAD probe (WebKit media stack) → 200, no body, accurate length/type."""
    data = b"a" * 128
    rid = _upload_and_finalize(client, data)
    r = client.head(f"/recordings/{rid}/audio")
    assert r.status_code == 200
    assert r.content == b""
    assert r.headers["content-length"] == str(len(data))
    assert r.headers["content-type"] == "audio/flac"
    assert r.headers["accept-ranges"] == "bytes"


def test_audio_head_valid_query_token_200(client: TestClient) -> None:
    """HEAD with ?token= (the WebKit <audio> probe path) must auth the same as GET."""
    data = b"a" * 128
    rid = _upload_and_finalize(client, data)
    r = client.head(
        f"/recordings/{rid}/audio?token=sekrit",
        headers={"authorization": ""},
    )
    assert r.status_code == 200
    assert r.content == b""


def test_audio_head_missing_file_404(client: TestClient) -> None:
    """Recording row exists but audio.flac does not → 404 (also on HEAD)."""
    rid = _make_recording(client)
    _force_state(rid, "done")
    assert client.head(f"/recordings/{rid}/audio").status_code == 404


def test_audio_valid_query_token_200(client: TestClient) -> None:
    """<audio> cannot send Authorization; ?token= works on the audio route."""
    data = b"a" * 128
    rid = _upload_and_finalize(client, data)
    r = client.get(
        f"/recordings/{rid}/audio?token=sekrit",
        headers={"authorization": ""},
    )
    assert r.status_code == 200
    assert r.content == data


def test_audio_valid_query_token_with_range_206(client: TestClient) -> None:
    data = b"a" * 128
    rid = _upload_and_finalize(client, data)
    r = client.get(
        f"/recordings/{rid}/audio?token=sekrit",
        headers={"authorization": "", "range": "bytes=0-9"},
    )
    assert r.status_code == 206
    assert r.content == data[:10]


def test_audio_wrong_query_token_401(client: TestClient) -> None:
    data = b"a" * 128
    rid = _upload_and_finalize(client, data)
    r = client.get(
        f"/recordings/{rid}/audio?token=nope",
        headers={"authorization": ""},
    )
    assert r.status_code == 401


def test_query_token_rejected_on_non_audio_route(client: TestClient) -> None:
    """The query-token escape hatch is scoped to /recordings/*/audio only."""
    rid = _make_recording(client)
    assert client.get("/recordings?token=sekrit", headers={"authorization": ""}).status_code == 401
    assert (
        client.get(f"/recordings/{rid}?token=sekrit", headers={"authorization": ""}).status_code
        == 401
    )


def test_summary_not_generated_404(client: TestClient) -> None:
    rid = _make_recording(client)
    r = client.get(f"/recordings/{rid}/summary")
    assert r.status_code == 404


def test_finalize_starts_pipeline(client: TestClient) -> None:
    from app import temporal_client

    rid = _make_recording(client)
    data = b"z" * 64
    client.put(
        f"/recordings/{rid}/audio",
        params={"offset": 0},
        content=data,
        headers={"content-length": str(len(data))},
    )
    r = client.post(f"/recordings/{rid}/finalize", json={"sha256": _sha(data)})
    assert r.status_code == 200
    temporal_client.start_pipeline.assert_awaited_once()


def test_segments_json_selectable(client: TestClient) -> None:
    rid = _make_recording(client)
    from pathlib import Path as P

    storage = os.environ["TRANSCRIPTER_STORAGE"]
    meta = P(storage) / "recordings" / rid / "meta"
    meta.mkdir(parents=True, exist_ok=True)
    (meta / "segments.json").write_text("{}", encoding="utf-8")
    r = client.get(f"/recordings/{rid}/artifacts/transcribe?file=segments.json")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/json")
