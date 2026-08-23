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


class DiarizationConfig(BaseModel):
    enabled: bool = True
    endpoint: str = "http://diarization:80"


class DatabaseConfig(BaseModel):
    url: str = "postgresql+psycopg://transcripter:transcripter@postgres/transcripter"


class StorageConfig(BaseModel):
    path: Path = Path("/storage")


class WorkerConfig(BaseModel):
    transcribe: TranscribeConfig = Field(default_factory=TranscribeConfig)
    summarize: SummarizeConfig = Field(default_factory=SummarizeConfig)
    diarization: DiarizationConfig = Field(default_factory=DiarizationConfig)
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)

    @property
    def recordings_root(self) -> Path:
        return self.storage.path / "recordings"


def load_config() -> WorkerConfig:
    path = os.environ.get("TRANSCRIPTER_CONFIG", "/etc/transcripter/config.yaml")
    with open(path) as f:
        raw = yaml.safe_load(f) or {}
    cfg = WorkerConfig.model_validate(raw)
    if cfg.transcribe.backend == "api" and not cfg.transcribe.base_url:
        raise ValueError("transcribe.backend=api requires transcribe.base_url (OpenAI-compatible URL incl. /v1)")
    if env_storage := os.environ.get("TRANSCRIPTER_STORAGE"):
        cfg.storage.path = Path(env_storage)
    if env_db := os.environ.get("TRANSCRIPTER_DB_URL"):
        cfg.database.url = env_db
    if env_diar := os.environ.get("DIARIZATION_ENDPOINT"):
        cfg.diarization.endpoint = env_diar
    return cfg
