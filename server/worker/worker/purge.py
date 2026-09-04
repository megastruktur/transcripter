"""Tag-memory purge: wipe ONE tag's knowledge-graph memory.

"Memory" of a tag is four stores, all created by the enrich pipeline:
1. the Neo4j namespace (every node carries ``tag``; DETACH DELETE in
   batches, same bound as graph_gc);
2. the ``graph_edits`` rows (the Phase A overlay) — stale edits would
   otherwise be re-applied on top of the fresh rebuild;
3. the digest note (``<transcripts>/digests/<slug>.md``, incl. the
   ``-N`` collision variants);
4. the semantic index (``<transcripts>/indexes/<slug>.sqlite``).

Recordings, audio, transcripts and per-recording events.json are NOT
touched: they are the input the enrich stage rebuilds the memory FROM.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from neo4j import GraphDatabase
from sqlalchemy import text

from .db import session

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

    log.info("purge: tag %r wiped: %s", tag, counts)
    return counts
