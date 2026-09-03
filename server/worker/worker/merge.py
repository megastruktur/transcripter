"""Merge word timestamps with diarization segments (max overlap attribution).

Stereo recordings: words carry `channel` ("mic"/"system") and diarization
speakers are namespaced by channel ("mic:spk_0"). Attribution is
channel-first — a mic word can never land on a system speaker, so the two
sources' timelines stay disjoint even where they overlap in time.
"""

import json
from pathlib import Path


def _candidates(segments: list[dict], channel: str | None) -> list[dict]:
    """Segments a word of `channel` may be attributed to.

    Channel namespacing is by speaker prefix (see _diarize_chunked). When
    no segment carries the prefix (mono diarization, or a stereo recording
    regenerated with diarization from a different path) fall back to ALL
    segments — channel info must never orphan a word."""
    if channel is None:
        return segments
    prefixed = [s for s in segments if str(s["speaker"]).startswith(f"{channel}:")]
    return prefixed or segments


def merge(words: list[dict], segments: list[dict]) -> list[dict]:
    """Assign each word the speaker of the diarization segment with max
    overlap (within the word's channel when the data carries channels)."""
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
        candidates = _candidates(segments, w.get("channel"))
        best_speaker, best_overlap = None, 0.0
        for seg in candidates:
            overlap = min(w["end"], seg["end"]) - max(w["start"], seg["start"])
            if overlap > best_overlap:
                best_overlap = overlap
                best_speaker = seg["speaker"]
        if best_speaker is None and candidates:
            # No overlap: nearest segment by center distance.
            center = (w["start"] + w["end"]) / 2
            best_speaker = min(
                candidates,
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
