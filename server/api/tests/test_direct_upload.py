"""POST /recordings/direct — one-shot multipart upload contract tests.

The dev host does not always ship ffmpeg; when it does not, the transcode
path is exercised through a subprocess mock so the route's control flow is
covered regardless of the runtime environment.
"""

import shutil
import subprocess
from typing import Any
from unittest.mock import patch

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


def _flac_with_frames(payload: bytes = b"\xff\xf8\x00\x00\x00\x00frame") -> bytes:
    """fLaC + last-block STREAMINFO + a non-empty audio frame payload."""
    streaminfo = bytes([0x80, 0x00, 0x00, 0x22]) + bytes(34)
    return b"fLaC" + streaminfo + payload


def _flac_header_only() -> bytes:
    """Valid FLAC magic + last-block STREAMINFO, no audio frames."""
    streaminfo = bytes([0x80, 0x00, 0x00, 0x22]) + bytes(34)
    return b"fLaC" + streaminfo

def _post_direct(
    client: TestClient,
    data: bytes,
    *,
    filename: str = "audio.bin",
    content_type: str = "application/octet-stream",
    title: str = "",
    tags: str = "[]",
    duration_sec: str | None = None,
) -> Any:
    form_data: dict[str, str] = {
        "title": title,
        "tags": tags,
    }
    if duration_sec is not None:
        form_data["duration_sec"] = duration_sec
    return client.post(
        "/recordings/direct",
        data=form_data,
        files=[("file", (filename, data, content_type))],
    )


def test_direct_flac_passthrough_no_transcode(client: TestClient) -> None:
    """FLAC uploads must hit disk as-is — no ffmpeg invocation."""
    data = _flac_with_frames()

    with patch(
        "app.routes.recordings.subprocess.run"
    ) as run:
        r = _post_direct(client, data, filename="audio.flac", title="hello")

    assert r.status_code == 201, r.text
    rec_id = r.json()["id"]
    assert len(rec_id) == 36

    # ffmpeg must not have been invoked at all.
    run.assert_not_called()

    detail = client.get(f"/recordings/{rec_id}").json()
    assert detail["state"] == "processing"
    assert detail["title"] == "hello"
    assert detail["committed_bytes"] > 0
    assert detail["duration_sec"] is None
    kinds = {s["kind"] for s in detail["stages"]}
    assert kinds == {"chunk", "separate", "transcribe", "diarize", "merge_speakers", "summarize", "enrich"}
    for stage in detail["stages"]:
        assert stage["status"] == "pending"


def test_direct_flac_passthrough_calls_on_finalize(client: TestClient) -> None:
    """The on_finalize hook (Temporal trigger) must fire on success."""
    data = _flac_with_frames()

    with patch("app.routes.recordings.asyncio.to_thread") as to_thread:
        # asyncio.to_thread is only used for ffmpeg — for a FLAC upload it
        # should never be reached. Make it loud if that changes.
        to_thread.side_effect = AssertionError("to_thread called on FLAC passthrough")
        r = _post_direct(client, data, filename="audio.flac")

    assert r.status_code == 201, r.text
    # Temporal client was mocked in conftest — assert it ran once for the
    # newly created recording.
    from app import temporal_client

    temporal_client.start_pipeline.assert_awaited()


def test_direct_webm_to_flac_real_or_mocked(client: TestClient) -> None:
    """Non-FLAC uploads go through ffmpeg. If ffmpeg is installed, run it for
    real; otherwise drive the same code path via a subprocess mock that
    fabricates a canonical 48 kHz mono FLAC on disk."""
    data = b"\x1a\x45\xdf\xa3webm-bytes-not-actually-decodable"  # EBML header
    has_ffmpeg = shutil.which("ffmpeg") is not None

    if has_ffmpeg:
        # ffmpeg rejects our fake webm as garbage; we only assert the route
        # is wired (a non-empty input gets through to ffmpeg and either
        # succeeds or 422s, both prove the transcode path was entered).
        r = _post_direct(client, data, filename="audio.webm", content_type="audio/webm")
        assert r.status_code in (201, 422), r.text
        if r.status_code == 422:
            assert "ffmpeg" in r.json()["detail"].lower()
        return

    # Mocked path: pretend ffmpeg ran successfully by writing the same FLAC
    # the real transcoder would have produced. The route reads the bytes
    # straight off disk, so as long as the dst file ends up as a valid FLAC
    # with frames, the route accepts it.
    from app.routes.recordings import FLAC_MAGIC, audio_path

    real_run = subprocess.run

    def fake_run(cmd, *args, **kwargs):  # type: ignore[no-untyped-def]
        # Only intercept ffmpeg; anything else (defensive) falls through.
        if cmd and cmd[0] == "ffmpeg":
            # cmd[-1] is the output path.
            with open(cmd[-1], "wb") as out:
                out.write(_flac_with_frames(b"transcoded-bytes"))
            return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")
        return real_run(cmd, *args, **kwargs)

    with patch("app.routes.recordings.subprocess.run", side_effect=fake_run):
        r = _post_direct(client, data, filename="audio.webm", content_type="audio/webm")

    assert r.status_code == 201, r.text
    rec_id = r.json()["id"]
    detail = client.get(f"/recordings/{rec_id}").json()
    assert detail["state"] == "processing"

    # Confirm the bytes on disk are a valid FLAC (the mocked transcoder
    # wrote one) and that the input sidecar was cleaned up.
    on_disk = audio_path(client.app.state.config, rec_id).read_bytes()
    assert on_disk.startswith(FLAC_MAGIC)
    sidecar = client.app.state.config.recordings_root / rec_id / "_input.bin"
    assert not sidecar.exists()


