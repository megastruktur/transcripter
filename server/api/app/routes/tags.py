"""Tag-scoped endpoints.

Wave C: POST /tags/{tag}/digest builds a markdown digest note of the
last N done recordings carrying the tag.

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

from fastapi import APIRouter, HTTPException, Path, Request
from pydantic import BaseModel, Field

from app import temporal_client
from app.config import ServerConfig

router = APIRouter(prefix="/tags")

_LOG = logging.getLogger("transcripter.api.tags")

# After trim+lowercase: must start with an alphanumeric, then any mix of
# lowercase alphanumerics, dots, underscores, dashes, and spaces (spaces
# are allowed — they survive every Obsidian / Linux / Windows file system
# that touches the transcripts dir, and the activity sanitizes the
# on-disk name to ``[a-z0-9._-]`` only).
_TAG_RE = re.compile(r"^[a-z0-9][a-z0-9 ._-]*$")


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
                "tag must match ^[a-z0-9][a-z0-9 ._-]*$ (lowercase alphanum, "
                "dots, underscores, dashes, spaces; must start with alphanum)"
            ),
        )


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