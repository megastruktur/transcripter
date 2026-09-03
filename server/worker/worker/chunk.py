"""Audio chunking stage: cut long recordings into sequential FLAC chunks.

Why this stage exists: whisper's repetition loop lives inside a single
request's decoder context (condition_on_previous_text over rolling windows).
A 2.5-h request can collapse into an identical-phrase loop that poisons
everything after the failure point (observed 2026-08-25: 369 repeats of one
phrase after 01:01, compression_ratio pinned at 8.755725). Cutting the audio
resets the decoder context per chunk, so a poisoned chunk costs ~10 min of
transcript instead of hours — and it works with ANY STT server version,
no `condition_on_previous_text` server knob required.

Layout per recording: meta/chunks/chunk_000.flac … + chunks.json manifest
(per-chunk start/end + transcribe/diarize progress so a failed stage resumes
only the missing chunks) + per-chunk raw results (chunk_NNN.segments.json,
chunk_NNN.diarization.json).
"""

import json
import os
import shutil
import signal
import subprocess
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Protocol

# Per-chunk ffmpeg budget. FLAC decode+re-encode runs at many times realtime
# (a 10-min chunk takes seconds); this only guards a wedged process/mount.
FFMPEG_TIMEOUT_SEC = 120

CHUNKS_DIRNAME = "chunks"
MANIFEST_NAME = "chunks.json"


class ChunkError(RuntimeError):
    """ffmpeg/ffprobe failure or timeout (message carries the stderr tail)."""


@dataclass
class ChunkEntry:
    index: int
    file: str  # chunk FLAC, relative to the chunks dir
    start: float  # seconds, global timeline
    end: float
    transcribe: str = "pending"  # pending|done
    transcribe_suspect: bool = False  # repetition-loop marker, see is_suspect()
    diarize: str = "pending"  # pending|done


@dataclass
class Manifest:
    duration_sec: float
    target_min: float
    overlap_sec: float
    chunks: list[ChunkEntry] = field(default_factory=list)
    version: int = 1

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=1)

    @staticmethod
    def from_json(text: str) -> "Manifest":
        data = json.loads(text)
        return Manifest(
            duration_sec=data["duration_sec"],
            target_min=data["target_min"],
            overlap_sec=data["overlap_sec"],
            version=data.get("version", 1),
            chunks=[ChunkEntry(**c) for c in data["chunks"]],
        )


def chunks_dir(meta: Path) -> Path:
    return meta / CHUNKS_DIRNAME


def channel_dir(meta: Path, channel: str) -> Path:
    """Per-channel chunk subdir (stereo): chunks/mic, chunks/system."""
    return chunks_dir(meta) / channel


def channel_names(meta: Path) -> list[str]:
    """Stereo channel layout of a recording: ["mic", "system"] when the
    per-channel manifests exist, [] for mono — the whole pipeline branches
    on this (mono keeps its single-file layout).

    Keyed on chunks/<ch>/chunks.json (retained forever), NOT on
    meta/channels/*.flac (deleted by cleanup_chunks after merge): a
    transcribe/diarize regenerate on a merged recording must still see the
    stereo layout even though the full-length channel FLACs are gone —
    chunk FLACs regenerate from stage 'chunk' anyway."""
    return [c for c in ("mic", "system") if manifest_path(meta, c).is_file()]


def manifest_path(meta: Path, channel: str | None = None) -> Path:
    return (channel_dir(meta, channel) if channel else chunks_dir(meta)) / MANIFEST_NAME


def load_manifest(meta: Path, channel: str | None = None) -> Manifest | None:
    p = manifest_path(meta, channel)
    if not p.exists():
        return None
    return Manifest.from_json(p.read_text(encoding="utf-8"))


def save_manifest(manifest: Manifest, meta: Path, channel: str | None = None) -> None:
    """Atomic write: the manifest is the crash/resume boundary."""
    d = channel_dir(meta, channel) if channel else chunks_dir(meta)
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / (MANIFEST_NAME + ".tmp")
    tmp.write_text(manifest.to_json(), encoding="utf-8")
    os.replace(tmp, d / MANIFEST_NAME)

