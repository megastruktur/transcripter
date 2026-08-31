"""Resumable upload endpoints.

POST   /recordings                → create recording (uuid) + dir + stage rows
PUT    /recordings/{id}/audio     → append chunk at ?offset=N (returns committed)
POST   /recordings/{id}/finalize  → verify sha256, size → state=processing
POST   /recordings/direct         → one-shot multipart upload (FLAC passthrough,
                                    other formats → ffmpeg transcode)
GET    /recordings                → paginated list {items,total,limit,offset}; ?limit=&offset=&q=&state= filter server-side
GET    /recordings/{id}           → detail with stages
PATCH  /recordings/{id}           → update title and/or tags; triggers re-export
DELETE /recordings/{id}           → catalog row + files
"""

import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import uuid as uuid_mod
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
)
from pydantic import BaseModel, Field
from sqlalchemy import cast, func, or_, select
from sqlalchemy.orm import Session
from sqlalchemy.types import Text

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
# Recording-type slug (Phase 0): system preset key that routes the
# pipeline. Unknown types are STORED as-is (the pipeline just matches no
# profile); only garbage shapes get a 400. EXACT twin of worker
# profiles._SAFE_TYPE — keep in sync.
TYPE_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
MAX_CHUNK = 16 * 1024 * 1024
MIN_FREE_BYTES = 2 * MAX_CHUNK


def _parse_type(raw: str | None) -> str | None:
    """Validate an optional recording-type slug; ``None``/empty → None,
    garbage shape → 400 (the client sent a broken type, not "no type")."""
    if raw is None or raw == "":
        return None
    if not TYPE_RE.match(raw):
        raise HTTPException(
            status_code=400,
            detail="type must match ^[a-z0-9][a-z0-9-]{0,31}$",
        )
    return raw


def _parse_recorded_at(raw: str | None) -> datetime | None:
    """Parse an optional ISO-8601 import backdate; ``None``/empty → None,
    unparseable → 400. Stored as NAIVE UTC (the column is a plain
    TIMESTAMP, matching created_at storage): an offset-bearing value is
    converted to UTC and stripped, a naive value is taken as UTC already.
    Serialized back as bare isoformat — UTC implied."""
    if raw is None or raw == "":
        return None
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise HTTPException(
            status_code=400, detail="recorded_at must be ISO-8601"
        ) from exc
    if dt.tzinfo is not None:
        dt = dt.astimezone(UTC).replace(tzinfo=None)
    return dt


class CreateRecording(BaseModel):
    title: str = ""
    total_bytes: int | None = None
    tags: list[str] = Field(default_factory=list)


class ChunkAck(BaseModel):
    committed: int


class FinalizeRequest(BaseModel):
    sha256: str = Field(min_length=64, max_length=64)
    duration_sec: float | None = None

class UpdateRequest(BaseModel):
    # All fields optional: PATCH updates only what is supplied (min one
    # field). The vault folder embeds the title in its name; a title
    # change is rename-only. A tags change rewrites artifacts (frontmatter
    # tags must be re-emitted) AND regenerates enrich (graph namespaces
    # are the tags). A type change re-runs summarize+enrich (new profile
    # routing). recorded_at only feeds the export frontmatter.
    title: str | None = None
    tags: list[str] | None = None
    type: str | None = None
    recorded_at: str | None = None


def _normalize_tags(raw: Iterable[str] | None) -> list[str]:
    """Trim, lower-case, drop blanks; preserve first-seen order, drop dupes.

    Empty strings and whitespace-only tags are silently discarded — they
    are user-input garbage rather than meaningful labels.
    """
    if raw is None:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            continue
        norm = item.strip().lower()
        if not norm or norm in seen:
            continue
        seen.add(norm)
        out.append(norm)
    return out


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

