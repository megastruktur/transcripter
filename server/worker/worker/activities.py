"""Temporal activities for the recording pipeline."""

import asyncio
import contextlib
import json
import logging
import os
import signal
import sys
import threading
import time
from collections.abc import Awaitable
from pathlib import Path
from typing import Any

from temporalio import activity
from temporalio.exceptions import ApplicationError

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
from .graph_gc import run_graph_gc as run_graph_gc_impl
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
    except asyncio.CancelledError:
        # Phase 3-F F1: a Temporal cancellation (heartbeat/StartToClose
        # timeout) bypasses except-Exception; the stage row must never be
        # stranded in `running`. Mark failed, then re-raise so Temporal
        # records the activity as cancelled.
        set_stage(rec_id, "chunk", StageStatus.failed, error="cancelled")
        raise
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
    except asyncio.CancelledError:
        set_stage(rec_id, "transcribe", StageStatus.failed, error="cancelled")
        raise
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
    except asyncio.CancelledError:
        set_stage(rec_id, "diarize", StageStatus.failed, error="cancelled")
        raise
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
    except asyncio.CancelledError:
        set_stage(rec_id, "merge_speakers", StageStatus.failed, error="cancelled")
        raise
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
    profile = None
    title = ""
    with session() as s:
        rec = s.get(Recording, rec_id)
        assert rec is not None, f"recording {rec_id} not found"
        title = rec.title or ""
        # Re-scan profiles per D11: editing a yaml in PROFILES_DIR is meant
        # to take effect on the next stage run, no worker restart needed.
        # Phase 0: routing is by recording.type (tags are user grouping
        # now); a typeless recording → None → built-in default prompt.
        from .profiles import match_profile_by_type

        profile = match_profile_by_type(rec.type, c.profiles.path)
    prompt_template = profile.summarize.prompt if profile is not None else None
    # Phase 3 recap: inject the first-tag digest note as prior context.
    # Needs the knob, the graph (digest notes are only written when the
    # graph is enabled), and at least one tag. Best-effort: a recap
    # failure must never fail the summarize stage.
    recap_block: str | None = None
    if c.summarize.recap and c.graph.enabled:
        with session() as s:
            rec = s.get(Recording, rec_id)
            tags = list(rec.tags) if rec is not None and rec.tags else []
        if tags:
            try:
                from .summarize import build_recap

                recap_block = await asyncio.to_thread(
                    build_recap, tags[0], c.transcripts.path
                )
            except Exception:
                log.exception(
                    "recap build failed for %s (tag %r) — continuing without",
                    rec_id,
                    tags[0],
                )
                recap_block = None
    try:
        from .summarize import summarize_transcript

        text = await _heartbeat_while(
            asyncio.to_thread(
                summarize_transcript,
                meta_dir(rec_id),
                c,
                prompt_template,
                title,
                recap_block,
            )
        )
        # Meta path is canonical (see §1 in wave-A impl plan): export.py
        # renames the artifact in the note folder to profile.output_artifact
        # when a profile matched; meta/summary.md stays the canonical name.
        (meta_dir(rec_id) / "summary.md").write_text(text, encoding="utf-8")
        # Phase 3 recap indicator for the client. set_stage REPLACES
        # details (db.set_stage), so the dict must be complete. Sessions
        # count is unknown at this layer — build_recap only sees the
        # digest note — so it stays 0 and the client renders 'Memory
        # applied' without a session count. Absent on skip/failure.
        set_stage(
            rec_id,
            "summarize",
            StageStatus.done,
            details={
                "recap": {
                    "used": recap_block is not None and recap_block != "",
                    "sessions": 0,
                    "chars": len(recap_block or ""),
                }
            },
        )
        return {"chars": len(text), "profile_id": profile.id if profile else None}
    except asyncio.CancelledError:
        set_stage(rec_id, "summarize", StageStatus.failed, error="cancelled")
        raise
    except Exception as e:
        log.exception("summarize failed for %s", rec_id)
        set_stage(rec_id, "summarize", StageStatus.failed, error=str(e))
        raise

