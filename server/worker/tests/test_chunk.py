"""chunk.py: plan geometry, manifest roundtrip, seam windows, suspect detection."""

import shutil
import subprocess
from itertools import pairwise
from pathlib import Path

import pytest

from worker.chunk import (
    ChunkError,
    Manifest,
    cut_chunks,
    is_suspect,
    keep_window,
    load_manifest,
    plan_chunks,
    save_manifest,
    shift_into,
)


class TestPlanChunks:
    def test_short_recording_single_chunk(self):
        assert plan_chunks(300.0, 10.0, 2.0) == [(0.0, 300.0)]

    def test_exact_target_single_chunk(self):
        assert plan_chunks(600.0, 10.0, 2.0) == [(0.0, 600.0)]

    def test_even_chunks(self):
        chunks = plan_chunks(1200.0, 10.0, 2.0)
        assert chunks[0] == (0.0, 600.0)
        # every interior chunk is exactly target-long
        assert all(e - s == 600.0 for s, e in chunks[:-1])

    def test_92min_recording_short_tail(self):
        # The handoff case: 92-min recording → 10 chunks, 2-min tail.
        chunks = plan_chunks(92 * 60.0, 10.0, 2.0)
        assert len(chunks) == 10
        assert chunks[-1] == (5382.0, 5520.0)
        assert chunks[-1][1] - chunks[-1][0] == pytest.approx(138.0)

    def test_overlap_leaves_no_gaps(self):
        duration = 2000.0
        chunks = plan_chunks(duration, 10.0, 2.0)
        assert chunks[0][0] == 0.0
        assert chunks[-1][1] == duration
        for (s1, e1), (s2, e2) in pairwise(chunks):
            assert e1 > s2  # neighbours overlap (no hole at the seam)
            assert e1 - s2 == pytest.approx(2.0)
        assert all(e > s for s, e in chunks)  # no zero/negative lengths

    def test_micro_tail_absorbed_into_previous_chunk(self):
        # Live case: 89.7-min recording (5382.4 s) ≈ 9×598 s step → the
        # naive plan ends with a 0.4-s chunk whose seam window is empty.
        chunks = plan_chunks(5382.4, 10.0, 2.0)
        assert len(chunks) == 9
        assert chunks[-1] == (4784.0, 5382.4)

    def test_tail_at_overlap_boundary_absorbed(self):
        # duration = 2×step exactly: tail would be (1196, 1196) → zero-length.
        chunks = plan_chunks(1196.0, 10.0, 2.0)
        assert chunks == [(0.0, 600.0), (598.0, 1196.0)]

    def test_real_short_tail_kept(self):
        # 92-min tail is 138 s ≫ 2×overlap — kept as its own chunk.
        chunks = plan_chunks(92 * 60.0, 10.0, 2.0)
        assert len(chunks) == 10

    def test_rejects_nonpositive_duration(self):
        with pytest.raises(ValueError):
            plan_chunks(0.0, 10.0, 2.0)

    def test_rejects_overlap_ge_target(self):
        with pytest.raises(ValueError):
            plan_chunks(3600.0, 10.0, 600.0)


class TestManifest:
    def test_roundtrip(self, tmp_path):
        meta = tmp_path / "meta"
        m = Manifest(duration_sec=100.0, target_min=10.0, overlap_sec=2.0)
        from worker.chunk import ChunkEntry

        m.chunks.append(ChunkEntry(index=0, file="chunk_000.flac", start=0.0, end=100.0))
        m.chunks[0].transcribe = "done"
        m.chunks[0].transcribe_suspect = True
        save_manifest(m, meta)

        loaded = load_manifest(meta)
        assert loaded is not None
        assert loaded.duration_sec == 100.0
        assert len(loaded.chunks) == 1
        c = loaded.chunks[0]
        assert (c.index, c.file, c.start, c.end) == (0, "chunk_000.flac", 0.0, 100.0)
        assert c.transcribe == "done"
        assert c.transcribe_suspect is True
        assert c.diarize == "pending"

    def test_load_missing_returns_none(self, tmp_path):
        assert load_manifest(tmp_path) is None