def plan_chunks(duration_sec: float, target_min: float, overlap_sec: float) -> list[tuple[float, float]]:
    """(start, end) pairs covering [0, duration): even target-length chunks,
    a short tail for the remainder, and `overlap_sec` shared between
    neighbours so no audio is lost at the cuts (no gaps, no negative
    lengths)."""
    if duration_sec <= 0:
        raise ValueError(f"duration_sec must be positive, got {duration_sec}")
    target = target_min * 60.0
    if overlap_sec >= target:
        raise ValueError(f"overlap_sec {overlap_sec} must be < target chunk length {target}")
    if duration_sec <= target:
        return [(0.0, duration_sec)]
    step = target - overlap_sec
    out: list[tuple[float, float]] = []
    start = 0.0
    while start < duration_sec:
        out.append((start, min(start + target, duration_sec)))
        start += step
    # Absorb a degenerate micro-tail into the previous chunk. One appears
    # when duration ≈ k×step (observed live: 0.4-s chunk on a 89.7-min
    # recording) — it would cost a full STT round-trip for seconds of audio,
    # and its midpoint seam window (keep_window) would be EMPTY, silently
    # dropping the recording's final moments from the merged transcript.
    while len(out) > 1 and out[-1][1] - out[-1][0] < 2 * overlap_sec:
        out.pop()
        out[-1] = (out[-1][0], duration_sec)
    return out


def probe_duration(audio: Path) -> float:
    """Container duration via ffprobe — fallback when the recording row has
    no duration_sec (e.g. an old upload finalized before durations were
    recorded)."""
    out = _run(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(audio),
        ],
        timeout=30,
    )
    try:
        return float(out.strip())
    except ValueError as e:
        raise ChunkError(f"ffprobe returned no duration for {audio}: {out!r}") from e


CHANNELS_DIRNAME = "channels"
_CHANNEL_SPLIT_TIMEOUT_SEC = 600


def probe_channels(audio: Path) -> int:
    """Audio channel count via ffprobe (1 = mono, 2 = stereo)."""
    out = _run(
        [
            "ffprobe", "-v", "error",
            "-select_streams", "a:0",
            "-show_entries", "stream=channels",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(audio),
        ],
        timeout=30,
    )
    try:
        return int(out.strip())
    except ValueError as e:
        raise ChunkError(f"ffprobe returned no channel count for {audio}: {out!r}") from e


def split_channels(audio: Path, meta: Path) -> list[tuple[str, Path]]:
    """Stereo → per-source mono FLACs: mic → L, system → R (client mix_samples
    interleaves exactly this way; see client recording.rs).

    Idempotent: an existing split is reused. Returns [] for mono (the whole
    pipeline keeps its single-file path). Output: meta/channels/mic.flac and
    meta/channels/system.flac.
    """
    if probe_channels(audio) < 2:
        return []
    d = meta / CHANNELS_DIRNAME
    mic, system = d / "mic.flac", d / "system.flac"
    if mic.is_file() and system.is_file():
        return [("mic", mic), ("system", system)]
    d.mkdir(parents=True, exist_ok=True)
    # Single pass, two outputs: channelsplit exposes both mono pads and each
    # is mapped to its own FLAC (two runs would decode the file twice).
    _run(
        [
            "ffmpeg", "-nostdin", "-v", "error", "-y",
            "-i", str(audio),
            "-filter_complex", "[0:a]channelsplit=channel_layout=stereo[l][r]",
            "-map", "[l]", "-c:a", "flac", str(mic),
            "-map", "[r]", "-c:a", "flac", str(system),
        ],
        timeout=_CHANNEL_SPLIT_TIMEOUT_SEC,
    )
    return [("mic", mic), ("system", system)]


