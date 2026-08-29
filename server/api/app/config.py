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

    @property
    def enabled(self) -> bool:
        return bool(self.uri)


class TranscriptsConfig(BaseModel):
    """Note-export dir (mirrors worker config). The API only READS the
    exported notes (digests) — the compose bind for api is read-only;
    the worker owns writes. Container path is FIXED (/transcripts)."""

    path: Path = Path("/transcripts")
    # Kept for parity with the worker's TranscriptsConfig: the same yaml
    # section feeds both processes (sentinel is a worker-only knob).
    sentinel: str = ""


class ServerConfig(BaseModel):
    storage: StorageConfig = Field(default_factory=StorageConfig)
    profiles: ProfilesConfig = Field(default_factory=ProfilesConfig)
    transcribe: TranscribeConfig = Field(default_factory=TranscribeConfig)
    summarize: SummarizeConfig = Field(default_factory=SummarizeConfig)
    diarization: DiarizationConfig = Field(default_factory=DiarizationConfig)
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    graph: GraphConfig = Field(default_factory=GraphConfig)
    transcripts: TranscriptsConfig = Field(default_factory=TranscriptsConfig)

    @property
    def recordings_root(self) -> Path:
        return self.storage.path / "recordings"


def load_config() -> ServerConfig:
    path = os.environ.get("TRANSCRIPTER_CONFIG", "/etc/transcripter/config.yaml")
    with open(path) as f:
        raw = yaml.safe_load(f) or {}
    cfg = ServerConfig.model_validate(raw)
    if env_storage := os.environ.get("TRANSCRIPTER_STORAGE"):
        cfg.storage.path = Path(env_storage)
    if env_db := os.environ.get("TRANSCRIPTER_DB_URL"):
        cfg.database.url = env_db
    if env_profiles := os.environ.get("PROFILES_DIR"):
        cfg.profiles.path = Path(env_profiles)
    # Keep /settings truthful when the worker's model comes from .env
    # (compose passes SUMMARIZE_MODEL to both services).
    if env_model := os.environ.get("SUMMARIZE_MODEL"):
        cfg.summarize.model = env_model
    return cfg
