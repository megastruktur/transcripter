"""Temporal activities for the recording pipeline."""

import asyncio
import contextlib
import json
import logging
import os
import signal
import sys
from collections.abc import Awaitable
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


# --- Timeout budgets ---------------------------------------------------------
# Measured on the platform CPU voice stack (Speaches large-v3, 16 threads):
# ~13 s of compute per minute of audio uncontended, ~21 s/min with two jobs
# contending. Budgets price ~2x the contended rate; 90-min recordings are
# the norm, 2.5 h the observed maximum. Unknown duration prices the 2.5 h
# maximum rather than under-budgeting a long upload.
_DEFAULT_MINUTES = 150  # 2.5 h fallback when duration_sec is unknown
TRANSCRIBE_BASE = 300.0  # upload + model spin-up, independent of length
TRANSCRIBE_PER_MIN = 40.0  # s per audio-minute (client AND Temporal budgets)
DIARIZE_BASE = 300.0
DIARIZE_PER_MIN = 40.0

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


def budget_transcribe(rec: Recording | None) -> float:
    """HTTP client budget, kept 30 s under the Temporal StartToClose budget.

    The gap guarantees the httpx ReadTimeout fires (a plain Exception →
    stage marked failed) before Temporal cancels the activity (a
    CancelledError that would bypass the except-Exception handler and leave
    the stage stuck in `running` until finalize).
    """
    return _budget(TRANSCRIBE_BASE, TRANSCRIBE_PER_MIN, rec) - 30.0


def budget_diarize(rec: Recording | None) -> float:
    return _budget(DIARIZE_BASE, DIARIZE_PER_MIN, rec) - 30.0


def _budget(base: float, per_min: float, rec: Recording | None) -> float:
    minutes = _minutes_of(rec)
    return base + per_min * minutes


def _minutes_of(rec: Recording | None) -> float:
    if rec is not None and rec.duration_sec:
        return rec.duration_sec / 60
    return _DEFAULT_MINUTES


async def _heartbeat_while[T](aw: Awaitable[T]) -> T:
    """Heartbeat every 60 s while a long ML call runs (Temporal heartbeat
    timeout is 120 s — see workflows.py). Outside an activity context (unit
    tests) heartbeats are a no-op."""
    hb = asyncio.create_task(_heartbeat_loop())
    try:
        return await asyncio.shield(aw)
    finally:
        hb.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await hb


async def _heartbeat_loop() -> None:
    while True:
        await asyncio.sleep(60)
        try:
            activity.heartbeat()
        except RuntimeError:  # not in an activity (unit tests): no-op
            pass


@activity.defn
async def transcribe(rec_id: str) -> dict:
    c = cfg()
    set_stage(rec_id, "transcribe", StageStatus.running, inc_attempts=True)
    with session() as s:
        rec = s.get(Recording, rec_id)
        assert rec is not None, f"recording {rec_id} not found"
        timeout_sec = budget_transcribe(rec)
    try:
        if c.transcribe.backend == "api":
            global _api
            if _api is None:
                key = os.environ.get(c.transcribe.api_key_env, "")
                _api = ApiTranscriber(c.transcribe.base_url, c.transcribe.model, key)
            result = await _heartbeat_while(
                asyncio.to_thread(
                    _api.transcribe, audio_file(rec_id), timeout_sec=timeout_sec
                )
            )
        else:
            global _local
            if _local is None or _local.model_name != c.transcribe.model:
                _local = LocalTranscriber(c.transcribe.model)
            result = await _heartbeat_while(
                asyncio.to_thread(_local.transcribe, audio_file(rec_id))
            )

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
    with session() as s:
        rec = s.get(Recording, rec_id)
        assert rec is not None, f"recording {rec_id} not found"
        timeout_sec = budget_diarize(rec)
    try:
        from .diarize import diarize_audio

        result = await _heartbeat_while(
            diarize_audio(audio_file(rec_id), cfg(), timeout_sec)
        )
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


def timeout_for(duration_sec: float | None, base: float, per_min: float) -> int:
    """Activity StartToCloseTimeout scaled by audio length.

    Workflow-side budget (workflows must not import DB-holding modules).
    Activities with a Recording at hand use budget_transcribe/budget_diarize
    for the matching HTTP timeout; the two must stay consistent — see
    budget_transcribe().
    """
    minutes = duration_sec / 60 if duration_sec else _DEFAULT_MINUTES
    return int(base + per_min * minutes)


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