# Diarization, merge_speakers, and enrich (wave B) are best-effort: the
# transcript is the load-bearing artifact; if knowledge-graph
# extraction can't happen (no graph backend, no profile with an
# enrich section, or just a flakey llama-server), the recording is
# still useful with just transcript + summary.
BEST_EFFORT_STAGES = frozenset({"diarize", "merge_speakers", "enrich"})


@activity.defn
async def enrich(rec_id: str) -> dict:
    """Wave-B knowledge-graph extraction stage (best-effort).

    Skipped when EITHER (a) no profile matches the recording's type AND
    ``graph.enrich_all`` is off, (b) a profile matched but carries no
    ``enrich`` section (the author opted out — a match means domain
    steering exists, its absence in an enrich block is deliberate), OR
    (c) the graph backend is not configured (empty ``graph.uri``). The
    same shape as ``diarize``/``merge_speakers`` so the activity is
    honest about WHY it did nothing.

    Phase 2 — no match + ``enrich_all`` → the built-in fallback prompt
    (``enrich._FALLBACK_ENRICH_PROMPT``, minimal generic ontology,
    ``profile_id='builtin-fallback'``).

    Otherwise: extract (json_object, ×3 attempts), dedup with slug+LLM,
    write one transaction PER NAMESPACE (see below), write the
    ``meta/events.json`` timeline artifact (Phase 1 client contract —
    see ``enrich.write_events_json``), and report the entity count.

    Phase 2 known-entities: when the profile enables
    ``enrich.known_entities``, a PRE-extraction snapshot of the target
    namespace (top-N entities, the recording's own nodes excluded — same
    rule as the dedup lookup) is rendered into the ``{known_entities}``
    prompt block so the model reuses established slugs. The snapshot
    comes from the FIRST namespace only: extraction is ONE LLM call, and
    per-namespace re-extraction would multiply the cost by the tag
    count. Mention consistency comes from slug reuse; dedup is already
    per-namespace downstream.

    Phase 2 auto-digest: AFTER a successful enrich, each affected
    namespace's digest note is refreshed INLINE (best-effort) when it is
    older than ``graph.auto_digest_window_sec`` or missing. Inline
    rather than signal-based: keeps ordering (the digest sees the
    just-written graph) and needs no signal infrastructure we don't
    have; the cost is enrich latency bounded by last_n=5 and the stale
    window. Failures log and continue — never fail enrich.

    Phase 0 namespaces: the extraction is written as a COPY into EVERY
    free tag of the recording (tags are pure user grouping now); a
    recording with no tags lands in the built-in ``untagged`` namespace.
    The first write purges the recording's old nodes in ALL namespaces
    (``origin_recording_id``-scoped DETACH DELETE — see
    ``enrich.write_to_graph``), so tag edits between regenerates leave
    no stale copies behind.
    """
    set_stage(rec_id, "enrich", StageStatus.running, inc_attempts=True)
    c = cfg()
    if not c.graph.enabled:
        # Phase 3-F F2: intentional skips raise a NON-RETRYABLE
        # ApplicationError — the stage row honestly says `skipped`, and
        # Temporal records a terminal failure instead of a success, so
        # the F2 retry policy (3 attempts for FIFO drain) can never
        # re-run a skip. The workflow's ActivityError catch keeps the
        # recording `done` (best-effort contract unchanged).
        set_stage(rec_id, "enrich", StageStatus.skipped, details={"reason": "graph disabled"})
        raise ApplicationError("skipped: graph disabled", non_retryable=True)
    profile = None
    title = ""
    recording_date = ""
    tags: list[str] = []
    with session() as s:
        rec = s.get(Recording, rec_id)
        assert rec is not None, f"recording {rec_id} not found"
        title = rec.title or ""
        # Phase 1 timeline key: coalesce(recorded_at, created_at) as an
        # ISO-8601 UTC string (recorded_at is the import backdate).
        stamp = rec.recorded_at or rec.created_at
        recording_date = stamp.isoformat()
        tags = list(rec.tags or [])
        # Phase 0: profile routing by recording.type; the TAGS are the
        # graph namespaces (every tag gets a copy; empty → ["untagged"]).
        from .profiles import match_profile_by_type

        profile = match_profile_by_type(rec.type, c.profiles.path)
    from .profiles import EnrichSpec

    # Local binding so the fallback branch below can narrow: a matched
    # profile without an enrich section = the author opted out (domain
    # steering exists) → skip EVEN with enrich_all on. Only the no-match
    # case consults enrich_all.
    enrich = profile.enrich if profile is not None else None
    if profile is not None and enrich is None:
        set_stage(rec_id, "enrich", StageStatus.skipped, details={"reason": "no profile with enrich"})
        raise ApplicationError("skipped: no profile with enrich", non_retryable=True)
    if profile is None and not c.graph.enrich_all:
        set_stage(rec_id, "enrich", StageStatus.skipped, details={"reason": "no profile with enrich"})
        raise ApplicationError("skipped: no profile with enrich", non_retryable=True)
    graph_tags = tags or ["untagged"]
    try:
        from .embeddings import _embedder, entity_vectors
        from .enrich import (
            _FALLBACK_ENRICH_PROMPT,
            extract_from_transcript,
            list_known_entities,
            pre_existing_lookup,
            render_known_entities,
            resolve_slugs,
            write_events_json,
            write_to_graph,
        )

        # Phase 2: no profile matched + enrich_all → the built-in
        # fallback prompt (generic ontology). A matched profile without
        # an enrich section never reaches here (skipped above); the
        # assert re-establishes that invariant for the type checker
        # across the try boundary.
        if profile is None:
            profile_id = "builtin-fallback"
            enrich_spec = EnrichSpec(
                prompt=_FALLBACK_ENRICH_PROMPT, known_entities=True
            )
        else:
            profile_id = profile.id
            enrich_spec = profile.enrich
            assert enrich_spec is not None

        # Phase 2 known-entities: pre-extraction snapshot of the FIRST
        # namespace (see docstring for why only the first). The
        # placeholder guard makes the lookup zero-cost when the prompt
        # doesn't use it; profile validation already guarantees the
        # placeholder whenever known_entities is enabled.
        known_entities_block = ""
        if (
            enrich_spec.known_entities is not False
            and "{known_entities}" in enrich_spec.prompt
        ):
            limit = (
                _KNOWN_ENTITIES_DEFAULT
                if enrich_spec.known_entities is True
                else int(enrich_spec.known_entities)
            )
            try:
                rows = await _heartbeat_while(
                    asyncio.to_thread(
                        list_known_entities,
                        c.graph.uri,
                        c.graph.user,
                        os.environ.get(c.graph.password_env, ""),
                        c.graph.database,
                        graph_tags[0],
                        rec_id,
                        limit,
                    )
                )
            except Exception:
                # Best-effort prompt enhancement: a flakey neo4j must not
                # fail the stage — render an empty namespace instead.
                log.exception(
                    "enrich: known-entities lookup failed; continuing without"
                )
                rows = []
            known_entities_block = render_known_entities(rows)

        # Extract (json_object + ×3 attempts) — synchronous LLM call.
        extracted = await _heartbeat_while(
            asyncio.to_thread(
                extract_from_transcript,
                meta_dir(rec_id) / "transcript.md",
                title,
                enrich_spec.prompt,
                c,
                known_entities_block,
            )
        )

        # Phase 3-F F3: soft gate BEFORE the dedup batch. One probe Y/N
        # (30 s) with 60 s ×2 backoff, 3 attempts: a starved FIFO queue
        # fails fast here and the whole LLM-dedup leg is skipped — the
        # 2.5 prefilter stays, gray-zone merges as "same" (per-call
        # error semantics, applied up front). Heartbeat-wrapped: the
        # backoffs (60+120 s) can outrun heartbeat_timeout alone.
        from .enrich import dedup_llm_gate

        llm_dedup = await _heartbeat_while(asyncio.to_thread(dedup_llm_gate, c))
        # Two-level dedup: slug collisions across the local extraction
        # (already-present in `extracted`) AND against the live graph.
        # Per Phase 0 the dedup runs per NAMESPACE (a copy per tag is
        # deduped against that namespace's own entities — namespaces are
        # deliberately independent groups).
        resolved_by_tag: dict[str, Any] = {}
        resolved_vecs: dict[str, dict[str, list[float]] | None] = {}
        for graph_tag in graph_tags:
            lookup = pre_existing_lookup(
                c.graph.uri,
                c.graph.user,
                os.environ.get(c.graph.password_env, ""),
                c.graph.database,
                tag=graph_tag,
                exclude_rec=rec_id,
            )
            try:
                try:
                    # Heartbeat-wrapped like the extraction: N collisions ×
                    # 30 s LLM calls can otherwise outrun heartbeat_timeout
                    # (CancelledError bypasses the except below and would
                    # strand the stage row in running).
                    resolved_by_tag[graph_tag] = await _heartbeat_while(
                        asyncio.to_thread(
                            resolve_slugs,
                            extracted,
                            c,
                            graph_tag,
                            lookup,
                            llm_dedup,
                        )
                    )
                except Exception:
                    # Dedup is best-effort: a flakey LLM (or neo4j) must
                    # never kill the stage. Fall back to the raw extraction
                    # — slug collisions inside the extraction itself are
                    # already handled by ``slugify``.
                    log.exception("enrich: dedup failed; falling back to raw extraction")
                    resolved_by_tag[graph_tag] = extracted
            finally:
                lookup.close()
            # Phase 2.5: FINAL-slug → vector dict for the graph write
            # (one batched embedder call per namespace; None when the
            # model is off/unavailable — write_to_graph then skips the
            # embedding property entirely).
            resolved_vecs[graph_tag] = entity_vectors(_embedder(c), resolved_by_tag[graph_tag])
        # Write to graph (sync neo4j driver via to_thread); heartbeat-
        # wrapped for the same reason as resolve_slugs above. The FIRST
        # namespace call purges this recording's stale nodes in every
        # namespace; the rest skip the redundant DELETE.
        _count = 0
        for idx, graph_tag in enumerate(graph_tags):
            _count += await _heartbeat_while(
                asyncio.to_thread(
                    write_to_graph,
                    rec_id,
                    graph_tag,
                    resolved_by_tag[graph_tag],
                    enrich_spec.node_labels,
                    c.graph.uri,
                    c.graph.user,
                    os.environ.get(c.graph.password_env, ""),
                    c.graph.database,
                    purge_origin=idx == 0,
                    recording_date=recording_date,
                    recording_title=title,
                    embeddings=resolved_vecs.get(graph_tag),
                )
            )
        # Phase 1 timeline artifact: meta/events.json from the FIRST
        # namespace's resolved extraction (namespaces are copies;
        # identical content). Written after the graph loop so a graph
        # failure never leaves a file describing nodes that were never
        # committed. Atomic — see write_events_json.
        await _heartbeat_while(
            asyncio.to_thread(
                write_events_json,
                meta_dir(rec_id) / "events.json",
                recording_id=rec_id,
                recording_date=recording_date,
                recording_title=title,
                profile_id=profile_id,
                namespaces=graph_tags,
                resolved=resolved_by_tag[graph_tags[0]],
            )
        )
        # Phase 3.5 semantic index: index this recording's segments in
        # EVERY namespace (copies, like the graph write). BEST-EFFORT —
        # a failing embedder must never fail enrich; details report how
        # many segments landed (0 + a warning when the backend is down).
        # Placed after write_events_json and before set_stage(done) so
        # the details payload can carry indexed_segments.
        from .semantic_index import index_segments

        indexed = 0
        for graph_tag in graph_tags:
            try:
                indexed += await _heartbeat_while(
                    asyncio.to_thread(
                        index_segments,
                        rec_id,
                        graph_tag,
                        title,
                        meta_dir(rec_id),
                        c.transcripts.path,
                        c,
                    )
                )
            except Exception:
                log.exception(
                    "enrich: semantic indexing failed for %s (tag %r)",
                    rec_id,
                    graph_tag,
                )
        details = {
            "events": len(extracted.events),
            "entities": len(extracted.entities),
            "relations": len(extracted.relations),
            "profile_id": profile_id,
            "namespaces": graph_tags,
            "indexed_segments": indexed,
        }
        set_stage(rec_id, "enrich", StageStatus.done, details=details)
        # Phase 2 auto-digest — ONLY on success (never after a skip or
        # failure). Best-effort per tag; see _auto_digest_tags.
        if c.graph.auto_digest:
            await _auto_digest_tags(graph_tags, c)
        return details
    except asyncio.CancelledError:
        set_stage(rec_id, "enrich", StageStatus.failed, error="cancelled")
        raise
    except Exception as e:
        log.exception("enrich failed for %s", rec_id)
        set_stage(rec_id, "enrich", StageStatus.failed, error=str(e))
        raise


