"""Temporal activities for the recording pipeline."""

import asyncio
import contextlib
import json
import logging
import os
import signal
import sys
import threading
from collections.abc import Awaitable
from pathlib import Path

from temporalio import activity

from .chunk import (
    Manifest,
    chunks_dir,
    cleanup_chunks,
    cut_chunks,
    is_suspect,
    keep_window,
    load_manifest,
    probe_duration,
    save_manifest,
    shift_into,
)
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
    TranscriptionResult,
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
_local_lock = threading.Lock()
_api: ApiTranscriber | None = None


def preload_local(model_name: str) -> None:
    """Load the shared local whisper model at worker startup.

    Assigns the module-level `_local` so the first transcribe activity
    reuses the warm instance instead of paying a second model load.
    """
    global _local
    with _local_lock:
        if _local is None or _local.model_name != model_name:
            _local = LocalTranscriber(model_name)
        _local.preload()


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

def _chunk_budget(len_sec: float, base: float, per_min: float) -> float:
    """Per-chunk HTTP budget: same shape as budget_transcribe (base +
    per-minute, 30 s under the Temporal share) but priced from the CHUNK's
    length, not the whole recording's."""
    return base + per_min * (len_sec / 60) - 30.0


@activity.defn
async def chunk(rec_id: str) -> dict:
    """Slice the recording into FLAC chunks + manifest (worker/chunk.py).

    Disabled by config: no manifest is written, which is exactly what sends
    transcribe/diarize down the whole-file path. Re-slices from scratch on
    regenerate, resetting every per-chunk status to pending.
    """
    c = cfg()
    if not c.chunk.enabled:
        set_stage(rec_id, "chunk", StageStatus.skipped, details={})
        return {"skipped": "chunking disabled", "chunks": 0}
    set_stage(rec_id, "chunk", StageStatus.running, inc_attempts=True)
    try:
        with session() as s:
            rec = s.get(Recording, rec_id)
            assert rec is not None, f"recording {rec_id} not found"
            duration = rec.duration_sec
        audio = audio_file(rec_id)
        if not duration:
            duration = await asyncio.to_thread(probe_duration, audio)
        manifest = await asyncio.to_thread(
            cut_chunks, audio, meta_dir(rec_id), duration,
            c.chunk.target_min, c.chunk.overlap_sec,
        )
        details = {"chunks": len(manifest.chunks), "target_min": c.chunk.target_min}
        set_stage(rec_id, "chunk", StageStatus.done, details=details)
        return details
    except Exception as e:
        log.exception("chunk failed for %s", rec_id)
        set_stage(rec_id, "chunk", StageStatus.failed, error=str(e))
        raise


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
    # Beat FIRST, then every 60 s. A beat delayed by the initial sleep can
    # arrive >120 s (heartbeat_timeout) after the previous call's last beat
    # when a chunk POST fails fast (e.g. "Server disconnected" at +2 s) and
    # the retry backoff + next loop's initial sleep stack up — observed live
    # 2026-08-25: heartbeat timeout killed a chunked transcribe mid-run.
    while True:
        try:
            activity.heartbeat()
        except RuntimeError:  # not in an activity (unit tests): no-op
            pass
        await asyncio.sleep(60)


# Per-chunk retry INSIDE the activity (the Temporal retry policy for
# transcribe is maximum_attempts=1 — a full-activity retry would re-run
# every chunk). Two attempts with a short backoff bridge a transient
# Speaches hiccup; a persistent failure fails the stage with the chunk
# coordinates, and regenerate resumes only the non-done chunks.
_CHUNK_ATTEMPTS = 2
_CHUNK_RETRY_BACKOFF_SEC = 5


