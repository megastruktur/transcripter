"""Config loading: fail-fast on backend=api without base_url."""

from pathlib import Path

import pytest

from worker.config import load_config


def _write(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "config.yaml"
    p.write_text(body)
    return p


def test_api_backend_without_base_url_fails_fast(tmp_path, monkeypatch):
    cfg_path = _write(
        tmp_path,
        "transcribe:\n  backend: api\n  model: Systran/faster-whisper-small\n",
    )
    monkeypatch.setenv("TRANSCRIPTER_CONFIG", str(cfg_path))
    with pytest.raises(ValueError, match="base_url"):
        load_config()


def test_api_backend_with_base_url_ok(tmp_path, monkeypatch):
    cfg_path = _write(
        tmp_path,
        "transcribe:\n"
        "  backend: api\n"
        '  base_url: "http://speaches:8000/v1"\n'
        "  model: Systran/faster-whisper-small\n",
    )
    monkeypatch.setenv("TRANSCRIPTER_CONFIG", str(cfg_path))
    cfg = load_config()
    assert cfg.transcribe.backend == "api"


def test_local_backend_needs_no_base_url(tmp_path, monkeypatch):
    cfg_path = _write(tmp_path, "transcribe:\n  backend: local\n  model: small\n")
    monkeypatch.setenv("TRANSCRIPTER_CONFIG", str(cfg_path))
    cfg = load_config()
    assert cfg.transcribe.backend == "local"
