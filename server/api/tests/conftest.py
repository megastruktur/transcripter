"""Test environment: defaults before any app import; Temporal mocked out."""

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

REPO_CONFIG = Path(__file__).resolve().parents[2] / "config.example.yaml"


@pytest.fixture(autouse=True)
def _test_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("TRANSCRIPTER_CONFIG", str(REPO_CONFIG))
    monkeypatch.setenv("TRANSCRIPTER_DB_URL", f"sqlite:///{tmp_path}/test.db")
    monkeypatch.setenv("TRANSCRIPTER_STORAGE", str(tmp_path / "storage"))

    from app import temporal_client

    monkeypatch.setattr(temporal_client, "start_pipeline", AsyncMock(return_value="wf-test"))
    monkeypatch.setattr(temporal_client, "regenerate_stage", AsyncMock(return_value="wf-test"))
    yield
