"""Stereo (dual-tap mic/system) pipeline: split → per-channel stages.

Contract under test — a stereo recording NEVER mixes its two sources:
  - transcribe emits ONE chronological segments.json with per-word `channel`;
  - diarize namespaces speakers per channel (mic:spk_0 vs system:spk_0);
  - merge attributes channel-first: a mic word can never take a system
    speaker even when the system segment overlaps it more.
"""

import json
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from worker import activities
from worker.chunk import (
    ChunkEntry,
    Manifest,
    channel_names,
    save_manifest,
)
from worker.config import TranscribeConfig, WorkerConfig
from worker.db import Base, Recording, RecordingState
from worker.diarize import DiarizationResult, DiarSegment
from worker.transcribe import Segment, TranscriptionResult, Word


@pytest.fixture
def rec(tmp_path, monkeypatch) -> Path:
    """rec1 with stereo markers: per-channel manifests (the durable marker
    channel_names keys on) + full-length channel FLACs. Same scaffolding as
    test_chunked_stages (sqlite catalog, no-op set_stage)."""
    recordings = tmp_path / "recordings" / "rec1"
    meta = recordings / "meta"
    meta.mkdir(parents=True)
    (recordings / "audio.flac").write_bytes(b"fLaC-stereo")
    (meta / "channels").mkdir()
    (meta / "channels" / "mic.flac").write_bytes(b"fLaC-m")
    (meta / "channels" / "system.flac").write_bytes(b"fLaC-s")
    _stereo_manifests(meta)


    cfg = WorkerConfig(
        storage=type(WorkerConfig().storage)(path=tmp_path),
        transcribe=TranscribeConfig(backend="api", base_url="http://speaches/v1", model="m"),
    )
    monkeypatch.setattr(activities, "_cfg", cfg)

    engine = create_engine(f"sqlite:///{tmp_path}/t.db")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    import worker.db as db_mod

    monkeypatch.setattr(db_mod, "_SessionLocal", Session)
    with Session() as s:
        s.add(Recording(id="rec1", state=RecordingState.processing, title="t", duration_sec=600.0))
        s.commit()
    monkeypatch.setattr(activities, "set_stage", lambda *a, **kw: None)
    return meta


def _stereo_manifests(meta: Path) -> None:
    """One 10-min chunk per channel, identical cut plan ([0, 600])."""
    for channel in ("mic", "system"):
        d = meta / "chunks" / channel
        d.mkdir(parents=True, exist_ok=True)
        m = Manifest(duration_sec=600.0, target_min=10.0, overlap_sec=2.0)
        m.chunks = [ChunkEntry(index=0, file="chunk_000.flac", start=0.0, end=600.0)]
        (d / "chunk_000.flac").write_bytes(b"fLaC")
        save_manifest(m, meta, channel)


class FakeApi:
    """ApiTranscriber stand-in: canned result per chunk file, records calls."""

    def __init__(self, results: dict[str, TranscriptionResult]):
        self.results = results
        self.calls: list[str] = []

    def transcribe(self, audio_path: Path, **kwargs) -> TranscriptionResult:
        self.calls.append(str(audio_path.parent.name))
        return self.results[audio_path.name]


async def _fake_diarize(audio: Path, cfg, timeout_sec: float) -> DiarizationResult:
    """Every chunk yields one speaker; identical labels per channel BEFORE
    namespacing — the point is that the namespace makes them distinct."""
    return DiarizationResult(
        speakers=["spk_0"],
        segments=[DiarSegment(start=0.0, end=600.0, speaker="spk_0")],
    )


