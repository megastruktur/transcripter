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


# --- Phase 2.5: embedding prefilter knobs -------------------------------------


def test_graph_embed_defaults(tmp_path, monkeypatch):
    """Phase 2.5 knobs (prefilter on, tau 0.90/0.75) + Phase 3.5 embed.*
    defaults: local backend, model at /models/bge-m3-int8."""
    cfg_path = _write(tmp_path, "transcribe:\n  backend: local\n")
    monkeypatch.setenv("TRANSCRIPTER_CONFIG", str(cfg_path))
    cfg = load_config()
    assert cfg.graph.embed_enabled is True
    assert cfg.graph.embed.backend == "local"
    assert str(cfg.graph.embed.model_path) == "/models/bge-m3-int8"
    assert cfg.graph.embed.configured_dimensions == 1024
    assert cfg.graph.embed_tau_high == 0.90
    assert cfg.graph.embed_tau_low == 0.75


def test_graph_embed_from_yaml(tmp_path, monkeypatch):
    """Phase 3.5 clean cutover: embed section carries backend/model_path
    (the flat embed_model_path key is gone), taus stay flat."""
    cfg_path = _write(
        tmp_path,
        "graph:\n"
        "  uri: bolt://neo4j:7687\n"
        "  embed_enabled: false\n"
        "  embed:\n"
        "    backend: local\n"
        "    model_path: /data/my-model\n"
        "  embed_tau_high: 0.95\n"
        "  embed_tau_low: 0.80\n",
    )
    monkeypatch.setenv("TRANSCRIPTER_CONFIG", str(cfg_path))
    cfg = load_config()
    assert cfg.graph.embed_enabled is False
    assert str(cfg.graph.embed.model_path) == "/data/my-model"
    assert cfg.graph.embed_tau_high == 0.95
    assert cfg.graph.embed_tau_low == 0.80


# --- Phase 3: summarize recap knob ----------------------------------------------


def test_summarize_recap_default_on(tmp_path, monkeypatch):
    """recap defaults to True — needs graph.enabled + a tag to fire."""
    cfg_path = _write(tmp_path, "transcribe:\n  backend: local\n")
    monkeypatch.setenv("TRANSCRIPTER_CONFIG", str(cfg_path))
    cfg = load_config()
    assert cfg.summarize.recap is True


def test_summarize_recap_from_yaml(tmp_path, monkeypatch):
    cfg_path = _write(
        tmp_path,
        "summarize:\n  enabled: true\n  model: m\n  base_url: http://x/v1\n  recap: false\n",
    )
    monkeypatch.setenv("TRANSCRIPTER_CONFIG", str(cfg_path))
    cfg = load_config()
    assert cfg.summarize.recap is False


# --- SUMMARIZE_MODEL env override ------------------------------------------------


def test_summarize_model_env_wins_over_yaml(tmp_path, monkeypatch):
    """SUMMARIZE_MODEL (compose passes it from .env) overrides config.yaml —
    same priority pattern as DIARIZATION_ENDPOINT."""
    cfg_path = _write(
        tmp_path,
        "summarize:\n  enabled: true\n  model: yaml-model\n  base_url: http://x/v1\n",
    )
    monkeypatch.setenv("TRANSCRIPTER_CONFIG", str(cfg_path))
    monkeypatch.setenv("SUMMARIZE_MODEL", "env-model")
    cfg = load_config()
    assert cfg.summarize.model == "env-model"


def test_summarize_model_env_empty_keeps_yaml(tmp_path, monkeypatch):
    """Unset/empty env (compose default) keeps the yaml value effective."""
    cfg_path = _write(
        tmp_path,
        "summarize:\n  enabled: true\n  model: yaml-model\n  base_url: http://x/v1\n",
    )
    monkeypatch.setenv("TRANSCRIPTER_CONFIG", str(cfg_path))
    monkeypatch.setenv("SUMMARIZE_MODEL", "")
    cfg = load_config()
    assert cfg.summarize.model == "yaml-model"


# --- Phase 3.5: pluggable embed backend -----------------------------------------


