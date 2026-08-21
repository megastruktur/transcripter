"""Merge word timestamps with diarization segments (max overlap attribution)."""

import json
from pathlib import Path


def merge(words: list[dict], segments: list[dict]) -> list[dict]:
    """Assign each word the speaker of the diarization segment with max overlap."""
    turns: list[dict] = []
    current_speaker = None
    current_words: list[dict] = []

    def flush() -> None:
        nonlocal current_words
        if current_words:
            turns.append(
                {
                    "speaker": current_speaker,
                    "start": current_words[0]["start"],
                    "end": current_words[-1]["end"],
                    "text": "".join(w["text"] for w in current_words).strip(),
                }
            )
            current_words = []

    for w in words:
        best_speaker, best_overlap = None, 0.0
        for seg in segments:
            overlap = min(w["end"], seg["end"]) - max(w["start"], seg["start"])
            if overlap > best_overlap:
                best_overlap = overlap
                best_speaker = seg["speaker"]
        if best_speaker is None and segments:
            # No overlap: nearest segment by center distance.
            center = (w["start"] + w["end"]) / 2
            best_speaker = min(
                segments,
                key=lambda s: abs(center - (s["start"] + s["end"]) / 2),
            )["speaker"]
        if best_speaker != current_speaker:
            flush()
            current_speaker = best_speaker
        current_words.append(w)
    flush()
    return turns


def write_diarized_transcript(meta: Path) -> int:
    segments_data = json.loads((meta / "segments.json").read_text())
    diar_data = json.loads((meta / "diarization.json").read_text())

    words = segments_data.get("words", [])
    # Group words into turns by speaker.
    turns = merge(words, diar_data["segments"])

    from .transcribe import fmt_ts

    lines = ["# Diarized transcript", ""]
    for t in turns:
        lines.append(f"**{t['speaker']} [{fmt_ts(t['start'])} – {fmt_ts(t['end'])}]:** {t['text']}")
        lines.append("")
    (meta / "diarized-transcript.md").write_text("\n".join(lines), encoding="utf-8")
    return len(turns)
