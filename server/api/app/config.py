"""Server configuration loading (config.yaml + env overrides)."""

import os
from pathlib import Path

import yaml
from pydantic import BaseModel, Field


class StorageConfig(BaseModel):
    path: Path = Path("/storage")


class TranscribeConfig(BaseModel):
    backend: str = "local"  # local | api
    model: str = "small"
    base_url: str = ""
    api_key_env: str = ""


class SummarizeConfig(BaseModel):
    enabled: bool = False
    model: str = ""
    base_url: str = ""
    api_key_env: str = ""


class DiarizationConfig(BaseModel):
    enabled: bool = True
    endpoint: str = "http://diarization:80"


class DatabaseConfig(BaseModel):
    url: str = "postgresql+psycopg://transcripter:transcripter@postgres/transcripter"


class ProfilesConfig(BaseModel):
    path: Path = Path("/etc/transcripter/profiles")


class EmbedConfig(BaseModel):
    """Phase 3.5 — pluggable embedding backend for tag search (the
    speaches pattern; twin of worker/config.py EmbedConfig). The API
    embeds the SEARCH QUERY through the same backend the worker used to
    index segments, so query and index live in one vector space.

    ``local``: bge-m3 ONNX int8 in-process (fixed 1024-d; the compose
    mounts the models volume on the api too). ``http``: any
    OpenAI-compatible POST {base_url}/embeddings — no ONNX deps needed.
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
    """Optional Neo4j knowledge-graph backend (mirrors worker/config.py).

    The graph is OFF whenever ``uri`` is empty: every caller short-circuits
    to a clean error / skipped status, so the core pipeline stays alive when
    the compose ``graph`` profile is disabled or the section is missing.
    ``password_env`` names the env var holding the password (never the
    secret in yaml)."""

    uri: str = ""
    user: str = "neo4j"
    password_env: str = "NEO4J_PASSWORD"
    database: str = "neo4j"
    # Phase 3.5 — search-side embedding backend (same yaml section the
    # worker reads; the api only consumes backend/model_path/base_url/
    # model/api_key_env/dimensions).
    embed: EmbedConfig = Field(default_factory=EmbedConfig)

    @property
    def enabled(self) -> bool:
        return bool(self.uri)


class VaultConfig(BaseModel):
    """Obsidian-vault dir (mirrors worker config). The API only READS the
    vault (digests, indexes, exported folders incl. the moved audio when
    a recording's storage copy is gone) — the compose bind for api is
    read-only; the worker owns writes. Container path is FIXED
    (/transcripts)."""

    path: Path = Path("/transcripts")
    # Kept for parity with the worker's VaultConfig: the same yaml
    # section feeds both processes (sentinel is a worker-only knob).
    sentinel: str = ""


# Legacy name (pre-vault): the yaml section was `transcripts:`.
TranscriptsConfig = VaultConfig


class ServerConfig(BaseModel):
    storage: StorageConfig = Field(default_factory=StorageConfig)
    profiles: ProfilesConfig = Field(default_factory=ProfilesConfig)
    transcribe: TranscribeConfig = Field(default_factory=TranscribeConfig)
    summarize: SummarizeConfig = Field(default_factory=SummarizeConfig)
    diarization: DiarizationConfig = Field(default_factory=DiarizationConfig)
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    graph: GraphConfig = Field(default_factory=GraphConfig)
    vault: VaultConfig = Field(default_factory=VaultConfig)

    @property
    def recordings_root(self) -> Path:
        return self.storage.path / "recordings"


def load_config() -> ServerConfig:
    path = os.environ.get("TRANSCRIPTER_CONFIG", "/etc/transcripter/config.yaml")
    with open(path) as f:
        raw = yaml.safe_load(f) or {}
    # Legacy yaml `transcripts:` section (pre-vault naming) folds into
    # `vault:` — `vault:` keys win where both are present.
    if isinstance(raw.get("transcripts"), dict):
        merged = dict(raw["transcripts"])
        merged.update(raw.get("vault") or {})
        raw["vault"] = merged
    cfg = ServerConfig.model_validate(raw)
    if env_storage := os.environ.get("TRANSCRIPTER_STORAGE"):
        cfg.storage.path = Path(env_storage)
    if env_vault := os.environ.get("VAULT_DIR") or os.environ.get("TRANSCRIPTS_DIR"):
        cfg.vault.path = Path(env_vault)
    if env_db := os.environ.get("TRANSCRIPTER_DB_URL"):
        cfg.database.url = env_db
    if env_profiles := os.environ.get("PROFILES_DIR"):
        cfg.profiles.path = Path(env_profiles)
    # Keep /settings truthful when the worker's model comes from .env
    # (compose passes SUMMARIZE_MODEL to both services).
    if env_model := os.environ.get("SUMMARIZE_MODEL"):
        cfg.summarize.model = env_model
    # Phase 3.5 — same EMBED_* override pattern as the worker (compose
    # passes them from .env to both services): switching the embedding
    # provider never touches the mounted config.yaml.
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
    return cfg
