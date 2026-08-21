"""Resumable upload endpoints.

POST   /recordings                → create recording (uuid) + dir + stage rows
PUT    /recordings/{id}/audio     → append chunk at ?offset=N (returns committed)
POST   /recordings/{id}/finalize  → verify sha256, size → state=processing
GET    /recordings                → list with stages
GET    /recordings/{id}           → detail with stages
DELETE /recordings/{id}           → catalog row + files
"""

import hashlib
import os
import re
import shutil
import uuid as uuid_mod
from pathlib import Path

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import ServerConfig
from app.db import (
    STAGE_KINDS,
    Recording,
    RecordingState,
    Stage,
    get_session,
)

router = APIRouter(prefix="/recordings")

UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
MAX_CHUNK = 16 * 1024 * 1024
MIN_FREE_BYTES = 2 * MAX_CHUNK


class CreateRecording(BaseModel):
    title: str = ""
    total_bytes: int | None = None


class ChunkAck(BaseModel):
    committed: int


class FinalizeRequest(BaseModel):
    sha256: str = Field(min_length=64, max_length=64)
    duration_sec: float | None = None


def audio_path(cfg: ServerConfig, rec_id: str) -> Path:
    return cfg.recordings_root / rec_id / "audio.flac"


def _cfg(request: Request) -> ServerConfig:
    return request.app.state.config


def _get(recording_id: str, session: Session) -> Recording:
    if not UUID_RE.match(recording_id):
        raise HTTPException(status_code=400, detail="invalid recording id")
    rec = session.get(Recording, recording_id)
    if rec is None:
        raise HTTPException(status_code=404, detail="recording not found")
    return rec


def free_bytes(path: Path) -> int:
    st = os.statvfs(path)
    return st.f_bavail * st.f_frsize


@router.post("", status_code=201)
def create_recording(
    body: CreateRecording,
    request: Request,
    session: Session = Depends(get_session),
) -> dict:
    cfg = _cfg(request)
    rec = Recording(
        id=str(uuid_mod.uuid4()),
        title=body.title,
        total_bytes=body.total_bytes,
    )
    session.add(rec)
    for kind in STAGE_KINDS:
        session.add(Stage(recording_id=rec.id, kind=kind))
    session.commit()

    rec_dir = cfg.recordings_root / rec.id
    (rec_dir / "meta").mkdir(parents=True, exist_ok=True)

    return {"id": rec.id}


@router.put("/{recording_id}/audio")
async def upload_chunk(
    recording_id: str,
    request: Request,
    offset: int,
    content_length: int = Header(...),
    session: Session = Depends(get_session),
) -> ChunkAck:
    cfg = _cfg(request)
    rec = _get(recording_id, session)

    if rec.state != RecordingState.uploading:
        raise HTTPException(status_code=409, detail=f"recording is {rec.state.value}")

    if content_length > MAX_CHUNK:
        raise HTTPException(status_code=413, detail="chunk too large (max 16MB)")
    if not 0 <= offset <= rec.committed_bytes:
        raise HTTPException(
            status_code=409,
            detail=f"offset {offset} out of range [0, {rec.committed_bytes}]",
        )

    cfg.storage.path.mkdir(parents=True, exist_ok=True)
    if free_bytes(cfg.storage.path) < MIN_FREE_BYTES:
        raise HTTPException(status_code=507, detail="insufficient storage space")

    body = await request.body()
    if len(body) != content_length:
        raise HTTPException(
            status_code=400, detail="body size does not match Content-Length"
        )

    # Resume: discard bytes already committed, then append the rest.
    target = audio_path(cfg, rec.id)
    target.parent.mkdir(parents=True, exist_ok=True)
    overlap = min(rec.committed_bytes - offset, len(body))
    with open(target, "ab") as f:
        f.write(body[overlap:])

    rec.committed_bytes += len(body) - overlap
    session.commit()
    return ChunkAck(committed=rec.committed_bytes)


@router.post("/{recording_id}/finalize")
def finalize(
    recording_id: str,
    body: FinalizeRequest,
    request: Request,
    session: Session = Depends(get_session),
) -> dict:
    cfg = _cfg(request)
    rec = _get(recording_id, session)

    if rec.state != RecordingState.uploading:
        return {"state": rec.state.value}

    target = audio_path(cfg, rec.id)
    h = hashlib.sha256()
    size = 0
    with open(target, "rb") as f:
        while chunk := f.read(1024 * 1024):
            h.update(chunk)
            size += len(chunk)

    if h.hexdigest() != body.sha256:
        # Session survives; client resumes from committed offset or re-finalizes.
        raise HTTPException(
            status_code=409,
            detail=f"sha256 mismatch: server={h.hexdigest()} client={body.sha256}",
        )

    rec.sha256 = body.sha256
    rec.duration_sec = body.duration_sec
    rec.state = RecordingState.processing
    session.commit()

    request.app.state.on_finalize(rec.id)  # hook wired to Temporal in T3
    return {"state": rec.state.value, "size": size}


@router.get("")
def list_recordings(
    session: Session = Depends(get_session),
) -> list[dict]:
    recs = session.scalars(
        select(Recording).order_by(Recording.created_at.desc())
    ).all()
    return [serialize_recording(r) for r in recs]


@router.get("/{recording_id}")
def get_recording(
    recording_id: str,
    session: Session = Depends(get_session),
) -> dict:
    rec = _get(recording_id, session)
    return serialize_recording(rec)


def serialize_recording(rec: Recording) -> dict:
    return {
        "id": rec.id,
        "title": rec.title,
        "state": rec.state.value,
        "committed_bytes": rec.committed_bytes,
        "total_bytes": rec.total_bytes,
        "duration_sec": rec.duration_sec,
        "created_at": rec.created_at.isoformat(),
        "stages": [
            {
                "kind": s.kind,
                "status": s.status.value,
                "attempts": s.attempts,
                "last_error": s.last_error,
                "updated_at": s.updated_at.isoformat(),
            }
            for s in rec.stages
        ],
    }


@router.delete("/{recording_id}", status_code=204)
def delete_recording(
    recording_id: str,
    request: Request,
    session: Session = Depends(get_session),
) -> Response:
    cfg = _cfg(request)
    rec = _get(recording_id, session)
    session.delete(rec)
    session.commit()
    shutil.rmtree(cfg.recordings_root / rec.id, ignore_errors=True)
    return Response(status_code=204)
