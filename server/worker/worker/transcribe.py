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
    @staticmethod
    def from_json(path: Path) -> "TranscriptionResult":
        """Inverse of to_json — used to resume a chunked transcription from
        persisted per-chunk results without re-POSTing done chunks."""
        data = json.loads(path.read_text(encoding="utf-8"))
        return TranscriptionResult(
            data.get("language", "unknown"),
            [Segment(s["start"], s["end"], s["text"]) for s in data.get("segments", [])],
            [Word(w["start"], w["end"], w["text"]) for w in data.get("words", [])],
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
        self._model: Any = None

    def _ensure_loaded(self) -> Any:
        if self._model is None:
            # Lazy import: tests and API-only environments never load the model.
            from faster_whisper import WhisperModel

            # download_root keeps the weights in the `models` docker volume
            # (compose worker mount) so a container recreate doesn't
            # re-download from huggingface.co. cpu_threads=8 was tuned for
            # the bundled local fallback; the API backend on the voice
            # stack is the primary path.
            self._model = WhisperModel(
                self.model_name,
                device="cpu",
                compute_type="int8",
                cpu_threads=8,
                download_root="/models",
            )
        return self._model

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

    def transcribe(
        self,
        audio_path: Path,
        timeout_sec: float = 600.0,
        prompt: str | None = None,
        condition_on_previous_text: bool | None = None,
    ) -> TranscriptionResult:
        """POST the audio; `timeout_sec` is the caller's scaled budget.

        The default covers unit tests and manual use; the pipeline passes
        activities.budget_transcribe() so client and Temporal budgets agree.

        `prompt`/`condition_on_previous_text` are the repetition-loop escape
        hatch for suspect chunks (see worker/chunk.py). Speaches 0.8.3
        ignores unknown form fields, so `condition_on_previous_text` is a
        forward hook: it takes effect once the voice stack runs a Speaches
        version that accepts it. Fields are sent only when not None.
        """
        import httpx

        data: dict[str, Any] = {
            "model": self.model,
            "response_format": "verbose_json",
            # Word timestamps are what diarization merging keys off;
            # without them merge has nothing to attribute. OpenAI's
            # form field is repeated with a `[]` suffix.
            "timestamp_granularities[]": ["word", "segment"],
        }
        if prompt is not None:
            data["prompt"] = prompt
        if condition_on_previous_text is not None:
            data["condition_on_previous_text"] = (
                "true" if condition_on_previous_text else "false"
            )

        with open(audio_path, "rb") as f:
            headers = {"authorization": f"Bearer {self.api_key}"} if self.api_key else {}
            r = httpx.post(
                f"{self.base_url}/audio/transcriptions",
                files={"file": (audio_path.name, f)},
                data=data,
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