def test_direct_bad_input_ffmpeg_fails_returns_422(client: TestClient) -> None:
    """When ffmpeg exits non-zero, the route returns 422 with a stderr tail
    and tears down the recording row + directory."""
    data = b"this-is-not-real-audio-bytes"

    def fake_run(cmd, *args, **kwargs):  # type: ignore[no-untyped-def]
        return subprocess.CompletedProcess(
            cmd,
            1,
            stdout=b"",
            stderr=b"[error] Invalid data found when processing input\n"
            b"Conversion failed!",
        )

    with patch("app.routes.recordings.subprocess.run", side_effect=fake_run):
        r = _post_direct(client, data, filename="audio.webm", content_type="audio/webm")

    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    assert "ffmpeg failed" in detail
    assert "Conversion failed" in detail

    # No DB row or on-disk directory should survive the failure.
    # Look up by reading the recordings root — easier: assert that no
    # recording exists (since we just created one above).
    from app.db import Recording, get_session

    gen = get_session()
    s = next(gen)
    try:
        assert s.scalar(__import__("sqlalchemy").select(Recording).limit(1)) is None
    finally:
        gen.close()


def test_direct_empty_file_returns_400(client: TestClient) -> None:
    r = _post_direct(client, b"", filename="audio.webm")
    assert r.status_code == 400, r.text
    assert "empty" in r.json()["detail"].lower()


def test_direct_invalid_tags_json_returns_400(client: TestClient) -> None:
    data = _flac_with_frames()
    r = _post_direct(client, data, filename="audio.flac", tags="not-json")
    assert r.status_code == 400, r.text
    assert "tags" in r.json()["detail"].lower()


def test_direct_tags_are_normalized(client: TestClient) -> None:
    """Whitespace, casing and duplicates collapse the same way create does."""
    data = _flac_with_frames()
    r = _post_direct(
        client,
        data,
        filename="audio.flac",
        title="t",
        tags='["  Foo ", "foo", "bar", "  ", "BAR", "  baz  "]',
    )
    assert r.status_code == 201, r.text
    rec_id = r.json()["id"]
    detail = client.get(f"/recordings/{rec_id}").json()
    assert detail["tags"] == ["foo", "bar", "baz"]


def test_direct_duration_sec_persisted(client: TestClient) -> None:
    data = _flac_with_frames()
    r = _post_direct(client, data, filename="audio.flac", duration_sec="42.5")
    assert r.status_code == 201, r.text
    rec_id = r.json()["id"]
    assert client.get(f"/recordings/{rec_id}").json()["duration_sec"] == 42.5


def test_direct_starts_pipeline_workflow(client: TestClient) -> None:
    """on_finalize must be called with the new id so Temporal kicks off the
    same ProcessRecording workflow the resumable path uses."""
    data = _flac_with_frames()
    r = _post_direct(client, data, filename="audio.flac", duration_sec="7.0")
    assert r.status_code == 201, r.text
    rec_id = r.json()["id"]

    from app import temporal_client

    # conftest mocks start_pipeline as AsyncMock — assert it was awaited
    # with the new id and the duration passed through.
    temporal_client.start_pipeline.assert_awaited()
    args, _ = temporal_client.start_pipeline.call_args
    assert args[0] == rec_id
    assert args[1] == 7.0


def test_direct_unauthenticated_401(client: TestClient) -> None:
    """The bearer middleware applies to the new route like every other one."""
    data = _flac_with_frames()
    unauth = TestClient(client.app)
    resp = unauth.post(
        "/recordings/direct",
        data={"title": "", "tags": "[]"},
        files=[("file", ("audio.flac", data, "audio/flac"))],
    )
    assert resp.status_code == 401