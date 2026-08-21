"""Temporal activities for the recording pipeline."""

import logging
import os
from datetime import timedelta
from pathlib import Path

from temporalio import activity

from .config import WorkerConfig, load_config
from .db import (
    Recording,
    RecordingState,
    StageStatus,
    session,
    set_stage,
)
from .transcribe import (
    ApiTranscriber,
    LocalTranscriber,
    segments_to_markdown,
)

log = logging.getLogger("transcripter.activities")

_cfg: WorkerConfig | None = None
_local: LocalTranscriber | None = None
_api: ApiTranscriber | None = None


def cfg() -> WorkerConfig:
    global _cfg
    if _cfg is None:
        _cfg = load_config()
    return _cfg


def meta_dir(rec_id: str) -> Path:
    return cfg().recordings_root / rec_id / "meta"


def audio_file(rec_id: str) -> Path:
    return cfg().recordings_root / rec_id / "audio.flac"


@activity.defn
async def transcribe(rec_id: str) -> dict:
    c = cfg()
    set_stage(rec_id, "transcribe", StageStatus.running, inc_attempts=True)
    try:
        if c.transcribe.backend == "api":
            global _api
            if _api is None:
                key = os.environ.get(c.transcribe.api_key_env, "")
                _api = ApiTranscriber(c.transcribe.base_url, c.transcribe.model, key)
            result = _api.transcribe(audio_file(rec_id))
        else:
            global _local
            if _local is None or _local.model_name != c.transcribe.model:
                _local = LocalTranscriber(c.transcribe.model)
            result = _local.transcribe(audio_file(rec_id))

        result.to_json(meta_dir(rec_id) / "segments.json")
        segments_to_markdown(result, meta_dir(rec_id) / "transcript.md")
        details = {"language": result.language, "segments": len(result.segments)}
        set_stage(rec_id, "transcribe", StageStatus.done, details=details)
        return details
    except Exception as e:
        log.exception("transcribe failed for %s", rec_id)
        set_stage(rec_id, "transcribe", StageStatus.failed, error=str(e))
        raise


@activity.defn
async def diarize(rec_id: str) -> dict:
    set_stage(rec_id, "diarize", StageStatus.running, inc_attempts=True)
    try:
        from .diarize import diarize_audio

        result = await diarize_audio(audio_file(rec_id), cfg())
        (meta_dir(rec_id) / "diarization.json").write_text(result.model_dump_json())
        details = {"speakers": result.speakers}
        set_stage(rec_id, "diarize", StageStatus.done, details=details)
        return details
    except Exception as e:
        log.exception("diarize failed for %s", rec_id)
        set_stage(rec_id, "diarize", StageStatus.failed, error=str(e))
        raise


@activity.defn
async def merge_speakers(rec_id: str) -> dict:
    set_stage(rec_id, "merge_speakers", StageStatus.running, inc_attempts=True)
    try:
        from .merge import write_diarized_transcript

        turns = write_diarized_transcript(meta_dir(rec_id))
        details = {"turns": turns}
        set_stage(rec_id, "merge_speakers", StageStatus.done, details=details)
        return details
    except Exception as e:
        log.exception("merge_speakers failed for %s", rec_id)
        set_stage(rec_id, "merge_speakers", StageStatus.failed, error=str(e))
        raise


@activity.defn
async def summarize(rec_id: str) -> dict:
    set_stage(rec_id, "summarize", StageStatus.running, inc_attempts=True)
    c = cfg()
    if not (c.summarize.enabled and c.summarize.model):
        set_stage(rec_id, "summarize", StageStatus.skipped)
        return {"skipped": True}
    try:
        from .summarize import summarize_transcript

        text = summarize_transcript(meta_dir(rec_id), c)
        (meta_dir(rec_id) / "summary.md").write_text(text, encoding="utf-8")
        set_stage(rec_id, "summarize", StageStatus.done)
        return {"chars": len(text)}
    except Exception as e:
        log.exception("summarize failed for %s", rec_id)
        set_stage(rec_id, "summarize", StageStatus.failed, error=str(e))
        raise


@activity.defn
async def finalize_recording(rec_id: str) -> dict:
    """Mark recording done/failed based on its stage statuses."""
    with session() as s:
        rec = s.query(Recording).filter(Recording.id == rec_id).one()
        failed = any(
            st.status == StageStatus.failed for st in rec.stages
        )
        rec.state = RecordingState.failed if failed else RecordingState.done
        s.commit()
    return {"state": rec.state.value}


def default_retry() -> dict:
    return {
        "maximum_attempts": 2,
        "initial_interval": timedelta(seconds=5),
        "maximum_interval": timedelta(seconds=60),
    }


def timeout_for(duration_sec: float | None, base: float, per_min: float) -> int:
    """Activity StartToCloseTimeout scaled by audio length (cold-start padded)."""
    if duration_sec:
        return int(base + per_min * (duration_sec / 60)) + 120
    return int(base + 30 * per_min) + 120
