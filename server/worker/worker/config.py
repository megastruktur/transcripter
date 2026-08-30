"""Shared config for worker (subset of API config; see server/api/app/config.py)."""

import os
from pathlib import Path

import yaml
from pydantic import BaseModel, Field


class TranscribeConfig(BaseModel):
    backend: str = "local"
    model: str = "small"
    base_url: str = ""
    api_key_env: str = ""


class SummarizeConfig(BaseModel):
    enabled: bool = False
    model: str = ""
    base_url: str = ""
    api_key_env: str = ""
    # Phase 3: when ON (and the graph is enabled and the recording carries
    # a tag), build_recap reads the tag's digest note and injects it into
    # the summarize prompt as prior context. No digest notes are ever
    # written without the graph, so recap without graph.enabled is a no-op.
    recap: bool = True
    # Recap-retrieval tail: KNN over the tag's Phase 3.5 semantic index
    # (segments of OTHER recordings) appended to the digest note. k hits,
    # budget caps the rendered block. Degradation is graceful: missing
    # index/backend → digest-only recap, never a stage failure.
    recap_k: int = 6
    recap_budget_chars: int = 1600


class DiarizationConfig(BaseModel):
    enabled: bool = True
    endpoint: str = "http://diarization:80"


class ChunkConfig(BaseModel):
    # OFF by default: short recordings gain nothing from slicing. Enable on
    # CPU voice stacks where a single long request can hit the whisper
    # repetition loop (see worker/chunk.py docstring).
    enabled: bool = False
    target_min: float = 10.0  # target chunk length
    overlap_sec: float = 2.0  # shared band between neighbours (seam safety)


class DatabaseConfig(BaseModel):
    url: str = "postgresql+psycopg://transcripter:transcripter@postgres/transcripter"


class StorageConfig(BaseModel):
    path: Path = Path("/storage")


class TranscriptsConfig(BaseModel):
    """Note-export settings. Container path is FIXED (/transcripts): the only
    host-side knob is the compose bind source (TRANSCRIPTS_DIR in .env) — a
    divergent in-container override would silently write into the container
    layer and be lost on recreate."""

    path: Path = Path("/transcripts")
    # Boot-race guard, e.g. ".transcripter": when set, export refuses to run
    # unless this entry exists under path (empty-mountpoint detection).
    sentinel: str = ""


class ProfilesConfig(BaseModel):
    """Profile loader root (wave A — yaml knowledge-graph profiles)."""

    path: Path = Path("/etc/transcripter/profiles")

class EmbedConfig(BaseModel):
    """Phase 3.5 — pluggable embedding backend (speaches pattern).

    ``local`` (default): the phase-2.5 bge-m3 ONNX int8 model in-process
    in the worker (fixed 1024-d; ``model_path`` is the export dir).
    ``http``: any OpenAI-compatible ``POST {base_url}/v1/embeddings``
    (LiteLLM route, Infinity, Ollama, OpenAI) — ``model`` and
    ``api_key_env`` name the remote model and the env var with the key,
    ``dimensions`` is required (the local export's 1024 is not knowable
    from a remote endpoint).

    Migration (2026-08-29): the flat phase-2.5 keys ``embed_enabled``,
    ``embed_model_path`` moved here as ``enabled``/``model_path``; the
    tau thresholds stay on GraphConfig (they calibrate DEDUP decisions,
    not the embedder). Switching the backend or model requires
    re-indexing + re-calibrating tau — index files carry {backend,
    model, dimensions} meta so mismatches are caught, not mixed.
    """

    backend: str = "local"
    model_path: Path = Path("/models/bge-m3-int8")
    # http backend
    base_url: str = ""
    model: str = ""
    api_key_env: str = ""
    dimensions: int = 0

    @property
    def configured_dimensions(self) -> int:
        """Local export dimensionality is fixed by the bge-m3 CLS pool;
        http backends report what the config declares (0 = unset)."""
        return 1024 if self.backend == "local" else self.dimensions


