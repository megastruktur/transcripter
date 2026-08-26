"""Per-stage regenerate + artifact access."""

import asyncio
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.config import ServerConfig
from app.db import STAGE_KINDS, Stage, get_session
from app.routes.recordings import _get

router = APIRouter(prefix="/recordings")

ARTIFACTS: dict[str, list[str]] = {
    "chunk": ["meta/chunks/chunks.json"],
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
    if rec.state == RecordingState.processing:
        raise HTTPException(status_code=409, detail="recording is already processing")
    # Recordings created before a stage kind existed (e.g. `chunk`) have no
    # stage row for it; the worker's set_stage() does .one() and would fail.
    # Backfill missing rows so any stage stays a valid regenerate target.
    existing = {st.kind for st in rec.stages}
    for kind in STAGE_KINDS:
        if kind not in existing:
            session.add(Stage(recording_id=rec.id, kind=kind))
    session.commit()

    import logging

    try:
        from app import temporal_client

        workflow_id = await temporal_client.regenerate_stage(rec.id, body.stage, rec.duration_sec)
    except Exception as e:
        logging.getLogger("transcripter.api").exception("regenerate %s", rec.id)
        detail = "temporal unavailable"
        if "workflow already started" in str(e).lower():
            detail = "workflow already running for this recording"
        raise HTTPException(status_code=503, detail=detail) from e

    rec.state = RecordingState.processing
    session.commit()
    return {"workflow_id": workflow_id, "stage": body.stage}


@router.get("/{recording_id}/artifacts/{stage}")
def get_artifact(
    recording_id: str,
    stage: str,
    request: Request,
    file: str | None = None,
    session: Session = Depends(get_session),
):
    _get(recording_id, session)
    if stage not in ARTIFACTS:
        raise HTTPException(status_code=404, detail=f"no artifacts for stage {stage}")

    candidates = ARTIFACTS[stage]
    if file:
        wanted = f"meta/{Path(file).name}"
        if wanted not in candidates:
            raise HTTPException(status_code=400, detail=f"unknown artifact {file}")
        candidates = [wanted]

    cfg = _cfg(request)
    rec_root = cfg.recordings_root / recording_id
    for rel in candidates:
        p: Path = rec_root / rel
        if p.exists():
            media = "text/markdown" if p.suffix == ".md" else "application/json"
            return FileResponse(p, media_type=media)
    raise HTTPException(status_code=404, detail="artifact not generated yet")


@router.api_route("/{recording_id}/audio", methods=["GET", "HEAD"])
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


_INFLIGHT: set[asyncio.Task] = set()  # keep refs so tasks are not GC'd


def trigger_pipeline_async(rec_id: str, duration_sec: float | None) -> None:
    """Called from sync finalize handler (threadpool → no running loop)."""

    async def _start() -> None:
        import logging

        from app import temporal_client

        try:
            await temporal_client.start_pipeline(rec_id, duration_sec)
        except Exception:
            logging.getLogger("transcripter.api").exception("start_pipeline failed for %s", rec_id)
            from app.db_helpers import set_recording_failed

            set_recording_failed(rec_id)

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop is not None and loop.is_running():
        task = loop.create_task(_start())
        _INFLIGHT.add(task)
        task.add_done_callback(_INFLIGHT.discard)
    else:
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