class TestKeepWindow:
    def test_first_middle_last(self):
        # 10-min chunks, 2 s overlap: interior chunks yield everything but
        # the outer half-overlaps (1 s each side).
        assert keep_window(0, 3, 600.0, 2.0) == (0.0, 599.0)
        assert keep_window(1, 3, 600.0, 2.0) == (1.0, 599.0)
        assert keep_window(2, 3, 138.0, 2.0) == (1.0, 138.0)

    def test_windows_partition_timeline(self):
        # No moment inside the overlap is claimed by two chunks, none lost.
        chunks = plan_chunks(1500.0, 10.0, 2.0)
        prev_hi = 0.0
        for i, (s, e) in enumerate(chunks):
            lo, hi = keep_window(i, len(chunks), e - s, 2.0)
            assert s + lo == pytest.approx(prev_hi)
            prev_hi = s + hi
        assert prev_hi == pytest.approx(1500.0)


class TestShiftInto:
    class Item:
        def __init__(self, start, end):
            self.start = start
            self.end = end

    def test_shift_and_midpoint_filter(self):
        items = [self.Item(0.0, 0.8), self.Item(0.5, 1.5), self.Item(598.0, 599.5)]
        kept = shift_into(items, chunk_start=600.0, lo=1.0, hi=599.0)
        # first item: midpoint 0.4 < lo → belongs to the previous chunk
        # second: midpoint 1.0 ≥ lo → kept, shifted
        # third: midpoint 598.75 < 599.0 → kept (just inside the seam)
        assert [(i.start, i.end) for i in kept] == [(600.5, 601.5), (1198.0, 1199.5)]


class TestIsSuspect:
    def test_majority_identical_is_suspect(self):
        texts = ["and then he said"] * 5 + ["something else", "another thing"]
        assert is_suspect(texts) is True

    def test_varied_text_not_suspect(self):
        assert is_suspect([f"phrase {i}" for i in range(8)]) is False

    def test_normalization_counts_case_and_whitespace(self):
        texts = ["Loop me", "loop   me", "LOOP ME", "loop me", "unique"]
        assert is_suspect(texts) is True

    def test_too_few_segments_not_suspect(self):
        assert is_suspect(["same", "same", "same"]) is False


HAS_FFMPEG = shutil.which("ffmpeg") is not None


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg not on PATH")
class TestCutChunks:
    def _tone_flac(self, path: Path, seconds: int) -> None:
        subprocess.run(
            [
                "ffmpeg", "-nostdin", "-v", "error", "-y",
                "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
                "-c:a", "flac", str(path),
            ],
            check=True,
        )

    def test_cut_produces_manifest_and_files(self, tmp_path):
        audio = tmp_path / "audio.flac"
        self._tone_flac(audio, 65)
        meta = tmp_path / "meta"
        meta.mkdir()

        m = cut_chunks(audio, meta, 65.0, target_min=0.5, overlap_sec=2.0)

        assert len(m.chunks) == 3  # 30 + 30 + tail(7)
        assert m.chunks[0].start == 0.0 and m.chunks[-1].end == 65.0
        d = meta / "chunks"
        for c in m.chunks:
            f = d / c.file
            assert f.exists() and f.stat().st_size > 0
            assert c.transcribe == "pending"
        # manifest persisted
        assert load_manifest(meta) is not None

    def test_cut_wipes_previous_run(self, tmp_path):
        audio = tmp_path / "audio.flac"
        self._tone_flac(audio, 65)
        meta = tmp_path / "meta"
        meta.mkdir()
        cut_chunks(audio, meta, 65.0, target_min=0.5, overlap_sec=2.0)
        stale = meta / "chunks" / "chunk_099.flac"
        stale.write_bytes(b"stale")

        m = cut_chunks(audio, meta, 65.0, target_min=0.5, overlap_sec=2.0)
        assert not stale.exists()
        assert all(c.transcribe == "pending" for c in m.chunks)

    def test_ffmpeg_failure_raises_chunk_error(self, tmp_path):
        audio = tmp_path / "audio.flac"
        audio.write_bytes(b"not a flac")
        meta = tmp_path / "meta"
        meta.mkdir()
        with pytest.raises(ChunkError):
            cut_chunks(audio, meta, 65.0, target_min=0.5, overlap_sec=2.0)
