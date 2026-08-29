"""Phase 3.5 — semantic index: per-tag sqlite-vec over transcript segments.

Vectors are NOT entities — they are SEGMENTS of the transcript, embedded
through the same ``embeddings.embed_texts`` client the dedup prefilter
uses (local ONNX bge-m3 or the http backend; no new model, no LLM
traffic). One index file per namespace (tag):
``<transcripts>/indexes/<tag-slug>.sqlite`` next to digests/.

What gets segmented (cheap → precise):

1. ``diarized-transcript.md`` exists → speaker turns from the merge
   (``**spk_1 [mm:ss – mm:ss]:** text`` lines) — existing data, zero
   cost, and the phase's main target (diarized sessions).
2. fallback → sliding windows over ``transcript.md`` segment lines:
   ~300 tokens, step 50 (neighbouring windows overlap so a hit is never
   cut at a topic boundary).

Schema per index file (self-describing for model-switch detection):

* ``segments`` — vec0 virtual table ``embedding float[dimensions]``,
  rowid-joined to
* ``segments_meta`` — (recording_id, session_title, ts_start, ts_end,
  speaker, text, indexed_at),
* ``index_meta`` — single row ``{backend, model, dimensions}``.

``index_segments()`` is idempotent per recording: DELETE by
recording_id then INSERT (regenerate-safe). It is BEST-EFFORT at the
enrich call site — a failure never fails the stage (details carry
``indexed_segments: N``). A backend/model/dimensions MISMATCH on write
triggers a full rebuild of that index file (indexing is idempotent, so
the rebuild is just: drop rows, re-create, re-insert this recording;
other recordings return via ``worker.backfill_index``).
"""

from __future__ import annotations

import logging
import re
import sqlite3
import struct
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .enrich import slugify

log = logging.getLogger("transcripter.semantic_index")

# Sliding-window fallback budget (transcript.md has no speaker turns):
# ~300 tokens per window, stepping 50 tokens so neighbours overlap.
_WINDOW_TOKENS = 300
_WINDOW_STEP = 50
# Heuristic words-per-token for the window math (mixed RU/EN transcripts
# measured ~0.75 words/token on bge-m3's tokenizer).
_WORDS_PER_TOKEN = 0.75

# diarized-transcript.md turn line: "**spk_1 [00:01:23 – 00:01:41]:** text"
_TURN_RE = re.compile(
    r"^\*\*(?P<speaker>[^*\[]+)\s*\[(?P<start>\d{1,2}:\d{2}(?::\d{2})?)\s*[–-]\s*"
    r"(?P<end>\d{1,2}:\d{2}(?::\d{2})?)\]:\*\*\s*(?P<text>.*)$"
)

# transcript.md segment line: "**[00:00:01 – 00:00:04]** text"
_SEGMENT_RE = re.compile(
    r"^\*\*\[(?P<start>\d{1,2}:\d{2}(?::\d{2})?)\s*[–-]\s*"
    r"(?P<end>\d{1,2}:\d{2}(?::\d{2})?)\]\*\*\s*(?P<text>.*)$"
)


@dataclass(frozen=True)
class Segment:
    """One indexable chunk of a transcript."""

    ts_start: float
    ts_end: float
    speaker: str
    text: str


def _ts_to_sec(ts: str) -> float:
    """mm:ss or hh:mm:ss → seconds."""
    parts = [int(p) for p in ts.split(":")]
    if len(parts) == 2:
        return float(parts[0] * 60 + parts[1])
    return float(parts[0] * 3600 + parts[1] * 60 + parts[2])


def _iter_markdown_lines(path: Path) -> list[str]:
    try:
        return path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []


def segment_from_diarized(meta_dir: Path) -> list[Segment] | None:
    """Speaker turns from diarized-transcript.md; None when absent."""
    lines = _iter_markdown_lines(meta_dir / "diarized-transcript.md")
    if not lines:
        return None
    out: list[Segment] = []
    for line in lines:
        m = _TURN_RE.match(line.strip())
        if m is None or not m.group("text").strip():
            continue
        out.append(
            Segment(
                ts_start=_ts_to_sec(m.group("start")),
                ts_end=_ts_to_sec(m.group("end")),
                speaker=m.group("speaker").strip(),
                text=m.group("text").strip(),
            )
        )
    return out


