"""Phase 3-F: enrich starvation fix — F1 CancelledError, F2 retry policy,
F3 dedup soft gate.

The live incident (2026-08-29): the second enrich run of an 82-min
recording sat in the shared LiteLLM FIFO queue; Temporal cancelled the
activity at 2400 s, CancelledError bypassed ``except Exception`` and the
stage row was stranded in ``running``. Three layers here:

* F1 — every ML activity marks its stage ``failed`` on CancelledError
  before re-raising (the invariant: no exit leaves a row running).
* F2 — ``_enrich_retry`` (3 attempts, 5-min backoff); skips raise a
  NON-RETRYABLE ApplicationError so a retry can never re-run them.
* F3 — ``dedup_llm_gate`` probes the proxy with ONE tiny Y/N before the
  dedup batch; after 3 failed probes the LLM leg is skipped and
  ``resolve_slugs`` resolves gray-zone pairs as "same" (per-call error
  semantics, applied up front).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from temporalio.exceptions import ApplicationError

from worker import activities
from worker.db import Base, Recording, RecordingState, Stage, StageStatus, session
from worker.enrich import (
    ExtractedEntity,
    ExtractedGraph,
    dedup_llm_gate,
    resolve_slugs,
)
from worker.workflows import _enrich_retry


@pytest.fixture(autouse=True)
def _db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    import worker.db as db_mod

    monkeypatch.setattr(db_mod, "_SessionLocal", Session)
    cfg = MagicMock()
    cfg.graph.enabled = True
    cfg.graph.uri = "bolt://n:7687"
    cfg.graph.user = "neo4j"
    cfg.graph.password_env = "NEO4J_PASSWORD"
    cfg.graph.database = "neo4j"
    cfg.summarize.base_url = "http://llm:8080/v1"
    cfg.summarize.model = "m"
    cfg.summarize.api_key_env = ""
    cfg.storage.path = tmp_path / "storage"
    cfg.recordings_root = tmp_path / "storage" / "recordings"
    monkeypatch.setattr(activities, "_cfg", cfg)
    import worker.enrich as _enrich_mod

    monkeypatch.setattr(_enrich_mod, "dedup_llm_gate", lambda c: True)


@pytest.fixture()
def recording_id() -> str:
    import uuid

    return str(uuid.uuid4())



# --- F1: CancelledError marks the stage failed ---------------------------------


def _seed(recording_id: str) -> None:
    with session() as s:
        s.add(
            Recording(
                id=recording_id,
                title="T",
                tags=["pathfinder"],
                state=RecordingState.done,
                sha256="x" * 64,
                committed_bytes=1,
                total_bytes=1,
                duration_sec=60.0,
            )
        )
        for kind in ("chunk", "transcribe", "diarize", "merge_speakers", "summarize", "enrich"):
            s.add(Stage(recording_id=recording_id, kind=kind))
        s.commit()


def _stage_status(recording_id: str, kind: str) -> StageStatus:
    with session() as s:
        st = s.query(Stage).filter_by(recording_id=recording_id, kind=kind).one()
        return st.status


def _cancel_sync(*_a: Any, **_k: Any) -> None:
    """Sync raiser: through asyncio.to_thread the CancelledError lands in
    the awaiting coroutine as a raised exception (shield propagates it),
    exercising exactly the F1 except-clause."""
    raise asyncio.CancelledError()


def test_enrich_cancelled_marks_stage_failed(recording_id: str, tmp_path: Path) -> None:
    _seed(recording_id)

    cfg = MagicMock()
    cfg.graph.enabled = True
    cfg.graph.enrich_all = True
    cfg.recordings_root = tmp_path / "storage" / "recordings"
    meta = cfg.recordings_root / recording_id / "meta"
    meta.mkdir(parents=True, exist_ok=True)
    (meta / "transcript.md").write_text("t", encoding="utf-8")

    with (
        patch("worker.activities.cfg", return_value=cfg),
        patch("worker.profiles.match_profile_by_type", return_value=None),
        patch("worker.enrich.extract_from_transcript", new=_cancel_sync),
        patch.dict("os.environ", {"NEO4J_PASSWORD": "x"}),
        pytest.raises(asyncio.CancelledError),
    ):
        asyncio.run(activities.enrich(recording_id))
    assert _stage_status(recording_id, "enrich") == StageStatus.failed


def test_chunk_cancelled_marks_stage_failed(recording_id: str, tmp_path: Path) -> None:
    _seed(recording_id)
    cfg = MagicMock()
    cfg.chunk.enabled = True
    cfg.chunk.target_min = 10.0
    cfg.chunk.overlap_sec = 2.0
    # duration already on the row: skips probe_duration (ffmpeg).
    with session() as s:
        rec = s.query(Recording).filter_by(id=recording_id).one()
        rec.duration_sec = 120.0
        s.commit()
    cfg.recordings_root = tmp_path / "storage" / "recordings"
    (cfg.recordings_root / recording_id / "meta").mkdir(parents=True, exist_ok=True)
    with (
        patch("worker.activities.cfg", return_value=cfg),
        patch("worker.activities.cut_chunks", new=_cancel_sync),
        pytest.raises(asyncio.CancelledError),
    ):
        asyncio.run(activities.chunk(recording_id))
    assert _stage_status(recording_id, "chunk") == StageStatus.failed


def test_summarize_cancelled_marks_stage_failed(
    recording_id: str, tmp_path: Path
) -> None:
    _seed(recording_id)
    cfg = MagicMock()
    cfg.summarize.enabled = True
    cfg.summarize.model = "m"
    cfg.recordings_root = tmp_path / "storage" / "recordings"
    meta = cfg.recordings_root / recording_id / "meta"
    meta.mkdir(parents=True, exist_ok=True)
    (meta / "transcript.md").write_text("t", encoding="utf-8")
    with (
        patch("worker.activities.cfg", return_value=cfg),
        patch("worker.profiles.match_profile_by_type", return_value=None),
        patch("worker.summarize.summarize_transcript", new=_cancel_sync),
        pytest.raises(asyncio.CancelledError),
    ):
        asyncio.run(activities.summarize(recording_id))
    assert _stage_status(recording_id, "summarize") == StageStatus.failed

# --- F2: retry policy shape ------------------------------------------------------


def test_enrich_retry_policy_shape() -> None:
    """3 attempts, 5-min backoff — sized to let a starved FIFO queue
    drain (see the 2026-08-29 incident), never re-running skips."""
    p = _enrich_retry()
    assert p.maximum_attempts == 3
    assert p.initial_interval is not None and p.initial_interval.total_seconds() == 300
    assert p.maximum_interval is not None and p.maximum_interval.total_seconds() == 300


def test_skip_raises_non_retryable_application_error(
    recording_id: str, tmp_path: Path
) -> None:
    """F2: skips mark the row skipped AND raise non-retryable — Temporal
    classifies the terminal failure so the retry policy stays out."""
    _seed(recording_id)
    cfg = MagicMock()
    cfg.graph.enabled = False
    with (
        patch("worker.activities.cfg", return_value=cfg),
        pytest.raises(ApplicationError) as ei,
    ):
        asyncio.run(activities.enrich(recording_id))
    assert ei.value.non_retryable is True
    assert _stage_status(recording_id, "enrich") == StageStatus.skipped


# --- F3: soft gate ----------------------------------------------------------------


def _gate_cfg() -> Any:
    cfg = MagicMock()
    cfg.summarize.base_url = "http://llm:8080/v1"
    cfg.summarize.model = "m"
    cfg.summarize.api_key_env = ""
    return cfg


def _ok_response(text: str = "Y") -> MagicMock:
    m = MagicMock()
    m.json.return_value = {"choices": [{"message": {"content": text}}]}
    return m


def test_gate_passes_first_probe() -> None:
    with patch("worker.enrich.httpx.post", return_value=_ok_response()) as p:
        assert dedup_llm_gate(_gate_cfg()) is True
    assert p.call_count == 1


def test_gate_backs_off_then_recovers() -> None:
    """Probe 1 fails (ReadTimeout), probe 2 succeeds → gate opens; the
    backoff between them is the documented 60 s ×1 (first retry)."""
    sleeps: list[float] = []
    with (
        patch(
            "worker.enrich.httpx.post",
            side_effect=[httpx.ReadTimeout("starved"), _ok_response()],
        ),
        patch("worker.enrich.time.sleep", side_effect=sleeps.append),
    ):
        assert dedup_llm_gate(_gate_cfg()) is True
    assert sleeps == [60.0]


def test_gate_three_failures_skip_llm_dedup() -> None:
    """3 failed probes → False; backoffs are 60 then 120 (×2)."""
    sleeps: list[float] = []
    with (
        patch(
            "worker.enrich.httpx.post",
            side_effect=httpx.ConnectError("down"),
        ),
        patch("worker.enrich.time.sleep", side_effect=sleeps.append),
    ):
        assert dedup_llm_gate(_gate_cfg()) is False
    assert sleeps == [60.0, 120.0]


def test_gate_429_counts_as_failure() -> None:
    bad = MagicMock()
    bad.raise_for_status.side_effect = httpx.HTTPStatusError(
        "429", request=MagicMock(), response=MagicMock(status_code=429)
    )
    sleeps: list[float] = []
    with (
        patch("worker.enrich.httpx.post", return_value=bad),
        patch("worker.enrich.time.sleep", side_effect=sleeps.append),
    ):
        assert dedup_llm_gate(_gate_cfg()) is False
    assert len(sleeps) == 2


# --- F3: resolve_slugs with llm_enabled=False ------------------------------------


def _vec_cfg() -> Any:
    cfg = _gate_cfg()
    cfg.graph.embed_enabled = True
    cfg.graph.embed_model_path = "/models/bge-m3-int8"
    cfg.graph.embed_tau_high = 0.90
    cfg.graph.embed_tau_low = 0.75
    return cfg


class _FakeEmbedder:
    def __init__(self, table: dict[str, list[float]]) -> None:
        self.table = table

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self.table[t] for t in texts]


def test_resolve_slugs_llm_disabled_merges_gray_zone() -> None:
    """Gate failed → gray-zone pair merges WITHOUT any LLM call (the
    exact verdict ask_same_entity returns on error)."""
    graph = ExtractedGraph(
        entities=[
            ExtractedEntity(slug="galahad", label="Sir Galahad", type="character"),
            ExtractedEntity(slug="galahad", label="Galahad", type="character"),
        ],
        relations=[],
    )
    fake = _FakeEmbedder({"Sir Galahad": [1.0, 0.0], "Galahad": [0.8, 0.6]})
    with (
        patch("worker.enrich._embedder", return_value=fake),
        patch("worker.enrich.httpx.post") as p,
    ):
        out = resolve_slugs(graph, _vec_cfg(), tag="t", llm_enabled=False)
    p.assert_not_called()
    assert len(out.entities) == 1


def test_resolve_slugs_llm_disabled_keeps_prefilter_zones() -> None:
    """tau_high still auto-merges and tau_low still splits with the LLM
    leg off — the prefilter is the surviving signal."""
    graph = ExtractedGraph(
        entities=[
            ExtractedEntity(slug="galahad", label="Sir Galahad", type="character"),
            ExtractedEntity(slug="galahad", label="Galahad", type="character"),
            ExtractedEntity(slug="orc", label="Orc", type="npc"),
            ExtractedEntity(slug="orc", label="Ancient Red Dragon", type="npc"),
        ],
        relations=[],
    )
    fake = _FakeEmbedder(
        {
            "Sir Galahad": [1.0, 0.0],
            "Galahad": [0.99, 0.1],  # ~0.995 >= tau_high → merge
            "Orc": [1.0, 0.0],
            "Ancient Red Dragon": [0.0, 1.0],  # 0.0 <= tau_low → split
        }
    )
    with (
        patch("worker.enrich._embedder", return_value=fake),
        patch("worker.enrich.httpx.post") as p,
    ):
        out = resolve_slugs(graph, _vec_cfg(), tag="t", llm_enabled=False)
    p.assert_not_called()
    labels = {e.label for e in out.entities}
    assert labels == {"Sir Galahad", "Orc", "Ancient Red Dragon"}


def test_resolve_slugs_llm_disabled_merges_missing_vectors() -> None:
    """No embedder at all → every collision merges (conservative same)."""
    graph = ExtractedGraph(
        entities=[
            ExtractedEntity(slug="galahad", label="Sir Galahad", type="character"),
            ExtractedEntity(slug="galahad", label="Galahad the Brave", type="character"),
        ],
        relations=[],
    )
    with (
        patch("worker.enrich._embedder", return_value=None),
        patch("worker.enrich.httpx.post") as p,
    ):
        out = resolve_slugs(graph, _vec_cfg(), tag="t", llm_enabled=False)
    p.assert_not_called()
    assert len(out.entities) == 1
