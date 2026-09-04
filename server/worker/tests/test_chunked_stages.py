"""Chunked transcribe/diarize/chunk activities: seams, resume, retry, suspect."""

import json
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from worker import activities
from worker.chunk import ChunkEntry, Manifest, save_manifest
from worker.config import TranscribeConfig, WorkerConfig
from worker.db import Base, Recording, RecordingState
from worker.diarize import DiarizationResult, DiarSegment
from worker.transcribe import Segment, TranscriptionResult, Word


@pytest.fixture
def rec(tmp_path, monkeypatch) -> Path:
    """rec1 with audio + meta dir, sqlite catalog, no-op set_stage."""
    recordings = tmp_path / "recordings" / "rec1"
    meta = recordings / "meta"
    meta.mkdir(parents=True)
    (recordings / "audio.flac").write_bytes(b"fLaC")
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
        s.add(Recording(id="rec1", state=RecordingState.processing, title="t", duration_sec=3600.0))
        s.commit()
    monkeypatch.setattr(activities, "set_stage", lambda *a, **kw: None)
    return meta


def _manifest(meta: Path, n: int = 2) -> Manifest:
    """Two 10-min chunks ([0,600], [598,1198]) with 2 s overlap."""
    m = Manifest(duration_sec=1198.0, target_min=10.0, overlap_sec=2.0)
    m.chunks = [
        ChunkEntry(index=0, file="chunk_000.flac", start=0.0, end=600.0),
        ChunkEntry(index=1, file="chunk_001.flac", start=598.0, end=1198.0),
    ][:n]
    d = meta / "chunks"
    d.mkdir(exist_ok=True)
    for c in m.chunks:
        (d / c.file).write_bytes(b"fLaC")
    save_manifest(m, meta)
    return m


class FakeApi:
    """ApiTranscriber stand-in: canned results per chunk file, records calls."""

    def __init__(self, results: dict[str, TranscriptionResult], fail_once: set[str] | None = None):
        self.results = results
        self.fail_once = fail_once or set()
        self.calls: list[dict] = []

    def transcribe(self, audio_path, timeout_sec=600.0, prompt=None, condition_on_previous_text=None):
        self.calls.append(
            {
                "file": audio_path.name,
                "timeout_sec": timeout_sec,
                "prompt": prompt,
                "c_o_p_t": condition_on_previous_text,
            }
        )
        if audio_path.name in self.fail_once:
            self.fail_once.discard(audio_path.name)
            raise ConnectionError("speaches hiccup")
        return self.results[audio_path.name]


def _two_chunk_results() -> dict[str, TranscriptionResult]:
    return {
        "chunk_000.flac": TranscriptionResult(
            "ru",
            [Segment(0.0, 0.8, " начало"), Segment(598.0, 599.5, " стык")],
            [Word(0.0, 0.4, "начало"), Word(598.0, 598.5, "стык")],
        ),
        "chunk_001.flac": TranscriptionResult(
            "ru",
            [Segment(0.5, 1.5, " продолжение"), Segment(500.0, 501.0, " конец")],
            [Word(500.0, 500.5, "конец")],
        ),
    }


@pytest.mark.asyncio
async def test_chunked_transcribe_shifts_and_seams(rec, monkeypatch):
    _manifest(rec)
    api = FakeApi(_two_chunk_results())
    monkeypatch.setattr(activities, "_api", api)

    details = await activities.transcribe("rec1")

    assert details["chunks"] == 2
    assert details["language"] == "ru"
    data = json.loads((rec / "segments.json").read_text())
    # chunk 0 window [0,599): both segments kept (mids 0.4, 598.75)
    # chunk 1 window [1,599): seg mid 1.0 kept → +598; seg mid 500.5 → +598
    assert [(s["start"], s["end"]) for s in data["segments"]] == [
        (0.0, 0.8),
        (598.0, 599.5),
        (598.5, 599.5),
        (1098.0, 1099.0),
    ]
    # word in chunk 1's first half-overlap is dropped (mid 0.2 < lo=1)? No:
    # the chunk-1 word starts at 500 → kept and shifted.
    assert [(w["start"], w["end"]) for w in data["words"]] == [
        (0.0, 0.4),
        (598.0, 598.5),
        (1098.0, 1098.5),
    ]
    assert (rec / "transcript.md").exists()
    # Per-chunk HTTP budget prices the CHUNK (10 min): 300+40*10-30 = 670 s.
    assert all(c["timeout_sec"] == 670.0 for c in api.calls)
    # Manifest marks both chunks done → idempotent re-run does zero POSTs.
    api.calls.clear()
    await activities.transcribe("rec1")
    assert api.calls == []


