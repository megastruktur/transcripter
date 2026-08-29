"""Phase 3.5 — read-side semantic index access (twin of worker
semantic_index.py, search half only).

The worker OWNS index writes (enrich + backfill); the api only opens
``<transcripts>/indexes/<tag>.sqlite`` read-only for KNN and availability
checks. The tag→filename slug must stay the exact twin of the worker's
``semantic_index.index_path``/``enrich.slugify`` — change in SYNC.

Phase 3.75 adds the global search's index enumeration (``iter_indexes``)
and the raw-file KNN (``knn_search_path``) so the cross-tag route never
touches Postgres — every hit's tag comes straight from the index files.
"""

from __future__ import annotations

import re
import sqlite3
import struct
from pathlib import Path
from typing import Any

# Unicode-aware slug twin of worker/enrich.slugify (casefold + non-word
# → dash + collapse + strip; Cyrillic survives).
_NON_ALNUM = re.compile(r"[^\w]+", re.UNICODE)
_MULTI_DASH = re.compile(r"-{2,}")


def slugify(label: str) -> str:
    s = _NON_ALNUM.sub("-", label.casefold()).strip("-")
    s = _MULTI_DASH.sub("-", s)
    return s or "unknown"


def index_path(transcripts_root: Path, tag: str) -> Path:
    """``<transcripts>/indexes/<tag-slug>.sqlite`` — same filename the
    worker's writer produces (digests use the same slug, so a tag's
    index and digest names agree)."""
    return transcripts_root / "indexes" / f"{slugify(tag)}.sqlite"


def _f32(vec: list[float]) -> bytes:
    return struct.pack(f"{len(vec)}f", *vec)


def _hit(row: tuple[Any, ...]) -> dict[str, Any]:
    """One KNN result row → the wire hit dict shared by the tag-scoped
    and global search routes (``text`` becomes ``snippet`` at the route
    layer; ``distance`` is vec0's — smaller is closer)."""
    return {
        "recording_id": row[0],
        "session_title": row[1],
        "ts_start": row[2],
        "ts_end": row[3],
        "speaker": row[4],
        "text": row[5],
        "distance": row[6],
    }


def _knn_query(path: Path, query_vec: list[float], k: int) -> list[dict[str, Any]]:
    """Read-only KNN over one openable index file; raises on a corrupt
    or missing vec0 table (the caller decides skip vs fail)."""
    import sqlite_vec

    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        db.enable_load_extension(True)
        sqlite_vec.load(db)
        db.enable_load_extension(False)
        rows = db.execute(
            "SELECT m.recording_id, m.session_title, m.ts_start, m.ts_end, "
            "m.speaker, m.text, distance "
            "FROM segments JOIN segments_meta AS m "
            "ON segments.rowid = m.rowid "
            "WHERE embedding MATCH ? AND k = ? "
            "ORDER BY distance",
            (_f32(query_vec), k),
        ).fetchall()
        return [_hit(r) for r in rows]
    finally:
        db.close()


def knn_search(
    transcripts_root: Path,
    tag: str,
    query_vec: list[float],
    k: int = 20,
) -> list[dict[str, Any]]:
    """KNN over the tag's index: nearest segments rowid-joined to meta.

    Returns [] when the index file does not exist (nothing indexed for
    the tag). Distance is vec0's (cosine-derived) — smaller is closer.
    """
    path = index_path(transcripts_root, tag)
    if not path.is_file():
        return []
    return _knn_query(path, query_vec, k)


def index_status(transcripts_root: Path, tag: str) -> dict[str, Any] | None:
    """Index meta + segment count for availability checks (search 503
    logic); None when the file is absent or not a valid index."""
    path = index_path(transcripts_root, tag)
    if not path.is_file():
        return None
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        try:
            meta = dict(db.execute("SELECT key, value FROM index_meta").fetchall())
            count = db.execute("SELECT count(*) FROM segments_meta").fetchone()[0]
        except sqlite3.OperationalError:
            return None
        return {"meta": meta, "segments": count}
    finally:
        db.close()


def iter_indexes(transcripts_root: Path) -> list[tuple[str, Path]]:
    """Every index file under ``<transcripts>/indexes/`` as
    ``(slug, path)``, sorted by slug — the global search's universe.

    ``slug`` is the filename stem (the worker's tag slug), NOT the raw
    tag: tags with spaces/casing live in the slug exactly as the worker
    wrote them, and index files carry no raw-tag column to recover the
    display form. Hits therefore report the slug; the client links to
    the recording, not the tag page.
    """
    d = transcripts_root / "indexes"
    if not d.is_dir():
        return []
    return sorted((p.stem, p) for p in d.glob("*.sqlite") if p.is_file())


def read_index_meta(path: Path) -> dict[str, str]:
    """``index_meta`` contents of one index file; ``{}`` when the file
    or table is missing/corrupt (the caller treats empty as mismatch)."""
    try:
        db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error:
        return {}
    try:
        return dict(db.execute("SELECT key, value FROM index_meta").fetchall())
    except sqlite3.Error:
        return {}
    finally:
        db.close()


def knn_search_path(path: Path, query_vec: list[float], k: int) -> list[dict[str, Any]]:
    """KNN over one already-located index file (global search entry —
    see ``iter_indexes``); raises on a missing/corrupt file, the route
    skips the tag with a warning instead of failing the whole search."""
    return _knn_query(path, query_vec, k)
