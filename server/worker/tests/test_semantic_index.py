"""Phase 3.5 semantic index — segmentation, sqlite-vec storage, KNN,
idempotency, model-switch rebuild, GC hooks.

The embedder is faked at ``worker.embeddings.embed_texts`` (indexing
imports it inside the function — patching the module attribute covers
both local and http configs). sqlite-vec runs for real (a real vec0
table per tmp index file), so the KNN/rowid-join/DELETE paths are
exercised against the actual extension.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from worker.semantic_index import (
    drop_dead_tag_indexes,
    drop_recording_from_indexes,
    index_path,
    index_segments,
    index_status,
    knn_search,
    segment_from_diarized,
    segment_from_transcript,
    segment_transcripts,
)

_DIM = 4


def _cfg(backend: str = "local", dimensions: int = _DIM) -> Any:
    """WorkerConfig-shaped namespace for the index code paths."""
    embed = SimpleNamespace(
        backend=backend,
        model_path=Path("/models/bge-m3-int8"),
        base_url="http://x/v1" if backend == "http" else "",
        model="remote-model" if backend == "http" else "",
        api_key_env="",
        # Tests declare the dims they fake; production local pins 1024
        # via the same property — the index only needs a consistent int.
        configured_dimensions=dimensions,
    )
    return SimpleNamespace(graph=SimpleNamespace(embed=embed))


def _fake_embed(count: int, fill: float = 0.1) -> list[list[float]]:
    return [[fill] * _DIM for _ in range(count)]


# --- segmentation ----------------------------------------------------------------


def _write(meta: Path, name: str, body: str) -> None:
    meta.mkdir(parents=True, exist_ok=True)
    (meta / name).write_text(body, encoding="utf-8")


DIARIZED = """# Diarized transcript

**spk_1 [00:00:01 – 00:00:05]:** привет, начинаем.

**spk_2 [00:00:06 – 00:00:12]:** привет! давай обсудим артефакт.

**spk_1 [00:01:23 – 00:01:41]:** итог: артефакт у нас.
"""

TRANSCRIPT = """# Transcript (ru)

**[00:00:01 – 00:00:05]** первое предложение о погоде и планах.

**[00:00:06 – 00:00:12]** второе предложение про артефакт и сокровище.