# ---------------------------------------------------------------------------
# Direct (one-shot) upload — POST /recordings/direct
# ---------------------------------------------------------------------------
# Mobile clients (Android WebView) don't have a reason to share the resumable
# protocol that desktop needs for large captures: a single multipart POST with
# the captured audio blob is enough. The server still owns canonicalisation:
# FLAC is stored as-is (recordings are canonical 48 kHz mono FLAC), anything
# else is decoded to WAV/whatever ffmpeg understands, downsampled to 48 kHz
# mono, and re-encoded as FLAC. The result lands at the same path as the
# resumable flow (recordings_root/<id>/audio.flac) so the worker pipeline is
# unchanged.

# 600 s — ffmpeg re-encode of a 90-min mobile capture takes ~10–30 s on
# modest hardware; the headroom here absorbs contention on a slow host and
# leaves room for unusually long captures without holding the request open
# indefinitely.
FFMPEG_TIMEOUT_SEC = 600.0
# Keep the ffmpeg stderr tail small: full ffmpeg output can be tens of KB
# of decoder probing, the actionable error is always in the last few lines.
FFMPEG_STDERR_TAIL = 2048


def _ffmpeg_transcode(src: Path, dst: Path) -> str:
    """Run ffmpeg synchronously; raise HTTPException(422) on failure.

    Returns the trimmed stderr tail for the success path so callers can log
    it (mostly empty — ffmpeg is chatty on -v error and quiet on success).
    """
    try:
        proc = subprocess.run(
            [
                "ffmpeg",
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(src),
                "-ac",
                "1",
                "-ar",
                "48000",
                str(dst),
            ],
            capture_output=True,
            timeout=FFMPEG_TIMEOUT_SEC,
            check=False,
        )
    except subprocess.TimeoutExpired:
        # A hung ffmpeg is a client-side problem (corrupt/pathological
        # input): translate to 422 instead of a bare 500 traceback.
        raise HTTPException(
            status_code=422,
            detail=f"ffmpeg timed out after {int(FFMPEG_TIMEOUT_SEC)}s",
        ) from None
    if proc.returncode != 0:
        tail = (proc.stderr or b"").decode("utf-8", errors="replace")
        if len(tail) > FFMPEG_STDERR_TAIL:
            tail = tail[-FFMPEG_STDERR_TAIL:]
        raise HTTPException(
            status_code=422,
            detail=f"ffmpeg failed (exit {proc.returncode}): {tail or 'no stderr'}",
        )
    return (proc.stderr or b"").decode("utf-8", errors="replace")[-FFMPEG_STDERR_TAIL:]