# Phase 2: top-N known entities rendered for ``known_entities: true``
# (an integer in the profile overrides the default cap).
_KNOWN_ENTITIES_DEFAULT = 25

_AUTO_DIGEST_LAST_N = 5


async def _auto_digest_tags(tags: list[str], c: WorkerConfig) -> None:
    """Refresh each tag's digest note when stale (Phase 2 auto-digest).

    ``digests/<slug>.md`` under the transcripts root — mtime older than
    ``graph.auto_digest_window_sec``, or missing → run
    ``digest.run_digest`` INLINE (heartbeat-wrapped to_thread) with
    ``last_n=5``. Inline rather than signal- or workflow-based: keeps
    ordering (the digest reads the graph the same activity just wrote)
    and needs no signal infrastructure; the price is enrich latency,
    bounded by last_n=5 and the stale window.

    Best-effort per tag: any failure is logged and the remaining tags
    still get their turn — enrich already succeeded and must never be
    retro-failed by a digest refresh.
    """
    from .digest import run_digest, safe_filename

    digests_dir = c.transcripts.path / "digests"
    now = time.time()
    for tag in tags:
        digest_file = digests_dir / safe_filename(tag)
        try:
            age = now - digest_file.stat().st_mtime
        except OSError:
            age = None  # missing (or unreadable) → treat as stale
        if age is not None and age < c.graph.auto_digest_window_sec:
            continue
        try:
            await _heartbeat_while(
                asyncio.to_thread(run_digest, tag, _AUTO_DIGEST_LAST_N, c, c.transcripts.path)
            )
        except Exception:
            log.exception("enrich: auto-digest failed for tag %r", tag)


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
async def export_transcript(args: dict) -> dict:
    """Export the recording's note folder (per-artifact files); best-effort,
    fully process-isolated.

    args: {"recording_id": str, "rename_only": bool}. rename_only=True (the
    PATCH-rename path) moves the folder to the new-title name without
    rewriting any files inside — Obsidian edits are sacred there.

    The actual I/O runs in `python -m worker.export_once` (start_new_session
    => own process group). On timeout the group gets SIGKILL and is ABANDONED
    — never waited: a D-state child parked on a dead mount cannot be waited
    on, and waiting would hang the activity. Errors (never exceptions to
    Temporal) land in the workflow result as transcript_note.
    """
    if not _export_children.try_acquire():
        return {"transcript_note": f"error: too many stuck export subprocesses (>{_EXPORT_MAX_CHILDREN}); skipping"}
    cmd = [sys.executable, "-m", "worker.export_once", args["recording_id"]]
    if args.get("rename_only"):
        cmd.append("--rename-only")
    proc = await asyncio.create_subprocess_exec(
        *cmd,
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


@activity.defn
async def tag_digest(args: dict) -> dict:
    """Wave-C tag digest activity.

    Single long-running step (Postgres pull + Neo4j read + LLM call + file
    write). The activity reads ``cfg.graph`` upfront and short-circuits
    with a clear error if the graph backend is not configured — the API
    layer should have rejected with 409 already, but a missing compose
    profile between request and execution is still possible and worth
    failing loud.

    The empty-selection path (no done recordings carry the tag) returns
    ``{"written": False, "reason": "..."}`` rather than raising: the API
    has already given the caller 202 with the workflow id, so failing
    here would just strand a workflow with no actionable error for the
    user. Empty is a legitimate outcome (a new tag, an archive churn, etc).

    LLM failures propagate (httpx errors are caught by Temporal's retry
    policy at the workflow level). Tag sanitization failures propagate as
    ValueError so the worker surfaces a clear last_error rather than
    silently dropping a note the user can't trace.
    """
    tag = args["tag"]
    last_n = int(args["last_n"])
    c = cfg()
    if not c.graph.enabled:
        # The API already returns 409 on this; if we got here the operator
        # toggled the config off between request and execution. Surfacing
        # it as a failure keeps the workflow query honest.
        raise RuntimeError(
            "graph backend not configured (graph.uri empty) — digest cannot run"
        )
    try:
        from .digest import run_digest

        return await _heartbeat_while(
            asyncio.to_thread(run_digest, tag, last_n, c, c.transcripts.path)
        )
    except Exception:
        log.exception("tag_digest failed for tag=%s", tag)
        raise

@activity.defn
async def rename_entity(args: dict) -> dict:
    """Phase 4: rename ONE entity node — label (+ optional type) and
    ``user_corrected: true`` — inside the tag's namespace.

    Not a pipeline stage (no stage row): the API has already validated
    the tag/slug pair against the events.json aggregation and returned
    202; this activity is the async half. The neo4j driver lives in the
    worker (same reason enrich/tag_digest do), hence an activity rather
    than an inline API call.

    Graph disabled → RuntimeError (the same loud shape as tag_digest:
    the API 409'd already, so reaching here means the config flipped
    between request and execution).
    """
    tag = args["tag"]
    slug = args["slug"]
    label = args["label"]
    type_ = args.get("type")
    c = cfg()
    if not c.graph.enabled:
        raise RuntimeError(
            "graph backend not configured (graph.uri empty) — rename cannot run"
        )
    try:
        from .enrich import rename_entity_in_graph

        result = await _heartbeat_while(
            asyncio.to_thread(
                rename_entity_in_graph,
                tag,
                slug,
                label,
                type_,
                c,
                c.graph.uri,
                c.graph.user,
                os.environ.get(c.graph.password_env, ""),
                c.graph.database,
            )
        )
    except Exception:
        log.exception("rename_entity failed for %s/%s", tag, slug)
        raise
    if not result.get("ok"):
        # The node vanished between the API's existence check and the
        # write (namespace copy never made, GC sweep, manual delete).
        # Raise non-retryable: a retry re-runs the same missing MATCH.
        raise ApplicationError(
            f"entity {tag}/{slug} not found in graph", non_retryable=True
        )
    log.info(
        "rename_entity: %s/%s → %r (re_embedded=%s)",
        tag,
        slug,
        label,
        result.get("re_embedded"),
    )
    return result


@activity.defn
async def graph_gc(_: dict) -> dict:
    """Phase 1 graph GC sweep (not a pipeline stage — no stage rows).

    Deletes every graph node whose ``origin_recording_id`` no longer
    exists in the recordings catalog (recording deleted in the API →
    its nodes would otherwise live in Neo4j forever, since enrich only
    purges a recording's nodes when THAT recording re-writes itself).

    Invoked by the ``GraphGc`` workflow on a Temporal Schedule
    (``graph.gc_interval_sec``); also safe to call ad hoc. Graph
    disabled → ``{"skipped": "graph disabled"}`` so a scheduled run on
    a graph-less deployment is a clean no-op, not an error storm.
    """
    c = cfg()
    return await _heartbeat_while(asyncio.to_thread(run_graph_gc_impl, c))