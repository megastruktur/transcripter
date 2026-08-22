"""Unit tests for pure pipeline logic (merge, transcribe helpers)."""

import json
from pathlib import Path

from worker.merge import merge


def test_merge_assigns_by_max_overlap():
    words = [
        {"start": 0.0, "end": 1.0, "text": " hello"},
        {"start": 1.0, "end": 2.0, "text": " world"},
        {"start": 2.0, "end": 3.0, "text": " privet"},
    ]
    segments = [
        {"start": 0.0, "end": 1.5, "speaker": "Speaker 1"},
        {"start": 1.5, "end": 3.5, "speaker": "Speaker 2"},
    ]
    turns = merge(words, segments)
    assert turns[0]["speaker"] == "Speaker 1"
    assert turns[0]["text"] == "hello world"
    assert turns[1]["speaker"] == "Speaker 2"
    assert turns[1]["text"] == "privet"


def test_merge_gap_falls_back_to_nearest():
    words = [{"start": 10.0, "end": 10.5, "text": " lone"}]
    segments = [
        {"start": 0.0, "end": 5.0, "speaker": "A"},
        {"start": 9.0, "end": 9.8, "speaker": "B"},
    ]
    turns = merge(words, segments)
    assert turns[0]["speaker"] == "B"


def test_merge_single_speaker_one_turn():
    words = [
        {"start": 0.0, "end": 1.0, "text": " a"},
        {"start": 1.0, "end": 2.0, "text": " b"},
    ]
    segments = [{"start": 0.0, "end": 2.0, "speaker": "Solo"}]
    turns = merge(words, segments)
    assert len(turns) == 1
    assert turns[0]["speaker"] == "Solo"
    assert turns[0]["text"] == "a b"


def test_write_diarized(tmp_path: Path):
    (tmp_path / "segments.json").write_text(
        json.dumps(
            {
                "language": "ru",
                "segments": [],
                "words": [
                    {"start": 0.0, "end": 1.0, "text": " alpha"},
                    {"start": 1.2, "end": 2.0, "text": " beta"},
                ],
            }
        )
    )
    (tmp_path / "diarization.json").write_text(
        json.dumps(
            {
                "speakers": ["S1", "S2"],
                "segments": [
                    {"start": 0.0, "end": 1.1, "speaker": "S1"},
                    {"start": 1.1, "end": 2.0, "speaker": "S2"},
                ],
            }
        )
    )
    from worker.merge import write_diarized_transcript

    turns = write_diarized_transcript(tmp_path)
    out = (tmp_path / "diarized-transcript.md").read_text()
    assert turns == 2
    assert "S1" in out and "S2" in out


def test_diarize_maps_linto_field_names():
    """LinTO emits seg_begin/seg_end/spk_id; merge consumes start/end/speaker."""
    import asyncio
    from types import SimpleNamespace

    import httpx

    from worker import diarize as diarize_mod

    payload = {
        "segments": [
            {"seg_begin": 1.077, "seg_end": 6.207, "seg_id": 1, "spk_id": "spk1"},
            {"seg_begin": 6.5, "seg_end": 9.0, "seg_id": 2, "spk_id": "spk2"},
        ],
        "speakers": [{"duration": 5.13, "nbr_seg": 1, "spk_id": "spk1"}],
    }

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, **kw):
            return httpx.Response(200, json=payload, request=httpx.Request("POST", url))

    orig = diarize_mod.httpx.AsyncClient
    diarize_mod.httpx.AsyncClient = lambda **kw: FakeClient()
    try:
        audio = Path(__file__)  # any readable file; the client is faked
        cfg = SimpleNamespace(diarization=SimpleNamespace(endpoint="http://diar"))
        result = asyncio.run(diarize_mod.diarize_audio(audio, cfg))
    finally:
        diarize_mod.httpx.AsyncClient = orig

    assert result.speakers == ["spk1", "spk2"]
    assert result.segments[0].start == 1.077
    assert result.segments[0].end == 6.207
    assert result.segments[0].speaker == "spk1"
    # Output must be consumable by merge() without further translation.
    turns = merge(
        [{"start": 2.0, "end": 3.0, "text": " hi"}],
        [s.model_dump() for s in result.segments],
    )
    assert turns[0]["speaker"] == "spk1"


def test_merge_speakers_activity_skips_without_diarization(tmp_path: Path, monkeypatch):
    """No usable diarization → skipped, not a `None`-labelled transcript."""
    import asyncio

    from worker import activities
    from worker.db import StageStatus

    calls: list[tuple[str, StageStatus]] = []
    monkeypatch.setattr(
        activities,
        "set_stage",
        lambda rec_id, kind, status, **kw: calls.append((kind, status)),
    )
    monkeypatch.setattr(activities, "meta_dir", lambda rec_id: tmp_path)

    # Case 1: diarization.json absent (the diarize stage failed).
    out = asyncio.run(activities.merge_speakers("rec"))
    assert out == {"skipped": "no diarization"}
    assert calls[-1] == ("merge_speakers", StageStatus.skipped)

    # Case 2: present but empty (diarizer found no speakers), and a previous
    # run left an artifact behind — it must not survive the skip.
    (tmp_path / "diarization.json").write_text(json.dumps({"speakers": [], "segments": []}))
    (tmp_path / "diarized-transcript.md").write_text("**None [00:00 – 00:15]:** stale")
    out = asyncio.run(activities.merge_speakers("rec"))
    assert out == {"skipped": "no diarization"}
    assert calls[-1] == ("merge_speakers", StageStatus.skipped)
    assert not (tmp_path / "diarized-transcript.md").exists()
