"""Abandoned-upload reaper.

A recording in state ``uploading`` whose row has not been touched within
``upload_ttl_hours`` is a client that never came back — crash, kill, or
lost network between the last chunk commit and finalize. Nothing
server-side ever moves such a row again (the pipeline starts only at
finalize), so without a reaper it wedges in ``uploading`` forever,
indistinguishable from a healthy in-flight upload. The sweep marks it
``failed``: visible as a problem and deletable from the UI. The partial
audio stays on disk until the recording itself is deleted.
"""

import asyncio
import logging
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import Recording, RecordingState, engine

log = logging.getLogger("transcripter.reaper")

SWEEP_INTERVAL_SEC = 900


def sweep_abandoned_uploads(
    session: Session, ttl_hours: float, now: datetime | None = None
) -> Sequence[str]:
    """Fail every ``uploading`` recording idle longer than the TTL.

    The idle comparison runs python-side on purpose: the catalog's
    DateTime columns come back tz-aware or naive depending on dialect and
    row age, and SQL-side comparison would inherit each dialect's
    normalization quirks. The candidate set (state=uploading) is tiny by
    construction.
    """
    now = now or datetime.now(UTC)
    cutoff = now - timedelta(hours=ttl_hours)
    candidates = session.scalars(
        select(Recording).where(Recording.state == RecordingState.uploading)
    ).all()
    reaped: list[str] = []
    for rec in candidates:
        touched = rec.updated_at or rec.created_at
        if touched.tzinfo is None:
            touched = touched.replace(tzinfo=UTC)
        if touched >= cutoff:
            continue
        rec.state = RecordingState.failed
        reaped.append(rec.id)
    if reaped:
        session.commit()
        log.warning("marked abandoned upload(s) failed: %s", ",".join(reaped))
    return reaped


async def reaper_loop(ttl_hours: float) -> None:
    """Sweep once at startup, then every SWEEP_INTERVAL_SEC.

    A failed sweep must never kill the loop — the next pass retries.
    """
    while True:
        try:
            with Session(engine()) as session:
                sweep_abandoned_uploads(session, ttl_hours)
        except Exception:
            log.exception("abandoned-upload sweep failed")
        await asyncio.sleep(SWEEP_INTERVAL_SEC)