async def _transcribe_file(
    c: WorkerConfig,
    audio: Path,
    timeout_sec: float,
    prompt: str | None = None,
    reset_context: bool = False,
) -> TranscriptionResult:
    """One audio file → one POST (or one local faster-whisper run).

    `reset_context` is the suspect-chunk escape hatch: empty prompt +
    condition_on_previous_text=false (the latter is ignored by Speaches
    0.8.3 and honored by versions that accept the field).
    """
    if c.transcribe.backend == "api":
        global _api
        if _api is None:
            key = os.environ.get(c.transcribe.api_key_env, "")
            _api = ApiTranscriber(c.transcribe.base_url, c.transcribe.model, key)
        return await _heartbeat_while(
            asyncio.to_thread(
                _api.transcribe,
                audio,
                timeout_sec=timeout_sec,
                prompt=prompt,
                condition_on_previous_text=False if reset_context else None,
            )
        )
    global _local
    with _local_lock:
        if _local is None or _local.model_name != c.transcribe.model:
            _local = LocalTranscriber(c.transcribe.model)
        local = _local
    return await _heartbeat_while(asyncio.to_thread(local.transcribe, audio))


async def _transcribe_chunked(
    rec_id: str, manifest: Manifest, c: WorkerConfig
) -> TranscriptionResult:
    """Sequential per-chunk transcription — NEVER parallel: the voice stack
    is one CPU box and concurrent large-v3 jobs run at ~half speed each
    (contention incident 2026-08-25). Progress persists per chunk, so a
    regenerate re-POSTs only non-done (or suspect) chunks."""
    meta = meta_dir(rec_id)
    d = chunks_dir(meta)
    total = len(manifest.chunks)
    segments = []
    words = []
    language = "unknown"
    for ch in manifest.chunks:
        result_path = d / f"chunk_{ch.index:03d}.segments.json"
        if ch.transcribe != "done" or ch.transcribe_suspect:
            chunk_path = d / ch.file
            if not chunk_path.exists():
                raise RuntimeError(
                    f"chunk file {ch.file} is gone (chunks are cleaned after "
                    "merge_speakers); regenerate from stage 'chunk'"
                )
            timeout_sec = _chunk_budget(ch.end - ch.start, TRANSCRIBE_BASE, TRANSCRIBE_PER_MIN)
            # A suspect chunk is re-squeezed with a reset decoder context.
            reset = ch.transcribe_suspect
            last_err: Exception | None = None
            res: TranscriptionResult | None = None
            for attempt in range(1, _CHUNK_ATTEMPTS + 1):
                try:
                    res = await _transcribe_file(
                        c, chunk_path, timeout_sec,
                        prompt="" if reset else None, reset_context=reset,
                    )
                    break
                except Exception as e:  # noqa: BLE001 — retry must catch ANY failure
                    last_err = e  # (httpx, parse, OS); re-raised after the last attempt
                    log.warning(
                        "transcribe chunk %d/%d attempt %d failed for %s: %s",
                        ch.index + 1, total, attempt, rec_id, e,
                    )
                    if attempt < _CHUNK_ATTEMPTS:
                        await asyncio.sleep(_CHUNK_RETRY_BACKOFF_SEC)
            if res is None:
                raise RuntimeError(f"chunk {ch.index + 1} of {total}: {last_err}")
            res.to_json(result_path)
            ch.transcribe = "done"
            ch.transcribe_suspect = is_suspect([s.text for s in res.segments])
            save_manifest(manifest, meta)  # resume boundary after every chunk
        else:
            res = TranscriptionResult.from_json(result_path)

        if language == "unknown" and res.language != "unknown":
            language = res.language
        lo, hi = keep_window(ch.index, total, ch.end - ch.start, manifest.overlap_sec)
        segments.extend(shift_into(res.segments, ch.start, lo, hi))
        words.extend(shift_into(res.words, ch.start, lo, hi))
    return TranscriptionResult(language, segments, words)


