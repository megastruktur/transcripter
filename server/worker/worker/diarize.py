"""Diarization HTTP client: DiariZen (primary) / LinTO (fallback dialect).

DiariZen (EEND-VC: overlap-aware local windows + global VBx clustering)
must see the WHOLE recording in one request — its global speaker
consistency is the entire point (the pilot resolved 6-7 speakers where
per-chunk runs fragmented into 36 labels). Never chunk for it.

LinTO (seg_begin/seg_end/spk_id dialect) remains parseable for rollback:
an external endpoint pinned via DIARIZATION_ENDPOINT may still be LinTO.
The response shape tells them apart — DiariZen speaks start/end/speaker
natively, LinTO speaks seg_begin/seg_end/spk_id.
"""

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


async def diarize_audio(audio: Path, cfg, timeout_sec: float = 3600.0) -> DiarizationResult:
    endpoint = cfg.diarization.endpoint.rstrip("/")
    async with httpx.AsyncClient(timeout=timeout_sec) as client:
        with open(audio, "rb") as f:
            r = await client.post(
                f"{endpoint}/diarization",
                files={"file": (audio.name, f)},
                headers={"accept": "application/json"},
            )
    r.raise_for_status()
    data = r.json()

    raw = data.get("segments", [])
    # Dialect sniff: DiariZen speaks start/end/speaker natively; LinTO
    # speaks seg_begin/seg_end/spk_id. Translate LinTO at this boundary
    # only — the rest of the pipeline (merge.py) speaks start/end/speaker.
    if raw and "seg_begin" in raw[0]:
        segments = [
            DiarSegment(
                start=float(s["seg_begin"]),
                end=float(s["seg_end"]),
                speaker=str(s["spk_id"]),
            )
            for s in raw
        ]
    else:
        segments = [
            DiarSegment(
                start=float(s["start"]),
                end=float(s["end"]),
                speaker=str(s["speaker"]),
            )
            for s in raw
        ]
    speakers = sorted({s.speaker for s in segments})
    return DiarizationResult(speakers=speakers, segments=segments)
