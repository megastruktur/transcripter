"""Resumable upload + catalog contract tests."""

import hashlib

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


def _upload_full(client: TestClient, data: bytes, chunk_size: int = 1024 * 256):
    r = client.post("/recordings", json={"title": "test", "total_bytes": len(data)})
    assert r.status_code == 201, r.text
    rec_id = r.json()["id"]

    offset = 0
    sent = 0
    while sent < len(data):
        chunk = data[sent : sent + chunk_size]
        r = client.put(
            f"/recordings/{rec_id}/audio",
            params={"offset": offset},
            content=chunk,
            headers={"content-length": str(len(chunk))},
        )
        assert r.status_code == 200, r.text
        offset = r.json()["committed"]
        sent += len(chunk)

    r = client.post(
        f"/recordings/{rec_id}/finalize",
        json={"sha256": hashlib.sha256(data).hexdigest(), "duration_sec": 12.5},
    )
    return rec_id, r


def test_create_returns_uuid_and_stages(client: TestClient) -> None:
    r = client.post("/recordings", json={"title": "x"})
    assert r.status_code == 201
    rid = r.json()["id"]
    assert len(rid) == 36
    detail = client.get(f"/recordings/{rid}").json()
    kinds = {s["kind"] for s in detail["stages"]}
    assert kinds == {"chunk", "transcribe", "diarize", "merge_speakers", "summarize"}


def test_invalid_uuid_rejected(client: TestClient) -> None:
    r = client.get("/recordings/../../etc/passwd")
    assert r.status_code in (400, 404)
    r = client.put("/recordings/not-a-uuid/audio", params={"offset": 0}, content=b"x")
    assert r.status_code == 400


def test_full_upload_and_finalize(client: TestClient) -> None:
    data = bytes(range(256)) * 4096  # 1 MiB
    rid, r = _upload_full(client, data)
    assert r.status_code == 200
    assert r.json()["state"] == "processing"

    detail = client.get(f"/recordings/{rid}").json()
    assert detail["duration_sec"] == 12.5
    assert detail["committed_bytes"] == len(data)


def test_resume_from_committed_offset(client: TestClient) -> None:
    data = b"0123456789abcdef" * 1024
    r = client.post("/recordings", json={})
    rid = r.json()["id"]

    first = data[:4096]
    r = client.put(
        f"/recordings/{rid}/audio",
        params={"offset": 0},
        content=first,
        headers={"content-length": str(len(first))},
    )
    committed = r.json()["committed"]
    assert committed == 4096

    # Client resumes: sends overlap + rest; server discards overlap.
    overlap = data[2048:]  # starts 2048 bytes before committed
    r = client.put(
        f"/recordings/{rid}/audio",
        params={"offset": 2048},
        content=overlap,
        headers={"content-length": str(len(overlap))},
    )
    assert r.json()["committed"] == len(data)

    r = client.post(
        f"/recordings/{rid}/finalize",
        json={"sha256": hashlib.sha256(data).hexdigest()},
    )
    assert r.status_code == 200, r.text


def test_finalize_bad_hash_409(client: TestClient) -> None:
    data = b"abc" * 100
    r0 = client.post("/recordings", json={})
    rid = r0.json()["id"]
    r = client.put(
        f"/recordings/{rid}/audio",
        params={"offset": 0},
        content=data,
        headers={"content-length": str(len(data))},
    )
    assert r.status_code == 200
    bad = "0" * 64
    r = client.post(f"/recordings/{rid}/finalize", json={"sha256": bad})
    assert r.status_code == 409


def test_finalize_without_audio_409(client: TestClient) -> None:
    r = client.post("/recordings", json={})
    rid = r.json()["id"]
    r = client.post(f"/recordings/{rid}/finalize", json={"sha256": "0" * 64})
    assert r.status_code == 409
    assert "no audio" in r.json()["detail"]


def test_offset_out_of_range_409(client: TestClient) -> None:
    r = client.post("/recordings", json={})
    rid = r.json()["id"]
    r = client.put(
        f"/recordings/{rid}/audio",
        params={"offset": 100},
        content=b"x",
        headers={"content-length": "1"},
    )
    assert r.status_code == 409


def test_chunk_too_large_413(client: TestClient) -> None:
    r = client.post("/recordings", json={})
    rid = r.json()["id"]
    # 17MB > 16MB limit — send header only; body intentionally smaller.
    r = client.put(
        f"/recordings/{rid}/audio",
        params={"offset": 0},
        content=b"x",
        headers={"content-length": str(17 * 1024 * 1024)},
    )
    assert r.status_code == 413


def test_list_and_delete(client: TestClient) -> None:
    data = b"xyz" * 512
    rid, r = _upload_full(client, data)
    assert r.status_code == 200

    lst = client.get("/recordings").json()
    assert any(item["id"] == rid for item in lst["items"])

    r = client.delete(f"/recordings/{rid}")
    assert r.status_code == 204
    assert client.get(f"/recordings/{rid}").status_code == 404


def _flac(payload: bytes = b"") -> bytes:
    """fLaC + a last-block STREAMINFO header, then `payload` as frame bytes."""
    streaminfo = bytes([0x80, 0x00, 0x00, 0x22]) + bytes(34)
    return b"fLaC" + streaminfo + payload


def test_finalize_rejects_flac_without_audio_frames(client: TestClient) -> None:
    """A capture that recorded no samples must not enter the pipeline."""
    rid, r = _upload_full(client, _flac())
    assert r.status_code == 422, r.text
    assert "no audio frames" in r.json()["detail"]
    # Unrecoverable, so it must not sit in `uploading` inviting retries.
    assert client.get(f"/recordings/{rid}").json()["state"] == "failed"


def test_finalize_accepts_flac_with_audio_frames(client: TestClient) -> None:
    rid, r = _upload_full(client, _flac(b"\xff\xf8frame-bytes"))
    assert r.status_code == 200, r.text
    assert client.get(f"/recordings/{rid}").json()["state"] == "processing"


def test_finalize_accepts_non_flac_payload(client: TestClient) -> None:
    """Container validation belongs to the decoder, not the upload layer."""
    _, r = _upload_full(client, b"not-a-flac-at-all")
    assert r.status_code == 200, r.text
