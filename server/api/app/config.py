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


class ServerConfig(BaseModel):
    storage: StorageConfig = Field(default_factory=StorageConfig)
    profiles: ProfilesConfig = Field(default_factory=ProfilesConfig)
    transcribe: TranscribeConfig = Field(default_factory=TranscribeConfig)
    summarize: SummarizeConfig = Field(default_factory=SummarizeConfig)
    diarization: DiarizationConfig = Field(default_factory=DiarizationConfig)
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)

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
    return cfg
