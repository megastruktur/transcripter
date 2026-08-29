"""Phase 1 graph GC: sweep stale recording-scoped nodes from Neo4j.

Every node ``write_to_graph`` creates carries ``origin_recording_id``
(entities, events — see enrich.py's leak audit). When a recording is
deleted from Postgres, its nodes survive in the graph forever: enrich
only purges a recording's nodes when THAT recording is re-written, and
a deleted recording is never re-written. This module is the periodic
counter-sweep: delete every node whose ``origin_recording_id`` no
longer exists in the recordings catalog.

Not per-recording, no stage rows: the activity is standalone (scheduled
via a Temporal Schedule, or callable directly) and returns a count.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from neo4j import GraphDatabase
from sqlalchemy import text

from .db import session

log = logging.getLogger("transcripter.graph_gc")

# Upper bound on nodes deleted per TRANSACTION: one huge DETACH DELETE
# over a large backlog can exceed the Neo4j tx timeout / heap. The loop
# keeps sweeping until nothing matches.
_BATCH_SIZE = 10000

_SWEEP_CYPHER = (
    "MATCH (n) "
    "WHERE n.origin_recording_id IS NOT NULL "
    "AND NOT n.origin_recording_id IN $ids "
    "DETACH DELETE n "
    "RETURN count(n) AS deleted"
)


def _recording_ids() -> list[str]:
    """All recording ids in the catalog (the graph is small relative to
    this list; a single IN-list parameter keeps the Cypher simple)."""
    with session() as s:
        return [row.id for row in s.execute(text("SELECT id FROM recordings"))]


def _live_tags() -> list[str]:
    """Distinct free tags across the catalog (the index namespaces).
    Recordings with no tags live in the built-in ``untagged`` namespace,
    so it is always live. Dialect-portable: pg unnest vs sqlite json_each
    (same shape as digest._select_recordings)."""
    with session() as s:
        if s.get_bind().dialect.name == "postgresql":
            rows = s.execute(
                text("SELECT DISTINCT unnest(tags) FROM recordings")
            ).fetchall()
        else:
            rows = s.execute(
                text(
                    "SELECT DISTINCT value FROM recordings, "
                    "json_each(recordings.tags)"
                )
            ).fetchall()
    return [row[0] for row in rows]


def run_graph_gc(cfg: Any) -> dict:
    """One GC pass. Returns ``{"deleted": N}``, or
    ``{"skipped": "graph disabled"}`` when the graph backend is off
    (no stage rows involved — this is not a per-recording stage)."""
    if not cfg.graph.enabled:
        return {"skipped": "graph disabled"}
    ids = _recording_ids()
    driver = GraphDatabase.driver(
        cfg.graph.uri,
        auth=(cfg.graph.user, os.environ.get(cfg.graph.password_env, "")),
    )
    total = 0
    try:
        with driver.session(database=cfg.graph.database) as session_:
            while True:
                # $ids is the CATALOG for the whole pass (not the
                # batch), so it never changes between rounds; the loop
                # ends when a batch deletes nothing.
                result = session_.run(_SWEEP_CYPHER, ids=ids)
                record = result.single(strict=True)
                deleted = int(record["deleted"])
                total += deleted
                if deleted < _BATCH_SIZE:
                    break
    finally:
        driver.close()
    # Phase 3.5: semantic index files of tags that vanished from the
    # catalog are dead weight — drop them in the same sweep. Best-effort
    # (a failure here must not fail the graph GC pass).
    dropped_indexes: list[str] = []
    try:
        from .semantic_index import drop_dead_tag_indexes

        dropped_indexes = drop_dead_tag_indexes(
            cfg.transcripts.path, _live_tags() + ["untagged"]
        )
        if dropped_indexes:
            log.info("graph_gc: dropped %d dead index file(s)", len(dropped_indexes))
    except Exception:
        log.exception("graph_gc: semantic index sweep failed")
    return {"deleted": total, "dropped_indexes": len(dropped_indexes)}