@pytest.mark.asyncio
async def test_chunked_transcribe_retries_failed_chunk(rec, monkeypatch):
    _manifest(rec)
    api = FakeApi(_two_chunk_results(), fail_once={"chunk_001.flac"})
    monkeypatch.setattr(activities, "_api", api)
    monkeypatch.setattr(activities, "_CHUNK_RETRY_BACKOFF_SEC", 0)

    details = await activities.transcribe("rec1")

    assert details["chunks"] == 2
    assert [c["file"] for c in api.calls] == [
        "chunk_000.flac",
        "chunk_001.flac",
        "chunk_001.flac",  # retry succeeded
    ]


@pytest.mark.asyncio
async def test_chunked_transcribe_failure_names_chunk(rec, monkeypatch):
    _manifest(rec)
    api = FakeApi(_two_chunk_results(), fail_once=set())
    api.results.pop("chunk_001.flac")

    def fail_chunk2(audio_path, **kw):
        if audio_path.name == "chunk_001.flac":
            raise TimeoutError("dead speaches")
        return api.results[audio_path.name]

    api.transcribe = fail_chunk2  # type: ignore[method-assign]
    monkeypatch.setattr(activities, "_api", api)

    monkeypatch.setattr(activities, "_CHUNK_RETRY_BACKOFF_SEC", 0)

    with pytest.raises(RuntimeError, match=r"chunk 2 of 2"):
        await activities.transcribe("rec1")

    # Resume state: chunk 0 persisted done; a retry must not re-POST it.
    m = activities.load_manifest(rec)
    assert m is not None
    assert m.chunks[0].transcribe == "done"
    assert m.chunks[1].transcribe == "pending"


@pytest.mark.asyncio
async def test_suspect_marked_then_rerun_with_reset_context(rec, monkeypatch):
    _manifest(rec)
    loopy = TranscriptionResult(
        "ru",
        [Segment(float(i), float(i) + 0.9, " одна и та же фраза") for i in range(5)]
        + [Segment(6.0, 7.0, " другое")],
        [],
    )
    api = FakeApi({"chunk_000.flac": loopy, "chunk_001.flac": _two_chunk_results()["chunk_001.flac"]})
    monkeypatch.setattr(activities, "_api", api)

    details = await activities.transcribe("rec1")

    assert details.get("suspect_chunks") == 1
    m = activities.load_manifest(rec)
    assert m is not None and m.chunks[0].transcribe_suspect is True
    assert m.chunks[1].transcribe_suspect is False

    # Regenerate: ONLY the suspect chunk is re-POSTed, with a reset decoder
    # context (empty prompt + condition_on_previous_text=false hook).
    api.calls.clear()
    await activities.transcribe("rec1")
    assert len(api.calls) == 1
    call = api.calls[0]
    assert call["file"] == "chunk_000.flac"
    assert call["prompt"] == ""
    assert call["c_o_p_t"] is False


