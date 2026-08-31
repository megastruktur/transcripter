"""Phase 3.75 — global cross-tag semantic search.

``GET /search?q=&k=20`` answers "when did we EVER discuss X" without
picking a tag: it walks every worker-built index file under
``<transcripts>/indexes/``, KNNs each, merges by distance and reports
every hit with the tag it came from. No new indexing, no index merging
— a read-side union over the per-tag files of Phase 3.5.

Failure semantics differ from the tag route deliberately: one bad index
must not kill the whole search. A meta mismatch (model switch → run
backfill for that tag) or a corrupt/unreadable file skips THAT tag with
a warning; only a dead embedding backend or a wholly unindexed vault
reply 503 ``{available: false}`` in the tag search's shape.
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request

from app.config import ServerConfig
from app.embeddings import embed_query, expected_index_meta
from app.semantic_index import iter_indexes, knn_search_path, read_index_meta

router = APIRouter(prefix="/search")

_LOG = logging.getLogger("transcripter.api.search")


@router.get("")
def global_search(
    request: Request,
    q: Annotated[str, Query(min_length=1)],
    k: Annotated[int, Query(ge=1, le=50)] = 20,
) -> dict:
    """Global semantic KNN across every per-tag index file.

    Each hit carries ``tag`` (the index filename slug — the worker's
    Unicode tag slug; files store no raw-tag column) alongside the tag
    search's hit fields. Meta-guard per file as in 3.5: mismatch or
    corrupt file → that tag is skipped with a warning, never a 500.
    """
    cfg: ServerConfig = request.app.state.config
    # embed first: a dead backend fails fast regardless of index state
    try:
        query_vec = embed_query(q.strip(), cfg)
    except Exception:  # noqa: BLE001 — backend down = unavailable, not a 500
        _LOG.warning("search: embedding backend failed", exc_info=True)
        query_vec = None
    if query_vec is None:
        raise HTTPException(
            status_code=503,
            detail={
                "available": False,
                "reason": "embedding backend unavailable",
            },
        )

    indexes = iter_indexes(cfg.vault.path)
    if not indexes:
        raise HTTPException(
            status_code=503,
            detail={
                "available": False,
                "reason": (
                    "no semantic indexes at all — index recordings first "
                    "(new recordings index automatically; run "
                    "`docker compose exec worker python -m worker.backfill_index` "
                    "for old ones)"
                ),
            },
        )

    expected = expected_index_meta(cfg)
    hits: list[dict] = []
    for slug, path in indexes:
        meta = read_index_meta(path)
        if any(meta.get(key) != value for key, value in expected.items()):
            _LOG.warning(
                "search: skipping tag index %s — meta %s != expected %s "
                "(re-index: `docker compose exec worker python -m "
                "worker.backfill_index`)",
                slug,
                meta,
                expected,
            )
            continue
        try:
            for h in knn_search_path(path, query_vec, k=k):
                h["tag"] = slug
                hits.append(h)
        except Exception:  # noqa: BLE001 — one corrupt file ≠ a dead search
            _LOG.warning("search: skipping unreadable index %s", path, exc_info=True)
            continue
    hits.sort(key=lambda h: h["distance"])
    return {
        "query": q.strip(),
        "k": k,
        "hits": [
            {
                "tag": h["tag"],
                "recording_id": h["recording_id"],
                "session_title": h["session_title"],
                "ts_start": h["ts_start"],
                "ts_end": h["ts_end"],
                "speaker": h["speaker"],
                "snippet": h["text"],
                "distance": h["distance"],
            }
            for h in hits[:k]
        ],
    }