def cut_chunks(
    audio: Path,
    meta: Path,
    duration_sec: float,
    target_min: float,
    overlap_sec: float,
    channel: str | None = None,
) -> Manifest:
    """Re-slice from scratch (idempotent regenerate): old chunk files and
    per-chunk results are wiped, the manifest is rewritten with all
    statuses pending. `channel` routes the manifest + chunk files into the
    per-channel subdir (stereo path); the timelines of the two channels are
    identical by construction (same cut plan over the same duration)."""
    d = channel_dir(meta, channel) if channel else chunks_dir(meta)
    if d.exists():
        shutil.rmtree(d)
    d.mkdir(parents=True)

    manifest = Manifest(duration_sec=duration_sec, target_min=target_min, overlap_sec=overlap_sec)
    for i, (start, end) in enumerate(plan_chunks(duration_sec, target_min, overlap_sec)):
        name = f"chunk_{i:03d}.flac"
        _run(
            [
                "ffmpeg", "-nostdin", "-v", "error", "-y",
                # -ss/-t as INPUT options + re-encode = sample-accurate cut
                # (-c copy cannot split FLAC on exact timestamps).
                "-ss", f"{start:.3f}", "-t", f"{end - start:.3f}",
                "-i", str(audio),
                "-c:a", "flac",
                str(d / name),
            ],
            timeout=FFMPEG_TIMEOUT_SEC,
        )
        manifest.chunks.append(ChunkEntry(index=i, file=name, start=start, end=end))
    save_manifest(manifest, meta, channel)
    return manifest


def cleanup_chunks(meta: Path) -> None:
    """Retention `until_merged`: drop the chunk FLACs once merge_speakers has
    consumed everything downstream needs — in BOTH layouts (mono flat
    chunks/, stereo per-channel chunks/{mic,system}/) — plus the full-length
    meta/channels/*.flac (re-derivable from audio.flac via split_channels).
    Manifests and per-chunk result JSONs stay (small; diagnostics + resume
    of later stages without re-running the STT)."""
    d = chunks_dir(meta)
    if d.exists():
        for p in d.rglob("chunk_*.flac"):
            p.unlink()
    chan = meta / CHANNELS_DIRNAME
    if chan.exists():
        for p in chan.glob("*.flac"):
            p.unlink()


def keep_window(index: int, total: int, chunk_len: float, overlap_sec: float) -> tuple[float, float]:
    """Local-time [lo, hi) window a chunk contributes to the merged result.

    Neighbouring chunks share `overlap_sec`; splitting that shared band at
    its midpoint assigns every moment to exactly one chunk, so merged
    transcripts neither duplicate nor drop speech at the seams."""
    lo = overlap_sec / 2 if index > 0 else 0.0
    hi = chunk_len - overlap_sec / 2 if index < total - 1 else chunk_len
    return lo, hi


class _Timed(Protocol):
    start: float
    end: float


def shift_into[T: _Timed](
    items: Iterable[T], chunk_start: float, lo: float, hi: float
) -> list[T]:
    """Shift chunk-local items onto the global timeline, keeping only those
    whose midpoint falls inside the chunk's keep-window."""
    out = []
    for it in items:
        mid = (it.start + it.end) / 2
        if lo <= mid < hi:
            it.start += chunk_start
            it.end += chunk_start
            out.append(it)
    return out


def is_suspect(texts: list[str]) -> bool:
    """Repetition-loop marker: >50% of the chunk's segments carry identical
    normalized text (needs ≥4 segments — a 2-segment chunk of a slow talker
    is not a loop)."""
    if len(texts) < 4:
        return False
    norm = [" ".join(t.lower().split()) for t in texts]
    top = max(norm.count(t) for t in set(norm))
    return top > len(norm) / 2


def _run(cmd: list[str], timeout: int) -> str:
    """Subprocess with a hard timeout and no zombies: own process group, on
    timeout SIGKILL the group and ABANDON (never wait — a D-state child
    parked on a dead mount cannot be waited on). Same pattern as
    activities.export_transcript."""
    proc = subprocess.Popen(
        cmd,
        start_new_session=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        raise ChunkError(f"{cmd[0]} timed out after {timeout}s (killed)") from None
    if proc.returncode != 0:
        tail = err.decode(errors="replace").strip().splitlines()[-3:]
        raise ChunkError(f"{cmd[0]} exited {proc.returncode}: {' | '.join(tail)}")
    return out.decode(errors="replace")