@activity.defn
async def transcribe(rec_id: str) -> dict:
    c = cfg()
    set_stage(rec_id, "transcribe", StageStatus.running, inc_attempts=True)
    with session() as s:
        rec = s.get(Recording, rec_id)
        assert rec is not None, f"recording {rec_id} not found"
        timeout_sec = budget_transcribe(rec)
    try:
        manifest = load_manifest(meta_dir(rec_id))
        if manifest is not None:
            result = await _transcribe_chunked(rec_id, manifest, c)
        else:
            result = await _transcribe_file(c, audio_file(rec_id), timeout_sec)

        result.to_json(meta_dir(rec_id) / "segments.json")
        segments_to_markdown(result, meta_dir(rec_id) / "transcript.md")
        details: dict = {"language": result.language, "segments": len(result.segments)}
        if manifest is not None:
            details["chunks"] = len(manifest.chunks)
            suspect = sum(1 for ch in manifest.chunks if ch.transcribe_suspect)
            if suspect:
                details["suspect_chunks"] = suspect
        set_stage(rec_id, "transcribe", StageStatus.done, details=details)
        return details
    except Exception as e:
        log.exception("transcribe failed for %s", rec_id)
        set_stage(rec_id, "transcribe", StageStatus.failed, error=str(e))
        raise


async def _diarize_chunked(rec_id: str, manifest: Manifest, c: WorkerConfig):
    """Sequential per-chunk diarization (never parallel — same CPU voice
    stack). Per-chunk progress persists, so the Temporal diarize retry
    resumes at the failed chunk instead of re-running them all.

    Speaker labels stay PER-CHUNK (spk_0 in chunk 1 is not spk_0 in chunk
    2) — accepted: merge_speakers attributes words by time overlap, which
    per-chunk labels satisfy."""
    from .diarize import DiarizationResult, diarize_audio

    meta = meta_dir(rec_id)
    d = chunks_dir(meta)
    total = len(manifest.chunks)
    segments = []
    speakers: set[str] = set()
    for ch in manifest.chunks:
        result_path = d / f"chunk_{ch.index:03d}.diarization.json"
        if ch.diarize != "done":
            chunk_path = d / ch.file
            if not chunk_path.exists():
                raise RuntimeError(
                    f"chunk file {ch.file} is gone (chunks are cleaned after "
                    "merge_speakers); regenerate from stage 'chunk'"
                )
            timeout_sec = _chunk_budget(ch.end - ch.start, DIARIZE_BASE, DIARIZE_PER_MIN)
            res = await _heartbeat_while(diarize_audio(chunk_path, c, timeout_sec))
            result_path.write_text(res.model_dump_json())
            ch.diarize = "done"
            save_manifest(manifest, meta)  # resume boundary after every chunk
        else:
            res = DiarizationResult.model_validate_json(
                result_path.read_text(encoding="utf-8")
            )
        lo, hi = keep_window(ch.index, total, ch.end - ch.start, manifest.overlap_sec)
        segments.extend(shift_into(res.segments, ch.start, lo, hi))
        speakers.update(res.speakers)
    return DiarizationResult(speakers=sorted(speakers), segments=segments)


@activity.defn
async def diarize(rec_id: str) -> dict:
    c = cfg()
    if not c.diarization.enabled:
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
        manifest = load_manifest(meta_dir(rec_id))
        if manifest is not None:
            result = await _diarize_chunked(rec_id, manifest, c)
        else:
            from .diarize import diarize_audio

            result = await _heartbeat_while(
                diarize_audio(audio_file(rec_id), c, timeout_sec)
            )
        out.write_text(result.model_dump_json())
        details: dict = {"speakers": result.speakers}
        if manifest is not None:
            details["chunks"] = len(manifest.chunks)
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
            cleanup_chunks(meta_dir(rec_id))
            return {"skipped": "no diarization"}

        turns = write_diarized_transcript(meta_dir(rec_id))
        details = {"turns": turns}
        set_stage(rec_id, "merge_speakers", StageStatus.done, details=details)
        # Chunk FLACs are retention `until_merged`: everything downstream
        # needed them for has been consumed. The manifest and per-chunk
        # JSONs stay (small; diagnostics + re-concat without re-running STT).
        cleanup_chunks(meta_dir(rec_id))
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

        text = await _heartbeat_while(asyncio.to_thread(summarize_transcript, meta_dir(rec_id), c))
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
