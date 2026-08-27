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


class WorkerConfig(BaseModel):
    transcribe: TranscribeConfig = Field(default_factory=TranscribeConfig)
    summarize: SummarizeConfig = Field(default_factory=SummarizeConfig)
    diarization: DiarizationConfig = Field(default_factory=DiarizationConfig)
    chunk: ChunkConfig = Field(default_factory=ChunkConfig)
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    transcripts: TranscriptsConfig = Field(default_factory=TranscriptsConfig)
    profiles: ProfilesConfig = Field(default_factory=ProfilesConfig)

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
    if env_profiles := os.environ.get("PROFILES_DIR"):
        cfg.profiles.path = Path(env_profiles)
    return cfg

