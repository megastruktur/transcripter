"""Phase 3.5 backfill: index existing recordings' transcripts.

    docker compose exec worker python -m worker.backfill_index [--tag TAG]

Walks every done recording with a transcript.md (no Temporal, no LLM —
pure embedding + sqlite writes) and runs the SAME ``index_segments``
the enrich activity calls, into each recording's own tags' index files.
Idempotent: re-running re-embeds and replaces (DELETE + INSERT per
recording). The first run against a fresh embed backend seeds every
namespace; subsequent runs are only needed after a model switch (the
index meta mismatch rebuild catches those lazily anyway — this script
is the eager path).
"""

from __future__ import annotations

import argparse
import logging
import sys

from .config import load_config
from .db import Recording, RecordingState, init_engine, session
from .semantic_index import index_segments

log = logging.getLogger("transcripter.backfill_index")


def _recordings(tag: str | None) -> list[tuple[str, str, list[str]]]:
    """(rec_id, title, tags, meta_dir) for done recordings carrying the
    tag filter (if any) that have a transcript.md."""
    with session() as s:
        rows = (
            s.query(Recording)
            .filter(Recording.state == RecordingState.done)
            .order_by(Recording.created_at)
            .all()
        )
        out: list[tuple[str, str, list[str]]] = []
        for r in rows:
            tags = list(r.tags or [])
            if tag is not None and tag not in tags:
                continue
            out.append((r.id, r.title or "", tags))
        return out


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--tag",
        help="only recordings carrying this free tag (default: all)",
    )
    args = parser.parse_args(argv)

    cfg = load_config()
    init_engine(cfg.database.url)
    roots = cfg.recordings_root

    recs = _recordings(args.tag)
    log.info("backfill_index: %d candidate recordings", len(recs))
    indexed = 0
    empty = 0
    failed = 0
    for rec_id, title, tags in recs:
        meta_dir = roots / rec_id / "meta"
        if not (meta_dir / "transcript.md").is_file():
            continue
        for t in tags or ["untagged"]:
            try:
                n = index_segments(rec_id, t, title, meta_dir, cfg.transcripts.path, cfg)
                indexed += n
                if n == 0:
                    empty += 1
            except Exception:
                failed += 1
                log.exception("backfill_index: %s (tag %r) failed", rec_id, t)
        log.info("%s: done (tags=%s)", rec_id, ",".join(tags) or "untagged")
    log.info(
        "backfill_index complete: %d segments indexed, %d empty segmentations, "
        "%d failures",
        indexed,
        empty,
        failed,
    )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