@router.post("/direct", status_code=201)
async def create_recording_direct(
    request: Request,
    file: UploadFile = File(...),
    title: str = Form(default=""),
    tags: str = Form(default="[]"),
    duration_sec: float | None = Form(default=None),
    type: str = Form(default=""),
    recorded_at: str = Form(default=""),
    session: Session = Depends(get_session),
) -> dict:
    """One-shot multipart upload. See module docstring for the contract.

    Validation runs before any DB / FS side effects: an empty file rejects
    with 400, malformed `tags` JSON rejects with 400. Import backdate
    fields (Phase 0): `type` is an optional slug (garbage → 400, unknown
    type stored as-is — the pipeline just matches no profile) and
    `recorded_at` an optional ISO-8601 timestamp (garbage → 400). After
    the recording row is committed, any transcode failure (or silent
    FLAC) tears down the directory + row so the client can retry from a
    clean slate.
    """
    cfg = _cfg(request)

    rec_type = _parse_type(type)
    rec_recorded_at = _parse_recorded_at(recorded_at)

    # Tags come in as a JSON-encoded string — multipart form fields can't
    # carry typed list[str] cleanly, and we want the client to be able to
    # submit an empty array (Form(default="[]") above) without a separate
    # "no tags" path.
    try:
        raw_tags = json.loads(tags) if tags else []
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail=f"invalid tags JSON: {exc.msg}") from exc


    # Probe the first 4 bytes without consuming the underlying stream.
    head = await file.read(4)
    if len(head) == 0:
        raise HTTPException(status_code=400, detail="empty audio file")
    await file.seek(0)
    is_flac = head == FLAC_MAGIC

    # Pre-allocate the id + directory + stage rows so the transcode can write
    # straight into the canonical path. On any post-commit failure we roll
    # back the row and rmtree the dir.
    rec_id = str(uuid_mod.uuid4())
    rec_dir = cfg.recordings_root / rec_id
    target = audio_path(cfg, rec_id)
    rec_dir.mkdir(parents=True, exist_ok=True)
    (rec_dir / "meta").mkdir(parents=True, exist_ok=True)

    # Same disk guard as the resumable flow: one-shot uploads are unbounded
    # in size, so reject before writing when storage is nearly full. Runs
    # AFTER the mkdir above so statvfs never sees a missing path.
    if free_bytes(cfg.storage.path) < MIN_FREE_BYTES:
        shutil.rmtree(rec_dir, ignore_errors=True)
        raise HTTPException(status_code=507, detail="storage almost full")

    rec = Recording(
        id=rec_id,
        title=title,
        tags=_normalize_tags(raw_tags),
        duration_sec=duration_sec,
        type=rec_type,
        recorded_at=rec_recorded_at,
    )
    session.add(rec)
    for kind in STAGE_KINDS:
        session.add(Stage(recording_id=rec.id, kind=kind))

    def _cleanup_on_failure() -> None:
        # The Recording/Stage rows were added but NEVER flushed (all failure
        # paths fire before the first commit): Session.delete() on a pending
        # instance raises InvalidRequestError. rollback() expunges them.
        try:
            session.rollback()
        except Exception:
            logging.getLogger("transcripter.api").exception(
                "cleanup: rollback failed for %s", rec_id
            )
        shutil.rmtree(rec_dir, ignore_errors=True)


    try:
        if is_flac:
            # Stream the upload straight to the canonical FLAC path; no
            # intermediate copy.
            with open(target, "wb") as out:
                while True:
                    chunk = await file.read(1024 * 1024)
                    if not chunk:
                        break
                    out.write(chunk)
        else:
            tmp_in = rec_dir / "_input.bin"
            with open(tmp_in, "wb") as out:
                while True:
                    chunk = await file.read(1024 * 1024)
                    if not chunk:
                        break
                    out.write(chunk)
            try:
                await asyncio.to_thread(_ffmpeg_transcode, tmp_in, target)
            finally:
                tmp_in.unlink(missing_ok=True)
    except HTTPException:
        # transcode failure / future validation 422s: tear down before
        # re-raising so no orphan dir / row survives a 4xx response.
        _cleanup_on_failure()
        raise
    except Exception:
        _cleanup_on_failure()
        raise

    # Empty (size==0) catch — protects against a race where the upload sent
    # only the probed 4 bytes and EOF landed right after.
    if target.stat().st_size == 0:
        _cleanup_on_failure()
        raise HTTPException(status_code=400, detail="empty audio file")

    # Re-use the silent-capture gate from the resumable flow: a header-only
    # FLAC is valid container-wise but Whisper will return an empty
    # transcript and diarization will 500 downstream.
    if not has_audio_frames(target):
        _cleanup_on_failure()
        raise HTTPException(
            status_code=422,
            detail=(
                "recording contains no audio frames — the capture produced "
                "silence; check microphone permissions and input device"
            ),
        )

    h = hashlib.sha256()
    size = 0
    with open(target, "rb") as f:
        while chunk := f.read(1024 * 1024):
            h.update(chunk)
            size += len(chunk)
    rec.committed_bytes = size
    rec.sha256 = h.hexdigest()
    rec.state = RecordingState.processing
    session.commit()

    request.app.state.on_finalize(rec.id, duration_sec)  # → Temporal
    return {"id": rec.id}


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
        tags=_normalize_tags(body.tags),
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
        # tags is TEXT[] on Postgres / JSON on sqlite; casting to Text gives
        # both dialects a common ilike surface — Postgres renders
        # `tags::text` (a "{a,b,c}" string the substring matches), sqlite
        # json_extract makes JSON usable as text for ilike.
        tags_as_text = cast(Recording.tags, Text)
        condition = or_(
            Recording.title.ilike(f"%{escaped}%", escape="\\"),
            Recording.id.ilike(f"%{escaped}%", escape="\\"),
            tags_as_text.ilike(f"%{escaped}%", escape="\\"),
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
async def update_recording(
    recording_id: str,
    body: UpdateRequest,
    session: Session = Depends(get_session),
) -> dict:
    rec = _get(recording_id, session)
    # PATCH semantics: only the supplied fields are touched. At least one
    # field must be set or the call is a no-op that would still trigger an
    # export cycle.
    if (
        body.title is None
        and body.tags is None
        and body.type is None
        and body.recorded_at is None
    ):
        raise HTTPException(status_code=400, detail="no fields to update")
    # Validate BEFORE mutating: a garbage type/recorded_at must not leave
    # a half-updated row.
    new_type = _parse_type(body.type) if body.type is not None else None
    new_recorded_at = (
        _parse_recorded_at(body.recorded_at) if body.recorded_at is not None else None
    )
    type_changed = body.type is not None and rec.type != new_type
    tags_changed = body.tags is not None and rec.tags != _normalize_tags(body.tags)
    if body.title is not None:
        rec.title = body.title.strip()
    if body.tags is not None:
        rec.tags = _normalize_tags(body.tags)
    if body.type is not None:
        rec.type = new_type
    if body.recorded_at is not None:
        rec.recorded_at = new_recorded_at
    session.commit()
    # Side effects, in order of blast radius. Everything is fire-and-
    # forget: the DB change stands even if Temporal is down (worker
    # .backfill is the recovery path).
    try:
        if rec.state == RecordingState.done and (type_changed or tags_changed):
            # Phase 0 (plan §0.3): a done recording whose tags/type changed
            # regenerates the knowledge graph — enrich writes into the
            # (new) tag namespaces, and the old namespaces are purged by
            # the origin-scoped DETACH DELETE. A type change additionally
            # re-runs summarize (different profile prompt/artifact); the
            # workflow then cascades enrich + export itself, so one
            # start at `summarize` covers both. A tags-only change starts
            # at `enrich` (profile routing is by type — tags can't change
            # the summarize profile any more).
            start_stage = "summarize" if type_changed else "enrich"
            await temporal_client.regenerate_stage(rec.id, start_stage, rec.duration_sec)
            # The regenerate workflow's finally-block exports the note
            # folder, so no separate start_export is needed on this path.
        else:
            await temporal_client.start_export(
                rec.id,
                rename_only=(
                    body.tags is None
                    and body.type is None
                    and body.recorded_at is None
                ),
            )
    except Exception:
        logging.getLogger("transcripter.api").exception("start_export failed for %s", rec.id)
    return serialize_recording(rec)


def serialize_recording(rec: Recording) -> dict:
    return {
        "id": rec.id,
        "title": rec.title,
        "tags": list(rec.tags or []),
        "type": rec.type,
        "recorded_at": rec.recorded_at.isoformat() if rec.recorded_at else None,
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
                "details": s.details or {},
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
    from app.vault import delete_recording_folders

    cfg = _cfg(request)
    rec = _get(recording_id, session)
    session.delete(rec)
    session.commit()
    shutil.rmtree(cfg.recordings_root / rec.id, ignore_errors=True)
    # Vault side: the exported folder (notes + .transcripter/ audio +
    # manifest) goes with the catalog row — id8-scoped, app-owned content.
    delete_recording_folders(cfg, rec.id)
    return Response(status_code=204)