@pytest.mark.asyncio
async def test_no_manifest_falls_back_to_whole_file(rec, monkeypatch):
    api = FakeApi({"audio.flac": TranscriptionResult("en", [Segment(0.0, 1.0, " hi")], [])})
    monkeypatch.setattr(activities, "_api", api)

    details = await activities.transcribe("rec1")

    assert "chunks" not in details
    assert len(api.calls) == 1
    assert api.calls[0]["file"] == "audio.flac"
    # Whole-file budget prices the RECORDING (60 min): 300+40*60-30 = 2670 s.
    assert api.calls[0]["timeout_sec"] == 2670.0


@pytest.mark.asyncio
async def test_chunked_transcribe_missing_files_tells_regenerate_chunk(rec, monkeypatch):
    _manifest(rec)
    # Simulate merge-retention: FLACs cleaned, a suspect chunk wants a re-run.
    m = activities.load_manifest(rec)
    assert m is not None
    m.chunks[1].transcribe_suspect = True
    save_manifest(m, rec)
    (rec / "chunks" / "chunk_001.flac").unlink()
    api = FakeApi(_two_chunk_results())
    monkeypatch.setattr(activities, "_api", api)

    with pytest.raises(RuntimeError, match="regenerate from stage 'chunk'"):
        await activities.transcribe("rec1")


@pytest.mark.asyncio
async def test_chunked_diarize_is_whole_file_now(rec, monkeypatch):
    """Chunks exist for ASR, but diarize must NOT consume them: DiariZen's
    global clustering needs the whole recording (per-chunk runs fragmented
    speaker identities — the 36-labels incident). One request, original
    audio, global speakers pass through unshifted."""
    _manifest(rec)
    calls: list[str] = []

    async def fake_diarize(audio, cfg, timeout_sec=None):
        calls.append(audio.name)
        return DiarizationResult(
            speakers=["spk_0", "spk_1"],
            segments=[
                DiarSegment(start=0.0, end=599.0, speaker="spk_0"),
                DiarSegment(start=599.0, end=1098.0, speaker="spk_1"),
            ],
        )

    monkeypatch.setattr(activities, "diarize_audio", fake_diarize)

    details = await activities.diarize("rec1")

    # Exactly one request, and it carried the ORIGINAL audio file.
    assert calls == ["audio.flac"]
    assert details["speakers"] == ["spk_0", "spk_1"]
    data = json.loads((rec / "diarization.json").read_text())
    # Global timeline arrives unshifted — no per-chunk stitching.
    assert [(s["start"], s["end"]) for s in data["segments"]] == [
        (0.0, 599.0),
        (599.0, 1098.0),
    ]


@pytest.mark.asyncio
async def test_chunk_activity_disabled_skips(rec, monkeypatch):
    result = await activities.chunk("rec1")
    assert result == {"skipped": "chunking disabled", "chunks": 0}
    assert activities.load_manifest(rec) is None


@pytest.mark.asyncio
async def test_chunk_activity_enabled_cuts(rec, monkeypatch):
    cfg = activities.cfg()
    cfg.chunk.enabled = True
    seen = {}

    def fake_cut(audio, meta, duration, target_min, overlap_sec, channel=None):
        seen.update(duration=duration, target_min=target_min, overlap=overlap_sec)
        return _manifest(meta)

    monkeypatch.setattr(activities, "cut_chunks", fake_cut)
    monkeypatch.setattr(activities, "split_channels", lambda audio, meta: [])

    result = await activities.chunk("rec1")

    assert result == {"chunks": 2, "target_min": 10.0}
    # duration from the recording row (3600 s), config defaults (10 min / 2 s)
    assert seen == {"duration": 3600.0, "target_min": 10.0, "overlap": 2.0}


@pytest.mark.asyncio
async def test_merge_cleans_chunk_flacs_keeps_manifest(rec, monkeypatch):
    _manifest(rec)
    # No diarization.json → merge takes the skipped path; cleanup still runs.
    result = await activities.merge_speakers("rec1")

    assert result == {"skipped": "no diarization"}
    assert list((rec / "chunks").glob("chunk_*.flac")) == []
    assert (rec / "chunks" / "chunks.json").exists()
