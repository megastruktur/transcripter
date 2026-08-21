"""Per-stage regenerate + artifact access."""

import asyncio
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.config import ServerConfig
from app.db import STAGE_KINDS, get_session
from app.routes.recordings import _get

router = APIRouter(prefix="/recordings")

ARTIFACTS: dict[str, list[str]] = {
    "transcribe": ["meta/transcript.md", "meta/segments.json"],
    "diarize": ["meta/diarization.json"],
    "merge_speakers": ["meta/diarized-transcript.md"],
    "summarize": ["meta/summary.md"],
}


class RegenerateRequest(BaseModel):
    stage: str


def _cfg(request: Request) -> ServerConfig:
    return request.app.state.config


@router.post("/{recording_id}/regenerate")
async def regenerate(
    recording_id: str,
    body: RegenerateRequest,
    request: Request,
    session: Session = Depends(get_session),
) -> dict:
    from app.db import RecordingState

    if body.stage not in STAGE_KINDS:
        raise HTTPException(status_code=400, detail=f"unknown stage {body.stage}")

    rec = _get(recording_id, session)
    if rec.state == RecordingState.uploading:
        raise HTTPException(status_code=409, detail="recording not uploaded yet")

    try:
        from app import temporal_client

        workflow_id = await temporal_client.regenerate_stage(rec.id, body.stage, rec.duration_sec)
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"temporal unavailable: {e}") from e

    rec.state = RecordingState.processing
    session.commit()
    return {"workflow_id": workflow_id, "stage": body.stage}


@router.get("/{recording_id}/artifacts/{stage}")
def get_artifact(
    recording_id: str,
    stage: str,
    request: Request,
    session: Session = Depends(get_session),
):
    _get(recording_id, session)
    if stage not in ARTIFACTS:
        raise HTTPException(status_code=404, detail=f"no artifacts for stage {stage}")

    cfg = _cfg(request)
    rec_root = cfg.recordings_root / recording_id
    for rel in ARTIFACTS[stage]:
        p: Path = rec_root / rel
        if p.exists():
            media = "text/markdown" if p.suffix == ".md" else "application/json"
            return FileResponse(p, media_type=media)
    raise HTTPException(status_code=404, detail="artifact not generated yet")


@router.get("/{recording_id}/audio")
def get_audio(
    recording_id: str,
    request: Request,
    session: Session = Depends(get_session),
):
    from app.db import RecordingState

    rec = _get(recording_id, session)
    if rec.state == RecordingState.uploading:
        raise HTTPException(status_code=409, detail="recording not uploaded yet")
    cfg = _cfg(request)
    p = cfg.recordings_root / recording_id / "audio.flac"
    if not p.exists():
        raise HTTPException(status_code=404, detail="audio file missing")
    return FileResponse(p, media_type="audio/flac")


def trigger_pipeline_async(rec_id: str, duration_sec: float | None) -> None:
    """Called from sync finalize handler; schedules Temporal start."""

    async def _start() -> None:
        from app import temporal_client

        await temporal_client.start_pipeline(rec_id, duration_sec)

    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_start())
    except RuntimeError:
        asyncio.run(_start())


@router.get("/{recording_id}/summary")
def summary_plain(
    recording_id: str,
    request: Request,
    session: Session = Depends(get_session),
) -> PlainTextResponse:
    _get(recording_id, session)
    cfg = _cfg(request)
    p = cfg.recordings_root / recording_id / "meta" / "summary.md"
    if not p.exists():
        raise HTTPException(status_code=404, detail="summary not generated")
    return PlainTextResponse(p.read_text(encoding="utf-8"), media_type="text/markdown")
