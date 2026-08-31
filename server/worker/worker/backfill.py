"""Backfill: re-export notes for all done recordings.

    docker compose exec worker python -m worker.backfill

Every recording goes through the SAME subprocess+timeout+abandon wrapper as
the export_transcript activity — a dead NAS mount must never wedge this
process either (r4#2). Refuses upfront unless the sentinel/mount looks
healthy: backfill is the designated recovery path, so it must fail fast and
loudly rather than silently produce nothing.
"""

import asyncio
import logging
import sys

from .activities import export_transcript
from .config import load_config
from .db import Recording, RecordingState, init_engine, session
from .export import check_sentinel

log = logging.getLogger("transcripter.backfill")

_CHILD_TIMEOUT_SEC = 120


async def _run_one(rec_id: str) -> dict:
    # Reuse the activity function directly (it is a plain async callable
    # behind the @activity.defn decorator for non-intercepted use).
    # Full export (rename_only omitted): backfill rewrites artifacts — it is
    # the designated recovery/migration path.
    return await export_transcript({"recording_id": rec_id})  # type: ignore[call-arg]


def _done_recording_ids() -> list[str]:
    with session() as s:
        return [
            r.id
            for r in s.query(Recording)
            .filter(Recording.state == RecordingState.done)
            .order_by(Recording.created_at)
            .all()
        ]




async def amain() -> int:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    cfg = load_config()
    init_engine(cfg.database.url)
    check_sentinel(cfg.vault.path, cfg.vault.sentinel)

    rec_ids = _done_recording_ids()
    log.info("backfill: %d done recordings", len(rec_ids))
    failed = 0
    for rec_id in rec_ids:
        res = await _run_one(rec_id)
        note = res.get("transcript_note", "")
        if note.startswith("error"):
            failed += 1
            log.error("%s: %s", rec_id, note)
        else:
            log.info("%s: %s", rec_id, note or "skipped")
    if failed:
        log.error("backfill finished with %d failures", failed)
        return 1
    log.info("backfill complete")
    return 0


def main() -> None:
    raise SystemExit(asyncio.run(amain()))


if __name__ == "__main__":
    main()
