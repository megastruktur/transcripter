"""Capture-preset sync endpoints.

GET    /presets          → flat list of ALL rows incl. tombstones, id-sorted
PUT    /presets/{id}     → upsert (resurrects a tombstone); server stamps updated_at
DELETE /presets/{id}     → tombstone (row kept); idempotent
"""

from __future__ import annotations

import re

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import CapturePreset, get_session, utcnow
from app.routes.recordings import TYPE_RE, _normalize_tags

router = APIRouter(prefix="/presets")

# Client-generated preset id, e.g. "preset-vet-x7k2": lowercase alnum +
# hyphens, ≤64 chars — same charset family as recording ids and type
# slugs; underscores/uppercase are invalid on purpose.
_PRESET_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_NAME_MAX = 100


class PresetUpsert(BaseModel):
    """PUT /presets/{preset_id} body. ``rec_type`` "" = no type."""

    model_config = ConfigDict(extra="forbid")

    name: str
    rec_type: str = ""
    tags: list[str] = Field(default_factory=list)


def _serialize(row: CapturePreset) -> dict:
    return {
        "id": row.id,
        "name": row.name,
        "rec_type": row.rec_type,
        "tags": row.tags,
        "deleted": row.deleted,
        "updated_at": row.updated_at.isoformat(),
    }


@router.get("")
def list_presets(session: Session = Depends(get_session)) -> list[dict]:
    """The sync feed: EVERY row including tombstones (clients drop rows
    with deleted=True), sorted by id for stable diffs between fetches."""
    rows = session.scalars(select(CapturePreset).order_by(CapturePreset.id)).all()
    return [_serialize(r) for r in rows]


@router.put("/{preset_id}")
def put_preset(
    body: PresetUpsert, preset_id: str, session: Session = Depends(get_session)
) -> dict:
    """Upsert by client-generated id. Last write wins BY DEFINITION: the
    server stamps updated_at itself, so no clock-skew handling. A PUT on
    a tombstone resurrects it (deleted=False). All validation happens
    BEFORE any mutation."""
    if not _PRESET_ID_RE.match(preset_id):
        raise HTTPException(
            status_code=400,
            detail="preset id must match ^[a-z0-9][a-z0-9-]{0,63}$",
        )
    name = body.name.strip()
    if not name or len(name) > _NAME_MAX:
        raise HTTPException(
            status_code=400, detail=f"name must be 1..{_NAME_MAX} chars after trim"
        )
    # "" = no type; anything else must be a valid type slug — keep in
    # sync with recordings.TYPE_RE (imported above, so one shared
    # object; underscore MUST be rejected).
    if body.rec_type != "" and not TYPE_RE.match(body.rec_type):
        raise HTTPException(
            status_code=400,
            detail="rec_type must match ^[a-z0-9][a-z0-9-]{0,31}$ or be empty",
        )

    row = session.get(CapturePreset, preset_id)
    if row is None:
        row = CapturePreset(id=preset_id)
        session.add(row)
    row.name = name
    row.rec_type = body.rec_type
    row.tags = _normalize_tags(body.tags)
    row.deleted = False
    row.updated_at = utcnow()
    session.commit()
    return _serialize(row)


@router.delete("/{preset_id}")
def delete_preset(preset_id: str, session: Session = Depends(get_session)) -> dict:
    """Tombstone, not row removal: the row stays so other devices learn
    of the deletion (deleted=True + fresh updated_at). Idempotent —
    deleting an already-deleted row just returns it."""
    row = session.get(CapturePreset, preset_id)
    if row is None:
        raise HTTPException(status_code=404, detail="preset not found")
    row.deleted = True
    row.updated_at = utcnow()
    session.commit()
    return _serialize(row)
