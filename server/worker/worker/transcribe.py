"""Transcription stage: faster-whisper local or OpenAI-compatible API."""

import json
import logging
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger("transcripter.transcribe")


@dataclass
class Word:
    start: float
    end: float
    text: str


@dataclass
class Segment:
    start: float
    end: float
    text: str


@dataclass
class TranscriptionResult:
    language: str
    segments: list[Segment]
    words: list[Word]

    def to_json(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "language": self.language,
                    "segments": [
                        {"start": s.start, "end": s.end, "text": s.text}
                        for s in self.segments
                    ],
                    "words": [
                        {"start": w.start, "end": w.end, "text": w.text} for w in self.words
                    ],
                },
                ensure_ascii=False,
                indent=1,
            )
        )


def segments_to_markdown(result: TranscriptionResult, path: Path) -> None:
    lines = [f"# Transcript ({result.language})", ""]
    for s in result.segments:
        lines.append(f"**[{fmt_ts(s.start)} – {fmt_ts(s.end)}]** {s.text.strip()}")
        lines.append("")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def fmt_ts(sec: float) -> str:
    m, s = divmod(int(sec), 60)
    h, m = divmod(m, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


class LocalTranscriber:
    """faster-whisper, lazily initialized once per worker process."""

    def __init__(self, model_name: str) -> None:
        self.model_name = model_name

    def _ensure_loaded(self) -> Any:
        # Lazy import: tests and API-only environments never load the model.
        from faster_whisper import WhisperModel

        # cpu_threads=8 was tuned for the bundled local fallback; the API
        # backend on the voice stack is the primary path.
        return WhisperModel(self.model_name, device="cpu", compute_type="int8", cpu_threads=8)

    def transcribe(self, audio_path: Path) -> TranscriptionResult:
        model = self._ensure_loaded()
        segments_iter: Iterator[Any]
        info: Any
        segments_iter, info = model.transcribe(str(audio_path), word_timestamps=True)
        segments: list[Segment] = []
        words: list[Word] = []
        for seg in segments_iter:
            segments.append(Segment(seg.start, seg.end, seg.text))
            for w in seg.words or []:
                words.append(Word(w.start, w.end, w.word))
        return TranscriptionResult(info.language, segments, words)


class ApiTranscriber:
    """OpenAI-compatible /audio/transcriptions endpoint (non-streaming)."""

    def __init__(self, base_url: str, model: str, api_key: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key

    def transcribe(self, audio_path: Path, timeout_sec: float = 600.0) -> TranscriptionResult:
        """POST the audio; `timeout_sec` is the caller's scaled budget.

        The default covers unit tests and manual use; the pipeline passes
        activities.budget_transcribe() so client and Temporal budgets agree.
        """
        import httpx

        with open(audio_path, "rb") as f:
            headers = {"authorization": f"Bearer {self.api_key}"} if self.api_key else {}
            r = httpx.post(
                f"{self.base_url}/audio/transcriptions",
                files={"file": (audio_path.name, f)},
                data={
                    "model": self.model,
                    "response_format": "verbose_json",
                    # Word timestamps are what diarization merging keys off;
                    # without them merge has nothing to attribute. OpenAI's
                    # form field is repeated with a `[]` suffix.
                    "timestamp_granularities[]": ["word", "segment"],
                },
                headers=headers,
                timeout=timeout_sec,
            )
        r.raise_for_status()
        data = r.json()
        segments = [
            Segment(s["start"], s["end"], s["text"]) for s in data.get("segments", [])
        ]
        # OpenAI/Speaches put words top-level; Groq nests them in segments.
        # Accept both: prefer the top-level array when present.
        raw_words = data.get("words") or [
            w for s in data.get("segments", []) for w in s.get("words", [])
        ]
        words = [Word(w["start"], w["end"], w["word"]) for w in raw_words]
        return TranscriptionResult(data.get("language", "unknown"), segments, words)