**[00:01:00 – 00:01:10]** третье: подводим итоги сессии.
"""


class TestSegmentation:
    def test_diarized_turns_preferred(self, tmp_path: Path) -> None:
        meta = tmp_path / "meta"
        _write(meta, "diarized-transcript.md", DIARIZED)
        _write(meta, "transcript.md", TRANSCRIPT)
        segs = segment_transcripts(meta)
        # diarized exists → speaker turns, NOT transcript windows.
        assert [s.speaker for s in segs] == ["spk_1", "spk_2", "spk_1"]
        assert segs[1].ts_start == 6.0 and segs[1].ts_end == 12.0
        assert "артефакт" in segs[1].text

    def test_diarized_absent_falls_back_to_windows(self, tmp_path: Path) -> None:
        meta = tmp_path / "meta"
        _write(meta, "transcript.md", TRANSCRIPT)
        segs = segment_transcripts(meta)
        assert len(segs) >= 1
        assert all(s.speaker == "" for s in segs)
        # Windows carry global time ranges from the source segments.
        assert segs[0].ts_start == 1.0
        assert segs[-1].ts_end == 70.0

    def test_no_artifacts_empty(self, tmp_path: Path) -> None:
        assert segment_transcripts(tmp_path / "meta") == []

    def test_diarized_present_but_empty_is_empty(self, tmp_path: Path) -> None:
        # A file with no parseable turns yields [] (not None) — it EXISTS,
        # so the transcript fallback must not run.
        meta = tmp_path / "meta"
        _write(meta, "diarized-transcript.md", "# Diarized transcript\n\ngarbage\n")
        assert segment_from_diarized(meta) is not None
        assert segment_transcripts(meta) == []

    def test_windows_overlap_short(self, tmp_path: Path) -> None:
        """A handful of segments under one window → single window."""
        meta = tmp_path / "meta"
        _write(
            meta,
            "transcript.md",
            "**[00:00:01 – 00:00:02]** alpha\n\n**[00:00:03 – 00:00:04]** beta\n",
        )
        segs = segment_from_transcript(meta)
        assert len(segs) == 1
        assert segs[0].text == "alpha beta"


# --- index_segments + storage ------------------------------------------------------


def _index_ok(
    tmp_path: Path,
    meta: Path,
    rec: str = "rec-1",
    tag: str = "pathfinder",
    title: str = "Session 1",
    cfg: Any | None = None,
    fill: float = 0.1,
) -> int:
    with patch(
        "worker.embeddings.embed_texts",
        side_effect=lambda texts, c: _fake_embed(len(texts), fill),
    ):
        return index_segments(
            rec, tag, title, meta, tmp_path, cfg or _cfg()
        )


class TestIndexSegments:
    def test_creates_index_and_rows(self, tmp_path: Path) -> None:
        meta = tmp_path / "meta"
        _write(meta, "diarized-transcript.md", DIARIZED)
        n = _index_ok(tmp_path, meta)
        assert n == 3
        path = index_path(tmp_path, "pathfinder")
        assert path.is_file()
        assert path.name == "pathfinder.sqlite"
        status = index_status(tmp_path, "pathfinder")
        assert status is not None
        assert status["segments"] == 3
        assert status["meta"]["backend"] == "local"
        assert status["meta"]["model"] == "onnx:bge-m3-int8"
        assert status["meta"]["dimensions"] == str(_DIM)

    def test_tag_slug_unicode(self, tmp_path: Path) -> None:
        """Cyrillic tags slugify (same enrich slugify as digests)."""
        meta = tmp_path / "meta"
        _write(meta, "diarized-transcript.md", DIARIZED)
        _index_ok(tmp_path, meta, tag="проба кириллица")
        assert (tmp_path / "indexes" / "проба-кириллица.sqlite").is_file()

    def test_idempotent_regenerate(self, tmp_path: Path) -> None:
        meta = tmp_path / "meta"
        _write(meta, "diarized-transcript.md", DIARIZED)
        _index_ok(tmp_path, meta, rec="r1")
        _index_ok(tmp_path, meta, rec="r1")
        # Same recording twice → still only its 3 rows.
        status = index_status(tmp_path, "pathfinder")
        assert status is not None and status["segments"] == 3

    def test_two_recordings_coexist(self, tmp_path: Path) -> None:
        meta = tmp_path / "meta"
        _write(meta, "diarized-transcript.md", DIARIZED)
        _index_ok(tmp_path, meta, rec="r1")
        _index_ok(tmp_path, meta, rec="r2")
        status = index_status(tmp_path, "pathfinder")
        assert status is not None and status["segments"] == 6

    def test_model_switch_rebuilds(self, tmp_path: Path) -> None:
        """Index meta mismatch (model change) drops the old rows — no
        mixed vector spaces."""
        meta = tmp_path / "meta"
        _write(meta, "diarized-transcript.md", DIARIZED)
        _index_ok(tmp_path, meta, rec="r-old")
        assert index_status(tmp_path, "pathfinder") is not None
        # Same dims, different model id (http backend).
        http_cfg = _cfg(backend="http")
        _index_ok(tmp_path, meta, rec="r-new", cfg=http_cfg)
        status = index_status(tmp_path, "pathfinder")
        assert status is not None
        assert status["segments"] == 3  # old rows gone
        assert status["meta"]["backend"] == "http"
        assert status["meta"]["model"] == "remote-model"

    def test_no_segments_returns_zero(self, tmp_path: Path) -> None:
        assert _index_ok(tmp_path, tmp_path / "empty-meta") == 0
        assert index_status(tmp_path, "pathfinder") is None

    def test_embed_unavailable_raises(self, tmp_path: Path) -> None:
        """embed_texts → None (local model off) raises; the enrich hook
        catches this and reports indexed_segments=0."""
        meta = tmp_path / "meta"
        _write(meta, "diarized-transcript.md", DIARIZED)
        with (
            patch("worker.embeddings.embed_texts", return_value=None),
            pytest.raises(RuntimeError, match="unavailable"),
        ):
            index_segments("r1", "t", "", meta, tmp_path, _cfg())


class TestKnnSearch:
    def test_knn_returns_closest(self, tmp_path: Path) -> None:
        meta = tmp_path / "meta"
        _write(meta, "diarized-transcript.md", DIARIZED)
        # Distinct fills so the nearest is deterministic.
        with patch(
            "worker.embeddings.embed_texts",
            side_effect=lambda texts, c: [[0.1 * (i + 1)] * _DIM for i, _ in enumerate(texts)],
        ):
            index_segments("r1", "pathfinder", "S", meta, tmp_path, _cfg())
        hits = knn_search(tmp_path, "pathfinder", [0.15] * _DIM, k=2)
        assert len(hits) == 2
        assert hits[0]["distance"] <= hits[1]["distance"]
        assert hits[0]["recording_id"] == "r1"
        assert hits[0]["session_title"] == "S"
        assert {"ts_start", "ts_end", "speaker", "text"} <= set(hits[0])

    def test_missing_index_empty(self, tmp_path: Path) -> None:
        assert knn_search(tmp_path, "nope", [0.0] * _DIM, k=5) == []


# --- GC hooks -----------------------------------------------------------------------


class TestGcHooks:
    def test_drop_recording_rows(self, tmp_path: Path) -> None:
        meta = tmp_path / "meta"
        _write(meta, "diarized-transcript.md", DIARIZED)
        _index_ok(tmp_path, meta, rec="r1")
        _index_ok(tmp_path, meta, rec="r2")
        removed = drop_recording_from_indexes(tmp_path, ["pathfinder"], "r1")
        assert removed == 3
        status = index_status(tmp_path, "pathfinder")
        assert status is not None and status["segments"] == 3

    def test_drop_missing_file_is_noop(self, tmp_path: Path) -> None:
        assert drop_recording_from_indexes(tmp_path, ["ghost"], "r1") == 0

    def test_drop_dead_tag_indexes(self, tmp_path: Path) -> None:
        meta = tmp_path / "meta"
        _write(meta, "diarized-transcript.md", DIARIZED)
        _index_ok(tmp_path, meta, tag="alive")
        _index_ok(tmp_path, meta, tag="dead-tag")
        dropped = drop_dead_tag_indexes(tmp_path, ["alive", "untagged"])
        assert dropped == ["dead-tag.sqlite"]
        assert (tmp_path / "indexes" / "alive.sqlite").is_file()
        assert not (tmp_path / "indexes" / "dead-tag.sqlite").exists()


# --- Segment unit edge cases -----------------------------------------------------------


class TestSegment:
    def test_ts_to_sec_formats(self) -> None:
        from worker.semantic_index import _ts_to_sec

        assert _ts_to_sec("01:23") == 83.0
        assert _ts_to_sec("1:02:03") == 3723.0
