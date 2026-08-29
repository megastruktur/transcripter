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


def test_transcripts_defaults(tmp_path, monkeypatch):
    cfg_path = _write(tmp_path, "transcribe:\n  backend: local\n  model: small\n")
    monkeypatch.setenv("TRANSCRIPTER_CONFIG", str(cfg_path))
    cfg = load_config()
    assert str(cfg.transcripts.path) == "/transcripts"
    assert cfg.transcripts.sentinel == ""


def test_transcripts_sentinel_from_yaml(tmp_path, monkeypatch):
    cfg_path = _write(
        tmp_path,
        "transcripts:\n  sentinel: '.obsidian'\n",
    )
    monkeypatch.setenv("TRANSCRIPTER_CONFIG", str(cfg_path))
    cfg = load_config()
    assert cfg.transcripts.sentinel == ".obsidian"
    assert str(cfg.transcripts.path) == "/transcripts"


def test_graph_gc_interval_default_off(tmp_path, monkeypatch):
    """gc_interval_sec defaults to 0 (schedule never registered)."""
    cfg_path = _write(tmp_path, "transcribe:\n  backend: local\n")
    monkeypatch.setenv("TRANSCRIPTER_CONFIG", str(cfg_path))
    cfg = load_config()
    assert cfg.graph.gc_interval_sec == 0


def test_graph_gc_interval_from_yaml(tmp_path, monkeypatch):
    cfg_path = _write(
        tmp_path,
        "graph:\n  uri: bolt://neo4j:7687\n  gc_interval_sec: 3600\n",
    )
    monkeypatch.setenv("TRANSCRIPTER_CONFIG", str(cfg_path))
    cfg = load_config()
    assert cfg.graph.gc_interval_sec == 3600
    assert cfg.graph.enabled is True


def test_graph_phase2_defaults(tmp_path, monkeypatch):
    """Phase 2: enrich_all and auto_digest default ON, window 3600."""
    cfg_path = _write(tmp_path, "transcribe:\n  backend: local\n")
    monkeypatch.setenv("TRANSCRIPTER_CONFIG", str(cfg_path))
    cfg = load_config()
    assert cfg.graph.enrich_all is True
    assert cfg.graph.auto_digest is True
    assert cfg.graph.auto_digest_window_sec == 3600


def test_graph_phase2_from_yaml(tmp_path, monkeypatch):
    cfg_path = _write(
        tmp_path,
        "graph:\n"
        "  uri: bolt://neo4j:7687\n"
        "  enrich_all: false\n"
        "  auto_digest: false\n"
        "  auto_digest_window_sec: 60\n",
    )
    monkeypatch.setenv("TRANSCRIPTER_CONFIG", str(cfg_path))
    cfg = load_config()
    assert cfg.graph.enrich_all is False
    assert cfg.graph.auto_digest is False
    assert cfg.graph.auto_digest_window_sec == 60
