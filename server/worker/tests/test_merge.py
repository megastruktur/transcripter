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
