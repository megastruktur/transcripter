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


def test_regenerate_starts_workflow(client: TestClient) -> None:
    rid = _make_recording(client)
    # Move out of uploading: fake finalize via direct upload
    data = b"q" * 64
    r = client.put(
        f"/recordings/{rid}/audio",
        params={"offset": 0},
        content=data,
        headers={"content-length": str(len(data))},
    )
    assert r.status_code == 200

    r = client.post(
        f"/recordings/{rid}/finalize",
        json={"sha256": hashlib.sha256(data).hexdigest(), "duration_sec": 1.0},
    )
    assert r.status_code == 200

    with patch("app.temporal_client.regenerate_stage", new_callable=AsyncMock) as m:
        m.return_value = "wf-123"
        r = client.post(f"/recordings/{rid}/regenerate", json={"stage": "diarize"})
    assert r.status_code == 200
    assert r.json()["workflow_id"] == "wf-123"
    m.assert_awaited_once()


def test_regenerate_temporal_down_503(client: TestClient) -> None:
    rid = _make_recording(client)
    data = b"w" * 32
    client.put(
        f"/recordings/{rid}/audio",
        params={"offset": 0},
        content=data,
        headers={"content-length": str(len(data))},
    )
    client.post(
        f"/recordings/{rid}/finalize",
        json={"sha256": hashlib.sha256(data).hexdigest()},
    )

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
