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


def overlap_windows(segments: list[dict], min_len: float = 0.3, pad: float = 0.5) -> list[dict]:
    """Time windows where 2+ speakers are active (Layer 2 separation input).

    Separation's segments cover overlaps (a moment can carry several
    speakers), so an overlap is simply a time covered by 2+ segments.
    Returns windows with `speakers` (the active set) padded by `pad`
    seconds for ASR context."""
    events: list[tuple[float, int, str]] = []  # (time, ±1 delta, speaker)
    for seg in segments:
        events.append((max(0.0, seg["start"]), 1, str(seg["speaker"])))
        events.append((seg["end"], -1, str(seg["speaker"])))
    events.sort(key=lambda e: (e[0], -e[1]))

    windows: list[dict] = []
    active: set[str] = set()
    start = None
    for time, delta, spk in events:
        was_multi = len(active) >= 2
        if delta > 0:
            active.add(spk)
        else:
            active.discard(spk)
        if not was_multi and len(active) >= 2:
            start = time
        elif was_multi and len(active) < 2:
            if start is not None and time - start >= min_len:
                windows.append(
                    {"start": max(0.0, start - pad), "end": time + pad, "speakers": None}
                )
            start = None
    return windows


def splice_overlaps(
    words: list[dict],
    separated_words: dict[str, list[dict]],
    windows: list[dict],
) -> list[dict]:
    """Replace mixed-transcript words inside overlap windows with words
    transcribed on per-speaker separated streams (Layer 2).

    `separated_words` maps speaker → word list from transcribing that
    speaker's separated WAV. Words outside windows pass through untouched
    (their mixed attribution is fine — separation streams lose a little
    fidelity to the mix). Returns the new word list, time-ordered."""
    result: list[dict] = []
    wi = 0
    for w in words:
        while wi < len(windows) and w["start"] >= windows[wi]["end"]:
            wi += 1
        in_window = wi < len(windows) and w["start"] >= windows[wi]["start"]
        if not in_window:
            result.append(w)
    # Insert separated words window by window, each tagged with its speaker.
    for win in windows:
        for spk, ws in separated_words.items():
            inside = [w for w in ws if win["start"] <= w["start"] < win["end"]]
            for w in inside:
                result.append({**w, "speaker": spk, "separated": True})
    result.sort(key=lambda w: w["start"])
    return result


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