@pytest.mark.asyncio
async def test_stereo_transcribe_tags_words_by_channel(rec, monkeypatch):
    # Distinct results per channel; a shared object would be mutated by the
    # second channel's tag pass (start/end shifts + channel overwrite).
    def result_for(channel: str) -> TranscriptionResult:
        text = " привет" if channel == "mic" else " hello"
        return TranscriptionResult(
            "ru",
            [Segment(0.0, 2.0, text.strip())],
            [Word(0.0, 0.5, text)],
        )

    api = FakeApi({})
    def _transcribe(path, **kw):
        api.calls.append(path.parent.name)
        return result_for(path.parent.name)

    api.transcribe = _transcribe  # type: ignore[method-assign]
    monkeypatch.setattr(activities, "_api", api)

    details = await activities.transcribe("rec1")

    # Both channels POSTed exactly once, into their own chunk dirs.
    assert sorted(api.calls) == ["mic", "system"]
    assert details["channels"] == 2
    data = json.loads((rec / "segments.json").read_text())
    assert {w["channel"] for w in data["words"]} == {"mic", "system"}
    assert {s["channel"] for s in data["segments"]} == {"mic", "system"}
    # Chronological single stream: both channels' items present.
    assert len(data["words"]) == 2
    assert len(data["segments"]) == 2
    assert (rec / "transcript.md").exists()


@pytest.mark.asyncio
async def test_stereo_diarize_namespaces_speakers(rec, monkeypatch):
    import worker.diarize as diarize_mod

    monkeypatch.setattr(diarize_mod, "diarize_audio", _fake_diarize)

    details = await activities.diarize("rec1")

    assert details["speakers"] == ["mic:spk_0", "system:spk_0"]
    data = json.loads((rec / "diarization.json").read_text())
    speakers = {s["speaker"] for s in data["segments"]}
    assert speakers == {"mic:spk_0", "system:spk_0"}
    # The per-chunk cache carries the namespaced labels (resume safety).
    cached = json.loads((rec / "chunks" / "mic" / "chunk_000.diarization.json").read_text())
    assert {s["speaker"] for s in cached["segments"]} == {"mic:spk_0"}


@pytest.mark.asyncio
async def test_stereo_merge_channel_first(rec, monkeypatch):
    from worker.merge import merge

    # Same wall-clock span, both channels active — the exact overlap case
    # the dual-tap exists for. Max-overlap alone would pick system:spk_0
    # for BOTH words (bigger segment); channel-first must diverge them.
    words = [
        {"start": 0.0, "end": 1.0, "text": " я", "channel": "mic"},
        {"start": 0.2, "end": 1.2, "text": " hi", "channel": "system"},
    ]
    segments = [
        {"start": 0.0, "end": 1.1, "speaker": "mic:spk_0"},
        {"start": 0.0, "end": 5.0, "speaker": "system:spk_0"},
    ]
    turns = merge(words, segments)
    assert [(t["speaker"], t["text"]) for t in turns] == [
        ("mic:spk_0", "я"),
        ("system:spk_0", "hi"),
    ]


@pytest.mark.asyncio
async def test_mono_untouched_when_no_channels_dir(rec, monkeypatch):
    # Strip the stereo markers: the recording must behave mono end-to-end.
    import shutil

    shutil.rmtree(rec / "channels")
    shutil.rmtree(rec / "chunks", ignore_errors=True)
    assert channel_names(rec) == []
    # And mono path keys off the flat manifest as before.
    m = Manifest(duration_sec=600.0, target_min=10.0, overlap_sec=2.0)
    m.chunks = [ChunkEntry(index=0, file="chunk_000.flac", start=0.0, end=600.0)]
    d = rec / "chunks"
    d.mkdir()
    (d / "chunk_000.flac").write_bytes(b"fLaC")
    save_manifest(m, rec)

    api = FakeApi(
        {
            "chunk_000.flac": TranscriptionResult(
                "ru", [Segment(0.0, 2.0, "ok")], [Word(0.0, 0.5, " ok")]
            )
        }
    )
    monkeypatch.setattr(activities, "_api", api)

    details = await activities.transcribe("rec1")

    assert "channels" not in details
    data = json.loads((rec / "segments.json").read_text())
    assert all(w.get("channel") is None for w in data["words"])
