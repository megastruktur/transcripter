"""Tag-scoped endpoints.

Wave C: POST /tags/{tag}/digest builds a markdown digest note of the
last N done recordings carrying the tag; GET /tags/{tag}/digest serves
the generated note back as text/markdown. Phase 3: GET
/tags/{tag}/timeline returns the tag's done sessions (newest first)
with their meta/events.json events, aggregated entities and the
digest-generated flag — served from Postgres + events.json only, no
graph session.

Tag normalization mirrors recordings.py: trim + lowercase. After that the
regex pins down file-system-safe forms (the digest activity writes
``digests/<tag>.md`` under the transcripts dir) — anything outside the
regex returns 400, with the same rationale the recordings module uses:
empty/whitespace tags are garbage rather than meaningful labels.
"""

from __future__ import annotations

import logging
import re
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from app import temporal_client
from app.config import ServerConfig
from app.db import get_session
from app.vault import find_digest, scan_timeline

router = APIRouter(prefix="/tags")

_LOG = logging.getLogger("transcripter.api.tags")


# After trim+lowercase: must start with a word character (Unicode letter,
# digit or underscore), then word chars, spaces, dots, underscores, dashes
# — free-form user grouping per Phase 0 (a tag like "dnd dark castle" or
# «мой замок» is fine). First char not a space; ≤64 chars so the
# slugified digest filename always fits a filename segment. EXACT TWIN of
# worker digest._SAFE_TAG_RE — the worker must accept everything this
# regex accepts; change the two IN SYNC.
_TAG_RE = re.compile(r"^[\w][ \w.-]{0,63}$", re.UNICODE)


class DigestRequest(BaseModel):
    last_n: int = Field(default=5, ge=1, le=50)


def _normalize_tag(raw: str) -> str:
    """Trim + lowercase. Mirrors recordings._normalize_tags semantics
    (one element)."""
    return raw.strip().lower()


def _validate_tag(tag: str) -> None:
    """Apply the regex; 400 on anything outside it.

    Why here and not in the worker: the activity would already produce a
    clear ValueError, but only after a Postgres pull + a (possibly
    expensive) Neo4j session — failing at the boundary is cheaper and
    gives the user instant feedback in the UI.
    """
    if not tag:
        raise HTTPException(status_code=400, detail="tag is empty")
    if not _TAG_RE.match(tag):
        raise HTTPException(
            status_code=400,
            detail=(
                "tag must match ^[\\w][ \\w.-]{0,63}$ (unicode word chars, "
                "spaces, dots, underscores, dashes; must not start with a space)"
            ),
        )




@router.get("/{tag}/timeline")
def get_timeline(
    tag: Annotated[str, Path()],
    request: Request,
    session: Session = Depends(get_session),
) -> dict:
    """Phase 3: per-tag session timeline (newest first) + aggregated
    entities + whether a digest note exists. Postgres + events.json
    artifacts only — no graph session, so a timeline renders with the
    graph profile off."""
    norm = _normalize_tag(tag)
    _validate_tag(norm)
    cfg: ServerConfig = request.app.state.config
    payload = scan_timeline(cfg, session, norm)
    if not payload["sessions"]:
        raise HTTPException(404, detail=f"no recordings for tag {norm}")
    return payload


@router.post("/{tag}/digest", status_code=202)
async def post_digest(
    body: DigestRequest,
    request: Request,
    tag: Annotated[str, Path()],
) -> dict:
    cfg: ServerConfig = request.app.state.config
    norm = _normalize_tag(tag)
    _validate_tag(norm)
    if not cfg.graph.enabled:
        # Same UX shape as the recordings PATCH-409: a concrete error
        # the client can surface in the UI, not a Temporal 500 cascade.
        raise HTTPException(
            status_code=409,
            detail=(
                "graph backend not configured (graph.uri empty) — start the "
                "compose graph profile or set graph.uri in config.yaml"
            ),
        )
    try:
        # Attribute access (not ``from app.temporal_client import start_digest``)
        # so the conftest's monkeypatch on the module attribute is honored.
        workflow_id = await temporal_client.start_digest(norm, body.last_n)
    except Exception:  # noqa: BLE001 — same blind-catch shape as recordings.update_recording
        # Temporal being unreachable should not look like a 500 to the
        # client: same shape as PATCH rename uses (log + 503).
        _LOG.exception("start_digest failed for tag=%s", norm)
        raise HTTPException(
            status_code=503, detail="temporal unavailable; try again later"
        )
    return {"workflow_id": workflow_id, "tag": norm, "last_n": body.last_n}


@router.get("/{tag}/digest")
def get_digest(tag: Annotated[str, Path()], request: Request) -> FileResponse:
    """Serve the generated digest note for a tag (Phase 1).

    The worker names files by slug, so the API cannot reconstruct the
    filename from the raw tag — lookup is frontmatter matching, shared
    with the vault scan (app.vault.find_digest). No graph required:
    reading a note must work even with the graph profile off.
    """
    norm = _normalize_tag(tag)
    _validate_tag(norm)
    md = find_digest(request.app.state.config, norm)
    if md is None:
        raise HTTPException(
            status_code=404, detail=f"digest not generated yet for tag {norm}"
        )
    return FileResponse(md, media_type="text/markdown")


class TagCount(BaseModel):
    tag: str
    count: int


class TagListResponse(BaseModel):
    items: list[TagCount]


@router.get("")
def list_tags(request: Request, session: Session = Depends(get_session)) -> dict:
    """Distinct free tags with recording counts (Phase 0): the source for
    the client's tag suggestions. Counts include recordings in ANY state
    (a tag on an uploading capture is real user intent) — ordering is
    count DESC then tag ASC so the UI shows popular tags first and the
    tail is deterministic. Dialect split mirrors worker digest
    ``_select_recordings``: Postgres unnests the TEXT[], SQLite explodes
    the JSON array (tests).
    """

    dialect_name = session.get_bind().dialect.name
    if dialect_name == "postgresql":
        rows = session.execute(
            text(
                "SELECT tag, count(*) AS count FROM recordings, "
                "unnest(recordings.tags) AS tag "
                "WHERE tag <> '' GROUP BY tag "
                "ORDER BY count DESC, tag ASC"
            )
        ).all()
    else:
        rows = session.execute(
            text(
                "SELECT value AS tag, count(*) AS count FROM recordings, "
                "json_each(recordings.tags) "
                "WHERE value <> '' GROUP BY value "
                "ORDER BY count DESC, tag ASC"
            )
        ).all()
    return {"items": [{"tag": row[0], "count": row[1]} for row in rows]}