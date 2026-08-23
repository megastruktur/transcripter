"""Settings route contract: shape, masking, diarization.enabled exposure."""

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("TRANSCRIPTER_TOKEN", "test-token")
    from app.main import app

    return TestClient(app)


def test_settings_shape_and_masking(client: TestClient) -> None:
    r = client.get("/settings", headers={"authorization": "Bearer test-token"})
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"transcribe", "summarize", "diarization"}
    # example config: backend local, base_url empty -> masked to ""
    assert body["transcribe"]["backend"] == "local"
    assert body["transcribe"]["base_url"] == ""
    # enabled flag exposed for the UI
    assert body["diarization"]["enabled"] is True
    assert body["diarization"]["endpoint"] == "http://diarization:80"


def test_settings_masks_nonempty_base_url(client: TestClient) -> None:
    from app.config import load_config

    original = client.app.state.config
    cfg = load_config()
    cfg.transcribe.base_url = "http://speaches:8000/v1"
    client.app.state.config = cfg
    try:
        r = client.get("/settings", headers={"authorization": "Bearer test-token"})
        assert r.json()["transcribe"]["base_url"] == "***"
    finally:
        client.app.state.config = original
