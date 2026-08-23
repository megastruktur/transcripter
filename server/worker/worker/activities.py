"""Temporal activities for the recording pipeline."""

import asyncio
import json
import logging
import os
import signal
import sys
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
    if not cfg().diarization.enabled:
        # Diarization disabled by config: no HTTP, no attempt counted, and no
        # stale speaker attribution may survive into a regenerated merge —
        # merge_speakers keys off diarization.json's presence. skipped also
        # clears last_error/details from any previous enabled run.
        (meta_dir(rec_id) / "diarization.json").unlink(missing_ok=True)
        (meta_dir(rec_id) / "diarized-transcript.md").unlink(missing_ok=True)
        set_stage(rec_id, "diarize", StageStatus.skipped, details={})
        return {"skipped": "diarization disabled"}
    set_stage(rec_id, "diarize", StageStatus.running, inc_attempts=True)
    # Drop a previous run's output up front: merge_speakers keys off this
    # file's presence, so a stale one would mask a failure here.
    out = meta_dir(rec_id) / "diarization.json"
    out.unlink(missing_ok=True)
    try:
        from .diarize import diarize_audio

        result = await diarize_audio(audio_file(rec_id), cfg())
        out.write_text(result.model_dump_json())
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

        # No diarization (stage failed, or it found no speakers) means there
        # is nothing to attribute: skip rather than emit a transcript whose
        # every turn is labelled `None`. Drop a previous run's artifact too,
        # so the UI cannot keep serving stale speaker attribution.
        diar = meta_dir(rec_id) / "diarization.json"
        if not diar.exists() or not json.loads(diar.read_text()).get("segments"):
            (meta_dir(rec_id) / "diarized-transcript.md").unlink(missing_ok=True)
            set_stage(rec_id, "merge_speakers", StageStatus.skipped)
            return {"skipped": "no diarization"}

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


# Diarization (and the merge that depends on it) is best-effort: a recording
# with a good transcript is still useful, so these stages do not fail it.
BEST_EFFORT_STAGES = frozenset({"diarize", "merge_speakers"})


@activity.defn
async def finalize_recording(rec_id: str) -> dict:
    """Mark recording done/failed based on its stage statuses."""
    with session() as s:
        rec = s.query(Recording).filter(Recording.id == rec_id).one()
        failed = any(
            st.status == StageStatus.failed and st.kind not in BEST_EFFORT_STAGES
            for st in rec.stages
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


# --- Transcript note export -------------------------------------------------

_EXPORT_TIMEOUT_SEC = 20
_EXPORT_MAX_CHILDREN = 4


class _ExportRegistry:
    """Live/abandoned export-subprocess PIDs with honest accounting.

    Before each spawn, a non-blocking reap sweep removes exited children
    (ECHILD => already reaped by asyncio's child watcher). Abandoned
    (SIGKILLed, never waited) PIDs stay registered and count against the cap:
    a persistently dead NAS mount must leak at most _EXPORT_MAX_CHILDREN
    processes, never unbounded.
    """

    def __init__(self) -> None:
        self._pids: set[int] = set()

    def _sweep(self) -> None:
        for pid in list(self._pids):
            try:
                pid_, status = os.waitpid(pid, os.WNOHANG)
                if pid_ != 0 or os.WIFEXITED(status) or os.WIFSIGNALED(status):
                    self._pids.discard(pid)
            except ChildProcessError:  # ECHILD: reaped elsewhere
                self._pids.discard(pid)

    def register(self, pid: int) -> None:
        self._pids.add(pid)

    def try_acquire(self) -> bool:
        self._sweep()
        return len(self._pids) < _EXPORT_MAX_CHILDREN

    def discard(self, pid: int) -> None:
        self._pids.discard(pid)


_export_children = _ExportRegistry()


@activity.defn
async def export_transcript(rec_id: str) -> dict:
    """Export the consolidated note; best-effort, fully process-isolated.

    The actual I/O runs in `python -m worker.export_once` (start_new_session
    => own process group). On timeout the group gets SIGKILL and is ABANDONED
    — never waited: a D-state child parked on a dead mount cannot be waited
    on, and waiting would hang the activity. Errors (never exceptions to
    Temporal) land in the workflow result as transcript_note.
    """
    if not _export_children.try_acquire():
        return {"transcript_note": f"error: too many stuck export subprocesses (>{_EXPORT_MAX_CHILDREN}); skipping"}
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "worker.export_once", rec_id,
        start_new_session=True,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _export_children.register(proc.pid)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=_EXPORT_TIMEOUT_SEC)
        if proc.returncode == 0:
            note = out.decode().strip() if out else ""
            return {"transcript_note": note} if note else {"transcript_note": "skipped"}
        tail = (err or b"").decode(errors="replace").strip().splitlines()[-3:]
        return {"transcript_note": "error: " + (" | ".join(tail) or f"exit {proc.returncode}")}
    except TimeoutError:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        return {"transcript_note": f"error: export subprocess timed out after {_EXPORT_TIMEOUT_SEC}s (killed; possible stale mount)"}
    finally:
        _export_children.discard(proc.pid)
