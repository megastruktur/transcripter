"""Diarization HTTP service: BUT-FIT DiariZen (EEND-VC hybrid).

Contract (drop-in for the `diarization` compose DNS name):
  POST /diarization  (multipart file=audio) →
      {"speakers": [...], "segments": [{"start", "end", "speaker"}]}
  GET  /healthcheck → {"status": "ok", "model_loaded": bool}

The whole recording is diarized in ONE pass: DiariZen's strength is its
global VBx clustering over the full timeline (the pilot resolved 6-7
speakers where per-chunk pipelines fragmented into 36 labels). The
service therefore never chunks; a 123-min upload holds ~1.6 GiB RSS.

Speakers come back as spk_0..spk_N (N ≤ max_speakers, default 20) —
globally consistent, no per-chunk renumbering.

Model: BUT-FIT/diarizen-wavlm-base-s80-md (pruned, ~350 MB, CC-BY-NC-4.0
weights + MIT code — personal self-host use). Override with DIARIZEN_MODEL
(e.g. the larger diarizen-wavlm-large-s80-md-v2 for quality runs).
"""

from __future__ import annotations

import logging
import os
import threading

import torchaudio
from fastapi import FastAPI, File, HTTPException, UploadFile
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("diarizen")

app = FastAPI(title="transcripter-diarizen")

_MODEL = None
_LOCK = threading.Lock()  # one CPU inference at a time
MODEL_ID = os.environ.get("DIARIZEN_MODEL", "BUT-FIT/diarizen-wavlm-base-s80-md")


class Segment(BaseModel):
    start: float
    end: float
    speaker: str


def get_model():
    global _MODEL
    if _MODEL is None:
        from diarizen.pipelines.inference import DiariZenPipeline

        log.info("loading %s", MODEL_ID)
        _MODEL = DiariZenPipeline.from_pretrained(MODEL_ID)
        log.info("model ready")
    return _MODEL


@app.api_route("/healthcheck", methods=["GET", "HEAD"])
def healthcheck() -> dict:
    return {"status": "ok", "model_loaded": _MODEL is not None}


@app.post("/diarization")
async def diarization(file: UploadFile = File(...)) -> dict:
    import io
    import tempfile
    from pathlib import Path

    data = await file.read()
    if not data:
        raise HTTPException(400, "empty upload")

    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        tmp.write(data)
        tmp_path = Path(tmp.name)

    try:
        with _LOCK:
            pipeline = get_model()
            try:
                result = pipeline(str(tmp_path))
            except Exception:
                log.exception("diarization failed")
                raise HTTPException(500, "diarization failed")
    finally:
        tmp_path.unlink(missing_ok=True)

    segments = []
    speakers: set[str] = set()
    for turn, _, label in result.itertracks(yield_label=True):
        speaker = f"spk_{label}"
        speakers.add(speaker)
        segments.append(
            Segment(start=round(turn.start, 3), end=round(turn.end, 3), speaker=speaker)
        )
    return {"speakers": sorted(speakers), "segments": [s.model_dump() for s in segments]}