def _windows(items: list[Segment]) -> list[Segment]:
    """Sliding windows over timestamped segments: ~300 tokens, step 50.

    Window text concatenates whole segments (a segment is never split);
    the token count is a words-heuristic — precision buys nothing here,
    overlap is what protects topic continuity.
    """
    if not items:
        return []
    target_words = int(_WINDOW_TOKENS * _WORDS_PER_TOKEN)
    step_words = max(1, int(_WINDOW_STEP * _WORDS_PER_TOKEN))
    out: list[Segment] = []
    start = 0
    while start < len(items):
        words = 0
        end = start
        while end < len(items) and (end == start or words < target_words):
            words += len(items[end].text.split())
            end += 1
        chunk = items[start:end]
        out.append(
            Segment(
                ts_start=chunk[0].ts_start,
                ts_end=chunk[-1].ts_end,
                speaker="",
                text=" ".join(seg.text for seg in chunk).strip(),
            )
        )
        if end >= len(items):
            break
        # Advance by ~step_words of CONTENT (segment granularity).
        adv = start
        stepped = 0
        while adv < end and stepped < step_words:
            stepped += len(items[adv].text.split())
            adv += 1
        start = max(start + 1, adv)
    return out


def segment_from_transcript(meta_dir: Path) -> list[Segment]:
    """Sliding windows over transcript.md timestamped segment lines."""
    items: list[Segment] = []
    for line in _iter_markdown_lines(meta_dir / "transcript.md"):
        m = _SEGMENT_RE.match(line.strip())
        if m is None or not m.group("text").strip():
            continue
        items.append(
            Segment(
                ts_start=_ts_to_sec(m.group("start")),
                ts_end=_ts_to_sec(m.group("end")),
                speaker="",
                text=m.group("text").strip(),
            )
        )
    return _windows(items)


def segment_transcripts(meta_dir: Path) -> list[Segment]:
    """Segmentation order (cheap → precise): diarized speaker turns when
    diarization ran, else sliding windows over transcript.md. Empty list
    when neither artifact exists (nothing to index)."""
    turns = segment_from_diarized(meta_dir)
    if turns is not None:
        return turns
    return segment_from_transcript(meta_dir)


# --- sqlite-vec index -----------------------------------------------------------


def _f32(vec: list[float]) -> bytes:
    return struct.pack(f"{len(vec)}f", *vec)


def index_path(transcripts_root: Path, tag: str) -> Path:
    """``<transcripts>/indexes/<tag-slug>.sqlite`` — slugify is the
    Unicode-aware enrich slug (Cyrillic tags survive; same function the
    digest filenames use, so a tag's index and digest slugs agree)."""
    d = transcripts_root / "indexes"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{slugify(tag)}.sqlite"


def _connect(path: Path, dimensions: int) -> sqlite3.Connection:
    import sqlite_vec

    db = sqlite3.connect(path)
    db.enable_load_extension(True)
    sqlite_vec.load(db)
    db.enable_load_extension(False)
    db.execute(
        f"CREATE VIRTUAL TABLE IF NOT EXISTS segments USING vec0("
        f"embedding float[{dimensions}])"
    )
    db.execute(
        "CREATE TABLE IF NOT EXISTS segments_meta ("
        "recording_id TEXT NOT NULL, "
        "session_title TEXT NOT NULL DEFAULT '', "
        "ts_start REAL NOT NULL, "
        "ts_end REAL NOT NULL, "
        "speaker TEXT NOT NULL DEFAULT '', "
        "text TEXT NOT NULL, "
        "indexed_at TEXT NOT NULL)"
    )
    db.execute(
        "CREATE TABLE IF NOT EXISTS index_meta ("
        "key TEXT PRIMARY KEY, value TEXT NOT NULL)"
    )
    return db


def _read_meta(db: sqlite3.Connection) -> dict[str, str]:
    try:
        return dict(db.execute("SELECT key, value FROM index_meta").fetchall())
    except sqlite3.OperationalError:
        return {}


def _write_meta(db: sqlite3.Connection, meta: dict[str, str]) -> None:
    db.execute("DELETE FROM index_meta")
    db.executemany(
        "INSERT INTO index_meta (key, value) VALUES (?, ?)", meta.items()
    )


def _dimensions_of(cfg: Any) -> int:
    embed_cfg = cfg.graph.embed
    dims = int(getattr(embed_cfg, "configured_dimensions", 0) or 0)
    if dims <= 0:
        raise ValueError(
            "graph.embed.dimensions unset — required for the http backend"
        )
    return dims


def _expected_meta(cfg: Any) -> dict[str, str]:
    embed_cfg = cfg.graph.embed
    backend = embed_cfg.backend
    if backend == "local":
        model = f"onnx:{Path(embed_cfg.model_path).name}"
    else:
        model = embed_cfg.model or "http"
    return {
        "backend": backend,
        "model": model,
        "dimensions": str(_dimensions_of(cfg)),
    }


