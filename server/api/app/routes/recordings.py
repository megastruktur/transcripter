"""Resumable upload endpoints.

POST   /recordings                → create recording (uuid) + dir + stage rows
PUT    /recordings/{id}/audio     → append chunk at ?offset=N (returns committed)
POST   /recordings/{id}/finalize  → verify sha256, size → state=processing
GET    /recordings                → paginated list {items,total,limit,offset}; ?limit=&offset=&q=&state= filter server-side
GET    /recordings/{id}           → detail with stages
PATCH  /recordings/{id}           → rename (trimmed title, empty allowed) + re-export note
DELETE /recordings/{id}           → catalog row + files
"""

import hashlib
import logging
import os
import re
import shutil
import uuid as uuid_mod
from pathlib import Path

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app import temporal_client
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

class RenameRequest(BaseModel):
    title: str = ""


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


# METADATA_BLOCK_HEADER: 1 byte (last-block flag + type) + 3 bytes big-endian
# length. A FLAC that captured no samples is magic + STREAMINFO and nothing
# more.
FLAC_MAGIC = b"fLaC"


def has_audio_frames(path: Path) -> bool:
    """True when the FLAC carries at least one audio frame after the metadata.

    A recording that captured no samples (mic held by another process, muted
    input) still encodes to a valid header-only stream. Whisper happily
    returns an empty transcript for it and diarization then 500s deep in the
    pipeline, so reject it here where the client can still act on it.

    Non-FLAC payloads pass: container validation belongs to the decoder, not
    to the upload layer.
    """
    size = path.stat().st_size
    with open(path, "rb") as f:
        if f.read(len(FLAC_MAGIC)) != FLAC_MAGIC:
            return True
        # Walk the metadata blocks; audio frames follow the one whose
        # last-block flag is set.
        while True:
            header = f.read(4)
            if len(header) < 4:
                return False  # truncated metadata: no frames to be had
            last = header[0] & 0x80
            f.seek(int.from_bytes(header[1:4], "big"), os.SEEK_CUR)
            if last:
                return f.tell() < size


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
        raise HTTPException(status_code=400, detail="body size does not match Content-Length")

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
    if not target.exists():
        raise HTTPException(
            status_code=409,
            detail="no audio uploaded yet — send chunks before finalize",
        )

    h = hashlib.sha256()
    size = 0
    with open(target, "rb") as f:
        while chunk := f.read(1024 * 1024):
            h.update(chunk)
            size += len(chunk)

    if h.hexdigest() != body.sha256:
        raise HTTPException(
            status_code=409,
            detail=f"sha256 mismatch: server={h.hexdigest()} client={body.sha256}",
        )

    if not has_audio_frames(target):
        # Unrecoverable: no retry can add samples that were never captured.
        # Mark it failed so it shows up in the UI instead of sitting in
        # `uploading` while the client retries a doomed finalize.
        rec.state = RecordingState.failed
        session.commit()
        raise HTTPException(
            status_code=422,
            detail=(
                "recording contains no audio frames — the capture produced "
                "silence; check microphone permissions and input device"
            ),
        )

    rec.sha256 = body.sha256
    rec.duration_sec = body.duration_sec
    rec.state = RecordingState.processing
    session.commit()

    request.app.state.on_finalize(rec.id, body.duration_sec)  # → Temporal
    return {"state": rec.state.value, "size": size}


@router.get("")
def list_recordings(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    q: str = Query(default="", max_length=200),
    state: RecordingState | None = None,
    session: Session = Depends(get_session),
) -> dict:
    stmt = select(Recording)
    count_stmt = select(func.count(Recording.id))
    if state is not None:
        stmt = stmt.where(Recording.state == state)
        count_stmt = count_stmt.where(Recording.state == state)
    needle = q.strip()
    if needle:
        escaped = needle.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        condition = or_(
            Recording.title.ilike(f"%{escaped}%", escape="\\"),
            Recording.id.ilike(f"%{escaped}%", escape="\\"),
        )
        stmt = stmt.where(condition)
        count_stmt = count_stmt.where(condition)
    total = session.scalar(count_stmt) or 0
    recs = session.scalars(
        # created_at is not unique; id is the deterministic tiebreak so
        # offset paging never shuffles rows with equal timestamps.
        stmt.order_by(Recording.created_at.desc(), Recording.id.desc())
        .limit(limit)
        .offset(offset)
    ).all()
    return {
        "items": [serialize_recording(r) for r in recs],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.get("/{recording_id}")
def get_recording(
    recording_id: str,
    session: Session = Depends(get_session),
) -> dict:
    rec = _get(recording_id, session)
    return serialize_recording(rec)

@router.patch("/{recording_id}")
async def rename_recording(
    recording_id: str,
    body: RenameRequest,
    session: Session = Depends(get_session),
) -> dict:
    rec = _get(recording_id, session)
    rec.title = body.title.strip()
    session.commit()
    # The exported Obsidian note embeds the title in its filename, so
    # re-export it. Fire-and-forget: the rename stands even if Temporal is
    # down (worker.backfill is the recovery path).
    try:
        await temporal_client.start_export(rec.id)
    except Exception:
        logging.getLogger("transcripter.api").exception("start_export failed for %s", rec.id)
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
