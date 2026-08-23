"""Auth middleware contract tests (roborev T1 review: lock in before T2)."""

import importlib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


def _load_app(monkeypatch: pytest.MonkeyPatch) -> object:
    import os

    if "TRANSCRIPTER_TOKEN" not in os.environ:
        monkeypatch.setenv("TRANSCRIPTER_TOKEN", "")
    from app import main

    return importlib.reload(main)


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("TRANSCRIPTER_TOKEN", "sekrit")
    main = _load_app(monkeypatch)
    return TestClient(main.app)


def test_health_public_without_token(client: TestClient) -> None:
    assert client.get("/health").status_code == 200


def test_protected_requires_token(client: TestClient) -> None:
    assert client.get("/recordings").status_code == 401


def test_protected_rejects_wrong_token(client: TestClient) -> None:
    r = client.get("/recordings", headers={"authorization": "Bearer wrong"})
    assert r.status_code == 401


def test_protected_accepts_valid_token(client: TestClient) -> None:
    r = client.get("/recordings", headers={"authorization": "Bearer sekrit"})
    # Route exists now (T2) — list of recordings, possibly empty.
    assert r.status_code == 200


def test_cors_preflight_bypasses_auth(client: TestClient) -> None:
    # Browsers/webviews never send Authorization on preflight; a 401 here makes
    # every cross-origin client unable to reach the API at all.
    r = client.options(
        "/recordings",
        headers={
            "origin": "http://localhost:5173",
            "access-control-request-method": "GET",
            "access-control-request-headers": "authorization",
        },
    )
    assert r.status_code == 200
    assert r.headers["access-control-allow-origin"] == "http://localhost:5173"


def test_no_token_env_allows_all(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TRANSCRIPTER_TOKEN", raising=False)
    main = _load_app(monkeypatch)
    c = TestClient(main.app)
    assert c.get("/recordings").status_code == 200  # auth disabled


def test_missing_config_fails_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRANSCRIPTER_TOKEN", "sekrit")
    monkeypatch.setenv("TRANSCRIPTER_CONFIG", "/nonexistent/config.yaml")
    from app import main

    with pytest.raises(SystemExit, match="not found"):
        importlib.reload(main)


def test_config_directory_fails_fast(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("TRANSCRIPTER_TOKEN", "sekrit")
    monkeypatch.setenv("TRANSCRIPTER_CONFIG", str(tmp_path))
    from app import main

    with pytest.raises(SystemExit, match="is a directory"):
        importlib.reload(main)
