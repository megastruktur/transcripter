"""Tag-memory purge: wipe ONE tag's knowledge-graph memory.

"Memory" of a tag is five stores, all created by the enrich pipeline:
1. the Neo4j namespace (every node carries ``tag``; DETACH DELETE in
   batches, same bound as graph_gc);
2. the ``graph_edits`` rows (the Phase A overlay) — stale edits would
   otherwise be re-applied on top of the fresh rebuild;
3. the digest note (``<transcripts>/digests/<slug>.md``, incl. the
   ``-N`` collision variants);
4. the semantic index (``<transcripts>/indexes/<slug>.sqlite``);
5. the per-recording ``meta/events.json`` timeline artifacts — for
   recordings whose ONLY tag is the purged one (2026-09-04 pathfinder
   incident: purge + regenerating a single recording left the OTHER
   recordings' timelines alive, and the vault tag page aggregated them
   back as if the memory had never been wiped). Multi-tag recordings
   keep their artifact: the file is shared by every tag's timeline of
   that recording, and wiping it would take the surviving tags'
   histories down with it.

Recordings, audio and transcripts are NOT touched: they are the input
the enrich stage rebuilds the memory FROM.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from neo4j import GraphDatabase
from sqlalchemy import text

from .db import Recording, RecordingState, session

log = logging.getLogger("transcripter.purge")

# Same bound as graph_gc: one huge DETACH DELETE can exceed the Neo4j
# tx timeout / heap; the loop sweeps until the namespace is empty.
_BATCH_SIZE = 10000

_PURGE_CYPHER = (
    "MATCH (n {tag: $tag}) "
    "WITH n LIMIT $limit "
    "DETACH DELETE n "
    "RETURN count(n) AS deleted"
)


def purge_tag_memory(cfg: Any, tag: str) -> dict:
    """Wipe the tag's memory (graph namespace + edits + digest + index).

    Idempotent: every step is a no-op when there is nothing to delete.
    Best-effort per store with a combined failure raise — the workflow
    surfaces the error to the API caller; a partial purge is safe
    because rebuild (enrich) rewrites every store anyway.
    """
    counts: dict[str, int] = {}

    # 1. Neo4j namespace, batched.
    driver = GraphDatabase.driver(
        cfg.graph.uri, auth=(cfg.graph.user, os.environ.get(cfg.graph.password_env, ""))
    )
    try:
        total = 0
        with driver.session(database=cfg.graph.database) as s:
            while True:
                rec = s.run(_PURGE_CYPHER, tag=tag, limit=_BATCH_SIZE).single()
                deleted = rec["deleted"] if rec else 0
                total += deleted
                if deleted < _BATCH_SIZE:
                    break
        counts["graph_nodes"] = total
    finally:
        driver.close()

    # 2. graph_edits rows for the tag (overlay would resurrect stale
    # corrections on the rebuilt graph otherwise).
    with session() as s:
        res = s.execute(text("DELETE FROM graph_edits WHERE tag = :tag"), {"tag": tag})
        counts["graph_edits"] = res.rowcount or 0
        s.commit()

    # 3+4. Digest note + semantic index files.
    transcripts = Path(cfg.vault.path)
    from .digest import _existing_digest_for_tag

    digests_dir = transcripts / "digests"
    removed: list[str] = []
    digest = _existing_digest_for_tag(digests_dir, tag)
    if digest is not None:
        try:
            digest.unlink()
            removed.append(digest.name)
        except OSError:
            log.exception("purge: digest note %s could not be removed", digest)
    counts["digest_files"] = len(removed)

    from .semantic_index import index_path

    idx = index_path(transcripts, tag)
    index_removed = 0
    if idx.is_file():
        try:
            idx.unlink()
            index_removed = 1
        except OSError:
            log.exception("purge: index %s could not be removed", idx)
    counts["index_files"] = index_removed

    # 5. Per-recording events.json timeline artifacts — single-tag
    # recordings only (see module docstring). The rebuild (or a manual
    # per-recording regenerate) re-writes the artifact from the fresh
    # extraction; until then the vault timeline hides the recording.
    counts["events_json"] = _purge_events_json(cfg, tag, transcripts)

    log.info("purge: tag %r wiped: %s", tag, counts)
    return counts


def _purge_events_json(cfg: Any, tag: str, transcripts: Path) -> int:
    """Delete ``meta/events.json`` for the tag's DONE recordings that
    carry NO other tag. Storage copy + every vault mirror. Returns the
    number of recordings whose artifact was removed (at least one copy).

    Best-effort per recording: an unlink failure is logged and the sweep
    continues (a half-purged tag is recoverable by re-running the purge
    — it is idempotent).
    """
    from sqlalchemy import text as _text

    from .export import Rec, scan_recording_folders

    recordings_root = Path(cfg.recordings_root)
    removed = 0
    with session() as s:
        if s.get_bind().dialect.name == "postgresql":
            tag_filter = Recording.tags.contains([tag])
            rows = (
                s.query(Recording.id, Recording.tags)
                .filter(Recording.state == RecordingState.done, tag_filter)
                .all()
            )
        else:
            # SQLite (unit tests): tags is JSON — same portable shape as
            # digest._select_recordings.
            tag_json = tag.replace("\\", "\\\\").replace('"', '\\"')
            rows = s.execute(
                _text(
                    "SELECT id, tags FROM recordings "
                    "WHERE state = 'done' AND EXISTS (SELECT 1 FROM json_each(recordings.tags) "
                    f"WHERE value = '{tag_json}')"
                )
            ).all()
    for rec_id, tags in rows:
        if isinstance(tags, str):  # sqlite raw row: tags arrives as JSON text
            try:
                tags = json.loads(tags)
            except ValueError:
                tags = []
        if [t for t in (tags or []) if t != tag]:
            continue  # another tag still owns the shared artifact
        # scan matches folders by the id8 suffix only; the rest of Rec
        # is irrelevant here (title/dates may be stale in the catalog).
        rec = Rec(id=rec_id, title="", created_at=datetime.now(UTC), duration_sec=None)
        copies = [recordings_root / rec_id / "meta" / "events.json"]
        copies += [
            folder / ".transcripter" / "meta" / "events.json"
            for folder in scan_recording_folders(transcripts, rec)
        ]
        hit = False
        for path in copies:
            try:
                if path.is_file():
                    path.unlink()
                    hit = True
            except OSError:
                log.exception("purge: events.json %s could not be removed", path)
        if hit:
            removed += 1
    return removed
