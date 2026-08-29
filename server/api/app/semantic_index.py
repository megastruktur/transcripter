"""Phase 3.5 — read-side semantic index access (twin of worker
semantic_index.py, search half only).

The worker OWNS index writes (enrich + backfill); the api only opens
``<transcripts>/indexes/<tag>.sqlite`` read-only for KNN and availability
checks. The tag→filename slug must stay the exact twin of the worker's
``semantic_index.index_path``/``enrich.slugify`` — change in SYNC.
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
        return [
            {
                "recording_id": r[0],
                "session_title": r[1],
                "ts_start": r[2],
                "ts_end": r[3],
                "speaker": r[4],
                "text": r[5],
                "distance": r[6],
            }
            for r in rows
        ]
    finally:
        db.close()


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
