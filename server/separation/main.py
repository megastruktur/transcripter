"""Speech separation HTTP service: pyannote SpeechSeparation (PixIT/ToTaToNet).

POST /separation (multipart "file") →
  {
    "speakers": ["SPEAKER_00", ...],
    "segments": [{"start": float, "end": float, "speaker": str}, ...]
  }
Segments cover the full timeline INCLUDING overlaps (a moment with two
active speakers appears once per speaker) — the diarization.json
contract the worker's merge stage consumes.

The per-speaker full-length WAVs (16 kHz mono, zeroed outside the
speaker's activity) power the worker's overlap-splice: transcribing a
short overlap window on the separated stream recovers words the mixed
transcript interleaved. They are fetched per speaker via
GET /separation/wavs/<job>/<speaker> after the JSON call.

Model: pyannote/speech-separation-ami-1.0 (gated; baked into the image
at build time — runtime needs no HF token).
"""

from __future__ import annotations

import io
import logging
import os
import shutil
import threading
import uuid
from pathlib import Path

import torch
import torchaudio
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("separation")

app = FastAPI(title="transcripter-separation")

_MODEL = None
_LOCK = threading.Lock()
DEVICE = os.environ.get("SEPARATION_DEVICE", "cpu")

_WAV_JOBS = Path(os.environ.get("SEPARATION_WAV_DIR", "/tmp/separation-wavs"))
_WAV_JOBS.mkdir(parents=True, exist_ok=True)
_WAV_TTL_SEC = int(os.environ.get("SEPARATION_WAV_TTL_SEC", "7200"))

def _patch_speechbrain() -> None:
    """pyannote 4.0.4 → speechbrain 1.x API break: pyannote passes
    token=/huggingface_cache_dir= into Pretrained.from_hparams, which
    speechbrain 1.x forwards to Pretrained.__init__ and crashes. Strip
    them; gated downloads authorize via the HF_TOKEN env var instead."""
    from speechbrain.inference.interfaces import Pretrained

    if getattr(Pretrained.from_hparams, "_transcripter_patched", False):
        return
    orig = Pretrained.from_hparams.__func__

    @classmethod
    def patched(cls, *args, **kwargs):
        for k in ("token", "revision", "huggingface_cache_dir"):
            kwargs.pop(k, None)
        # pyannote's run_opts (torch.device) trips speechbrain's own
        # device-type check; drop it and let speechbrain default to CPU.
        kwargs.pop("run_opts", None)
        return orig(cls, *args, **kwargs)

    patched._transcripter_patched = True
    Pretrained.from_hparams = patched


def get_model():
    global _MODEL
    with _LOCK:
        if _MODEL is None:
            _patch_speechbrain()
            from pyannote.audio import Pipeline

            # The image bakes the full HF cache (pipeline + submodels);
            # loading by repo id resolves everything from that cache.
            model_id = os.environ.get(
                "SEPARATION_MODEL", "pyannote/speech-separation-ami-1.0"
            )
            log.info("loading SpeechSeparation pipeline %s", model_id)
            _MODEL = Pipeline.from_pretrained(model_id)
            _MODEL = _MODEL.to(torch.device(DEVICE))
            log.info("pipeline ready on %s", DEVICE)
    return _MODEL


@app.get("/healthcheck")
def healthcheck() -> dict:
    return {"status": "ok", "model_loaded": _MODEL is not None}


@app.post("/separation")
async def separation(file: UploadFile = File(...)) -> dict:
    data = await file.read()
    if not data:
        raise HTTPException(400, "empty file")

    model = get_model()
    # Decode once: pyannote re-decodes the source per window otherwise.
    waveform, sample_rate = torchaudio.load(io.BytesIO(data))

    try:
        diarization, separation = model(
            {"waveform": waveform, "sample_rate": sample_rate},
        )
    except Exception as e:  # noqa: BLE001 — surface any model failure as 500
        log.exception("separation failed")
        raise HTTPException(500, f"separation failed: {e}") from e

    segments = [
        {
            "start": round(float(seg.start), 3),
            "end": round(float(seg.end), 3),
            "speaker": str(spk),
        }
        for seg, _, spk in diarization.itertracks(yield_label=True)
    ]
    speakers = sorted({s["speaker"] for s in segments})
    if not speakers:
        return {"speakers": [], "segments": [], "job": None}

    # Per-speaker full-length streams: average the sliding-window outputs.
    arr = separation.data  # (num_chunks, num_samples, num_speakers)
    if arr.ndim == 3:
        arr = arr.mean(axis=0)
    if sample_rate != 16000:
        arr = torchaudio.functional.resample(
            torch.from_numpy(arr).T, sample_rate, 16000
        ).T.numpy()

    job = uuid.uuid4().hex[:12]
    out_dir = _WAV_JOBS / job
    out_dir.mkdir(parents=True, exist_ok=True)
    for idx, spk in enumerate(speakers):
        torchaudio.save(
            str(out_dir / f"{spk}.wav"),
            torch.from_numpy(arr[:, idx]).unsqueeze(0).float(),
            16000,
        )
    _gc_wav_jobs()

    return {"speakers": speakers, "segments": segments, "job": job}


@app.get("/separation/wavs/{job}/{speaker}")
def get_wav(job: str, speaker: str) -> FileResponse:
    if "/" in job or "/" in speaker or ".." in (job, speaker):
        raise HTTPException(400, "bad path")
    p = _WAV_JOBS / job / f"{speaker}.wav"
    if not p.is_file():
        raise HTTPException(404, "no wav for job/speaker")
    return FileResponse(p, media_type="audio/wav")


def _gc_wav_jobs() -> None:
    """Best-effort TTL sweep of fetched-wav jobs."""
    now = __import__("time").time()
    for d in _WAV_JOBS.iterdir():
        if d.is_dir() and now - d.stat().st_mtime > _WAV_TTL_SEC:
            shutil.rmtree(d, ignore_errors=True)