def index_segments(
    recording_id: str,
    tag: str,
    title: str,
    meta_dir: Path,
    transcripts_root: Path,
    cfg: Any,
) -> int:
    """Embed + store this recording's segments in the tag's index file.

    Idempotent: DELETE by recording_id, then INSERT. A backend/model/
    dimensions mismatch against index_meta rebuilds the whole index file
    (model switches must re-index — never mix vector spaces). Returns
    the number of indexed segments; raises on embed/backend failure —
    the enrich call site catches and degrades.
    """
    segments = segment_transcripts(meta_dir)
    if not segments:
        return 0
    from .embeddings import embed_texts

    vectors = embed_texts([seg.text for seg in segments], cfg)
    if vectors is None:
        raise RuntimeError("embedding backend unavailable (no vectors)")
    expected = _expected_meta(cfg)
    dims = int(expected["dimensions"])
    if any(len(v) != dims for v in vectors):
        raise RuntimeError(
            f"embedding backend returned {len(vectors[0]) if vectors else 0}-d "
            f"vectors, config declares {dims}"
        )

    path = index_path(transcripts_root, tag)
    db = _connect(path, dims)
    try:
        current = _read_meta(db)
        # Model-switch guard: a mismatched index file describes a
        # DIFFERENT vector space — drop its rows; this recording seeds
        # the new space, others return via backfill_index.
        mismatch = any(current.get(k) != v for k, v in expected.items())
        if current and mismatch:
            log.warning(
                "semantic index %s: meta mismatch (%s -> %s); rebuilding",
                path.name,
                current,
                expected,
            )
            db.execute("DELETE FROM segments")
            db.execute("DELETE FROM segments_meta")
        # Idempotency: purge this recording's previous rows (regenerate).
        stale = db.execute(
            "SELECT rowid FROM segments_meta WHERE recording_id = ?",
            (recording_id,),
        ).fetchall()
        for (rowid,) in stale:
            db.execute("DELETE FROM segments WHERE rowid = ?", (rowid,))
            db.execute("DELETE FROM segments_meta WHERE rowid = ?", (rowid,))
        now = datetime.now(UTC).isoformat()
        for seg, vec in zip(segments, vectors, strict=True):
            cur = db.execute(
                "INSERT INTO segments_meta (recording_id, session_title, "
                "ts_start, ts_end, speaker, text, indexed_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    recording_id,
                    title,
                    seg.ts_start,
                    seg.ts_end,
                    seg.speaker,
                    seg.text,
                    now,
                ),
            )
            db.execute(
                "INSERT INTO segments (rowid, embedding) VALUES (?, ?)",
                (cur.lastrowid, _f32(vec)),
            )
        _write_meta(db, expected)
        db.commit()
        return len(segments)
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
    path = transcripts_root / "indexes" / f"{slugify(tag)}.sqlite"
    if not path.is_file():
        return []
    import sqlite_vec

    db = sqlite3.connect(path)
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
    logic); None when the file is absent."""
    path = transcripts_root / "indexes" / f"{slugify(tag)}.sqlite"
    if not path.is_file():
        return None
    db = sqlite3.connect(path)
    try:
        meta = _read_meta(db)
        count = db.execute("SELECT count(*) FROM segments_meta").fetchone()[0]
        return {"meta": meta, "segments": count}
    except sqlite3.OperationalError:
        return None
    finally:
        db.close()


# --- GC hooks --------------------------------------------------------------------


def drop_recording_from_indexes(
    transcripts_root: Path, tags: list[str], recording_id: str
) -> int:
    """Recording deletion: remove its segments from each of its tags'
    index files. Best-effort per file; returns segments removed."""
    removed = 0
    for tag in tags:
        path = transcripts_root / "indexes" / f"{slugify(tag)}.sqlite"
        if not path.is_file():
            continue
        try:
            db = _connect(path, 1)  # dims unused for DELETE-only access
            try:
                stale = db.execute(
                    "SELECT rowid FROM segments_meta WHERE recording_id = ?",
                    (recording_id,),
                ).fetchall()
                for (rowid,) in stale:
                    db.execute("DELETE FROM segments WHERE rowid = ?", (rowid,))
                db.execute(
                    "DELETE FROM segments_meta WHERE recording_id = ?",
                    (recording_id,),
                )
                db.commit()
                removed += len(stale)
            finally:
                db.close()
        except sqlite3.Error:
            log.exception("semantic index GC: %s failed", path.name)
    return removed


def drop_dead_tag_indexes(transcripts_root: Path, live_tags: list[str]) -> list[str]:
    """Graph GC: delete index files of tags that no longer exist in the
    catalog. Returns the removed filenames (for the GC payload)."""
    import os

    d = transcripts_root / "indexes"
    if not d.is_dir():
        return []
    live = {f"{slugify(t)}.sqlite" for t in live_tags}
    dropped: list[str] = []
    for name in os.listdir(d):
        if name.endswith(".sqlite") and name not in live:
            (d / name).unlink()
            dropped.append(name)
    return dropped