class GraphConfig(BaseModel):
    """Optional Neo4j knowledge-graph backend (wave B).

    The graph is OFF whenever ``uri`` is empty: every caller short-circuits
    to a clean ``skipped`` status, so the core pipeline stays alive when
    the compose ``graph`` profile is disabled or the section is missing.
    ``password_env`` names the environment variable holding the password —
    we never put the secret in config.yaml (it would land in the git
    history and the container's environment dump).
    """

    uri: str = ""
    user: str = "neo4j"
    password_env: str = "NEO4J_PASSWORD"
    database: str = "neo4j"
    # Periodic graph GC interval in seconds (0 = off): every tick a
    # Temporal Schedule runs the GraphGc workflow, which deletes graph
    # nodes whose recording no longer exists in the catalog. The first
    # scheduled run naturally cleans any backlog.
    gc_interval_sec: int = 0
    # Phase 2: when NO profile matches the recording's type, enrich with
    # the built-in fallback prompt (minimal generic ontology) instead of
    # skipping. A profile that MATCHED but has no enrich section still
    # means opted out — only the no-match case falls back.
    enrich_all: bool = True
    # Phase 2: after a successful enrich, auto-refresh each affected
    # namespace's digest note (digests/<slug>.md) when it is older than
    # this window (or missing). A window of 0 means "refresh on every
    # enrich run"; switch auto_digest off to stop auto-refresh entirely.
    auto_digest: bool = True
    auto_digest_window_sec: int = 3600
    # Phase 2.5 — bge-m3 embedding prefilter for entity dedup. When ON
    # and the ONNX model loads, slug collisions with cosine >=
    # embed_tau_high are auto-merged, cosine <= embed_tau_low auto-split,
    # and only the gray zone reaches the LLM Y/N call. Vectors ride on
    # entity nodes (embedding property + embedding_bge_m3 vector index).
    # A missing model path or failed ONNX session latches the prefilter
    # OFF for the process (one warning) and dedup behaves exactly as
    # before — embeddings never crash the stage.
    embed_enabled: bool = True
    embed_tau_high: float = 0.90
    embed_tau_low: float = 0.75
    # Phase 3.5: backend selection + model coordinates live here; the
    # flat embed_model_path migrated into embed.model_path.
    embed: EmbedConfig = Field(default_factory=EmbedConfig)

    @property
    def enabled(self) -> bool:
        return bool(self.uri)


class WorkerConfig(BaseModel):
    transcribe: TranscribeConfig = Field(default_factory=TranscribeConfig)
    summarize: SummarizeConfig = Field(default_factory=SummarizeConfig)
    diarization: DiarizationConfig = Field(default_factory=DiarizationConfig)
    chunk: ChunkConfig = Field(default_factory=ChunkConfig)
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    transcripts: TranscriptsConfig = Field(default_factory=TranscriptsConfig)
    profiles: ProfilesConfig = Field(default_factory=ProfilesConfig)
    graph: GraphConfig = Field(default_factory=GraphConfig)

    @property
    def recordings_root(self) -> Path:
        return self.storage.path / "recordings"


def load_config() -> WorkerConfig:
    path = os.environ.get("TRANSCRIPTER_CONFIG", "/etc/transcripter/config.yaml")
    with open(path) as f:
        raw = yaml.safe_load(f) or {}
    cfg = WorkerConfig.model_validate(raw)
    if cfg.transcribe.backend == "api" and not cfg.transcribe.base_url:
        raise ValueError(
            "transcribe.backend=api requires transcribe.base_url (OpenAI-compatible URL incl. /v1)"
        )
    # Phase 3.5: backend must be one of the two implementations, and the
    # http one needs an endpoint (env overrides re-checked after they run).
    if cfg.graph.embed.backend not in ("local", "http"):
        raise ValueError(
            f"graph.embed.backend must be 'local' or 'http' (got {cfg.graph.embed.backend!r})"
        )
    if env_storage := os.environ.get("TRANSCRIPTER_STORAGE"):
        cfg.storage.path = Path(env_storage)
    if env_db := os.environ.get("TRANSCRIPTER_DB_URL"):
        cfg.database.url = env_db
    if env_diar := os.environ.get("DIARIZATION_ENDPOINT"):
        cfg.diarization.endpoint = env_diar
    # Same priority pattern as DIARIZATION_ENDPOINT: a value set in the
    # environment (compose passes SUMMARIZE_MODEL from .env) wins over
    # config.yaml; unset/empty keeps the yaml value effective.
    if env_model := os.environ.get("SUMMARIZE_MODEL"):
        cfg.summarize.model = env_model
    if env_profiles := os.environ.get("PROFILES_DIR"):
        cfg.profiles.path = Path(env_profiles)
    # Phase 3.5: same pattern as SUMMARIZE_MODEL — compose passes
    # EMBED_BACKEND/EMBED_BASE_URL/EMBED_MODEL from .env so switching the
    # embedding provider never touches the mounted config.yaml.
    if env_backend := os.environ.get("EMBED_BACKEND"):
        if env_backend not in ("local", "http"):
            raise ValueError(
                f"EMBED_BACKEND must be 'local' or 'http' (got {env_backend!r})"
            )
        cfg.graph.embed.backend = env_backend
    if env_base := os.environ.get("EMBED_BASE_URL"):
        cfg.graph.embed.base_url = env_base
    if env_embed_model := os.environ.get("EMBED_MODEL"):
        cfg.graph.embed.model = env_embed_model
    # Post-override consistency: an env-driven backend switch to http must
    # still carry a base_url (the yaml check above ran before overrides).
    if cfg.graph.embed.backend == "http" and not cfg.graph.embed.base_url:
        raise ValueError(
            "graph.embed.backend=http requires graph.embed.base_url "
            "(config.yaml or EMBED_BASE_URL)"
        )
    return cfg