def test_graph_embed_http_from_yaml(tmp_path, monkeypatch):
    cfg_path = _write(
        tmp_path,
        "graph:\n"
        "  uri: bolt://neo4j:7687\n"
        "  embed:\n"
        "    backend: http\n"
        "    base_url: http://infinity:7000/v1\n"
        "    model: bge-m3\n"
        "    api_key_env: EMBED_API_KEY\n"
        "    dimensions: 1024\n",
    )
    monkeypatch.setenv("TRANSCRIPTER_CONFIG", str(cfg_path))
    cfg = load_config()
    assert cfg.graph.embed.backend == "http"
    assert cfg.graph.embed.base_url == "http://infinity:7000/v1"
    assert cfg.graph.embed.model == "bge-m3"
    assert cfg.graph.embed.api_key_env == "EMBED_API_KEY"
    assert cfg.graph.embed.configured_dimensions == 1024


def test_graph_embed_bad_backend_fails_fast(tmp_path, monkeypatch):
    cfg_path = _write(
        tmp_path,
        "graph:\n  embed:\n    backend: quantum\n",
    )
    monkeypatch.setenv("TRANSCRIPTER_CONFIG", str(cfg_path))
    with pytest.raises(ValueError, match="backend"):
        load_config()


def test_graph_embed_http_without_base_url_fails(tmp_path, monkeypatch):
    cfg_path = _write(
        tmp_path,
        "graph:\n  embed:\n    backend: http\n",
    )
    monkeypatch.setenv("TRANSCRIPTER_CONFIG", str(cfg_path))
    with pytest.raises(ValueError, match="base_url"):
        load_config()


def test_embed_backend_env_overrides_yaml(tmp_path, monkeypatch):
    """EMBED_BACKEND/EMBED_BASE_URL/EMBED_MODEL (compose from .env) win
    over config.yaml — same pattern as SUMMARIZE_MODEL."""
    cfg_path = _write(
        tmp_path,
        "graph:\n"
        "  embed:\n"
        "    backend: local\n"
        "    model_path: /models/bge-m3-int8\n",
    )
    monkeypatch.setenv("TRANSCRIPTER_CONFIG", str(cfg_path))
    monkeypatch.setenv("EMBED_BACKEND", "http")
    monkeypatch.setenv("EMBED_BASE_URL", "http://litellm:4000/v1")
    monkeypatch.setenv("EMBED_MODEL", "bge-m3")
    cfg = load_config()
    assert cfg.graph.embed.backend == "http"
    assert cfg.graph.embed.base_url == "http://litellm:4000/v1"
    assert cfg.graph.embed.model == "bge-m3"


def test_embed_backend_env_empty_keeps_yaml(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "TRANSCRIPTER_CONFIG",
        _write(
            tmp_path,
            "graph:\n  embed:\n    backend: http\n    base_url: http://x/v1\n",
        ),
    )
    monkeypatch.setenv("EMBED_BACKEND", "")
    monkeypatch.setenv("EMBED_BASE_URL", "")
    monkeypatch.setenv("EMBED_MODEL", "")
    cfg = load_config()
    assert cfg.graph.embed.backend == "http"
    assert cfg.graph.embed.base_url == "http://x/v1"


def test_embed_backend_env_bad_value_fails(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "TRANSCRIPTER_CONFIG",
        _write(tmp_path, "graph:\n  embed:\n    backend: local\n"),
    )
    monkeypatch.setenv("EMBED_BACKEND", "quantum")
    with pytest.raises(ValueError, match="EMBED_BACKEND"):
        load_config()


def test_embed_backend_env_http_without_base_url_fails(tmp_path, monkeypatch):
    """Env switches backend to http but no base_url anywhere → loud."""
    monkeypatch.setenv(
        "TRANSCRIPTER_CONFIG",
        _write(tmp_path, "graph:\n  embed:\n    backend: local\n"),
    )
    monkeypatch.setenv("EMBED_BACKEND", "http")
    monkeypatch.delenv("EMBED_BASE_URL", raising=False)
    with pytest.raises(ValueError, match="base_url"):
        load_config()
