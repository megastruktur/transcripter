"""Vault overview endpoint (Phase 3).

One row per distinct free tag: session count, aggregated entity count,
last activity, and digest state (ready/stale/none). The heavy lifting —
tag aggregation, events.json reads, digest frontmatter matching — lives
in app.vault and is shared with the /tags/{tag}/timeline endpoint.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.config import ServerConfig
from app.db import get_session
from app.vault import scan_vault

router = APIRouter(prefix="/vault")


@router.get("")
def get_vault(
    request: Request, session: Session = Depends(get_session)
) -> dict:
    cfg: ServerConfig = request.app.state.config
    return {"items": scan_vault(cfg, session)}
