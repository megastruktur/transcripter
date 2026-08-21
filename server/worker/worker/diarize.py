"""Diarization via LinTO HTTP service."""

import logging
from pathlib import Path

import httpx
from pydantic import BaseModel

log = logging.getLogger("transcripter.diarize")


class DiarSegment(BaseModel):
    start: float
    end: float
    speaker: str


class DiarizationResult(BaseModel):
    speakers: list[str]
    segments: list[DiarSegment]


async def diarize_audio(audio: Path, cfg) -> DiarizationResult:
    endpoint = cfg.diarization.endpoint.rstrip("/")
    async with httpx.AsyncClient(timeout=3600) as client:
        with open(audio, "rb") as f:
            r = await client.post(
                f"{endpoint}/diarization",
                files={"file": (audio.name, f)},
                headers={"accept": "application/json"},
            )
    r.raise_for_status()
    data = r.json()

    segments = [
        DiarSegment(start=float(s["start"]), end=float(s["end"]), speaker=str(s["speaker"]))
        for s in data.get("segments", [])
    ]
    speakers = sorted({s.speaker for s in segments})
    return DiarizationResult(speakers=speakers, segments=segments)
